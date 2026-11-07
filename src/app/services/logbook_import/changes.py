"""What an import changes on a dive: the difference between two reads of it.

The planner cannot say. A fill decides inside its SQL which blanks it writes, and no plan object
carries per-member intents, so a predicate over the plan would be a second account of the writer
that has to be kept true by hand. The import reads the dive through the detail route's loader
before the first file writes to it and after the last, and the changes are the members those two
documents disagree on - a fact about the write rather than a forecast of it. A pure function of
the two, so nothing here knows what a recording is.
"""

from typing import Any

# What the write itself mints rather than says: identity and bookkeeping, at every depth. A
# recording's `uuid` is new on every pass, and `attach_order` a file's rank among its dive's.
_MINTED = frozenset({"id", "uuid", "user_uuid", "created_at", "updated_at", "attach_order"})

# A reference to a record, compared whole: which record it names is the content, so the rule
# above must not reach inside it.
_REFERENCES = frozenset({"gear_item"})


def _entry(member: str, before: Any, after: Any) -> dict[str, Any]:
    return {"member": member, "before": before, "after": after}


def _walk(member: str, before: Any, after: Any, entries: list[dict[str, Any]]) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in (*before, *(key for key in after if key not in before)):
            if key in _MINTED:
                continue
            path = f"{member}/{key}" if member else key
            if key not in _REFERENCES:
                _walk(path, before.get(key), after.get(key), entries)
            elif before.get(key) != after.get(key):
                entries.append(_entry(path, before.get(key), after.get(key)))
    elif isinstance(before, list) and isinstance(after, list):
        for index in range(max(len(before), len(after))):
            path = f"{member}/{index}"
            if index >= len(before):
                entries.append(_entry(path, None, after[index]))
            elif index >= len(after):
                entries.append(_entry(path, before[index], None))
            else:
                _walk(path, before[index], after[index], entries)
    elif before != after:
        entries.append(_entry(member, before, after))


def dive_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """Every member whose value differs between two reads of one dive, in the read's order: a
    scalar as one entry, and a list element added or removed as one entry naming the element."""
    entries: list[dict[str, Any]] = []
    _walk("", before, after, entries)
    return entries


def same_content(one: Any, other: Any) -> bool:
    """Whether two values of a member say the same, whatever the write minted for each."""
    entries: list[dict[str, Any]] = []
    _walk("value", one, other, entries)
    return not entries


def value_at(document: Any, member: str) -> Any:
    """What a read holds at a change's `member`, or `None` where it holds nothing there."""
    value = document
    for step in member.split("/"):
        if isinstance(value, dict):
            value = value.get(step)
        elif isinstance(value, list) and step.isdigit() and int(step) < len(value):
            value = value[int(step)]
        else:
            return None
    return value
