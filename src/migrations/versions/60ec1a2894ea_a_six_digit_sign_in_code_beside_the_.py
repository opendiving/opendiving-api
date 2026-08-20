"""a six-digit sign-in code beside the magic link

`authentication_request` grows a public `uuid` and the two columns that back the code the
sign-in email now prints next to the link: `code_hash` and `code_attempts`. See
*"The sign-in email carries a code as well as a link"* in `DECISIONS.md`.

Autogenerate drafted this and got both `NOT NULL` adds wrong in the same way - it emits
them with no default, which is fine against the empty table CI builds and fails outright
on any instance with a live sign-in request in flight. Both are added with a server
default and then have it dropped, so existing rows are filled and the models stay the only
place a default is declared (`migrations/env.py` doesn't set `compare_server_default`, so
leaving one behind would drift silently rather than being caught by `alembic check`).

`gen_random_uuid()` rather than `uuidv7()` for the backfill: v7's time ordering buys
nothing on a column that is only ever looked up by equality, and every row this touches is
a magic-link request that expires within the hour anyway. The app mints v7 for new rows
through `PublicUUIDMixin`; nothing reads either shape's structure.

Nothing backfills `code_hash`. A request created before this ran was emailed a link and no
code, so `NULL` - "this row has no code" - is the truth about it, and
`verify_email_code` rejects exactly that.

Revision ID: 60ec1a2894ea
Revises: c3c2c4dd4c27
Create Date: 2026-08-20 22:23:56.729844

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "60ec1a2894ea"
down_revision: str | None = "c3c2c4dd4c27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("authentication_request", sa.Column("code_hash", sa.String(length=64), nullable=True))
    op.add_column(
        "authentication_request",
        sa.Column("code_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "authentication_request",
        sa.Column("uuid", sa.UUID(), nullable=False, server_default=sa.text("gen_random_uuid()")),
    )

    op.alter_column("authentication_request", "code_attempts", server_default=None)
    op.alter_column("authentication_request", "uuid", server_default=None)

    op.create_index(op.f("ix_authentication_request_uuid"), "authentication_request", ["uuid"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_authentication_request_uuid"), table_name="authentication_request")
    op.drop_column("authentication_request", "uuid")
    op.drop_column("authentication_request", "code_attempts")
    op.drop_column("authentication_request", "code_hash")
