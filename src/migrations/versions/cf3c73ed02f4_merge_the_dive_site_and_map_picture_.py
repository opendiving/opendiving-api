"""merge the dive-site and map-picture heads

Revision ID: cf3c73ed02f4
Revises: 4989969ee80e, b4fa9c171ebb
Create Date: 2026-10-01 11:22:02.434052

"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "cf3c73ed02f4"
down_revision: str | Sequence[str] | None = ("4989969ee80e", "b4fa9c171ebb")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
