"""The shape of `export.json` - the complete, structured copy of a diver's logbook.

This is the half of the export that answers *"rebuild my logbook"*, where the UDDF
document answers *"take my dives anywhere"*. UDDF is a dive-interchange format and has no
slot for gear sets, service history, c-card records, a cylinder's `role`, or the order a
drift dive visited its sites; all of that lives here, alongside everything UDDF does
carry, so nothing a diver entered is reachable only through the lossy file.

**Versioned from day one.** `format` and `version` are the first two keys so that a
reader can dispatch on them before parsing anything else, and `version` is an integer
that increments when the shape changes incompatibly. Adding a field is not a version
bump; removing or re-meaning one is.

Resources reference each other by public `uuid`, never by the internal integer ids -
those are an implementation detail of this database and would be actively misleading in
a file that outlives it.

Values are **not** re-scaled or re-unitised: depths are meters, pressures bar,
temperatures Celsius, durations seconds, exactly as the API serves them, and the embedded
profile keeps the integer scales `GET /dive/{uuid}/profile` uses (see `dive_profile.py`
for why they are integers). The one derived value anywhere in here is `archive_path`,
which is a fact about the zip rather than about the logbook.
"""

import uuid as uuid_pkg
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, Field

from ..core.schemas import PublicUUIDSchema
from .certification import CertificationAgency, CertificationSide
from .dive import DiveStartTime
from .dive_mixture import DiveMixtureBase
from .dive_profile import DiveProfileRead
from .gear_item import GearType
from .gear_service import ServiceKind

EXPORT_FORMAT = "opendiving-export"
# Still 1 after a trip's free-text `location` became the structured `locations` list below,
# which the rule above would otherwise increment for: the app is pre-launch and nothing has
# ever read a version-1 file, so there is no reader for the bump to tell anything.
EXPORT_VERSION = 1


class ExportGenerator(BaseModel):
    """What wrote the file. Mirrors UDDF's `<generator>`, for the same reason: a reader
    that hits something odd wants to know which version produced it."""

    name: str
    version: str | None = None


class ExportUser(PublicUUIDSchema):
    name: str
    username: str
    email: str
    # A diver-set preference, and the only account-level setting there is. Here because
    # `/export/archive` says nothing in the account is reachable only through the app.
    gear_service_emails: bool
    created_at: datetime


class ExportStoredFile(PublicUUIDSchema):
    """A binary the archive carries: a dive-computer export, or one side of a c-card.

    `sha256` is the stored digest of the bytes, not one computed at export time, so
    checking a extracted file against it verifies the whole round trip - database column
    to zip member - rather than just that the zip is internally consistent.

    `archive_path` is null when `export.json` is produced outside an archive - the writer
    supports it, but only the archive endpoint uses it today, and there is no zip for the
    path to point into otherwise.
    """

    original_filename: str
    content_type: str
    byte_size: int
    sha256: str
    archive_path: str | None = None


class ExportDiveFile(ExportStoredFile):
    parser_key: Annotated[str, Field(description="Identifier of the parser that read this file, e.g. `suunto_xml`")]


class ExportCertificationFile(ExportStoredFile):
    side: CertificationSide


class ExportDive(PublicUUIDSchema):
    """One dive, with everything that hangs off it embedded rather than referenced.

    `dive_site_uuids` is in visit order - index 0 is the primary site - which is the
    ordering UDDF cannot express and the reason this list is here at all.

    `profile` is the full per-sample payload, in the same integer scales the API serves,
    so the JSON alone can redraw every curve without re-parsing `source_file`.
    """

    dive_number: int
    start_time: DiveStartTime
    duration: Annotated[int, Field(description="Dive duration in seconds")]
    notes: str
    max_depth: float | None = None
    avg_depth: float | None = None
    bottom_temperature: Annotated[float | None, Field(default=None, description="In degrees Celsius")]
    visibility: Annotated[int | None, Field(default=None, description="Underwater visibility in meters")]
    weight: Annotated[float | None, Field(default=None, description="Total ballast carried, in kilograms")]
    cns_start: float | None = None
    cns_end: float | None = None
    otu_start: float | None = None
    otu_end: float | None = None
    surface_pressure_bar: float | None = None
    # Decimal degrees, as stored. UDDF 3.2.2 has nowhere to put a per-dive position - its
    # only `<geography>` hangs off a `<site>`, and neither `informationbeforedive` nor
    # `waypoint` has a coordinate element - so this file and `dives.csv` are the only two
    # export formats that carry them. See DECISIONS.md.
    entry_latitude: float | None = None
    entry_longitude: float | None = None
    exit_latitude: float | None = None
    exit_longitude: float | None = None
    trip_uuid: uuid_pkg.UUID | None = None
    dive_site_uuids: Annotated[list[uuid_pkg.UUID], Field(default_factory=list, description="In visit order")]
    gear_item_uuids: Annotated[list[uuid_pkg.UUID], Field(default_factory=list, description="In the diver's own order")]
    # `DiveMixtureBase` rather than the API's `DiveMixtureRead`, which carries the
    # internal row `id`. Nothing here references a cylinder, so that id would be the
    # one integer key in the file - see this module's docstring.
    mixtures: Annotated[list[DiveMixtureBase], Field(default_factory=list)]
    source_file: ExportDiveFile | None = None
    profile: DiveProfileRead | None = None
    created_at: datetime


class ExportTripLocation(BaseModel):
    """One place a trip went, as the geocoder described it when the diver picked it.

    A value object with no `uuid`, because it has none to export: trip locations are per-trip
    rows replaced wholesale with the trip, so nothing in this file - or in the database -
    references one. The bounding box travels with the point because it is what the geocoder
    said the place *covers*, and a reader redrawing the trip's map wants the region rather
    than a pin in the middle of a country.
    """

    name: str
    display_name: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    bbox_south: float | None = None
    bbox_north: float | None = None
    bbox_west: float | None = None
    bbox_east: float | None = None


class ExportTrip(PublicUUIDSchema):
    name: str
    locations: Annotated[
        list[ExportTripLocation],
        Field(default_factory=list, description="Places this trip went to, in the order the diver listed them"),
    ]
    start_date: date
    end_date: date | None = None
    notes: str
    is_deleted: Annotated[
        bool,
        Field(
            default=False,
            description="True for a record the diver deleted that something in this export still references - "
            "the app goes on showing those too. Present so a reader can tell them apart rather than being handed "
            "one back as if it were live.",
        ),
    ]
    created_at: datetime


class ExportDiveSite(PublicUUIDSchema):
    name: str
    location: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    notes: str
    is_deleted: Annotated[
        bool,
        Field(
            default=False,
            description="True for a record the diver deleted that something in this export still references - "
            "the app goes on showing those too. Present so a reader can tell them apart rather than being handed "
            "one back as if it were live.",
        ),
    ]
    created_at: datetime


class ExportGearItem(PublicUUIDSchema):
    name: str
    brand: str | None = None
    type: GearType | None = None
    notes: str
    rented: bool
    is_archived: bool
    archived_at: datetime | None = None
    dive_count: int
    is_deleted: Annotated[
        bool,
        Field(
            default=False,
            description="True for a record the diver deleted that something in this export still references - "
            "the app goes on showing those too. Present so a reader can tell them apart rather than being handed "
            "one back as if it were live.",
        ),
    ]
    created_at: datetime


class ExportGearSet(PublicUUIDSchema):
    name: str
    weight: float | None = None
    gear_item_uuids: Annotated[list[uuid_pkg.UUID], Field(default_factory=list, description="In the set's own order")]
    created_at: datetime


class ExportGearServiceSchedule(PublicUUIDSchema):
    gear_item_uuid: uuid_pkg.UUID
    kind: ServiceKind
    label: str | None = None
    starts_on: date
    interval_months: int | None = None
    interval_dives: int | None = None
    dive_count_at_start: int
    is_active: bool
    last_service_on: date | None = None
    next_due_on: date | None = None
    next_due_at_dive_count: int | None = None
    is_deleted: Annotated[
        bool,
        Field(
            default=False,
            description="True for a record the diver deleted that something in this export still references - "
            "the app goes on showing those too. Present so a reader can tell them apart rather than being handed "
            "one back as if it were live.",
        ),
    ]
    created_at: datetime


class ExportGearServiceRecord(PublicUUIDSchema):
    gear_item_uuid: uuid_pkg.UUID
    gear_service_schedule_uuid: uuid_pkg.UUID | None = None
    kind: ServiceKind
    serviced_on: date
    dive_count_at_service: int
    label: str | None = None
    performed_by: str | None = None
    notes: str
    created_at: datetime


class ExportCertification(PublicUUIDSchema):
    agency: CertificationAgency
    agency_other: str | None = None
    name: str
    certification_number: str | None = None
    certified_on: date | None = None
    expires_on: date | None = None
    instructor_name: str | None = None
    instructor_number: str | None = None
    training_center: str | None = None
    notes: str
    files: Annotated[list[ExportCertificationFile], Field(default_factory=list)]
    created_at: datetime


class ExportEnvelope(BaseModel):
    """The whole document.

    **Never used to serialize.** `services/export/envelope.py` streams the file a record
    at a time so a thousand-dive log with its profiles never sits in memory whole, which
    means this model would drift out of step with the real output if nothing checked it.
    `tests/test_export_json.py` closes that gap by validating the streamed bytes against
    this model - so it is the format's specification, kept honest by a test rather than
    by being on the write path.
    """

    format: str = EXPORT_FORMAT
    version: int = EXPORT_VERSION
    exported_at: datetime
    generator: ExportGenerator
    user: ExportUser
    dives: Annotated[list[ExportDive], Field(default_factory=list)]
    trips: Annotated[list[ExportTrip], Field(default_factory=list)]
    dive_sites: Annotated[list[ExportDiveSite], Field(default_factory=list)]
    gear_items: Annotated[list[ExportGearItem], Field(default_factory=list)]
    gear_sets: Annotated[list[ExportGearSet], Field(default_factory=list)]
    gear_service_schedules: Annotated[list[ExportGearServiceSchedule], Field(default_factory=list)]
    gear_service_records: Annotated[list[ExportGearServiceRecord], Field(default_factory=list)]
    certifications: Annotated[list[ExportCertification], Field(default_factory=list)]
