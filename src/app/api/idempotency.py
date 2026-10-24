"""`Idempotency-Key` on the create routes: a create sent twice under one key makes one record.

A covered route declares `IdempotentCreate(<the record's model>, read="<its GET path>")` in its
decorator's `dependencies`, and its body is untouched. Without the header nothing happens. With
it, the first request claims an `IdempotencyHold` before the body runs, and the session hooks
below give the hold the new record's uuid in the record's own transaction; a repeat from the same
account to the same path answers that record as it now reads, with the status the first
answered, and performs nothing.

`read` is the path of the record's `GET` as its router declares it, looked up in the app's
routes at the first replay, because every module defines that handler below its create.
"""

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, NoReturn

from fastapi import Depends, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.routing import BaseRoute

from ..core.db.database import async_get_db
from ..core.db.models import PublicUUIDMixin
from ..core.exceptions.http_exceptions import NotFoundException, UnprocessableEntityException
from ..models.idempotency_hold import MAX_IDEMPOTENCY_KEY_LENGTH, IdempotencyHold
from .dependencies import get_current_user

HOLD_INDEX = "ux_idempotency_hold_user_id_route_key"

IN_FLIGHT = "A request with this Idempotency-Key is still being performed. Retry it shortly."
DIFFERENT_REQUEST = "This Idempotency-Key was already used for a different request."
DELETED = "The request with this Idempotency-Key created a record that has since been deleted."

ReadRoute = Callable[..., Awaitable[Any]]

_PENDING = "idempotency_hold"


@dataclass(frozen=True)
class _Pending:
    hold: IdempotencyHold
    record: type[PublicUUIDMixin]


class IdempotentReplay(Exception):
    """A repeat's answer, raised from the dependency so the route's body never runs."""

    def __init__(self, status_code: int, content: Any) -> None:
        super().__init__(status_code)
        self.status_code = status_code
        self.content = content


async def answer_replay(request: Request, exc: IdempotentReplay) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.content)


class IdempotentCreate:
    def __init__(self, record: type[PublicUUIDMixin], *, read: str) -> None:
        self.record = record
        self.read = read
        self._read_endpoint: ReadRoute | None = None

    def read_endpoint(self, routes: Sequence[BaseRoute]) -> ReadRoute:
        if self._read_endpoint is None:
            (endpoint,) = [
                context.original_route.endpoint
                for context in iter_route_contexts(routes)
                if isinstance(context.original_route, APIRoute)
                and context.original_route.path == self.read
                and "GET" in (context.original_route.methods or set())
            ]
            self._read_endpoint = endpoint
        return self._read_endpoint

    async def __call__(
        self,
        request: Request,
        current_user: Annotated[dict, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(async_get_db)],
        idempotency_key: Annotated[
            str | None,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=MAX_IDEMPOTENCY_KEY_LENGTH,
                description=(
                    "Optional, chosen by the client. A repeat of this create from the same account under "
                    "the same key answers the record the first one made, as it reads now, and makes "
                    "nothing; a different body under the key is a 422, a key whose create is still "
                    "running a 409, and one whose record has since been deleted a 410."
                ),
            ),
        ] = None,
    ) -> None:
        if idempotency_key is None:
            return
        route = request.url.path
        digest = hashlib.sha256(await request.body()).hexdigest()
        held = await db.scalar(
            select(IdempotencyHold).where(
                IdempotencyHold.user_id == current_user["id"],
                IdempotencyHold.route == route,
                IdempotencyHold.key == idempotency_key,
            )
        )
        if held is not None:
            await self._replay(held, digest, request, current_user, db)

        # Claimed before the body runs, so a concurrent request under the key waits on this
        # row's index entry rather than reaching the route's own checks, and is answered 409
        # once the first commits. A first that rolls back releases it, and the second creates.
        hold = IdempotencyHold(
            user_id=current_user["id"],
            route=route,
            key=idempotency_key,
            request_digest=digest,
            status_code=request.scope["route"].status_code,
        )
        db.add(hold)
        try:
            await db.flush()
        except IntegrityError as exc:
            await db.rollback()
            if HOLD_INDEX in str(exc.orig):
                raise HTTPException(status_code=409, detail=IN_FLIGHT) from exc
            raise
        db.sync_session.info[_PENDING] = _Pending(hold=hold, record=self.record)

    async def _replay(
        self, held: IdempotencyHold, digest: str, request: Request, current_user: dict, db: AsyncSession
    ) -> NoReturn:
        if held.request_digest != digest:
            raise UnprocessableEntityException(DIFFERENT_REQUEST)
        if held.record_uuid is None:
            raise RuntimeError("A committed idempotency hold names no record.")
        try:
            record = await self.read_endpoint(request.app.routes)(
                request=request, uuid=held.record_uuid, current_user=current_user, db=db
            )
        except NotFoundException:
            raise HTTPException(status_code=410, detail=DELETED) from None
        response_model = request.scope["route"].response_model
        raise IdempotentReplay(
            held.status_code, jsonable_encoder(response_model.model_validate(record, from_attributes=True))
        )


def _name_the_record(session: Session, pending: _Pending) -> None:
    if pending.hold.record_uuid is not None:
        return
    created = [instance for instance in session.new if isinstance(instance, pending.record)]
    if len(created) > 1:
        raise RuntimeError(f"A create under an Idempotency-Key made {len(created)} {pending.record.__name__} rows.")
    if created:
        pending.hold.record_uuid = created[0].uuid


@event.listens_for(Session, "before_flush")
def _before_flush(session: Session, flush_context: Any, instances: Any) -> None:
    if (pending := session.info.get(_PENDING)) is not None:
        _name_the_record(session, pending)


@event.listens_for(Session, "before_commit")
def _before_commit(session: Session) -> None:
    """Name the record a `commit()` is about to flush, and refuse to commit a hold without one.

    Runs before the commit's own flush, which is why it names the record itself rather than
    leaving it to `before_flush`.
    """
    if session.in_nested_transaction() or (pending := session.info.pop(_PENDING, None)) is None:
        return
    _name_the_record(session, pending)
    if pending.hold.record_uuid is None:
        raise RuntimeError(f"A create under an Idempotency-Key committed without its {pending.record.__name__}.")


@event.listens_for(Session, "after_rollback")
def _after_rollback(session: Session) -> None:
    session.info.pop(_PENDING, None)
