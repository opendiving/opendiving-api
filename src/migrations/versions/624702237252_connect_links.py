"""connect links: the invite link's table renamed, a person's tie renamed, Hide and Disconnect

`connect_link` is created and filled from `invite_link`, every account's token carried over, and
`invite_link` is dropped; a new table rather than `op.rename_table`, which would leave the key,
the sequence and the indexes under the old name. `person.linked_user_id` becomes
`connected_user_id`, its index, partial unique index, check and foreign key renamed with it, so
every tie stands. `person.connection_hidden_at` is new and null on every row, and
`account_disconnect` starts empty. Stored audit rows take the renamed event values.

The container being replaced reads the old names until it stops.

Revision ID: 624702237252
Revises: c705e85dec9b
Create Date: 2026-11-23 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "624702237252"
down_revision: str | None = "c705e85dec9b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENT_VALUES = (
    ("invite_link_created", "connect_link_created"),
    ("invite_link_removed", "connect_link_removed"),
)
_PERSON_RENAMES = (
    ("INDEX", "ix_person_linked_user_id", "ix_person_connected_user_id"),
    ("INDEX", "ux_person_user_id_linked_user_id", "ux_person_user_id_connected_user_id"),
    ("CONSTRAINT", "ck_person_not_linked_to_its_owner", "ck_person_not_connected_to_its_owner"),
    ("CONSTRAINT", "person_linked_user_id_fkey", "person_connected_user_id_fkey"),
)


def _move_links(source: str, target: str) -> None:
    op.create_table(
        target,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f(f"ix_{target}_token"), target, ["token"], unique=True)
    op.create_index(op.f(f"ix_{target}_user_id"), target, ["user_id"], unique=True)
    op.execute(f"INSERT INTO {target} (user_id, token, created_at) SELECT user_id, token, created_at FROM {source}")
    op.drop_index(op.f(f"ix_{source}_user_id"), table_name=source)
    op.drop_index(op.f(f"ix_{source}_token"), table_name=source)
    op.drop_table(source)


def _rename_person_tie(old_column: str, new_column: str, renames: Sequence[tuple[str, str, str]]) -> None:
    op.alter_column("person", old_column, new_column_name=new_column)
    for kind, old, new in renames:
        if kind == "INDEX":
            op.execute(f"ALTER INDEX {old} RENAME TO {new}")
        else:
            op.execute(f"ALTER TABLE person RENAME CONSTRAINT {old} TO {new}")


def _rename_events(pairs: Sequence[tuple[str, str]]) -> None:
    for old, new in pairs:
        op.execute(f"UPDATE auth_audit_event SET event_type = '{new}' WHERE event_type = '{old}'")


def upgrade() -> None:
    _move_links("invite_link", "connect_link")
    _rename_person_tie("linked_user_id", "connected_user_id", _PERSON_RENAMES)
    op.add_column("person", sa.Column("connection_hidden_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "account_disconnect",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("disconnected_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["disconnected_user_id"], ["user.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_account_disconnect_disconnected_user_id"), "account_disconnect", ["disconnected_user_id"], unique=False
    )
    op.create_index(
        "ux_account_disconnect_user_id_disconnected_user_id",
        "account_disconnect",
        ["user_id", "disconnected_user_id"],
        unique=True,
    )
    _rename_events(_EVENT_VALUES)


def downgrade() -> None:
    _rename_events([(new, old) for old, new in _EVENT_VALUES])
    op.drop_index("ux_account_disconnect_user_id_disconnected_user_id", table_name="account_disconnect")
    op.drop_index(op.f("ix_account_disconnect_disconnected_user_id"), table_name="account_disconnect")
    op.drop_table("account_disconnect")
    op.drop_column("person", "connection_hidden_at")
    _rename_person_tie("connected_user_id", "linked_user_id", [(kind, new, old) for kind, old, new in _PERSON_RENAMES])
    _move_links("connect_link", "invite_link")
