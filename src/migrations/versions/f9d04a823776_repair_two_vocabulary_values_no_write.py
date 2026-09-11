"""repair two vocabulary values no write path could have produced

Revision ID: f9d04a823776
Revises: d7a49b1c58e2
Create Date: 2026-09-11 20:30:00.000000

Two literal values, both left behind by test fixtures that wrote SQLAlchemy models
directly into a developer's `opendive` during the window when the suite used the dev
database instead of its own (repaired in #136, `tests/conftest.py`):

- `gear_service_schedule.kind` and `gear_service_record.kind` of `'inspection'`, which
  `tests/helpers/generators.py` wrote until it was corrected to `'visual_inspection'`.
  `'inspection'` is not a former spelling of anything - `ServiceKind` has read
  `VISUAL_INSPECTION = "visual_inspection"` since the enum was introduced (#13) - so
  there is no rename to complete, only a fixture's value to replace with the one it
  itself now uses.
- `dive.water_type` of `'soda'`, written by `test_dive_check_constraints.py`'s
  `test_any_water_type_string_is_accepted_by_the_database`, which exists to record that
  the column is deliberately unconstrained. It is cleared to NULL rather than mapped:
  NULL is what this column already means by "not recorded", and there is no water type
  `'soda'` could honestly become. Same least-inventive repair as revision
  `c4d81e6b3f57`'s `avg_depth`.

**This names two literals; it is not a vocabulary sweep.** A `WHERE kind NOT IN (...)`
would be `ck_`-by-another-name, applied retroactively and without the DDL that would let
anyone see it - and it would rewrite exactly the value the unconstrained column exists to
admit: one written by a newer build against an older schema. See *"`GearItem.type` is a
closed vocabulary, but has no DB `CHECK` constraint"* in DECISIONS.md for why these
columns are plain `VARCHAR`, and *"A stored vocabulary is read back as a string"* for the
500 that made these rows visible.

**No self-hoster has either value.** Neither is reachable through the API - both enums are
Pydantic fields on every write path - so this runs as a no-op everywhere except a
developer's own database from before #136. It ships as a revision rather than as a psql
snippet in a README because that is the only repair mechanism a database has: the
alternative locally is `docker compose down -v`, which costs the developer everything else
in it.

**The rename skips a row that would collide.** `ux_gear_service_schedule_item_kind_label`
is unique over (item, kind, lower(label)), so renaming an `'inspection'` schedule onto an
item that already has a `'visual_inspection'` one with the same label would abort the
upgrade - inside the API's startup `alembic upgrade head`, on a container that then never
comes up. Such a row is left as it is, which is safe because it is no longer a 500: the
read schemas carry an unrecognized value through. The read change is what lets this one be
conservative.

`downgrade()` is a no-op. Re-introducing `'inspection'` would restore the defect, the
cleared `'soda'` is not recoverable (nothing records what it was), and a database stepped
back is otherwise identical to one that never went forward.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f9d04a823776"
down_revision: str | None = "d7a49b1c58e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Module-level so `tests/test_vocabulary_repair.py` can run the same text against a live
# Postgres. The collision arm below is the half worth pinning: getting it wrong aborts the
# API's startup `alembic upgrade head` rather than failing a request.
REPAIRS: tuple[str, ...] = (
    """
    UPDATE gear_service_schedule AS s
       SET kind = 'visual_inspection'
     WHERE s.kind = 'inspection'
       AND NOT EXISTS (
           SELECT 1
             FROM gear_service_schedule AS other
            WHERE other.gear_item_id = s.gear_item_id
              AND other.kind = 'visual_inspection'
              AND COALESCE(LOWER(other.label), '') = COALESCE(LOWER(s.label), '')
       )
    """,
    "UPDATE gear_service_record SET kind = 'visual_inspection' WHERE kind = 'inspection'",
    "UPDATE dive SET water_type = NULL WHERE water_type = 'soda'",
)


def upgrade() -> None:
    for statement in REPAIRS:
        op.execute(statement)


def downgrade() -> None:
    """Nothing to undo - see the module docstring."""
