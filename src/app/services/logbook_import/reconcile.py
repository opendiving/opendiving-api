"""Holding a modified dive to the values its review showed, after the apply's pass has written.

The diver reviewed final values, and those persist even where the stored ones moved between the
review and the apply. The second pass cannot do that on its own: a fill writes blanks and never
overwrites, which is the right rule for a file and the wrong one here, where a value edited in
between would stand against the import the diver approved. So the value entries of a dive's
`changes` are written where the stored value is not the reviewed one.

Only those. An element - a recording, a file, a profile, a cylinder row - and a derived member -
a fix, `gas_use` - are what the second pass produces from the same bytes, and a mixture's link to
a gear item is not the import's to set; each is read back, and where it is not what the review
showed the row says so. A cylinder has no identity of its own: a patch replaces the whole list
and reads order it by row id, so a cylinder value is written by its index only while the list
kept its length and `gas_number` there and the row can hold it, and otherwise has nowhere to land.
"""

import uuid as uuid_pkg
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.utils.datetime_offset import split_dive_start_time
from ...models.dive_mixture import DiveMixture
from ...models.dive_recording import DiveRecording
from ...schemas.dive_mixture import DiveMixtureRead
from ...schemas.logbook_import import ImportDiveNote, ImportNoteCode
from ..dive_files import FILLABLE_MIXTURE_FIELDS, fill_is_storable
from ..dive_recordings import DECO_MODEL_COLUMNS, DEVICE_COLUMNS, READOUT_COLUMNS
from .changes import same_content, value_at

_MIXTURE_MEMBERS = frozenset({*FILLABLE_MIXTURE_FIELDS, "po2_limit", "role", "usage"})
_RECORDING_MEMBERS = frozenset({*READOUT_COLUMNS, "mode", "salinity"})

_CYLINDERS_MOVED = (
    "This dive's cylinders changed after the review, so a value the review showed for one was not written."
)
_ELEMENT_MOVED = "This dive changed after the review, so part of what the review showed did not reach it."


def _nested_columns(columns: dict[str, str], rest: list[str], after: Any) -> dict[str, Any] | None:
    """The columns of a nested member: one of its scalars, or the whole of it arriving at once."""
    if not rest:
        members = after or {}
        return {column: members.get(member) for member, column in columns.items()}
    column = columns.get(rest[0])
    return None if column is None or len(rest) > 1 else {column: after}


def _recording_columns(path: list[str], after: Any) -> dict[str, Any] | None:
    """The columns a recording member is stored in, or `None` for a member the pass derives."""
    head, *rest = path
    if head == "device":
        return _nested_columns(DEVICE_COLUMNS, rest, after)
    if head == "deco_model":
        return _nested_columns(DECO_MODEL_COLUMNS, rest, after)
    if head == "started_at" and not rest:
        if after is None:
            return {"start_time": None, "utc_offset_minutes": None}
        start_time, offset, _ = split_dive_start_time(datetime.fromisoformat(after))
        return {"start_time": start_time, "utc_offset_minutes": offset}
    return {head: after} if head in _RECORDING_MEMBERS and not rest else None


def _recording_with(stored: dict[str, Any], uuid: str) -> dict[str, Any] | None:
    return next((one for one in stored["recordings"] if one["uuid"] == uuid), None)


def _gradient_factors_hold(recording: dict[str, Any], columns: dict[str, Any]) -> bool:
    """`ck_dive_recording_deco_gf_low_within_high`, over the row the write would leave."""
    deco = recording["deco_model"] or {}
    low = columns.get("deco_gf_low", deco.get("gf_low"))
    high = columns.get("deco_gf_high", deco.get("gf_high"))
    return low is None or high is None or low <= high


class _Reconciliation:
    """One dive's value entries, gathered per row and written one `UPDATE` a row: `CHECK`s are not
    deferrable, so a pair of members written apart could break one between the two statements,
    and a row whose reviewed values no longer fit what it holds is left whole, as a fill leaves
    it, since a refusal here would fail the apply on every retry."""

    def __init__(self, db: AsyncSession, reviewed: dict[str, Any], stored: dict[str, Any]) -> None:
        self._db = db
        self._reviewed = reviewed
        self._stored = stored
        self._recordings: dict[str, dict[str, Any]] = {}
        self._mixtures: dict[int, dict[str, Any]] = {}
        self.wrote = False
        self.moved: set[str] = set()

    def _gather_recording(self, change: dict[str, Any], index: int, path: list[str]) -> bool:
        """Whether this entry is a recording's value, gathered where it has moved. Keyed by the
        recording's uuid, since a deletion or a promotion in between shifts every ordinal."""
        columns = _recording_columns(path, change["after"])
        if columns is None:
            return False
        reviewed = self._reviewed["recordings"][index]
        recording = _recording_with(self._stored, reviewed["uuid"])
        if recording is None:
            self.moved.add(_ELEMENT_MOVED)
        elif value_at(recording, "/".join(path)) != change["after"]:
            self._recordings.setdefault(recording["uuid"], {}).update(columns)
        return True

    def _gather_mixture(self, change: dict[str, Any], index: int, member: str) -> None:
        reviewed, stored = self._reviewed["mixtures"], self._stored["mixtures"]
        if len(stored) != len(reviewed) or stored[index]["gas_number"] != reviewed[index]["gas_number"]:
            self.moved.add(_CYLINDERS_MOVED)
        elif stored[index][member] != change["after"]:
            self._mixtures.setdefault(index, {})[member] = change["after"]

    def gather(self, change: dict[str, Any]) -> bool:
        """Whether this entry is a value entry, and so settled here."""
        head, *rest = change["member"].split("/")
        if head == "recordings" and len(rest) > 1:
            return self._gather_recording(change, int(rest[0]), rest[1:])
        if head == "mixtures" and len(rest) == 2 and rest[1] in _MIXTURE_MEMBERS:
            self._gather_mixture(change, int(rest[0]), rest[1])
            return True
        return False

    async def write(self) -> None:
        for uuid, columns in self._recordings.items():
            recording = _recording_with(self._stored, uuid)
            assert recording is not None
            if not _gradient_factors_hold(recording, columns):
                self.moved.add(_ELEMENT_MOVED)
                continue
            await self._db.execute(
                update(DiveRecording).where(DiveRecording.uuid == uuid_pkg.UUID(uuid)).values(**columns)
            )
            self.wrote = True
        for index, members in self._mixtures.items():
            row = self._stored["mixtures"][index]
            if not fill_is_storable(DiveMixtureRead.model_validate(row), members):
                self.moved.add(_CYLINDERS_MOVED)
                continue
            await self._db.execute(update(DiveMixture).where(DiveMixture.id == row["id"]).values(**members))
            self.wrote = True


@dataclass(frozen=True, slots=True)
class HeldValues:
    """What holding one dive's values came to: whether anything was written, which a caller
    answers with the dive's derived figures; the entries that are not values, for `unlanded`;
    and the sentences for the values with nowhere to land."""

    wrote: bool
    others: list[dict[str, Any]]
    moved: list[str]


async def hold_reviewed_values(
    db: AsyncSession, *, reviewed: dict[str, Any], changes: list[dict[str, Any]], stored: dict[str, Any]
) -> HeldValues:
    """Set every reviewed value of one dive that has a place to land.

    `reviewed` is the row's after-state and `stored` the dive as the second pass left it, both in
    the read shape.
    """
    reconciliation = _Reconciliation(db, reviewed, stored)
    others = [change for change in changes if not reconciliation.gather(change)]
    await reconciliation.write()
    return HeldValues(wrote=reconciliation.wrote, others=others, moved=sorted(reconciliation.moved))


def unlanded(changes: list[dict[str, Any]], stored: dict[str, Any]) -> list[str]:
    """The sentence for entries the second pass did not produce, once, or none."""
    landed = all(same_content(change["after"], value_at(stored, change["member"])) for change in changes)
    return [] if landed else [_ELEMENT_MOVED]


def review_note(message: str) -> ImportDiveNote:
    return ImportDiveNote(code=ImportNoteCode.REVIEW_NOT_HELD, kind=None, collection="dives", message=message)
