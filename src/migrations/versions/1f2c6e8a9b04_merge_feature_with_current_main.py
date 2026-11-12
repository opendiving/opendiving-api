"""Merge unit preferences with the current main migration head."""

from collections.abc import Sequence

revision: str = "1f2c6e8a9b04"
down_revision: tuple[str, str] = ("e18246c9ab70", "65940d01a99c")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
