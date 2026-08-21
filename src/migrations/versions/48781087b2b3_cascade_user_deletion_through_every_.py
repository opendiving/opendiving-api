"""cascade user deletion through every owned row

Ten foreign keys pointed at `user.id` with no `ondelete` rule, so `DELETE FROM "user"`
failed for any account that had ever logged a dive, saved a site or earned a c-card. See
*"The ten cascades that were never declared"* in `DECISIONS.md` for why they are declared
here rather than hand-rolled in the account purge job built on top of them.

Nothing issues a `DELETE FROM "user"` yet - this revision only makes one possible.

Two mechanical traps shaped the file, both worth knowing before editing it:

- **Autogenerate does not detect an `ondelete` change.** Running `--autogenerate` after
  adding `ondelete="CASCADE"` to the models produces an empty revision and looks like it
  worked. Every statement below is hand-written; `alembic check` stays quiet either way,
  since it compares the same things autogenerate does.
- **Postgres cannot `ALTER` a constraint's delete rule.** Each one is `DROP CONSTRAINT`
  plus `ADD CONSTRAINT` under the same name. The names are Postgres's own
  `<table>_<column>_fkey` default - the baseline declares these FKs unnamed and there is no
  `naming_convention` on the metadata - and they were read off the live database rather
  than inferred.

`authentication_provider`, `authentication_request` and `webauthn_credential` already
cascade and are not touched.

No index work. An unindexed FK with `ON DELETE CASCADE` seq-scans the child table on every
parent delete, and a *partial* index on a cascade target counts as none (the RI lookup
carries no predicate for one to be implied by) - the trap
*"The indexes are the part that needed care"* in `DECISIONS.md` documents. Each of the ten
tables was checked against the live schema and already has a plain btree leading with
`user_id`: eight from `index=True`, plus `ux_dive_file_user_id_sha256` and the unique
`ix_user_dive_stats_user_id`.

Revision ID: 48781087b2b3
Revises: b24933e17c19
Create Date: 2026-08-21 02:10:33.930333

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "48781087b2b3"
down_revision: str | None = "b24933e17c19"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: `(table, constraint name)`, one per FK that gains the cascade. The column is `user_id`
#: on all ten and is not carried separately.
_CASCADING_FKS: tuple[tuple[str, str], ...] = (
    ("certification", "certification_user_id_fkey"),
    ("dive", "dive_user_id_fkey"),
    ("dive_file", "dive_file_user_id_fkey"),
    ("dive_site", "dive_site_user_id_fkey"),
    ("gear_item", "gear_item_user_id_fkey"),
    ("gear_service_record", "gear_service_record_user_id_fkey"),
    ("gear_service_schedule", "gear_service_schedule_user_id_fkey"),
    ("gear_set", "gear_set_user_id_fkey"),
    ("trip", "trip_user_id_fkey"),
    ("user_dive_stats", "user_dive_stats_user_id_fkey"),
)


def _redeclare(ondelete: str | None) -> None:
    """Drop and re-add all ten, since `ALTER CONSTRAINT` cannot change a delete rule.

    `ondelete=None` renders no `ON DELETE` clause at all, which is Postgres's `NO ACTION`
    default and so is exactly what `downgrade` has to put back.
    """
    for table, name in _CASCADING_FKS:
        op.drop_constraint(name, table, type_="foreignkey")
        op.create_foreign_key(name, table, "user", ["user_id"], ["id"], ondelete=ondelete)


def upgrade() -> None:
    _redeclare("CASCADE")


def downgrade() -> None:
    _redeclare(None)
