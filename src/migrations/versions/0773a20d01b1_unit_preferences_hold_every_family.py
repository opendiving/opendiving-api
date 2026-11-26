"""unit preferences hold every family

Revision ID: 0773a20d01b1
Revises: 28d1ff486573
Create Date: 2026-11-26 16:00:00.000000

Every `user.unit_preferences` is completed to all five families in one statement: a family the
account had overridden keeps its system, and every other family takes the row's `units`. A
complete record is left as stored. A new account's record is all five at `metric`.

`user.units` stays on the table and leaves the mapper, and the previous build still reads it;
the revision that drops it fills from it any family a record still lacks.

`downgrade()` keeps, of each record, only the families whose system differs from the row's
`units`, which is what the previous build reads as overrides, so each account reads as it did.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0773a20d01b1"
down_revision: str | None = "28d1ff486573"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FAMILIES = ("depth", "pressure", "volume", "weight", "temperature")
_METRIC = "{" + ", ".join(f'"{family}": "metric"' for family in _FAMILIES) + "}"


def upgrade() -> None:
    from_units = ", ".join(f"'{family}', units" for family in _FAMILIES)
    every_family = ", ".join(f"'{family}'" for family in _FAMILIES)
    op.execute(
        f"""
        UPDATE "user"
        SET unit_preferences = (jsonb_build_object({from_units}) || unit_preferences::jsonb)::json
        WHERE NOT unit_preferences::jsonb ?& ARRAY[{every_family}]
        """
    )
    op.alter_column("user", "unit_preferences", server_default=_METRIC)


def downgrade() -> None:
    op.alter_column("user", "unit_preferences", server_default="{}")
    op.execute(
        """
        UPDATE "user"
        SET unit_preferences = COALESCE(
            (SELECT json_object_agg(key, value) FROM json_each_text(unit_preferences) WHERE value <> units),
            '{}'::json
        )
        """
    )
