"""a profile carries its depth silhouette

Revision ID: 0f941e1c4130
Revises: e8b670ac5780
Create Date: 2026-09-30 22:07:43.000000

`dive_profile.depth_silhouette`, filled on every stored row from the depth series in the row's
own `data`. No file is read, so an imported or merged profile gets one too. The derivation is
a frozen copy of `derive_depth_silhouette` as it stands at this revision, not an import of it:
a revision is frozen history. `downgrade()` drops the column.
"""

from collections.abc import Sequence
from itertools import pairwise
from typing import Any

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "0f941e1c4130"
down_revision: str | None = "e8b670ac5780"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POINTS = 64
_BATCH = 200

# Keyset rather than one fetch of every id, and the depth series alone rather than `data`: the
# other nine channels are most of each payload.
_SELECT_DEPTHS = sa.text(
    "SELECT id, data->'depth' AS depth FROM dive_profile WHERE id > :after ORDER BY id LIMIT :limit"
).columns(id=sa.Integer, depth=JSONB)
_UPDATE_SILHOUETTE = sa.text("UPDATE dive_profile SET depth_silhouette = :silhouette WHERE id = :id").bindparams(
    sa.bindparam("silhouette", type_=JSONB)
)


def silhouette(depth: Any) -> dict[str, Any] | None:
    """A stored depth series `{"t": [...], "v": [...]}` as `{"span": ..., "values": [...]}`."""
    if not isinstance(depth, dict):
        return None
    times, readings = depth.get("t"), depth.get("v")
    if not isinstance(times, list) or not isinstance(readings, list) or len(times) != len(readings):
        return None
    if len(times) < 2 or times[-1] <= times[0]:
        return None
    start, span = times[0], times[-1] - times[0]

    deepest: dict[int, int] = {}
    for moment, centimeters in zip(times, readings, strict=True):
        index = min((moment - start) * POINTS // span, POINTS - 1)
        deepest[index] = max(centimeters, deepest.get(index, centimeters))

    values: list[int] = []
    for left, right in pairwise(sorted(deepest)):
        low, high = deepest[left], deepest[right]
        values.extend(low + round((high - low) * (index - left) / (right - left)) for index in range(left, right))
    values.append(deepest[POINTS - 1])
    return {"span": span, "values": values}


def _fill(connection: sa.Connection) -> None:
    after = 0
    while rows := connection.execute(_SELECT_DEPTHS, {"after": after, "limit": _BATCH}).all():
        updates = [{"id": row.id, "silhouette": shape} for row in rows if (shape := silhouette(row.depth)) is not None]
        if updates:
            connection.execute(_UPDATE_SILHOUETTE, updates)
        after = rows[-1].id


def upgrade() -> None:
    op.add_column("dive_profile", sa.Column("depth_silhouette", JSONB(), nullable=True))
    if not context.is_offline_mode():
        _fill(op.get_bind())


def downgrade() -> None:
    op.drop_column("dive_profile", "depth_silhouette")
