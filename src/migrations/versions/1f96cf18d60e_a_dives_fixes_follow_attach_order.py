"""a dive's fixes follow its files' attach order

Revision ID: 1f96cf18d60e
Revises: 43f5f35e2922
Create Date: 2026-10-24 18:00:00.000000

Re-derives every dive's entry and exit fixes from its files' stored fixes in attach order
(`dive_file.id`) alone, the order `derive_dive_fixes` now takes; the worker's first pass derived
them recordings first. SQL over columns, so it needs neither the blob store nor the reader. Only
files whose fixes have been read count, and a fix no such file gives is kept, as the worker's
derivation keeps it: an install upgrading straight past `43f5f35e2922` finds every file unread,
writes nothing here, and the worker derives in the new order when it reads them.

`downgrade()` is a no-op: the recordings-first answer is the previous build's, and that build's
next derivation writes it again.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1f96cf18d60e"
down_revision: str | None = "43f5f35e2922"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# One statement per fix, each taken on its own; loaded by `tests/test_fix_order_rederivation.py`.
REDERIVATIONS = tuple(
    f"""
    UPDATE dive
    SET {fix}_latitude = first.latitude, {fix}_longitude = first.longitude
    FROM (
        SELECT DISTINCT ON (dive_id) dive_id, {fix}_latitude AS latitude, {fix}_longitude AS longitude
        FROM dive_file
        WHERE fixes_read AND {fix}_latitude IS NOT NULL
        ORDER BY dive_id, id
    ) AS first
    WHERE dive.id = first.dive_id
      AND (dive.{fix}_latitude, dive.{fix}_longitude) IS DISTINCT FROM (first.latitude, first.longitude)
    """
    for fix in ("entry", "exit")
)


def upgrade() -> None:
    for statement in REDERIVATIONS:
        op.execute(statement)


def downgrade() -> None:
    pass
