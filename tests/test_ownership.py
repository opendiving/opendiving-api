"""Unit tests for `api.dependencies.fetch_owned_or_raise`.

This is the single implementation behind every "fetch one resource the caller owns"
route in `api/v1` - dives, dive sites, trips, gear items, gear sets and certifications
all reach it through a thin per-entity wrapper. It used to be seven hand-rolled copies,
so these tests exist to keep the one that replaced them honest.
"""

import uuid as uuid_pkg
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from src.app.api.dependencies import fetch_owned_or_raise
from src.app.core.exceptions.http_exceptions import ForbiddenException, NotFoundException


class _Row(BaseModel):
    id: int
    user_id: int


def _crud(row: _Row | None) -> Any:
    crud = AsyncMock()
    crud.get = AsyncMock(return_value=row)
    return crud


CALLER = {"id": 7, "uuid": uuid_pkg.uuid4()}


class TestFetchOwnedOrRaise:
    @pytest.mark.asyncio
    async def test_returns_the_row_when_the_caller_owns_it(self):
        row = _Row(id=1, user_id=7)

        result = await fetch_owned_or_raise(
            db=AsyncMock(),
            crud=_crud(row),
            uuid=uuid_pkg.uuid4(),
            current_user=CALLER,
            schema=_Row,
            not_found_message="Thing not found",
        )

        assert result is row

    @pytest.mark.asyncio
    async def test_missing_row_is_a_404(self):
        with pytest.raises(NotFoundException, match="Thing not found"):
            await fetch_owned_or_raise(
                db=AsyncMock(),
                crud=_crud(None),
                uuid=uuid_pkg.uuid4(),
                current_user=CALLER,
                schema=_Row,
                not_found_message="Thing not found",
            )

    @pytest.mark.asyncio
    async def test_someone_elses_row_is_a_403(self):
        with pytest.raises(ForbiddenException):
            await fetch_owned_or_raise(
                db=AsyncMock(),
                crud=_crud(_Row(id=1, user_id=8)),
                uuid=uuid_pkg.uuid4(),
                current_user=CALLER,
                schema=_Row,
                not_found_message="Thing not found",
            )

    @pytest.mark.asyncio
    async def test_soft_deleted_rows_are_excluded_by_default(self):
        crud = _crud(_Row(id=1, user_id=7))

        await fetch_owned_or_raise(
            db=AsyncMock(),
            crud=crud,
            uuid=uuid_pkg.uuid4(),
            current_user=CALLER,
            schema=_Row,
            not_found_message="Thing not found",
        )

        assert crud.get.await_args.kwargs["is_deleted"] is False

    @pytest.mark.asyncio
    async def test_include_deleted_drops_the_filter(self):
        """The delete routes pass this so deleting an already-deleted row is a no-op
        rather than a 404.
        """
        crud = _crud(_Row(id=1, user_id=7))

        await fetch_owned_or_raise(
            db=AsyncMock(),
            crud=crud,
            uuid=uuid_pkg.uuid4(),
            current_user=CALLER,
            schema=_Row,
            not_found_message="Thing not found",
            include_deleted=True,
        )

        assert "is_deleted" not in crud.get.await_args.kwargs

    @pytest.mark.asyncio
    async def test_the_row_is_looked_up_by_public_uuid(self):
        """Never by the internal integer id - that's the whole reason the public API
        exposes uuids.
        """
        crud = _crud(_Row(id=1, user_id=7))
        uuid = uuid_pkg.uuid4()

        await fetch_owned_or_raise(
            db=AsyncMock(),
            crud=crud,
            uuid=uuid,
            current_user=CALLER,
            schema=_Row,
            not_found_message="Thing not found",
        )

        assert crud.get.await_args.kwargs["uuid"] == uuid


class TestEveryOwnedRouteUsesIt:
    def test_no_route_file_hand_rolls_the_check(self):
        """The regression this guards: the same fetch/404/403 block was copy-pasted into
        seven route files, so a fix to one (notably the "authorize before `@cache`"
        ordering) silently missed the other six.
        """
        from pathlib import Path

        routes_dir = Path(__file__).resolve().parents[1] / "src" / "app" / "api" / "v1"
        offenders = []

        for path in sorted(routes_dir.glob("*.py")):
            source = path.read_text()
            # The signature of the old inline block: a cast straight into an ownership
            # comparison against the caller's internal id.
            if 'user_id != current_user["id"]' in source:
                offenders.append(path.name)

        assert offenders == [], f"hand-rolled ownership check(s) still in: {offenders}"
