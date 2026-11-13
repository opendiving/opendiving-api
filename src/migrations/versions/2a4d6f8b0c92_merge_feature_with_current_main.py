"""Merge MCP authorization with the current main migration head."""

from collections.abc import Sequence

revision: str = "2a4d6f8b0c92"
down_revision: tuple[str, str] = ("fb31c1ea56da", "65940d01a99c")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
