"""a stored import review's recordings and files carry created_at

Revision ID: c3f81a6d9e20
Revises: 90c6f077a7d4
Create Date: 2026-11-19 22:30:00.000000

A dive read's recordings and their files gained a required `created_at`, and an import's `review`
stores dive reads and is served as stored. This writes it into every recording and file of every
review stored at shape 2 and records shape 3: the row's own value where the uuid still names one of
the account's, and otherwise the import's `created_at`, which is when the pass that made the review
ran - a recording or file the review made takes a new identifier at the apply, so it names nothing.

`downgrade()` is a no-op: a review carrying one more member is still what the earlier build serves.
"""

import uuid as uuid_pkg
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "c3f81a6d9e20"
down_revision: str | None = "90c6f077a7d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _recordings(review: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for row in review.get("dives") or []:
        dive = row.get("dive") if isinstance(row, dict) else None
        if isinstance(dive, dict):
            yield from (one for one in dive.get("recordings") or [] if isinstance(one, dict))


def _files(review: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for recording in _recordings(review):
        yield from (one for one in recording.get("files") or [] if isinstance(one, dict))


def stamped(
    review: dict[str, Any], recordings: dict[str, datetime], files: dict[str, datetime], fallback: datetime
) -> dict[str, Any]:
    """`review` with each recording's and file's `created_at` set, from the stored row by uuid and
    `fallback` where none is found. In place."""
    for members, found in ((_recordings(review), recordings), (_files(review), files)):
        for member in members:
            created = found.get(str(_parsed(member.get("uuid"))), fallback)
            # The spelling a dive read's JSON gives a UTC instant.
            member["created_at"] = created.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return review


def _parsed(value: Any) -> uuid_pkg.UUID | None:
    try:
        return uuid_pkg.UUID(str(value))
    except ValueError:
        return None


def _created(
    connection: sa.Connection, table: str, user_id: int, members: Iterator[dict[str, Any]]
) -> dict[str, datetime]:
    uuids = {parsed for member in members if (parsed := _parsed(member.get("uuid"))) is not None}
    if not uuids:
        return {}
    found = connection.execute(
        sa.text(
            f"SELECT CAST(uuid AS text) AS uuid, created_at FROM {table} WHERE user_id = :user_id AND uuid IN :uuids"
        ).bindparams(sa.bindparam("uuids", expanding=True, type_=sa.Uuid)),
        {"user_id": user_id, "uuids": sorted(uuids)},
    ).all()
    return {row.uuid: row.created_at for row in found}


def upgrade() -> None:
    if context.is_offline_mode():
        return
    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            "SELECT id, user_id, created_at, review FROM logbook_import WHERE review_shape = 2 ORDER BY id"
        ).columns(id=sa.Integer, user_id=sa.Integer, created_at=sa.DateTime(timezone=True), review=sa.JSON)
    ).all()
    update = sa.text("UPDATE logbook_import SET review = :review, review_shape = 3 WHERE id = :id").bindparams(
        sa.bindparam("review", type_=sa.JSON)
    )
    for row in rows:
        recordings = _created(connection, "dive_recording", row.user_id, _recordings(row.review))
        files = _created(connection, "dive_file", row.user_id, _files(row.review))
        connection.execute(update, {"id": row.id, "review": stamped(row.review, recordings, files, row.created_at)})


def downgrade() -> None:
    pass
