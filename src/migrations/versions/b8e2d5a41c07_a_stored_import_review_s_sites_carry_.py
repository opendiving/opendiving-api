"""a stored import review's sites carry their owner and proposal status

Revision ID: b8e2d5a41c07
Revises: 4d88602e2ab9
Create Date: 2026-11-15 15:00:00.000000

A dive read's sites gained `owner` and `proposal_status`, and an import's `review` stores dive
reads and is served as stored. This writes both members into every site of every review stored at
shape 1, as a read made now would give them for the import's account, and records shape 2. A site
the review names that no longer exists is the account's: a Commons record is never deleted.

`downgrade()` is a no-op: a review carrying two more members is still what the earlier build serves.
"""

import uuid as uuid_pkg
from collections.abc import Iterator, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import context, op

# revision identifiers, used by Alembic.
revision: str = "b8e2d5a41c07"
down_revision: str | None = "4d88602e2ab9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_READ_ON_THE_SITE = ("waiting", "declined")


def _sites(review: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for row in review.get("dives") or []:
        dive = row.get("dive") if isinstance(row, dict) else None
        if isinstance(dive, dict):
            yield from (site for site in dive.get("dive_sites") or [] if isinstance(site, dict))


def marked(review: dict[str, Any], owners: dict[str, int | None], statuses: dict[str, str | None]) -> dict[str, Any]:
    """`review` with each site's two members set, from its owning account by uuid (`None` on a
    Commons record) and the account's latest proposal status on it by uuid. In place."""
    for site in _sites(review):
        uuid = str(_parsed(site.get("uuid")))
        site["owner"] = "opendivingcommons" if uuid in owners and owners[uuid] is None else "account"
        status = statuses.get(uuid)
        site["proposal_status"] = status if status in _READ_ON_THE_SITE else None
    return review


def _parsed(value: Any) -> uuid_pkg.UUID | None:
    try:
        return uuid_pkg.UUID(str(value))
    except ValueError:
        return None


def upgrade() -> None:
    if context.is_offline_mode():
        return
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, user_id, review FROM logbook_import WHERE review_shape = 1 ORDER BY id").columns(
            id=sa.Integer, user_id=sa.Integer, review=sa.JSON
        )
    ).all()
    update = sa.text("UPDATE logbook_import SET review = :review, review_shape = 2 WHERE id = :id").bindparams(
        sa.bindparam("review", type_=sa.JSON)
    )
    for row in rows:
        uuids = {parsed for site in _sites(row.review) if (parsed := _parsed(site.get("uuid"))) is not None}
        owners: dict[str, int | None] = {}
        statuses: dict[str, str | None] = {}
        if uuids:
            found = connection.execute(
                sa.text(
                    "SELECT CAST(s.uuid AS text) AS uuid, s.user_id, "
                    "(SELECT p.status FROM dive_site_proposal p WHERE p.user_id = :user_id AND p.dive_site_id = s.id "
                    "ORDER BY p.id DESC LIMIT 1) AS status "
                    "FROM dive_site s WHERE s.uuid IN :uuids"
                ).bindparams(sa.bindparam("uuids", expanding=True, type_=sa.Uuid)),
                {"user_id": row.user_id, "uuids": sorted(uuids)},
            ).all()
            owners = {site.uuid: site.user_id for site in found}
            statuses = {site.uuid: site.status for site in found}
        connection.execute(update, {"id": row.id, "review": marked(row.review, owners, statuses)})


def downgrade() -> None:
    pass
