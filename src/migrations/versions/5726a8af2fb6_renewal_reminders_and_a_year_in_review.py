"""renewal reminders and a year in review

Revision ID: 5726a8af2fb6
Revises: a9b7dc451f00
Create Date: 2026-09-26 06:54:34.993682

Two opt-out preferences on `user`, true for every existing account through their server
default, and the bookkeeping the two worker jobs keep: the (stage, date) a certification or
the insurance was last reminded about, and the year a diver's review was last sent for.
Additive, with nothing to backfill: a null is "never sent", which is true of every row here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5726a8af2fb6"
down_revision: str | None = "a9b7dc451f00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("certification", sa.Column("expiry_notified_stage", sa.String(length=16), nullable=True))
    op.add_column("certification", sa.Column("expiry_notified_for", sa.Date(), nullable=True))
    op.add_column("user", sa.Column("renewal_reminder_emails", sa.Boolean(), server_default="true", nullable=False))
    op.add_column("user", sa.Column("year_in_review_emails", sa.Boolean(), server_default="true", nullable=False))
    op.add_column("user", sa.Column("insurance_notified_stage", sa.String(length=16), nullable=True))
    op.add_column("user", sa.Column("insurance_notified_for", sa.Date(), nullable=True))
    op.add_column("user", sa.Column("year_in_review_sent_for", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("user", "year_in_review_sent_for")
    op.drop_column("user", "insurance_notified_for")
    op.drop_column("user", "insurance_notified_stage")
    op.drop_column("user", "year_in_review_emails")
    op.drop_column("user", "renewal_reminder_emails")
    op.drop_column("certification", "expiry_notified_for")
    op.drop_column("certification", "expiry_notified_stage")
