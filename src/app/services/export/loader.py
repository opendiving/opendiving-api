"""One batched read of everything a diver's export contains.

Every writer in this package (UDDF, CSV, `logbook.divejson`) needs the same graph, so it is
read once into an `ExportBundle` and handed to all three rather than each of them
issuing its own queries. The read is deliberately flat: a fixed twenty-three `SELECT`s over
whole tables scoped to one `user_id`, with no per-dive query anywhere. A logbook is a few
hundred dives and a handful of sites, trips, courses and gear items, so "load the lot" costs
less
than the round trips a lazier shape would need - and the archive walks all of it anyway.

**The two things this does not load are the binary payloads**: uploaded exports and card
images live in the blob store rather than in the database at all now (this reads only
their `storage_key`-bearing rows' scalar columns), and `dive_profile.data` stays
`deferred`. Those are fetched one row at a time by whoever actually needs them
(`archive.py` for the blobs, `uddf.py`/`envelope.py` for the profile series), so peak
memory is one file plus one profile rather than a diver's entire history of both. That
is the one place this module accepts an N+1 on purpose.

**One collection is scoped through the dives rather than by column.** `species` has no
`user_id` at all - the catalog is global (see `models/species.py`) - so it is loaded by the
set of species ids this user's live dives actually reference, which is the closest thing to
"theirs" that exists. That is why it does not go through `_owned`, and why an export is a
projection of the catalog rather than a copy of it.

**Scoping is "everything the caller can still see".** The `user_id` filter is absolute,
and the only other filter is soft-delete liveness on the three tables that still have the
column - `Dive`, `GearServiceRecord` and `Certification`. Nothing is resurrected: this
module used to read deleted-but-still-referenced trips, dive sites, gear items and
schedules back so that a uuid in `logbook.divejson` (an `xs:IDREF` in UDDF) always resolved,
and those five tables are hard-deleted now, so a dangling reference cannot be created in
the first place. See `_owned`.
"""

import uuid as uuid_pkg
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...crud.crud_dive_mixtures import get_mixtures_for_dives
from ...crud.crud_trip_parts import get_parts_for_trips
from ...crud.crud_trips import EARLIEST_PART_START
from ...models.certification import Certification
from ...models.certification_file import CertificationFile
from ...models.course import Course
from ...models.dive import Dive
from ...models.dive_dive_site import DiveDiveSite
from ...models.dive_file import DiveFile
from ...models.dive_form_preset import DiveFormPreset
from ...models.dive_gear_item import DiveGearItem
from ...models.dive_profile import DiveProfile
from ...models.dive_recording import DiveRecording
from ...models.dive_site import DiveSite
from ...models.dive_species import DiveSpecies
from ...models.gear_item import GearItem
from ...models.gear_service_record import GearServiceRecord
from ...models.gear_service_schedule import GearServiceSchedule
from ...models.gear_set import GearSet
from ...models.gear_set_item import GearSetItem
from ...models.species import Species
from ...models.trip import Trip
from ...models.user import User
from ...schemas.certification import CertificationFileInfo
from ...schemas.dive import DiveFileInfo
from ...schemas.dive_mixture import DiveMixtureRead
from ...schemas.trip import TripPartRead
from ..certification_files import get_file_infos_for_certifications
from ..dive_profiles import ProfileGasAttribution, get_gas_attribution_for_dives
from ..dive_recordings import DECO_MODEL_COLUMNS, DEVICE_COLUMNS, get_file_infos_for_recordings


@dataclass(frozen=True, slots=True)
class ExportFileRow:
    """One stored export, as an export writer addresses it: the metadata plus the row id
    the digest map and the archive's path plan are keyed on."""

    id: int
    info: DiveFileInfo


@dataclass(frozen=True, slots=True)
class ExportRecordingRow:
    """One recording, with its files in attach order and its profile's summary.

    Deliberately **not** `RecordingRead`, the API's shape. That one publishes a device as an
    object and a start as a combined string, which is right for a response and wrong here:
    the writers need the column pair (`start_time`, `utc_offset_minutes`) to decide whether
    a recording's start differs from the dive's, and they need the file row ids the digest
    map is keyed on. Sharing the response model would have meant a second query for both.
    """

    id: int
    uuid: uuid_pkg.UUID
    ordinal: int
    device: dict[str, Any]
    # The recording's own settings, keyed by **member** name the way `device` is, with the
    # columns that are NULL left out - so `_recording` can ask "did this row record a model
    # at all" of an empty dict rather than of five nulls, exactly as it already does for the
    # device.
    mode: str | None
    deco_model: dict[str, Any]
    start_time: datetime | None
    utc_offset_minutes: int | None
    files: list[ExportFileRow]
    has_profile: bool


@dataclass(frozen=True, slots=True)
class ExportBundle:
    """A diver's whole logbook, minus the binary payloads.

    Collections are ordered the way the export should present them - dives oldest
    first, everything else the way its list endpoint sorts - so no writer has to sort
    and none of them can disagree about the order. That is what makes a golden-file
    test meaningful.

    The `*_by_dive` maps carry a key for every dive, so a dive with no cylinders reads
    as an empty list rather than a `KeyError` in a writer.
    """

    user: User
    dives: list[Dive]
    mixtures_by_dive: dict[int, list[DiveMixtureRead]]
    site_ids_by_dive: dict[int, list[int]]
    gear_ids_by_dive: dict[int, list[int]]
    species_ids_by_dive: dict[int, list[int]]
    # **One entry per dive, holding an ordered list of recordings**, where there used to be
    # one file and one profile per dive. Every writer in this package walks it: the DiveJSON
    # envelope writes `recordings[]` per dive, the archive plans a member per file, `dives.csv`
    # joins their filenames, and the UDDF writer takes the first recording's profile.
    recordings_by_dive: dict[int, list[ExportRecordingRow]]
    attribution_by_dive: dict[int, ProfileGasAttribution]
    trips: list[Trip]
    # Keyed for every trip in `trips`, so a trip with no parts at all reads as an empty
    # list rather than a `KeyError` in a writer - same contract as the `*_by_dive` maps.
    parts_by_trip: dict[int, list[TripPartRead]]
    courses: list[Course]
    dive_sites: list[DiveSite]
    gear_items: list[GearItem]
    # The species this user's live dives reference, and only those. Unlike every other list
    # here it is not "the user's rows" - there is no such thing for a global table - so an
    # export carries the slice of the catalog the logbook actually needs.
    species: list[Species]
    gear_sets: list[GearSet]
    item_ids_by_set: dict[int, list[int]]
    # The account's saved dive-form presets, alphabetically, as `GET /dive-form-presets`
    # serves them. Not logbook data - they ride in the `diver` member's extension, for
    # the reason that member carries `units` at all.
    dive_form_presets: list[DiveFormPreset]
    schedules: list[GearServiceSchedule]
    service_records: list[GearServiceRecord]
    certifications: list[Certification]
    cert_files_by_cert: dict[int, list[CertificationFileInfo]]
    # Keyed the way each file is addressed elsewhere: a dive-computer export by its own row
    # id (a dive has as many as its recordings hold), a certification image by side. Held
    # apart from the `*Info` schemas above because neither of those carries the digest -
    # they are response shapes, and a response has the `ETag` for that.
    dive_file_sha256: dict[int, str]
    cert_file_sha256: dict[tuple[int, str], str]

    trip_by_id: dict[int, Trip] = field(init=False)
    course_by_id: dict[int, Course] = field(init=False)
    dive_site_by_id: dict[int, DiveSite] = field(init=False)
    gear_item_by_id: dict[int, GearItem] = field(init=False)
    species_by_id: dict[int, Species] = field(init=False)
    schedule_by_id: dict[int, GearServiceSchedule] = field(init=False)

    def __post_init__(self) -> None:
        # `object.__setattr__` because the dataclass is frozen: these are lookup indexes
        # derived from the lists above rather than independent inputs, so building them
        # here keeps the two from ever drifting apart at a call site.
        object.__setattr__(self, "trip_by_id", {trip.id: trip for trip in self.trips})
        object.__setattr__(self, "course_by_id", {course.id: course for course in self.courses})
        object.__setattr__(self, "dive_site_by_id", {site.id: site for site in self.dive_sites})
        object.__setattr__(self, "gear_item_by_id", {item.id: item for item in self.gear_items})
        object.__setattr__(self, "species_by_id", {species.id: species for species in self.species})
        object.__setattr__(self, "schedule_by_id", {schedule.id: schedule for schedule in self.schedules})

    def sites_for(self, dive: Dive) -> list[DiveSite]:
        """A dive's sites in visit order; index 0 is the primary site."""
        return [site for site_id in self.site_ids_by_dive[dive.id] if (site := self.dive_site_by_id.get(site_id))]

    def gear_for(self, dive: Dive) -> list[GearItem]:
        """A dive's gear in the order the diver listed it."""
        return [item for item_id in self.gear_ids_by_dive[dive.id] if (item := self.gear_item_by_id.get(item_id))]

    def species_for(self, dive: Dive) -> list[Species]:
        """A dive's species in the order the diver listed them.

        The `.get()` here cannot miss the way the two above can: `species` is loaded *from*
        these very join rows, so every id in the map is in the list by construction.
        """
        return [
            species
            for species_id in self.species_ids_by_dive[dive.id]
            if (species := self.species_by_id.get(species_id))
        ]

    # Both of the above resolve through `.get()` rather than indexing, and there is exactly
    # one way left to hit the miss: a join row pointing at *another user's* site or item.
    # That cannot be created through the API - every write validates ownership first
    # (`resolve_dive_site_ids_for_user`, `resolve_gear_item_ids_for_user`) - so it would
    # mean hand-edited data. Skipping is the only defensible answer either way: the row is
    # not this caller's to export, and a 500 on the one endpoint that exists so a diver can
    # leave with their data is the worst possible failure mode for a bad row nobody can see.

    def trip_for(self, dive: Dive) -> Trip | None:
        """The dive's trip, or `None` - which is what a dive whose trip was deleted has,
        the FK's `ON DELETE SET NULL` having cleared the column."""
        return None if dive.trip_id is None else self.trip_by_id.get(dive.trip_id)

    def course_for(self, row: Dive | Certification) -> Course | None:
        """The training course a dive was logged on, or that issued a certification.

        One method for both because `course_id` means the same thing on either row, and
        both answer `None` the same way: the FK's `ON DELETE SET NULL` clears the column
        when the course goes, so a stale id never reaches this.
        """
        return None if row.course_id is None else self.course_by_id.get(row.course_id)


async def _ordered_ids_by_dive(
    db: AsyncSession, dive_ids: list[int]
) -> tuple[dict[int, list[int]], dict[int, list[int]], dict[int, list[int]]]:
    """The site, gear and species links for a set of dives, as ordered integer ids.

    The existing `get_dive_sites_for_dives`/`get_gear_items_for_dives`/
    `get_species_for_dives` return the *summaries* a dive response embeds, which would mean
    holding a second copy of every site, item and species alongside
    `ExportBundle.dive_sites`/`gear_items`/`species`. Export links by id into those lists
    instead, so it wants the join rows and nothing else.
    """
    sites: dict[int, list[int]] = {dive_id: [] for dive_id in dive_ids}
    gear: dict[int, list[int]] = {dive_id: [] for dive_id in dive_ids}
    species: dict[int, list[int]] = {dive_id: [] for dive_id in dive_ids}
    if not dive_ids:
        return sites, gear, species

    site_rows = await db.execute(
        select(DiveDiveSite.dive_id, DiveDiveSite.dive_site_id)
        .where(DiveDiveSite.dive_id.in_(dive_ids))
        .order_by(DiveDiveSite.dive_id, DiveDiveSite.position)
    )
    for row in site_rows:
        sites[row.dive_id].append(row.dive_site_id)

    gear_rows = await db.execute(
        select(DiveGearItem.dive_id, DiveGearItem.gear_item_id)
        .where(DiveGearItem.dive_id.in_(dive_ids))
        .order_by(DiveGearItem.dive_id, DiveGearItem.position)
    )
    for row in gear_rows:
        gear[row.dive_id].append(row.gear_item_id)

    species_rows = await db.execute(
        select(DiveSpecies.dive_id, DiveSpecies.species_id)
        .where(DiveSpecies.dive_id.in_(dive_ids))
        .order_by(DiveSpecies.dive_id, DiveSpecies.position)
    )
    for row in species_rows:
        species[row.dive_id].append(row.species_id)

    return sites, gear, species


async def _referenced_species(db: AsyncSession, species_ids_by_dive: dict[int, list[int]]) -> list[Species]:
    """The catalog rows a diver's dives point at, ordered by scientific name.

    The one read in this module that is **not** scoped by a `user_id` column, because
    `species` does not have one - it is a global table (see `models/species.py`). Scoping is
    through the join rows above, which were themselves read from this user's live dives, so
    an export still contains only what that logbook references and nothing about anybody
    else's.

    Ordered here rather than by a writer, matching the bundle's pre-sorted contract: `id`
    breaks ties so two species sharing a name still come out in a stable order run after run.
    """
    ids = {species_id for ids in species_ids_by_dive.values() for species_id in ids}
    if not ids:
        return []

    rows = await db.execute(select(Species).where(Species.id.in_(ids)).order_by(Species.scientific_name, Species.id))
    return list(rows.scalars().all())


async def _owned(db: AsyncSession, model: Any, *, user_id: int, order_by: Any) -> list[Any]:
    """One user's rows from a table, in a stable order - the live ones, where the table
    still has a notion of liveness.

    Three of the tables read through here soft-delete (`Dive`, `GearServiceRecord`,
    `Certification`); the rest hard-delete, and asking a `Trip` for `is_deleted`
    would be an `AttributeError` rather than a filter that quietly matches everything. The
    check is on the model rather than a per-call flag so that a soft-deleting table added
    to this bundle later is filtered by default: the failure mode of forgetting is a
    deleted dive appearing in a diver's export, which is the one direction that must not
    be the accident. `api.dependencies.fetch_owned_or_raise` branches the same way.

    This used to take a `still_referenced` set as well, and resurrect any dead row
    something else in the export still pointed at - a dive site on its dives, a gear item
    on its dives and sets, a trip on its dives, a schedule on its records. Without it a
    uuid in `logbook.divejson` named nothing the file defined, and the UDDF `xs:IDREF` of the
    same reference produced a document that would not validate. The five tables that
    needed it are hard-deleted now, so the join row goes with the row it points at and a
    dangling reference cannot exist to be repaired. See "The row goes, and so does
    everything pointing at it" in DECISIONS.md.
    """
    conditions = [model.user_id == user_id]
    if hasattr(model, "is_deleted"):
        conditions.append(model.is_deleted.is_(False))
    rows = await db.execute(select(model).where(*conditions).order_by(*order_by))
    return list(rows.scalars().all())


async def load_export_bundle(db: AsyncSession, *, user_id: int) -> ExportBundle:
    """Read one user's whole logbook.

    Raises `LookupError` if the user row is gone, which cannot happen through the
    endpoints (the caller is resolved from their own bearer token) but is worth failing
    loudly on rather than exporting an archive addressed to nobody.

    The order of the reads below still matters, though no longer for the reason it was
    written for - `_owned` used to need every referrer read before the table it referenced,
    so it knew which dead rows to bring back. What is left is ordinary data dependency:
    `dive_ids` comes from `dives`, `item_ids_by_set` from `gear_sets`, `parts_by_trip`
    from `trips`, `cert_files_by_cert` from `certifications`, and `species` from the join
    rows `_ordered_ids_by_dive` read. Reorder on those, not on
    the strength of the resurrection having gone. `courses` has no such dependency - it is
    read beside `trips` because that is where it belongs to a reader, not because anything
    needs it there.
    """
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise LookupError(f"No user with id {user_id}")

    # Chronological, which is the order a logbook is read in and the order UDDF's
    # repetition group implies. `dive_number` and `id` only break ties, so two dives
    # logged at the same instant still come out in a stable order run after run.
    dives = await _owned(db, Dive, user_id=user_id, order_by=(Dive.start_time, Dive.dive_number, Dive.id))
    dive_ids = [dive.id for dive in dives]

    site_ids_by_dive, gear_ids_by_dive, species_ids_by_dive = await _ordered_ids_by_dive(db, dive_ids)

    gear_sets = await _owned(db, GearSet, user_id=user_id, order_by=(GearSet.name, GearSet.id))
    item_ids_by_set: dict[int, list[int]] = {gear_set.id: [] for gear_set in gear_sets}
    if gear_sets:
        set_rows = await db.execute(
            select(GearSetItem.gear_set_id, GearSetItem.gear_item_id)
            .where(GearSetItem.gear_set_id.in_(item_ids_by_set))
            .order_by(GearSetItem.gear_set_id, GearSetItem.position)
        )
        for row in set_rows:
            item_ids_by_set[row.gear_set_id].append(row.gear_item_id)

    service_records = await _owned(
        db, GearServiceRecord, user_id=user_id, order_by=(GearServiceRecord.serviced_on, GearServiceRecord.id)
    )

    schedules = await _owned(
        db, GearServiceSchedule, user_id=user_id, order_by=(GearServiceSchedule.gear_item_id, GearServiceSchedule.id)
    )
    # A trip stores no dates, so oldest-first is the same correlated aggregate the list
    # endpoint orders by, read the other way up. `NULLS LAST` is spelled out because a trip
    # whose parts carry none has no span to place, and the end of the bundle is where it
    # belongs rather than the start.
    trips = await _owned(db, Trip, user_id=user_id, order_by=(EARLIEST_PART_START.asc().nulls_last(), Trip.id))
    # `id` breaks ties rather than `uuid`, for both of these and matching every other
    # collection here: the bundle's contract is a stable order run after run, not the list
    # endpoint's ordering (which runs newest-first with the dateless rows last).
    courses = await _owned(db, Course, user_id=user_id, order_by=(Course.start_date, Course.id))
    dive_sites = await _owned(db, DiveSite, user_id=user_id, order_by=(DiveSite.name, DiveSite.id))
    gear_items = await _owned(db, GearItem, user_id=user_id, order_by=(GearItem.name, GearItem.id))
    certifications = await _owned(
        db, Certification, user_id=user_id, order_by=(Certification.certified_on, Certification.id)
    )

    return ExportBundle(
        user=user,
        dives=dives,
        mixtures_by_dive=await get_mixtures_for_dives(db=db, dive_ids=dive_ids),
        site_ids_by_dive=site_ids_by_dive,
        gear_ids_by_dive=gear_ids_by_dive,
        species_ids_by_dive=species_ids_by_dive,
        recordings_by_dive=await _recordings_by_dive(db, dive_ids),
        attribution_by_dive=await get_gas_attribution_for_dives(db=db, dive_ids=dive_ids),
        trips=trips,
        # After the `trips` read above, which is what supplies the ids.
        parts_by_trip=await get_parts_for_trips(db=db, trip_ids=[trip.id for trip in trips]),
        courses=courses,
        dive_sites=dive_sites,
        gear_items=gear_items,
        # After `_ordered_ids_by_dive` above, which is what says which species to read.
        species=await _referenced_species(db, species_ids_by_dive),
        gear_sets=gear_sets,
        item_ids_by_set=item_ids_by_set,
        dive_form_presets=await _owned(
            db, DiveFormPreset, user_id=user_id, order_by=(DiveFormPreset.name, DiveFormPreset.id)
        ),
        schedules=schedules,
        service_records=service_records,
        certifications=certifications,
        cert_files_by_cert=await get_file_infos_for_certifications(
            db=db, certification_ids=[cert.id for cert in certifications]
        ),
        dive_file_sha256=await _dive_file_digests(db, dive_ids),
        cert_file_sha256=await _certification_file_digests(db, [cert.id for cert in certifications]),
    )


async def _dive_file_digests(db: AsyncSession, dive_ids: list[int]) -> dict[int, str]:
    """Every stored export's digest, keyed by **file row id** rather than by dive.

    Keyed per file because a dive now has as many as its recordings hold, and both readers
    of this map - the envelope's `sha256` member and the archive's path plan - address one
    file at a time. Keyed by the row id rather than by the file's uuid because that is what
    `ExportFileRow` already carries and what the two of them join on.
    """
    if not dive_ids:
        return {}
    rows = await db.execute(select(DiveFile.id, DiveFile.sha256).where(DiveFile.dive_id.in_(dive_ids)))
    return {row.id: row.sha256 for row in rows}


async def _recordings_by_dive(db: AsyncSession, dive_ids: list[int]) -> dict[int, list[ExportRecordingRow]]:
    """Every dive's recordings, in order, with their files and whether they have samples.

    Three queries for the whole logbook, matching this module's flat-read contract: the
    recordings, their files, and the set of recording ids that have a profile row. The
    profiles' *payloads* stay out of it deliberately - `envelope.py` and `uddf.py` load those
    one at a time, which is what keeps peak memory to one profile rather than a logbook's.
    """
    if not dive_ids:
        return {}

    rows = (
        (
            await db.execute(
                select(DiveRecording)
                .where(DiveRecording.dive_id.in_(dive_ids))
                .order_by(DiveRecording.dive_id, DiveRecording.ordinal)
            )
        )
        .scalars()
        .all()
    )
    recording_ids = [row.id for row in rows]
    files = await get_file_infos_for_recordings(db, recording_ids=recording_ids)
    file_ids = await _file_ids_by_recording(db, recording_ids)
    profiled = set(
        (await db.execute(select(DiveProfile.recording_id).where(DiveProfile.recording_id.in_(recording_ids))))
        .scalars()
        .all()
    )

    by_dive: dict[int, list[ExportRecordingRow]] = {dive_id: [] for dive_id in dive_ids}
    for row in rows:
        infos = files.get(row.id, [])
        ids = file_ids.get(row.id, [])
        by_dive.setdefault(row.dive_id, []).append(
            ExportRecordingRow(
                id=row.id,
                uuid=row.uuid,
                ordinal=row.ordinal,
                device={
                    member: getattr(row, column)
                    for member, column in DEVICE_COLUMNS.items()
                    if getattr(row, column) is not None
                },
                mode=row.mode,
                deco_model={
                    member: getattr(row, column)
                    for member, column in DECO_MODEL_COLUMNS.items()
                    if getattr(row, column) is not None
                },
                start_time=row.start_time,
                utc_offset_minutes=row.utc_offset_minutes,
                # `zip` is safe rather than lossy here: both come from `dive_file` filtered
                # on the same recording and ordered by `id`, so they are the same rows in the
                # same order by construction.
                files=[ExportFileRow(id=file_id, info=info) for file_id, info in zip(ids, infos, strict=True)],
                has_profile=row.id in profiled,
            )
        )
    return by_dive


async def _file_ids_by_recording(db: AsyncSession, recording_ids: list[int]) -> dict[int, list[int]]:
    """Each recording's file row ids, in attach order - the join key `get_file_infos_for_
    recordings` deliberately does not publish, because a response has no business carrying
    an internal id."""
    if not recording_ids:
        return {}
    rows = await db.execute(
        select(DiveFile.recording_id, DiveFile.id)
        .where(DiveFile.recording_id.in_(recording_ids))
        .order_by(DiveFile.recording_id, DiveFile.id)
    )
    ids: dict[int, list[int]] = {}
    for row in rows:
        ids.setdefault(row.recording_id, []).append(row.id)
    return ids


async def _certification_file_digests(db: AsyncSession, certification_ids: list[int]) -> dict[tuple[int, str], str]:
    if not certification_ids:
        return {}
    rows = await db.execute(
        select(CertificationFile.certification_id, CertificationFile.side, CertificationFile.sha256).where(
            CertificationFile.certification_id.in_(certification_ids)
        )
    )
    return {(row.certification_id, row.side): row.sha256 for row in rows}
