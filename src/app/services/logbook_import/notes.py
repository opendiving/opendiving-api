"""What an import tells a diver: one list of notes under each dive, and the rest grouped.

A note is the import's own - a record skipped, a value worked out - or a finding of the file
reader that read one of the import's files: a value the file did not record, worked out,
read one way where it could have been read another, or lost. Both reach a diver in one shape
(`ImportDiveNote`), on the row of the dive they are about, and everything about no row is one
`ImportNoteGroup` per habit. Three rules decide which findings reach a diver at all.

**A finding about how a file writes a value or names a record is never shown** (`HIDDEN`).

**A finding about one of the dive's values goes when the dive does not hold this file's value
there** (`left_out`): another file of the dive supplied it, so the line is not news. The
comparison is the dive as the import leaves it against this file's own reading, both as the
planner reads a document; `DECISIONS.md`, *"A finding goes when the dive's value is not the
file's"*, has the rest.

**Nothing is shown twice, and nothing vanishes into a cap**: a row lists `MAX_DIVE_NOTES` and
what it leaves out joins the groups. A row's note names no file, so a line one file of the row
repeats from another is listed once; a line one file gives twice is about two things, and stays.
"""

import uuid as uuid_pkg
from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import divejson
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.utils.datetime_offset import split_dive_start_time
from ...crud.crud_dive_mixtures import get_mixtures_for_dives
from ...models.dive import Dive
from ...models.dive_recording import DiveRecording
from ...schemas.dive_mixture import DiveMixtureRead
from ...schemas.logbook_import import (
    IMPORT_NOTE_KINDS,
    ImportDive,
    ImportDiveNote,
    ImportDocument,
    ImportNote,
    ImportNoteGroup,
)
from ...schemas.parsed_dive import DiveMixtureSchema
from ..dive_files import FILLABLE_MIXTURE_FIELDS, pair_cylinders
from ..recording_shape import READOUT_BOUNDS, READOUT_COLUMN, bounded, shape_readouts
from .planner import COLLECTIONS, DIVE_BOUNDS, read_cylinder, read_position

# How many notes one dive's row lists. Far more than any dive's files raise - a handful - and
# what keeps a logbook whose every dive repeats one habit from multiplying the report by it.
# What a row leaves out is grouped instead, so every note is shown once.
MAX_DIVE_NOTES = 20

# How many groups a report lists. A thousand-dive logbook with one habit per record makes
# thousands of notes, and grouping is where a cap can live, since the counts stay complete.
MAX_NOTE_GROUPS = 100

# Enough places to point at without becoming the report: "here, and here, and elsewhere".
MAX_NOTE_WHERES = 3

# **The reader's findings no diver is shown**, by code: those whose subject is how a file
# writes a value or names a record rather than a value of the dive. Readings a device writes
# as zero or below to mean it has none, two readings at one instant, an identity or a cylinder
# number taken from a record's place in the file, and gases the computer lists as disabled.
# A sentinel standing for a whole value the diver can fill in - a 0 bar start pressure, a
# position of exactly 0/0 - is an absence of that value and is shown.
HIDDEN = frozenset(
    {
        "identity_from_position",
        "gas_numbers_assigned",
        "negative_readings",
        "time_to_surface_zero",
        "readings_at_one_instant",
        "disabled_gases",
    }
)

# The kinds a finding about one of the dive's values can go for. A loss is a loss, and is
# never left out.
_COMPARED_KINDS = frozenset({"absent", "inferred", "resolved"})
_STATED_KINDS = frozenset({"inferred", "resolved"})

# Which part of a dive's start a finding about `started_at` is about, by its code.
_START_PARTS = {
    "start_time_zone_missing": "utc_offset",
    "start_time_in_utc": "utc_offset",
    "start_time_z_read_as_local": "utc_offset",
    "start_time_seconds_missing": "seconds",
    "start_time_of_day_missing": "date_only",
}
_FIGURES = frozenset({"submerged_time", "max_depth", "avg_depth", "rating"})
_CHOICES = frozenset({"current", "type"})
_POSITIONS = frozenset({"entry_position", "exit_position"})
_READOUTS = frozenset(bound.field for bound in READOUT_BOUNDS)


def _ignore(_: str) -> None:
    """A reading's drops are the planner's to note; a comparison only reads."""


@dataclass(frozen=True, slots=True)
class Finding:
    """One of a file's findings, and the record it is about: the record with the longest
    source path the finding's own starts with, segment by segment - the package's rule."""

    note: divejson.Note
    owner: uuid_pkg.UUID | None
    collection: str | None


def findings(document: ImportDocument, conversion: divejson.Conversion | None) -> list[Finding]:
    """A converted file's findings a diver may be shown, each beside the record it is about.

    A finding's path counts the *source's* records, and a dive the converter could not carry
    keeps its place in that count, so `dive/3` names a dive by `Conversion.sources` and never
    by position. One about a file, or a record that was not carried, is about none.
    """
    if conversion is None:
        return []
    owners = {where: uuid_pkg.UUID(uuid) for uuid, where in conversion.sources.items()}
    collections = {
        record.uuid: name
        for name in COLLECTIONS
        for record in getattr(document, name)
        if isinstance(getattr(record, "uuid", None), uuid_pkg.UUID)
    }
    found = []
    for note in conversion.notes:
        if note.code in HIDDEN:
            continue
        segments = note.where.split("/")
        owner = next(
            (owner for depth in range(len(segments), 0, -1) if (owner := owners.get("/".join(segments[:depth])))),
            None,
        )
        found.append(Finding(note, owner, None if owner is None else collections.get(owner)))
    return found


# ------------------------------------------------------------------ the comparison


@dataclass(frozen=True, slots=True)
class DiveSource:
    """One file's document dive, as the comparison reads it.

    `recordings` places each of the dive's recordings, by its index in the document, on the
    stored dive and ordinal the write gave it.
    """

    dive: ImportDive
    index: int
    converted: bool
    inferred: frozenset[str]
    recordings: dict[int, tuple[int, int]] = field(default_factory=dict)

    def path(self, member: str) -> str:
        return f"dives/{self.index}/{member}"


def inferred_paths(document: ImportDocument) -> frozenset[str]:
    """The members a document lists as worked out rather than recorded (`extensions.divejson.inferred`)."""
    extensions = document.extensions or {}
    own = extensions.get("divejson")
    pointers = own.get("inferred") if isinstance(own, dict) else None
    if not isinstance(pointers, list):
        return frozenset()
    return frozenset(pointer.lstrip("/") for pointer in pointers if isinstance(pointer, str))


@dataclass(frozen=True, slots=True)
class StoredDive:
    """The dive as the import leaves it, read for the comparison.

    `values` holds the dive's columns by the member name a finding uses, a start in its three
    parts; `readouts` each recording's by ordinal and column; `cylinders` the dive's rows in
    saved order.
    """

    values: dict[str, Any]
    readouts: dict[int, dict[str, float | None]]
    cylinders: list[DiveMixtureRead]


async def stored_dives(db: AsyncSession, dive_ids: Sequence[int]) -> dict[int, StoredDive]:
    """Each dive's stored values the comparison reads, three queries for any number of dives."""
    if not dive_ids:
        return {}
    rows = await db.execute(
        select(
            Dive.id,
            Dive.start_time,
            Dive.utc_offset_minutes,
            Dive.start_date_only,
            Dive.submerged_time,
            Dive.max_depth,
            Dive.avg_depth,
            Dive.rating,
            Dive.current,
            Dive.type,
            Dive.entry_latitude,
            Dive.entry_longitude,
            Dive.exit_latitude,
            Dive.exit_longitude,
        ).where(Dive.id.in_(dive_ids))
    )
    columns = [READOUT_COLUMN.get(name, name) for name in sorted(_READOUTS)]
    readouts: dict[int, dict[int, dict[str, float | None]]] = {}
    for row in await db.execute(
        select(
            DiveRecording.dive_id, DiveRecording.ordinal, *(getattr(DiveRecording, column) for column in columns)
        ).where(DiveRecording.dive_id.in_(dive_ids))
    ):
        readouts.setdefault(row.dive_id, {})[row.ordinal] = {column: getattr(row, column) for column in columns}
    cylinders = await get_mixtures_for_dives(db, list(dive_ids))
    return {
        row.id: StoredDive(
            values={
                "utc_offset": row.utc_offset_minutes,
                "seconds": (row.start_time.second, row.start_time.microsecond),
                "date_only": row.start_date_only,
                "submerged_time": row.submerged_time,
                "max_depth": row.max_depth,
                "avg_depth": row.avg_depth,
                "rating": row.rating,
                "current": row.current,
                "type": row.type,
                "entry_position": _pair(row.entry_latitude, row.entry_longitude),
                "exit_position": _pair(row.exit_latitude, row.exit_longitude),
            },
            readouts=readouts.get(row.id, {}),
            cylinders=cylinders.get(row.id, []),
        )
        for row in rows
    }


def _pair(latitude: float | None, longitude: float | None) -> tuple[float, float] | None:
    return None if latitude is None or longitude is None else (latitude, longitude)


def _start_parts(started_at: datetime | date | None) -> dict[str, Any]:
    if started_at is None:
        return {"utc_offset": None, "seconds": None, "date_only": None}
    start, offset, date_only = split_dive_start_time(started_at)
    return {"utc_offset": offset, "seconds": (start.second, start.microsecond), "date_only": date_only}


def _figures(dive: ImportDive) -> dict[str, Any]:
    """The dive's figures as the planner reads them, before it works out any it lacks."""
    figures = bounded(dive, DIVE_BOUNDS, _ignore)
    if "submerged_time" in figures:
        figures["submerged_time"] = int(figures["submerged_time"])
    maximum, average = figures.get("max_depth"), figures.get("avg_depth")
    if maximum is not None and average is not None and average > maximum:
        del figures["avg_depth"]
    return figures


def _dive_value(source: DiveSource, name: str) -> Any:
    dive = source.dive
    if name in _START_PARTS.values():
        return _start_parts(dive.started_at)[name]
    if name in _FIGURES:
        return _figures(dive).get(name)
    if name in _CHOICES:
        choice = getattr(dive, name)
        return None if choice is None else choice.value
    if name in _POSITIONS:
        return _pair(*read_position(getattr(dive, name), name, _ignore))
    return None  # pragma: no cover - every name `_slot` gives is one of the above


def _cylinder_rows(source: DiveSource) -> list[dict[str, Any]]:
    return [
        read_cylinder(cylinder, index, converted=source.converted, drop=_ignore)
        for index, cylinder in enumerate(source.dive.cylinders)
    ]


def _paired(source: DiveSource, stored: StoredDive) -> dict[int, int]:
    """Which of the dive's cylinder rows each of this file's cylinders is, by index -> row id.

    Paired by `pair_cylinders`, mix first and then order, the rule every write that lands a
    file's cylinders on a dive's rows pairs them by.
    """
    incoming = [DiveMixtureSchema(**row) for row in _cylinder_rows(source)]
    index_of = {id(cylinder): index for index, cylinder in enumerate(incoming)}
    pairs, _ = pair_cylinders(incoming, stored.cylinders)
    return {index_of[id(pair.incoming)]: pair.row.id for pair in pairs}


# Where a member of a document dive lands on the stored dive: ("dive", name),
# ("cylinder", row id, column) or ("recording", ordinal, column).
type _Slot = tuple[Hashable, ...]


def _slot(source: DiveSource, member: str, code: str, dive_id: int, stored: StoredDive) -> tuple[_Slot, str] | None:
    """Where `member` of `source`'s document lands on the stored dive, and the member path it
    is in that document - or `None` where the map cannot place it, which keeps its line."""
    prefix = f"dives/{source.index}/"
    if not member.startswith(prefix):
        return None
    parts = member[len(prefix) :].split("/")
    match parts:
        case ["started_at"] if code in _START_PARTS:
            return ("dive", _START_PARTS[code]), "started_at"
        case [name] if name in _FIGURES | _CHOICES | _POSITIONS:
            return ("dive", name), name
        case ["cylinders", index, column] if index.isdigit() and column in FILLABLE_MIXTURE_FIELDS:
            row = _paired(source, stored).get(int(index))
            return None if row is None else (("cylinder", row, column), member[len(prefix) :])
        case ["recordings", index, readout] if index.isdigit() and readout in _READOUTS:
            placed = source.recordings.get(int(index))
            if placed is None or placed[0] != dive_id or placed[1] not in stored.readouts:
                return None
            return ("recording", placed[1], readout), member[len(prefix) :]
    return None


def _stored_value(slot: _Slot, stored: StoredDive) -> Any:
    match slot:
        case ("dive", str(name)):
            return stored.values.get(name)
        case ("cylinder", row_id, str(column)):
            return next((getattr(row, column) for row in stored.cylinders if row.id == row_id), None)
        case ("recording", int(ordinal), str(readout)):
            return stored.readouts[ordinal].get(READOUT_COLUMN.get(readout, readout))
    return None  # pragma: no cover - `_slot` gives no other shape


def _own_value(source: DiveSource, slot: _Slot, stored: StoredDive) -> tuple[Any, str] | None:
    """What `source`'s own document holds at `slot`, and its path there, or `None` where it
    holds nothing that lands there."""
    match slot:
        case ("dive", str(name)):
            member = "started_at" if name in _START_PARTS.values() else name
            return _dive_value(source, name), member
        case ("cylinder", row_id, str(column)):
            index = next((index for index, row in _paired(source, stored).items() if row == row_id), None)
            if index is None:
                return None
            return _cylinder_rows(source)[index].get(column), f"cylinders/{index}/{column}"
        case ("recording", int(ordinal), str(readout)):
            index = next((index for index, placed in source.recordings.items() if placed[1] == ordinal), None)
            if index is None or index >= len(source.dive.recordings):
                return None
            readouts = shape_readouts(source.dive.recordings[index], _ignore)
            return readouts.get(READOUT_COLUMN.get(readout, readout)), f"recordings/{index}/{readout}"
    return None  # pragma: no cover - `_slot` gives no other shape


def left_out(
    note: divejson.Note, source: DiveSource, others: Iterable[DiveSource], dive_id: int, stored: StoredDive
) -> bool:
    """Whether a finding about one of the dive's values is no longer true of the dive.

    It goes when what the dive holds at its member - a value, or nothing - is not what this
    file's own document holds there; or, for a value worked out or read one way of several,
    when the dive holds a value another file of the row states outright. A loss never goes,
    and nor does a finding the map cannot place.
    """
    if note.kind not in _COMPARED_KINDS or note.member is None:
        return False
    placed = _slot(source, note.member, note.code, dive_id, stored)
    if placed is None:
        return False
    slot, _ = placed
    held = _stored_value(slot, stored)
    own = _own_value(source, slot, stored)
    if own is None:  # pragma: no cover - a member `_slot` places is one its own file holds a reading of
        return False
    if held != own[0]:
        return True
    if note.kind not in _STATED_KINDS or held is None:
        return False
    for other in others:
        theirs = _own_value(other, slot, stored)
        if theirs is not None and theirs[0] == held and other.path(theirs[1]) not in other.inferred:
            return True
    return False


# ------------------------------------------------------------------ the report


def import_note(note: ImportNote) -> ImportDiveNote:
    return ImportDiveNote(
        code=note.code.value, kind=IMPORT_NOTE_KINDS.get(note.code), collection=note.collection, message=note.message
    )


def finding_note(found: Finding) -> ImportDiveNote:
    return ImportDiveNote(
        code=found.note.code, kind=found.note.kind, collection=found.collection, message=found.note.message
    )


type LineKey = tuple[str, str | None, str]


def line_key(note: ImportDiveNote) -> LineKey:
    """What makes two notes the same line to a diver: neither names the file it came from."""
    return note.code, note.collection, note.message


def grouped(notes: Iterable[tuple[ImportDiveNote, str | None]]) -> tuple[list[ImportNoteGroup], int]:
    """Notes on no row as one group per code, collection and sentence, in first-seen order,
    each with up to `MAX_NOTE_WHERES` of its places - and how many groups the cap left out."""
    groups: dict[LineKey, ImportNoteGroup] = {}
    for note, where in notes:
        key = line_key(note)
        group = groups.get(key)
        if group is None:
            group = groups[key] = ImportNoteGroup(**note.model_dump(), count=0)
        group.count += 1
        if where is not None and len(group.wheres) < MAX_NOTE_WHERES:
            group.wheres.append(where)
    listed = list(groups.values())
    return listed[:MAX_NOTE_GROUPS], max(len(listed) - MAX_NOTE_GROUPS, 0)
