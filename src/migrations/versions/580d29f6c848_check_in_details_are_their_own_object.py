"""check-in details are their own object

Revision ID: 580d29f6c848
Revises: bb0a5f425d41
Create Date: 2026-10-07 12:00:00.000000

The eight check-in columns and the insurance's reminder pair leave `user` for three tables:
`checkin_details` (one row per account: email, phone, date of birth), and the ordered lists
`checkin_emergency_contact` and `checkin_insurance_policy`, the policy row carrying the pair.

**Copy, then drop.** Each account's details are copied before the columns go, every text
column as `NULLIF(btrim(col), '')`, so a blank the old columns accepted is not copied: a
contact without a name and a policy without a provider - which the export already omitted -
stay behind, and the two child rows are written at position 0 only where the anchor is there.
The tables are created outright rather than renamed, so every table the models declare is one
a revision creates.

**The drop rides the deploy that unmaps the columns**, against *A column the serving build maps
is dropped a deploy after it is unmapped* in DECISIONS.md. The copy has to precede the drop, and
splitting the two across deploys buys nothing a second deploy would not cost: the overlap fails
the previous build's signed-in requests only until the switch.

**`downgrade()` loses what the old shape cannot hold**: the check-in email, and every contact
and policy past the first. It re-adds the columns, copies the first row of each kind back with
its reminder pair, and drops the tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "580d29f6c848"
down_revision: str | None = "bb0a5f425d41"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_COPY_DETAILS = sa.text(
    """
    INSERT INTO checkin_details (user_id, phone, date_of_birth)
    SELECT id, NULLIF(btrim(phone), ''), date_of_birth
    FROM "user"
    WHERE date_of_birth IS NOT NULL
       OR NULLIF(btrim(phone), '') IS NOT NULL
       OR NULLIF(btrim(emergency_contact_name), '') IS NOT NULL
       OR NULLIF(btrim(emergency_contact_phone), '') IS NOT NULL
       OR NULLIF(btrim(emergency_contact_relationship), '') IS NOT NULL
       OR NULLIF(btrim(insurance_provider), '') IS NOT NULL
       OR NULLIF(btrim(insurance_policy_number), '') IS NOT NULL
       OR insurance_expires_on IS NOT NULL
    """
)

_COPY_CONTACTS = sa.text(
    """
    INSERT INTO checkin_emergency_contact (user_id, position, name, phone, relationship)
    SELECT id, 0, btrim(emergency_contact_name),
           NULLIF(btrim(emergency_contact_phone), ''),
           NULLIF(btrim(emergency_contact_relationship), '')
    FROM "user"
    WHERE btrim(emergency_contact_name) <> ''
    """
)

_COPY_POLICIES = sa.text(
    """
    INSERT INTO checkin_insurance_policy (
        user_id, position, provider, number, expires_on, notified_stage, notified_for
    )
    SELECT id, 0, btrim(insurance_provider),
           NULLIF(btrim(insurance_policy_number), ''),
           insurance_expires_on,
           insurance_notified_stage,
           insurance_notified_for
    FROM "user"
    WHERE btrim(insurance_provider) <> ''
    """
)

_COPY_BACK_DETAILS = sa.text(
    """
    UPDATE "user" AS u
    SET phone = d.phone, date_of_birth = d.date_of_birth
    FROM checkin_details AS d
    WHERE d.user_id = u.id
    """
)

_COPY_BACK_FIRST_CONTACT = sa.text(
    """
    UPDATE "user" AS u
    SET emergency_contact_name = c.name,
        emergency_contact_phone = c.phone,
        emergency_contact_relationship = c.relationship
    FROM (
        SELECT DISTINCT ON (user_id) user_id, name, phone, relationship
        FROM checkin_emergency_contact
        ORDER BY user_id, position, id
    ) AS c
    WHERE c.user_id = u.id
    """
)

_COPY_BACK_FIRST_POLICY = sa.text(
    """
    UPDATE "user" AS u
    SET insurance_provider = p.provider,
        insurance_policy_number = p.number,
        insurance_expires_on = p.expires_on,
        insurance_notified_stage = p.notified_stage,
        insurance_notified_for = p.notified_for
    FROM (
        SELECT DISTINCT ON (user_id) user_id, provider, number, expires_on, notified_stage, notified_for
        FROM checkin_insurance_policy
        ORDER BY user_id, position, id
    ) AS p
    WHERE p.user_id = u.id
    """
)

# The columns as they stood, in the order the downgrade puts them back.
_USER_COLUMNS = (
    ("date_of_birth", sa.Date()),
    ("phone", sa.String(length=32)),
    ("emergency_contact_name", sa.String(length=255)),
    ("emergency_contact_phone", sa.String(length=32)),
    ("emergency_contact_relationship", sa.String(length=64)),
    ("insurance_provider", sa.String(length=255)),
    ("insurance_policy_number", sa.String(length=64)),
    ("insurance_expires_on", sa.Date()),
    ("insurance_notified_stage", sa.String(length=16)),
    ("insurance_notified_for", sa.Date()),
)


def upgrade() -> None:
    op.create_table(
        "checkin_details",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=True),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("date_of_birth", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_checkin_details_user_id"), "checkin_details", ["user_id"], unique=True)
    op.create_table(
        "checkin_emergency_contact",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column("relationship", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_checkin_emergency_contact_user_id_position",
        "checkin_emergency_contact",
        ["user_id", "position"],
        unique=False,
    )
    op.create_table(
        "checkin_insurance_policy",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=255), nullable=False),
        sa.Column("number", sa.String(length=64), nullable=True),
        sa.Column("expires_on", sa.Date(), nullable=True),
        sa.Column("notified_stage", sa.String(length=16), nullable=True),
        sa.Column("notified_for", sa.Date(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["user.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_checkin_insurance_policy_user_id_position",
        "checkin_insurance_policy",
        ["user_id", "position"],
        unique=False,
    )

    # Copy, then drop: the drop destroys what these read, and nothing puts it back.
    op.execute(_COPY_DETAILS)
    op.execute(_COPY_CONTACTS)
    op.execute(_COPY_POLICIES)
    for name, _ in _USER_COLUMNS:
        op.drop_column("user", name)


def downgrade() -> None:
    for name, column_type in _USER_COLUMNS:
        op.add_column("user", sa.Column(name, column_type, nullable=True))
    op.execute(_COPY_BACK_DETAILS)
    op.execute(_COPY_BACK_FIRST_CONTACT)
    op.execute(_COPY_BACK_FIRST_POLICY)

    op.drop_index("ix_checkin_insurance_policy_user_id_position", table_name="checkin_insurance_policy")
    op.drop_table("checkin_insurance_policy")
    op.drop_index("ix_checkin_emergency_contact_user_id_position", table_name="checkin_emergency_contact")
    op.drop_table("checkin_emergency_contact")
    op.drop_index(op.f("ix_checkin_details_user_id"), table_name="checkin_details")
    op.drop_table("checkin_details")
