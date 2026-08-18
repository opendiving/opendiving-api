"""A hand-built `ExportBundle`, so the export writers can be tested without a database.

`load_export_bundle` is the only thing in `services/export` that touches Postgres; every
writer takes the bundle it returns and is otherwise pure. Constructing one here means the
UDDF, CSV and JSON tests run in the same sub-second, no-database mode as the rest of the
suite - and, more usefully, it means the awkward shapes can be *made* rather than hoped
for. The dev corpus is all single-tank air with one profile between five hundred dives;
trimix, gas switches, multi-cylinder pressure channels and a dive with no depth at all
only exist here.

The rows are unattached SQLAlchemy instances. `id` is `init=False` on every model, so it
is assigned after construction - that is what `_with_id` is for.
"""

import uuid as uuid_pkg
from datetime import UTC, date, datetime
from typing import Any

from src.app.models.certification import Certification
from src.app.models.dive import Dive
from src.app.models.dive_site import DiveSite
from src.app.models.gear_item import GearItem
from src.app.models.gear_service_record import GearServiceRecord
from src.app.models.gear_service_schedule import GearServiceSchedule
from src.app.models.gear_set import GearSet
from src.app.models.trip import Trip
from src.app.models.user import User
from src.app.schemas.certification import CertificationFileInfo, CertificationSide
from src.app.schemas.dive import DiveFileInfo
from src.app.schemas.dive_mixture import DiveMixtureRead
from src.app.schemas.dive_profile import DiveProfileInfo
from src.app.schemas.trip import TripLocationRead
from src.app.services.dive_profiles import ProfileGasAttribution
from src.app.services.export.loader import ExportBundle

# Fixed uuids, so a golden file stays golden. uuid7's first hex digit is its version
# nibble in the third group; nothing here depends on that, only on the values being
# stable and distinct.
UUIDS = {
    name: uuid_pkg.UUID(f"019f0000-0000-7000-8000-{index:012d}")
    for index, name in enumerate(
        (
            "user",
            "dive-air",
            "dive-trimix",
            "dive-bare",
            "site-reef",
            "site-wall",
            "trip",
            "gear-regulator",
            "gear-regulator-2",
            "gear-suit",
            "gear-other",
            "gear-set",
            "schedule",
            "record",
            "certification",
            "dive-file",
            "card-front",
            "card-back",
        )
    )
}

EXPORTED_AT = datetime(2026, 8, 14, 9, 30, tzinfo=UTC)
CREATED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _with_id[T](row: T, row_id: int) -> T:
    row.id = row_id  # type: ignore[attr-defined]
    return row


def make_user() -> User:
    return _with_id(
        User(
            name="Ada Lovelace",
            username="ada",
            email="ada@example.com",
            uuid=UUIDS["user"],
            created_at=CREATED_AT,
        ),
        1,
    )


def make_dive(row_id: int, uuid: uuid_pkg.UUID, **overrides: Any) -> Dive:
    defaults: dict[str, Any] = {
        "user_id": 1,
        "dive_number": row_id,
        # 08:15 local on a +02:00 offset, i.e. 06:15 UTC - so a test that reads the
        # exported string back can tell the combined form from the stored instant.
        "start_time": datetime(2026, 6, 1, 6, 15, tzinfo=UTC),
        "utc_offset_minutes": 120,
        "duration": 2700,
        "notes": "",
    }
    defaults.update(overrides)
    return _with_id(Dive(**defaults, uuid=uuid, created_at=CREATED_AT), row_id)


def make_dive_site(row_id: int, uuid: uuid_pkg.UUID, **overrides: Any) -> DiveSite:
    defaults: dict[str, Any] = {"user_id": 1, "name": "Yolanda"}
    defaults.update(overrides)
    return _with_id(DiveSite(**defaults, uuid=uuid, created_at=CREATED_AT), row_id)


def mixture(**overrides: Any) -> DiveMixtureRead:
    defaults: dict[str, Any] = {"id": 1, "volume": 12.0, "oxygen": 21.0, "helium": 0.0}
    defaults.update(overrides)
    return DiveMixtureRead(**defaults)


def build_bundle(
    *,
    dives: list[Dive] | None = None,
    mixtures_by_dive: dict[int, list[DiveMixtureRead]] | None = None,
    site_ids_by_dive: dict[int, list[int]] | None = None,
    gear_ids_by_dive: dict[int, list[int]] | None = None,
    file_by_dive: dict[int, DiveFileInfo | None] | None = None,
    profile_by_dive: dict[int, DiveProfileInfo | None] | None = None,
    trips: list[Trip] | None = None,
    locations_by_trip: dict[int, list[TripLocationRead]] | None = None,
    dive_sites: list[DiveSite] | None = None,
    gear_items: list[GearItem] | None = None,
    gear_sets: list[GearSet] | None = None,
    item_ids_by_set: dict[int, list[int]] | None = None,
    schedules: list[GearServiceSchedule] | None = None,
    service_records: list[GearServiceRecord] | None = None,
    certifications: list[Certification] | None = None,
    cert_files_by_cert: dict[int, list[CertificationFileInfo]] | None = None,
    dive_file_sha256: dict[int, str] | None = None,
    cert_file_sha256: dict[tuple[int, str], str] | None = None,
) -> ExportBundle:
    """An `ExportBundle` with every per-dive map defaulted to "nothing for any dive".

    A stored file always has a digest in the database (`sha256` is `NOT NULL`), so
    supplying `file_by_dive` without `dive_file_sha256` fills in a placeholder rather than
    producing a bundle that cannot exist - the export treats a missing digest as "the row
    vanished mid-read" and skips the file, which would silently empty half these tests.
    Pass both explicitly to exercise that path.
    """
    dives = dives or []
    dive_ids = [dive.id for dive in dives]
    files = file_by_dive or {}
    certificate_files = cert_files_by_cert or {}
    if dive_file_sha256 is None:
        dive_file_sha256 = {dive_id: "0" * 64 for dive_id, info in files.items() if info is not None}
    if cert_file_sha256 is None:
        cert_file_sha256 = {
            (cert_id, info.side.value): "0" * 64 for cert_id, infos in certificate_files.items() for info in infos
        }
    return ExportBundle(
        user=make_user(),
        dives=dives,
        mixtures_by_dive={**{dive_id: [] for dive_id in dive_ids}, **(mixtures_by_dive or {})},
        site_ids_by_dive={**{dive_id: [] for dive_id in dive_ids}, **(site_ids_by_dive or {})},
        gear_ids_by_dive={**{dive_id: [] for dive_id in dive_ids}, **(gear_ids_by_dive or {})},
        file_by_dive={**dict.fromkeys(dive_ids), **(file_by_dive or {})},
        profile_by_dive={**dict.fromkeys(dive_ids), **(profile_by_dive or {})},
        attribution_by_dive={dive_id: ProfileGasAttribution() for dive_id in dive_ids},
        trips=trips or [],
        locations_by_trip={**{trip.id: [] for trip in (trips or [])}, **(locations_by_trip or {})},
        dive_sites=dive_sites or [],
        gear_items=gear_items or [],
        gear_sets=gear_sets or [],
        item_ids_by_set={**{gear_set.id: [] for gear_set in (gear_sets or [])}, **(item_ids_by_set or {})},
        schedules=schedules or [],
        service_records=service_records or [],
        certifications=certifications or [],
        cert_files_by_cert=cert_files_by_cert or {},
        dive_file_sha256=dive_file_sha256,
        cert_file_sha256=cert_file_sha256,
    )


def full_bundle() -> ExportBundle:
    """The awkward-case logbook every writer test runs against.

    Three dives on purpose:

    - **air** - one cylinder, a trip, two sites in visit order, gear, and every optional
      scalar filled in, so nothing is exercised only by its absence.
    - **trimix** - two cylinders with different `po2_limit`s and `role`s, so the mix
      dedup key and the multi-cylinder paths are covered. It carries the stored file.
    - **bare** - no depth, no cylinders, no site, no trip, empty notes: the dive that
      makes UDDF's *mandatory* `<greatestdepth>` a decision rather than a copy.
    """
    reef = make_dive_site(
        1,
        UUIDS["site-reef"],
        name="Shark Reef",
        location="Ras Mohammed",
        latitude=27.7278,
        longitude=34.2564,
        notes="Current picks up after slack.",
    )
    wall = make_dive_site(2, UUIDS["site-wall"])
    trip = _with_id(
        Trip(
            user_id=1,
            name="Red Sea 2026",
            start_date=date(2026, 5, 30),
            end_date=date(2026, 6, 6),
            notes="Liveaboard",
            uuid=UUIDS["trip"],
            created_at=CREATED_AT,
        ),
        1,
    )
    # Two places, and deliberately unalike: one as the geocoder returned it, box and all,
    # and one the diver typed when the provider had nothing - the free-text escape hatch,
    # which every writer has to render without coordinates to lean on.
    trip_locations = [
        TripLocationRead(
            name="Sharm el-Sheikh",
            display_name="Sharm el-Sheikh, South Sinai, Egypt",
            latitude=27.9158,
            longitude=34.3300,
            bbox_south=27.8,
            bbox_north=28.0,
            bbox_west=34.2,
            bbox_east=34.4,
        ),
        TripLocationRead(name="Ras Mohammed"),
    ]
    regulator = _with_id(
        GearItem(
            user_id=1,
            name="XTX50",
            brand="Apeks",
            type="regulator",
            notes="Cold-water kit",
            uuid=UUIDS["gear-regulator"],
            created_at=CREATED_AT,
        ),
        1,
    )
    suit = _with_id(
        GearItem(
            user_id=1, name="Fusion", brand="Bare", type="drysuit", uuid=UUIDS["gear-suit"], created_at=CREATED_AT
        ),
        2,
    )
    # A second item of the *same brand*, which is the ordinary case for a diver who buys a
    # matched set - and the one that catches a `<manufacturer>` id keyed on the brand
    # instead of on the occurrence, since `xs:ID` has to be unique across the document.
    # Same `type` too, so it also covers two pieces inside one `equipmentType` element.
    second_regulator = _with_id(
        GearItem(
            user_id=1,
            name="XTX200",
            brand="Apeks",
            type="regulator",
            uuid=UUIDS["gear-regulator-2"],
            created_at=CREATED_AT,
        ),
        4,
    )
    # No `type` at all - the column is nullable, and it has to land somewhere in
    # `equipmentType` rather than being dropped.
    untyped = _with_id(GearItem(user_id=1, name="Slate", uuid=UUIDS["gear-other"], created_at=CREATED_AT), 3)

    gear_set = _with_id(GearSet(user_id=1, name="Tech", weight=6.0, uuid=UUIDS["gear-set"], created_at=CREATED_AT), 1)
    schedule = _with_id(
        GearServiceSchedule(
            user_id=1,
            gear_item_id=1,
            kind="service",
            starts_on=date(2026, 1, 1),
            interval_months=12,
            last_service_on=date(2026, 1, 1),
            next_due_on=date(2027, 1, 1),
            uuid=UUIDS["schedule"],
            created_at=CREATED_AT,
        ),
        1,
    )
    record = _with_id(
        GearServiceRecord(
            user_id=1,
            gear_item_id=1,
            kind="service",
            serviced_on=date(2026, 1, 1),
            dive_count_at_service=40,
            gear_service_schedule_id=1,
            performed_by="Blue Ocean",
            notes="Full strip",
            uuid=UUIDS["record"],
            created_at=CREATED_AT,
        ),
        1,
    )
    certification = _with_id(
        Certification(
            user_id=1,
            agency="padi",
            name="Open Water Diver",
            certification_number="1234567",
            certified_on=date(2019, 6, 1),
            notes="",
            uuid=UUIDS["certification"],
            created_at=CREATED_AT,
        ),
        1,
    )

    air = make_dive(
        1,
        UUIDS["dive-air"],
        max_depth=28.4,
        avg_depth=16.2,
        bottom_temperature=24.9,
        visibility=20,
        weight=6.5,
        water_type="salt",
        # Zero on purpose, and not a stand-in for "not recorded": the Red Sea is at sea
        # level, so 0 is what this dive's altitude honestly is - and it is the one value
        # that tells a `is not None` guard apart from a truthiness one, in the writers
        # that have to emit it and in the CSV cell that has to show it.
        altitude=0,
        trip_id=1,
        notes='Strong current, "the wall" was worth it.\nSaw a thresher.',
        cns_end=8.0,
        otu_end=21.0,
        surface_pressure_bar=1.013,
        entry_latitude=27.727800,
        entry_longitude=34.256400,
        exit_latitude=27.729100,
        exit_longitude=34.257200,
    )
    # An exit position and no entry one, which is not a half-filled dive but the ordinary
    # answer for a wrist computer: a receiver gets no fix underwater, and every GPS-
    # carrying export in the corpus logs its first fix *after* the diver surfaced. The
    # writers have to render one position of two without inventing the other.
    trimix = make_dive(
        2,
        UUIDS["dive-trimix"],
        max_depth=52.0,
        avg_depth=30.0,
        trip_id=1,
        notes="Deco 20 min",
        exit_latitude=27.731500,
        exit_longitude=34.259000,
    )
    bare = make_dive(3, UUIDS["dive-bare"], dive_number=3, duration=1200)

    return build_bundle(
        dives=[air, trimix, bare],
        mixtures_by_dive={
            1: [mixture(id=1, volume=12.0, start_pressure=200.0, end_pressure=70.0, oxygen=32.0, gas_number=1)],
            2: [
                mixture(
                    id=2,
                    volume=24.0,
                    oxygen=21.0,
                    helium=35.0,
                    start_pressure=232.0,
                    end_pressure=90.0,
                    po2_limit=1.4,
                    gas_number=1,
                    role="bottom",
                ),
                mixture(id=3, volume=11.1, oxygen=50.0, start_pressure=200.0, po2_limit=1.6, gas_number=2, role="deco"),
            ],
        },
        site_ids_by_dive={1: [1, 2], 2: [2]},
        gear_ids_by_dive={1: [1, 2, 3], 2: [1]},
        file_by_dive={
            2: DiveFileInfo(
                uuid=UUIDS["dive-file"],
                original_filename="Suunto Ocean 2026-06-01.json",
                content_type="application/json",
                byte_size=2048,
                parser_key="suunto_json",
            )
        },
        profile_by_dive={
            2: DiveProfileInfo(
                uuid=UUIDS["dive-file"], duration_seconds=2700, depth_sample_count=4, channels=["depth"], max_depth=52.0
            )
        },
        trips=[trip],
        locations_by_trip={1: trip_locations},
        dive_sites=[reef, wall],
        gear_items=[regulator, second_regulator, suit, untyped],
        gear_sets=[gear_set],
        item_ids_by_set={1: [1, 2]},
        schedules=[schedule],
        service_records=[record],
        certifications=[certification],
        cert_files_by_cert={
            1: [
                CertificationFileInfo(
                    uuid=UUIDS["card-front"],
                    side=CertificationSide.FRONT,
                    content_type="image/jpeg",
                    byte_size=1024,
                    original_filename="card front.jpg",
                ),
                CertificationFileInfo(
                    uuid=UUIDS["card-back"],
                    side=CertificationSide.BACK,
                    content_type="image/png",
                    byte_size=2048,
                    original_filename="card back.png",
                ),
            ]
        },
        dive_file_sha256={2: "a" * 64},
        cert_file_sha256={(1, "front"): "b" * 64, (1, "back"): "c" * 64},
    )


# The profile the `trimix` dive carries, in the stored integer scales: depth in cm,
# temperature in 0.1 C, pressure in 0.1 bar. Three channels and two events, every reading
# landing on a depth sample - the ordinary case, where `uddf.py::_waypoints` has nothing
# to snap. `OFF_GRID_PROFILE` below is the one that disagrees.
TRIMIX_PROFILE: dict[str, Any] = {
    "depth": {"t": [0, 30, 60, 90], "v": [0, 1800, 5200, 300]},
    "ceiling": {"t": [60, 90], "v": [600, 300]},
    "temperature": {"t": [0, 60], "v": [249, 181]},
    "pressure": [
        {"gas_number": 1, "t": [0, 60], "v": [2320, 1400]},
        {"gas_number": 2, "t": [90], "v": [2000]},
        # A cylinder the dive has no mixture for: its readings have no `<mix>` to point
        # at and must be dropped from the UDDF rather than emitted with a dangling ref.
        {"gas_number": 9, "t": [30], "v": [1111]},
    ],
    "events": [
        {"t": 0, "type": "gas_switch", "gas_number": 1},
        {"t": 90, "type": "gas_switch", "gas_number": 2},
        {"t": 60, "type": "safety_stop"},
        {"t": 60, "type": "other", "label": "Ceiling Broken"},
    ],
}

# The same shape, with every non-depth reading deliberately *between* depth samples - what
# a real device produces, and the only fixture that exercises the snapping in
# `uddf.py::_waypoints`. The depth axis is 0/10/20/30 and the readings are placed to pin
# each rule: 4 -> 0 and 27 -> 30 (plain nearest), 12 and 13 both -> 10 with the closer one
# winning (13's 99.9 C is absurd on purpose - it is what a last-wins bug would emit), 15 ->
# 10 on a tie the earlier sample takes, 7 and 8 -> 10 as two markers on one waypoint, and
# and the switch at 24 -> 30, because a switch is never moved backwards.
OFF_GRID_PROFILE: dict[str, Any] = {
    "depth": {"t": [0, 10, 20, 30], "v": [0, 1000, 2000, 1500]},
    "temperature": {"t": [4, 12, 13, 27], "v": [250, 240, 999, 220]},
    "pressure": [{"gas_number": 1, "t": [15], "v": [2000]}],
    "events": [
        {"t": 7, "type": "safety_stop"},
        {"t": 8, "type": "other", "label": "Deco"},
        {"t": 24, "type": "gas_switch", "gas_number": 1},
    ],
}
