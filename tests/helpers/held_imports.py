"""An import's routes called directly on a real session, for tests of what they store."""

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from src.app.api.v1 import logbook_import as import_routes
from src.app.models.user import User
from src.app.schemas.logbook_import import (
    ImportCheckInSubmission,
    ImportPortraitChoice,
    LogbookImportApply,
    LogbookImportStatus,
)
from tests.helpers.import_parts import import_request


def caller(user: User) -> dict[str, Any]:
    return {"id": user.id, "uuid": user.uuid, "username": user.username, "is_superuser": False}


def body_of(response: Response) -> dict[str, Any]:
    body: dict[str, Any] = json.loads(bytes(response.body))
    return body


async def upload(db: AsyncSession, user: User, files: list[tuple[str, bytes]]) -> dict[str, Any]:
    """`POST /imports`, answering the import's body."""
    return body_of(
        await import_routes.create_logbook_import(request=import_request(files), current_user=caller(user), db=db)
    )


async def apply(
    db: AsyncSession,
    user: User,
    uuid: Any,
    *,
    check_in: ImportCheckInSubmission | None = None,
    portrait: ImportPortraitChoice | None = None,
) -> dict[str, Any]:
    """`POST /import/{uuid}/apply`, answering the import's body."""
    return body_of(
        await import_routes.apply_logbook_import(
            uuid=uuid,
            choices=LogbookImportApply(check_in_details=check_in, portrait=portrait),
            current_user=caller(user),
            db=db,
        )
    )


async def discard(db: AsyncSession, user: User, uuid: Any, status: LogbookImportStatus) -> None:
    """`DELETE /import/{uuid}`."""
    await import_routes.delete_logbook_import(uuid=uuid, status=status, current_user=caller(user), db=db)


async def upload_and_apply(db: AsyncSession, user: User, files: list[tuple[str, bytes]]) -> dict[str, Any]:
    review = await upload(db, user, files)
    return await apply(db, user, review["uuid"])
