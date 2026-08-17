"""One batched read of everything a diver's export contains.

Every writer in this package (UDDF, CSV, `export.json`) needs the same graph, so it is
read once into an `ExportBundle` and handed to all three rather than each of them
issuing its own queries. The read is deliberately flat: a fixed twenty `SELECT`s over
whole tables scoped to one `user_id`, with no per-dive query anywhere. A logbook is a few
hundred dives and a handful of sites, trips and gear items, so "load the lot" costs less
than the round trips a lazier shape would need - and the archive walks all of it anyway.

**The two things this does not load are the binary payloads**: `dive_file.data` and
`certification_file.data` stay `deferred`, and `dive_profile.data` is not selected at
all. Those are fetched one row at a time by whoever actually needs them (`archive.py`
for the blobs, `uddf.py`/`envelope.py` for the profile series), so peak memory is one
file plus one profile rather than a diver's entire history of both. That is the one
place this module accepts an N+1 on purpose.

**Scoping is "everything the caller can still see", which is not the same as "what the
list endpoints return".** The `user_id` filter is absolute and never varies. The
soft-delete filter does: four tables go on showing a deleted row wherever something else
still references it - a dive site stays on its dives, a gear item on its dives and sets,
a trip on its dives, a schedule on its records - so those rows are read back too, flagged
`is_deleted` in `export.json`. See `_owned`, which is where that rule lives and where the
consequence of getting it wrong is spelled out.
"""

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...crud.crud_dive_mixtures import get_mixtures_for_dives
from ...crud.crud_trip_locations import get_locations_for_trips
from ...models.certification import Certification
from ...models.certification_file import CertificationFile
from ...models.dive import Dive
from ...models.dive_dive_site import DiveDiveSite
from ...models.dive_file import DiveFile
from ...models.dive_gear_item import DiveGearItem
from ...models.dive_site import DiveSite
from ...models.gear_item import GearItem
from ...models.gear_service_record import GearServiceRecord
from ...models.gear_service_schedule import GearServiceSchedule
from ...models.gear_set import GearSet
from ...models.gear_set_item import GearSetItem
from ...models.trip import Trip
from ...models.user import User
from ...schemas.certification import CertificationFileInfo
from ...schemas.dive import DiveFileInfo
from ...schemas.dive_mixture import DiveMixtureRead
from ...schemas.dive_profile import DiveProfileInfo
from ...schemas.trip import TripLocationRead
from ..certification_files import get_file_infos_for_certifications
from ..dive_files import get_file_infos_for_dives
from ..dive_profiles import ProfileGasAttribution, get_gas_attribution_for_dives, get_profile_infos_for_dives


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
    file_by_dive: dict[int, DiveFileInfo | None]
    profile_by_dive: dict[int, DiveProfileInfo | None]
    attribution_by_dive: dict[int, ProfileGasAttribution]
    trips: list[Trip]
    # Keyed for every trip in `trips`, so a trip nobody named a place for reads as an empty
    # list rather than a `KeyError` in a writer - same contract as the `*_by_dive` maps.
    locations_by_trip: dict[int, list[TripLocationRead]]
    dive_sites: list[DiveSite]
    gear_items: list[GearItem]
    gear_sets: list[GearSet]
    item_ids_by_set: dict[int, list[int]]
    schedules: list[GearServiceSchedule]
    service_records: list[GearServiceRecord]
    certifications: list[Certification]
    cert_files_by_cert: dict[int, list[CertificationFileInfo]]
    # Keyed the way each file is addressed elsewhere: a dive has at most one export, a
    # certification at most one image per side. Held apart from the `*Info` schemas above
    # because neither of those carries the digest - they are response shapes, and a
    # response has the `ETag` for that.
    dive_file_sha256: dict[int, str]
    cert_file_sha256: dict[tuple[int, str], str]

    trip_by_id: dict[int, Trip] = field(init=False)
    dive_site_by_id: dict[int, DiveSite] = field(init=False)
    gear_item_by_id: dict[int, GearItem] = field(init=False)
    schedule_by_id: dict[int, GearServiceSchedule] = field(init=False)

    def __post_init__(self) -> None:
        # `object.__setattr__` because the dataclass is frozen: these are lookup indexes
        # derived from the lists above rather than independent inputs, so building them
        # here keeps the two from ever drifting apart at a call site.
        object.__setattr__(self, "trip_by_id", {trip.id: trip for trip in self.trips})
        object.__setattr__(self, "dive_site_by_id", {site.id: site for site in self.dive_sites})
        object.__setattr__(self, "gear_item_by_id", {item.id: item for item in self.gear_items})
        object.__setattr__(self, "schedule_by_id", {schedule.id: schedule for schedule in self.schedules})

    def sites_for(self, dive: Dive) -> list[DiveSite]:
        """A dive's sites in visit order; index 0 is the primary site."""
        return [site for site_id in self.site_ids_by_dive[dive.id] if (site := self.dive_site_by_id.get(site_id))]

    def gear_for(self, dive: Dive) -> list[GearItem]:
        """A dive's gear in the order the diver listed it."""
        return [item for item_id in self.gear_ids_by_dive[dive.id] if (item := self.gear_item_by_id.get(item_id))]

    # Both of the above resolve through `.get()` rather than indexing, and after `_owned`
    # reads deleted-but-referenced rows back there is exactly one way left to hit the
    # miss: a join row pointing at *another user's* site or item. That cannot be created
    # through the API - every write validates ownership first (`resolve_dive_site_ids_for_user`,
    # `resolve_gear_item_ids_for_user`) - so it would mean hand-edited data. Skipping is
    # the only defensible answer either way: the row is not this caller's to export, and a
    # 500 on the one endpoint that exists so a diver can leave with their data is the
    # worst possible failure mode for a bad row nobody can see.

    def trip_for(self, dive: Dive) -> Trip | None:
        """The dive's trip, or `None` - including when the trip has since been deleted."""
        return None if dive.trip_id is None else self.trip_by_id.get(dive.trip_id)


async def _ordered_ids_by_dive(
    db: AsyncSession, dive_ids: list[int]
) -> tuple[dict[int, list[int]], dict[int, list[int]]]:
    """The site and gear links for a set of dives, as ordered integer ids.

    The existing `get_dive_sites_for_dives`/`get_gear_items_for_dives` return the
    *summaries* a dive response embeds, which would mean holding a second copy of every
    site and item alongside `ExportBundle.dive_sites`/`gear_items`. Export links by id
    into those lists instead, so it wants the join rows and nothing else.
    """
    sites: dict[int, list[int]] = {dive_id: [] for dive_id in dive_ids}
    gear: dict[int, list[int]] = {dive_id: [] for dive_id in dive_ids}
    if not dive_ids:
        return sites, gear

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

    return sites, gear


async def _owned(
    db: AsyncSession,
    model: Any,
    *,
    user_id: int,
    order_by: Any,
    still_referenced: set[int] | None = None,
) -> list[Any]:
    """One user's rows from a soft-deleting table: the live ones, plus any dead one that
    something else in this export still points at.

    The second half is not a nicety. A soft-deleted dive site **stays attached to the
    dives logged at it** (`erase_dive_site` flags the site and leaves the join rows), and
    the same is true of a gear item on its dives and gear sets (`erase_gear_item`, which
    leaves both join tables alone), of a trip on its dives (`erase_trip`), and of a service
    schedule on its records (which
    `_schedule_uuids_by_id` resolves with no `is_deleted` filter). Leaving those out
    would put a uuid in `export.json` that nothing in the file defines - and in UDDF,
    where the same reference is an `xs:IDREF`, would produce a document that does not
    validate.

    So the rule is: **an export holds every record something in it still references**,
    which for these four tables is a superset of what their list endpoints return. The
    resurrected rows carry `is_deleted: true` in `export.json`, so a reader can tell them
    from the live ones rather than being handed a site the diver thought they removed.

    Note this deliberately outlives what the *app* shows, and now on every surface: the
    dive reads stopped rendering deleted sites, trips and gear items
    (`get_dive_sites_for_dive`, `get_trip_uuids_by_ids`, `get_gear_items_for_dive`) and the
    gear-set reads stopped rendering deleted items (`get_gear_items_for_set`), so export is
    the only place left where a diver can see that a dive was logged at a site they since
    removed, or that a set once held kit they since deleted. The IDREF argument alone
    already requires the resurrection; being the last copy of the association is a
    consequence, not the reason.

    It is not a durable copy, and nothing here can make it one. A dive whose hidden site,
    trip or gear the diver edits away - which an ordinary `PATCH /dive` does silently,
    since the client submits back the shortened list it was shown - loses the row itself,
    and then there is nothing left for `still_referenced` to name. A `PATCH /gear-set`
    carrying `gear_item_uuids` severs a set's membership the same way. See "The links
    outlive the delete, but not the dive's next edit" in DECISIONS.md.

    One query rather than a filtered read plus a patch-up, so the ordering stays the
    database's and the `user_id` scope cannot be forgotten on the second pass.
    """
    live = model.is_deleted.is_(False)
    visible = live if not still_referenced else or_(live, model.id.in_(still_referenced))
    rows = await db.execute(select(model).where(model.user_id == user_id, visible).order_by(*order_by))
    return list(rows.scalars().all())


async def load_export_bundle(db: AsyncSession, *, user_id: int) -> ExportBundle:
    """Read one user's whole logbook.

    Raises `LookupError` if the user row is gone, which cannot happen through the
    endpoints (the caller is resolved from their own bearer token) but is worth failing
    loudly on rather than exporting an archive addressed to nobody.

    The order of the reads below is load-bearing: everything that *references* a
    soft-deleting table is read first, so `_owned` knows which dead rows have to come
    back with the live ones.
    """
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        raise LookupError(f"No user with id {user_id}")

    # Chronological, which is the order a logbook is read in and the order UDDF's
    # repetition group implies. `dive_number` and `id` only break ties, so two dives
    # logged at the same instant still come out in a stable order run after run.
    dives = await _owned(db, Dive, user_id=user_id, order_by=(Dive.start_time, Dive.dive_number, Dive.id))
    dive_ids = [dive.id for dive in dives]

    site_ids_by_dive, gear_ids_by_dive = await _ordered_ids_by_dive(db, dive_ids)

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

    # Schedules before gear items, because a schedule is itself a referrer. Deleting a
    # gear item soft-deletes its schedules and deliberately *keeps* its service records
    # (`soft_delete_schedules_for_gear_item`), so a live record drags back a dead schedule
    # which is then the only thing still naming a dead item.
    schedules = await _owned(
        db,
        GearServiceSchedule,
        user_id=user_id,
        order_by=(GearServiceSchedule.gear_item_id, GearServiceSchedule.id),
        still_referenced={
            record.gear_service_schedule_id for record in service_records if record.gear_service_schedule_id is not None
        },
    )

    trips = await _owned(
        db,
        Trip,
        user_id=user_id,
        order_by=(Trip.start_date, Trip.id),
        still_referenced={dive.trip_id for dive in dives if dive.trip_id is not None},
    )
    dive_sites = await _owned(
        db,
        DiveSite,
        user_id=user_id,
        order_by=(DiveSite.name, DiveSite.id),
        still_referenced={site_id for site_ids in site_ids_by_dive.values() for site_id in site_ids},
    )
    gear_items = await _owned(
        db,
        GearItem,
        user_id=user_id,
        order_by=(GearItem.name, GearItem.id),
        # Four referrers, and the last two are the ones easy to miss: an item stays in the
        # export because a dive used it, a set contains it, a service *record* logs work on
        # it, or a *schedule* is still measured against it. Deleting an item that was never
        # dived and never in a set but had one service logged reaches only the last two.
        still_referenced={item_id for item_ids in gear_ids_by_dive.values() for item_id in item_ids}
        | {item_id for item_ids in item_ids_by_set.values() for item_id in item_ids}
        | {record.gear_item_id for record in service_records}
        | {schedule.gear_item_id for schedule in schedules},
    )
    certifications = await _owned(
        db, Certification, user_id=user_id, order_by=(Certification.certified_on, Certification.id)
    )

    return ExportBundle(
        user=user,
        dives=dives,
        mixtures_by_dive=await get_mixtures_for_dives(db=db, dive_ids=dive_ids),
        site_ids_by_dive=site_ids_by_dive,
        gear_ids_by_dive=gear_ids_by_dive,
        file_by_dive=await get_file_infos_for_dives(db=db, dive_ids=dive_ids),
        profile_by_dive=await get_profile_infos_for_dives(db=db, dive_ids=dive_ids),
        attribution_by_dive=await get_gas_attribution_for_dives(db=db, dive_ids=dive_ids),
        trips=trips,
        # After `_owned`, so a soft-deleted trip that a dive still points at keeps its
        # places too - the export shows that trip, and a trip without its locations would
        # read as one the diver never said anything about.
        locations_by_trip=await get_locations_for_trips(db=db, trip_ids=[trip.id for trip in trips]),
        dive_sites=dive_sites,
        gear_items=gear_items,
        gear_sets=gear_sets,
        item_ids_by_set=item_ids_by_set,
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
    if not dive_ids:
        return {}
    rows = await db.execute(select(DiveFile.dive_id, DiveFile.sha256).where(DiveFile.dive_id.in_(dive_ids)))
    return {row.dive_id: row.sha256 for row in rows}


async def _certification_file_digests(db: AsyncSession, certification_ids: list[int]) -> dict[tuple[int, str], str]:
    if not certification_ids:
        return {}
    rows = await db.execute(
        select(CertificationFile.certification_id, CertificationFile.side, CertificationFile.sha256).where(
            CertificationFile.certification_id.in_(certification_ids)
        )
    )
    return {(row.certification_id, row.side): row.sha256 for row in rows}
