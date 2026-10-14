from sqlalchemy.ext.asyncio import AsyncSession

from ..crud.crud_tags import tag_ids_by_name

# The words every account starts with, so the tag picker offers something on a diver's first
# dive. Each names a kind of dive no structured field records: entry, water type, altitude,
# depth and the dive's `type` all have fields of their own, and a tag repeating one would be a
# second place to say it. Lower case, as Subsurface's own default tags are - an import carrying
# `Night` lands on this row, the comparison being case-folded.
#
# The live definition. The revisions that backfilled existing accounts (`7025991d9229`, then
# `f3f873a94d1c` for the words added since) carry frozen copies, and are owed nothing by this
# one; a word added here needs a revision of its own to reach the accounts already there.
#
# Ordinary rows once seeded: the diver renames or deletes them like any other tag, and nothing
# puts a deleted one back.
DEFAULT_TAGS: tuple[str, ...] = ("cave", "cleanup", "drift", "ice", "night", "photo", "survey", "wreck")


async def seed_default_tags(db: AsyncSession, *, user_id: int) -> None:
    """Give the account whichever default tags it lacks. Does not commit: registration calls
    it inside the transaction that creates the `User` row, so the account and its tags land
    together or not at all."""
    await tag_ids_by_name(db, user_id=user_id, names=DEFAULT_TAGS)
