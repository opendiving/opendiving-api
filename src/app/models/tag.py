from typing import Any

from sqlalchemy import ColumnElement, ForeignKey, Index, SQLColumnExpression, String, func, text
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin

#: The collation `casefold()` folds under: Unicode full case folding, so `Großes Riff` and
#: `GROSSES RIFF` are one key where `lower()` keeps the ß. Built into Postgres 18.
FOLD_COLLATION = "pg_unicode_fast"


#: The index's expression, textual - see `Tag.__table_args__`.
FOLDED_NAME = text(f"casefold(name::text COLLATE {FOLD_COLLATION})")


def folded(column: SQLColumnExpression[str]) -> ColumnElement[Any]:
    """The key a tag is unique on, and every SQL lookup of a name compares on."""
    return func.casefold(column.collate(FOLD_COLLATION))


class Tag(Base, PublicUUIDMixin, TimestampMixin):
    """A word from the diver's own vocabulary, listed on dives through `dive_tag`.

    A record rather than a string on the dive so it renames across every dive and counts
    its dives. Unique per diver on the trimmed, case-folded name - DiveJSON's §3 rule 8 -
    which is a departure from the `lower(name)` of *"Case-insensitive per-user
    uniqueness"* in DECISIONS.md, on purpose.

    No `SoftDeleteMixin`, and nothing deletes a tag when its last dive drops it: the diver
    keeps the word until they delete it.
    """

    __tablename__ = "tag"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    # Stored trimmed, so the folded key is the trimmed one.
    name: Mapped[str] = mapped_column(String(64))

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # Spelled as Postgres reports it back, with the `varchar` -> `text` cast it inserts:
            # `alembic check` strips a `::` cast through to the end of its type name, which
            # swallows the collation too, so the two spellings of one expression compare equal
            # only when both carry a cast. The same expression as `folded(Tag.name)`, whose
            # queries this index serves.
            Index("ux_tag_user_id_name_folded", "user_id", FOLDED_NAME, unique=True),
            # Serves `GET /tags`: `WHERE user_id = ... ORDER BY name`, under the database's
            # default collation - the Unicode one above orders by code point.
            Index("ix_tag_user_id_name", "user_id", "name"),
        )
