"""a dive's avg_depth may not exceed its max_depth

A mean cannot be deeper than a maximum, so a dive claiming otherwise records at least one
wrong number. Until now the app had no comparison anywhere - only the independent
`ck_dive_max_depth_positive` and `ck_dive_avg_depth_positive` - so `POST /dive` accepted
`avg_depth` 30 with `max_depth` 20, and the export then produced a document the DiveJSON
reference validator rejects (spec §6.2, and §3's cross-member arithmetic list). See *"A
dive's average depth cannot exceed its maximum, and nothing can store one that does"* in
DECISIONS.md.

**Existing violations are repaired rather than met with a failed upgrade.** A migration
that simply added the constraint would abort on the first offending row and take the
container's startup `alembic upgrade head` down with it, on a database nobody can log into
to fix. So the rows are repaired first, and the repair is the least-inventive one
available: `avg_depth` is cleared to NULL - "not recorded", which is what the app already
means by an absent average - while `max_depth` is kept.

The asymmetry is deliberate. Both numbers cannot be right and nothing on the row says
which is wrong, so *something* has to go; `max_depth` is the one the rest of the app
depends on (the dive list, the user's stats, UDDF's mandatory `<greatestdepth>`), and
`avg_depth` feeds only gas arithmetic, which declines to compute rather than compute
wrongly when it is absent. Rejected: swapping the pair, which invents the reading that the
diver typed them the wrong way round; and clearing both, which discards a number nothing
suggests is wrong.

`downgrade()` drops the constraint only. The cleared averages are not recoverable - by
construction, since the value that was there is the one the constraint says cannot be
true - and a database stepped back is otherwise identical to one that never went forward.

Revision ID: c4d81e6b3f57
Revises: b1c7f0e4a2d9
Create Date: 2026-09-04 12:20:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d81e6b3f57"
down_revision: str | None = "b1c7f0e4a2d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_dive_avg_depth_within_max"


def upgrade() -> None:
    op.execute(
        "UPDATE dive SET avg_depth = NULL "
        "WHERE avg_depth IS NOT NULL AND max_depth IS NOT NULL AND avg_depth > max_depth"
    )
    op.create_check_constraint(
        _CONSTRAINT,
        "dive",
        "avg_depth IS NULL OR max_depth IS NULL OR avg_depth <= max_depth",
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "dive", type_="check")
