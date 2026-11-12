"""Merge independent unit preferences and dive-computer recording tags."""

from collections.abc import Sequence

revision: str = "e18246c9ab70"
down_revision: tuple[str, str] = ("a8c9743e21b0", "3b5134d444b3")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
