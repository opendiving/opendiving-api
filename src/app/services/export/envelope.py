"""Streams `logbook.divejson` - a complete DiveJSON 1.0 copy of a diver's logbook.

The shape, and why the app's own JSON format became the published one, is documented on
`schemas/export.py`. This module is only concerned with producing it without ever holding
it whole.

**Why it is streamed rather than serialized from `ExportEnvelope`.** Every dive embeds
its full profile, and a profile is a few thousand samples across up to ten channels. A
model instance for a thousand-dive log, plus the encoded JSON of the same, is hundreds of
megabytes resident - for a file the caller is going to write straight to a socket or a
temp file. So the envelope's scalars are emitted once, and each dive is loaded, encoded
and dropped one at a time. Streaming is also what puts `format` and `version` first, which
the format asks of a writer (spec §4) and which nothing about a dict would guarantee.

That trade has a cost: the declared shape (`ExportEnvelope`) is not on the write path and
could drift from what is actually written. `tests/test_export_json.py` closes it by
validating this generator's output against that model, against the vendored JSON Schema
and against the spec's beyond-schema rules - which is why the model is worth declaring at
all.
"""

import json
import uuid as uuid_pkg
from collections.abc import AsyncIterator
from datetime import datetime
from enum import StrEnum
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.utils.datetime_offset import combine_start_time
from ...models.course import Course
from ...models.dive import Dive
from ...models.trip import Trip
from ...models.user import User
from ...schemas.certification import CertificationAgency, CertificationSide
from ...schemas.course import CourseStatus
from ...schemas.dive import DecoAlgorithm, DiveMode, Salinity, WaterType
from ...schemas.dive_mixture import DiveMixtureRead, GasRole, TankUsage
from ...schemas.export import (
    DIVEJSON_FORMAT,
    DIVEJSON_PRODUCER_KEY,
    DIVEJSON_VERSION,
    EXPORT_EXTENSIONS,
    ExportBoundingBox,
    ExportCertification,
    ExportCourse,
    ExportCylinder,
    ExportDecoModel,
    ExportDevice,
    ExportDive,
    ExportDiver,
    ExportDiveSite,
    ExportEmergencyContact,
    ExportGearItem,
    ExportGearServiceRecord,
    ExportGearServiceSchedule,
    ExportGearSet,
    ExportGenerator,
    ExportInsurance,
    ExportLocation,
    ExportPosition,
    ExportRecording,
    ExportSpecies,
    ExportStoredFile,
    ExportTrip,
    ExportTripPart,
)
from ...schemas.gear_item import GearType
from ...schemas.gear_service import ServiceKind
from ...schemas.location import DIVE_SITE_LOCATION_PREFIX, LocationRead, location_from_row
from ...schemas.trip import TripPartRead
from ...schemas.user import EMERGENCY_CONTACT_FIELDS, INSURANCE_FIELDS, is_blank
from ...schemas.user_picture import PictureKind
from ..dive_profiles import LoadedProfile, load_profile, to_read_schema
from .loader import ExportBundle, ExportFileRow, ExportRecordingRow
from .paths import ArchivePaths


def _encode(value: Any) -> bytes:
    """One JSON value, compact, as UTF-8, and with absent members left out.

    `exclude_none=True` is the format's "nothing invented" rule at the serializer (spec
    §5.4): there is exactly one spelling of "not recorded" and it is absence, so a `None`
    anywhere in these models means the member is simply not written. The vendored schema
    rejects an explicit null, so this is enforced rather than stylistic.

    `ensure_ascii=False` because the file is declared UTF-8 and a diver's notes read
    better as themselves than as `\\u00e4`-escapes; compact because the payload is
    dominated by profile arrays, where indentation would multiply the size of the thing
    for no reader's benefit.
    """
    return json.dumps(jsonable_encoder(value, exclude_none=True), ensure_ascii=False).encode("utf-8")


def _encode_collection(records: list[Any]) -> bytes:
    """A top-level collection, one record per line.

    The layout `dives` gets from being streamed a dive at a time, given to the collections
    that are small enough to encode in one go - so every top-level array reads the same way
    in a diff or a pager, rather than `dives` alone being legible and the other nine each
    arriving as one unbounded line. The whitespace is between values only, where JSON gives
    it no meaning, so nothing about the document changes for a reader that parses it.
    """
    if not records:
        return b"[]"
    return b"[\n" + b",\n".join(_encode(record) for record in records) + b"\n]"


def _speakable(value: str | None, vocabulary: type[StrEnum]) -> bool:
    """Whether a stored value can be written into a DiveJSON document at all.

    Unlike the read schemas, the export shapes in `schemas/export.py` cannot widen: their
    enums are the format's own, on objects the schema closes
    (`additionalProperties: false`), so a value outside one would make the document
    invalid - which is the one thing a writer must not produce. But raising is not the
    alternative: these columns carry no DB `CHECK` on purpose and revision `f9d04a823776`
    deliberately leaves one unrepaired value behind, so a stored value outside a vocabulary
    is data the writer has to have an answer for. See *"A stored vocabulary is read back as
    a string"* in DECISIONS.md.

    The answer is the format's own, split by whether the member is REQUIRED - which is a
    question for the schema, not for intuition. A course's `agency` and `status` both read
    like REQUIRED members and neither is (`$defs/course` requires only uuid/name; spec
    §6.17 marks both O).

    - REQUIRED (`gear_service_schedule.type`, `gear_service_record.type`,
      `certification.agency`) - the record is uninterpretable and is omitted, which is the
      writer's side of the rule the reader already follows (spec §5.6, and
      `logbook_import/planner.py::_agency`, which skips for that reason).
    - OPTIONAL (`gear_item.type`, `dive.water_type`, `dive_recording.mode`/`salinity`,
      `dive_mixture.role`/`usage`, `course.agency`/`status`) - `_sayable` below drops the
      *field* and keeps the record.
      A diver's cylinder must not vanish from their export over how its category is spelt,
      and neither must their course.

    **An omitted record is a record nothing may reference.** DiveJSON checks referential
    closure, so dropping a schedule without also clearing what points at it produces a
    document the validator rejects - which is worse than the 500, because it fails at the
    far end, on someone else's importer. `gear_service_schedule` is the one omittable
    collection anything references, and `gear_service_record.gear_service_schedule_uuid` is
    OPTIONAL, so it resolves through `_schedule_uuid` and becomes absent - a state it
    already has a meaning for.

    Nothing is lost from an archive either way: the CSVs carry every row with its stored
    value, having no vocabulary to keep.
    """
    return value in set(vocabulary)


def _sayable[T: StrEnum](value: str | None, vocabulary: type[T]) -> T | None:
    """An OPTIONAL member's value, or `None` when the format has no word for it - see
    `_speakable` for why that is a different answer from skipping the record."""
    return vocabulary(value) if value is not None and _speakable(value, vocabulary) else None


def _export_course(course: Course) -> ExportCourse:
    """One course, as the document spells it. Never omitted: nothing a course stores is a
    REQUIRED member the format could find unreadable."""
    agency, agency_other = _course_agency(course)
    return ExportCourse(
        uuid=course.uuid,
        name=course.name,
        agency=agency,
        agency_other=agency_other,
        status=_sayable(course.status, CourseStatus),
        starts_on=course.start_date,
        ends_on=course.end_date,
        instructor_name=course.instructor_name,
        instructor_number=course.instructor_number,
        training_center=course.training_center,
        notes=_text(course.notes),
        created_at=course.created_at,
    )


def _course_agency(course: Course) -> tuple[CertificationAgency | None, str | None]:
    """A course's `agency`/`agency_other` pair as `$defs/course` admits it.

    That object pairs the two: `agency_other` is REQUIRED beside `other` and forbidden
    beside anything else, an absent `agency` included. So the pair is written together or
    not at all, and `agency` being OPTIONAL (spec §6.17) means "not at all" costs the two
    fields rather than the diver's course.

    Three stored pairs reach "not at all": no agency, which is the state the column now
    has; a value the vocabulary has no word for, which §5.6 already reads as absent; and an
    `other` with nothing to name it, which claims an agency without producing one. The
    reader takes the same three the same way (`logbook_import/planner.py::_agency` and
    `_course_agency`), and a stray `agency_other` beside a named agency is dropped there
    too.
    """
    agency = _sayable(course.agency, CertificationAgency)
    if agency is not CertificationAgency.OTHER:
        return agency, None
    named = course.agency_other
    return (agency, named) if (named or "").strip() else (None, None)


def _text(value: str | None) -> str | None:
    """A free-text column as the format spells it: an empty one is *absent*.

    These columns are `NOT NULL` with `""` meaning "the diver wrote nothing", so this app
    cannot tell a blank note from no note. DiveJSON can (spec §5.4 makes an empty string a
    recorded empty value), and writing `""` would claim the stronger of the two readings
    about data that never carried the distinction.
    """
    return value or None


def _position(latitude: float | None, longitude: float | None) -> ExportPosition | None:
    """Both halves or nothing - half a coordinate is unrepresentable (spec §6).

    The `ck_dive_*_position_pair` constraints already make a half pair unstorable, so this
    is the same rule stated where the document is built rather than a second guess at it.
    """
    if latitude is None or longitude is None:
        return None
    return ExportPosition(latitude=latitude, longitude=longitude)


def _mixture(mixture: DiveMixtureRead) -> ExportCylinder:
    """A read cylinder as the format's shape, `po2_limit` spelled `ppo2_limit`.

    `role`/`usage` go through `_sayable` because the format's shape types them as enums, so
    a value the read shape carried through has to be dropped here rather than handed over,
    or this rebuild raises mid-stream on exactly the row the read schemas were widened for.
    """
    return ExportCylinder(
        volume=mixture.volume,
        start_pressure=mixture.start_pressure,
        end_pressure=mixture.end_pressure,
        oxygen=mixture.oxygen,
        helium=mixture.helium,
        ppo2_limit=mixture.po2_limit,
        gas_number=mixture.gas_number,
        role=_sayable(mixture.role, GasRole),
        usage=_sayable(mixture.usage, TankUsage),
    )


def _filled(value: str | None) -> str | None:
    """A check-in text column as the format spells it: `""` is absent, never an empty member."""
    return None if is_blank(value) else value


def _emergency_contacts(user: User) -> list[ExportEmergencyContact] | None:
    """The account's one contact, or nothing - and nothing, too, for one with no name.

    `name` is REQUIRED in the format and a row saved before `PATCH /user` required it can
    still lack one. The diver still sees it in the app; the export leaves it out until they
    name someone, since the app's writer has no report channel to say it dropped anything.
    """
    name, phone, relationship = (getattr(user, field) for field in EMERGENCY_CONTACT_FIELDS)
    if is_blank(name):
        return None
    return [ExportEmergencyContact(name=name, phone=_filled(phone), relationship=_filled(relationship))]


def _insurances(user: User) -> list[ExportInsurance] | None:
    """The account's one policy, or nothing, on `_emergency_contacts`'s terms: no provider,
    no policy."""
    provider, number, expires_on = (getattr(user, field) for field in INSURANCE_FIELDS)
    if is_blank(provider):
        return None
    return [ExportInsurance(provider=provider, number=_filled(number), expires_on=expires_on)]


def _portrait_file(bundle: ExportBundle, paths: ArchivePaths | None) -> ExportStoredFile | None:
    """The portrait's original as a Stored File, with the crop this app frames it with.

    The original rather than the rendition: the format carries the picture whole and leaves
    the framing to each reader (spec §6.1), and the rendition's bytes are this app's, with no
    filename the diver gave them. The crop is this app's framing, so it rides this producer's
    key, where an import into this app reads it back.
    """
    picture = bundle.pictures.get(PictureKind.PORTRAIT)
    if (
        picture is None
        or picture.original_sha256 is None
        or picture.original_content_type is None
        or picture.original_byte_size is None
        or picture.original_filename is None
    ):
        return None
    member = None if paths is None else paths.pictures.get(PictureKind.PORTRAIT)
    crop = {"x": picture.crop_x, "y": picture.crop_y, "width": picture.crop_width, "height": picture.crop_height}
    return ExportStoredFile(
        uuid=picture.uuid,
        original_filename=picture.original_filename,
        content_type=picture.original_content_type,
        byte_size=picture.original_byte_size,
        sha256=picture.original_sha256,
        archive_path=None if member is None else member.name,
        extensions={DIVEJSON_PRODUCER_KEY: {"crop": crop}},
    )


def _diver(bundle: ExportBundle, paths: ArchivePaths | None) -> ExportDiver:
    user = bundle.user
    return ExportDiver(
        uuid=user.uuid,
        name=user.name,
        username=user.username,
        email=user.email,
        phone=_filled(user.phone),
        born_on=user.date_of_birth,
        emergency_contacts=_emergency_contacts(user),
        insurances=_insurances(user),
        portrait_file=_portrait_file(bundle, paths),
        created_at=user.created_at,
        # The account-level preferences. They are here because `/export/archive` says
        # nothing in the account is reachable only through the app - and under this
        # producer's key because they are application settings rather than logbook data,
        # which the format gives no core member (spec §6.1). `units` in particular is the
        # diver's own setting travelling with a document whose measurements deliberately do
        # not bend to it (see `schemas/export.py`).
        #
        # The presets are `{name, hidden_fields}` and nothing else: `uuid`, `user_uuid` and
        # `created_at` identify a row in *this* instance, and a document that is going to be
        # read somewhere else has no use for them. Ordered the way `GET /dive-form-presets`
        # orders them, which is what makes the golden-file test meaningful.
        extensions={
            DIVEJSON_PRODUCER_KEY: {
                "units": user.units,
                "gear_service_emails": user.gear_service_emails,
                "dive_form_hidden_fields": list(user.dive_form_hidden_fields),
                "dive_form_presets": [
                    {"name": preset.name, "hidden_fields": list(preset.hidden_fields)}
                    for preset in bundle.dive_form_presets
                ],
            }
        },
    )


def _location(location: LocationRead | None) -> ExportLocation | None:
    """One place, for either host - a trip part's or a dive site's locality.

    Shared because §6.9 defines the object once: a second copy for the site half is how the
    two would come to spell the same place differently.
    """
    if location is None:
        return None
    position = _position(location.latitude, location.longitude)
    corners = (location.bbox_south, location.bbox_north, location.bbox_west, location.bbox_east)
    # A box needs its point: the spec makes `bbox` depend on `position`, because a rectangle
    # with no centre is a frame around nothing a reader can place.
    bbox = None
    if position is not None and all(corner is not None for corner in corners):
        south, north, west, east = corners
        bbox = ExportBoundingBox(south=south, north=north, west=west, east=east)  # type: ignore[arg-type]
    return ExportLocation(
        name=location.name,
        full_name=location.full_name,
        position=position,
        bbox=bbox,
    )


def _trip_part(part: TripPartRead) -> ExportTripPart:
    return ExportTripPart(
        starts_on=part.start_date,
        ends_on=part.end_date,
        location=_location(part.location),
    )


def _trip(trip: Trip, parts: list[TripPartRead]) -> ExportTrip:
    """A trip as `$defs/trip` describes it: a name and a sequence of parts.

    Every stored part is written, in the diver's own order, including one carrying neither
    a date nor a place - §6.9a makes the empty object conforming, and dropping it would
    lose a stretch the diver added and a position the rest are numbered by. Nothing here
    derives a span: the trip member that held one is gone from the format, and a reader
    that wants a range takes the earliest start and the latest end across these.
    """
    return ExportTrip(
        uuid=trip.uuid,
        name=trip.name,
        parts=[_trip_part(part) for part in parts],
        notes=_text(trip.notes),
        created_at=trip.created_at,
    )


def _stored_file(bundle: ExportBundle, file: ExportFileRow, paths: ArchivePaths | None) -> ExportStoredFile | None:
    """One stored export as the format's Stored File, or `None` if its digest has gone.

    The metadata and the digest are two statements of the same read transaction, so a file
    deleted between them leaves one with a row and the other without a key. Narrow, but
    `archive._write_blobs` already handles the same race for the bytes, and a download that
    500s because a file vanished mid-export is the wrong answer to it.
    """
    digest = bundle.dive_file_sha256.get(file.id)
    if digest is None:
        return None
    return ExportStoredFile(
        uuid=file.info.uuid,
        original_filename=file.info.original_filename,
        content_type=file.info.content_type,
        byte_size=file.info.byte_size,
        sha256=digest,
        archive_path=None if paths is None else paths.dive_files.get(file.id),
        # Which of this app's parsers understood the file. Parser registries are
        # application-specific, so the format has no core member for one (spec §6.7)
        # and it rides this producer's key - where the archive-restore path reads it
        # back.
        extensions={DIVEJSON_PRODUCER_KEY: {"parser_key": file.info.parser_key}},
    )


def _recording(
    bundle: ExportBundle,
    dive: Dive,
    row: ExportRecordingRow,
    *,
    profile: LoadedProfile | None,
    paths: ArchivePaths | None,
) -> ExportRecording | None:
    """One recording, or `None` when nothing about it survived to be written.

    §3's rule 4 - a recording carries at least one of its device, its profile, its files and
    a readout - is satisfied here rather than asserted: a row with none of them left after the
    digest race above describes nothing, and a document is better without it than with an
    empty object a reader has to skip.

    **`started_at` is written only when it differs from the dive's**, §6.4a's absent-means-
    the-dive's rule. Compared on the stored column pair rather than on the combined string,
    because two recordings of one dive may legitimately carry different offsets and a
    string comparison would call `12:17:38Z` and `15:17:38+03:00` different starts.
    """
    files = [stored for file in row.files if (stored := _stored_file(bundle, file, paths)) is not None]
    device = ExportDevice(**row.device) if row.device else None
    # **§3's rule 4 counts the readouts and not the settings**, which the spec states
    # outright: a mode or a salinity with no device, no samples, no file and no readout
    # behind it is a setting nothing recorded a dive with, so a row carrying only those is
    # still dropped. A readout alone is a record - a computer's own arithmetic.
    if device is None and profile is None and not files and not row.readouts:
        return None

    # Through `_sayable`, the same as `water_type` and a mixture's `role`: the column has no
    # `CHECK`, so it really can hold a value the format has no word for, and an OPTIONAL
    # member's answer to that is to drop the *field* and keep the record. Dropping the
    # recording instead would lose a diver's profile over how a mode is spelt.
    deco_model = (
        ExportDecoModel(
            **{member: value for member, value in row.deco_model.items() if member != "algorithm"},
            algorithm=_sayable(row.deco_model.get("algorithm"), DecoAlgorithm),
        )
        if row.deco_model
        else None
    )

    started_at = None
    if row.start_time is not None and (
        row.start_time != dive.start_time or row.utc_offset_minutes != dive.utc_offset_minutes
    ):
        started_at = combine_start_time(row.start_time, row.utc_offset_minutes)
    return ExportRecording(
        device=device,
        mode=_sayable(row.mode, DiveMode),
        deco_model=deco_model,
        salinity=_sayable(row.salinity, Salinity),
        started_at=started_at,
        surface_pressure=row.readouts.get("surface_pressure_bar"),
        cns_start=row.readouts.get("cns_start"),
        cns_end=row.readouts.get("cns_end"),
        otu_start=row.readouts.get("otu_start"),
        otu_end=row.readouts.get("otu_end"),
        source_files=files,
        profile=None if profile is None else to_read_schema(profile),
    )


def _dive(
    bundle: ExportBundle,
    dive: Dive,
    *,
    profiles: dict[int, LoadedProfile | None],
    paths: ArchivePaths | None,
) -> ExportDive:
    recordings = [
        written
        for row in bundle.recordings_by_dive.get(dive.id, [])
        if (written := _recording(bundle, dive, row, profile=profiles.get(row.id), paths=paths)) is not None
    ]

    trip = bundle.trip_for(dive)
    course = bundle.course_for(dive)
    return ExportDive(
        uuid=dive.uuid,
        number=dive.dive_number,
        # The API's one rule for this column everywhere: one combined offset-aware
        # string, never the stored UTC instant next to a separate offset. That is also the
        # format's rule (spec §5.2), which is where it came from.
        started_at=combine_start_time(dive.start_time, dive.utc_offset_minutes),
        duration=dive.duration,
        notes=_text(dive.notes),
        max_depth=dive.max_depth,
        avg_depth=dive.avg_depth,
        bottom_temperature=dive.bottom_temperature,
        visibility=dive.visibility,
        weight=dive.weight,
        water_type=_sayable(dive.water_type, WaterType),
        altitude=dive.altitude,
        entry_position=_position(dive.entry_latitude, dive.entry_longitude),
        exit_position=_position(dive.exit_latitude, dive.exit_longitude),
        trip_uuid=None if trip is None else trip.uuid,
        course_uuid=None if course is None else course.uuid,
        site_uuids=[site.uuid for site in bundle.sites_for(dive)],
        gear_uuids=[item.uuid for item in bundle.gear_for(dive)],
        species_uuids=[species.uuid for species in bundle.species_for(dive)],
        cylinders=[_mixture(mixture) for mixture in bundle.mixtures_by_dive[dive.id]],
        recordings=recordings,
        created_at=dive.created_at,
    )


def _certifications(bundle: ExportBundle, paths: ArchivePaths | None) -> list[ExportCertification]:
    exported = []
    for certification in bundle.certifications:
        # Keyed by side rather than collected into a list: a card has one front and one
        # back, and the format says so with two members precisely so a document cannot
        # claim two fronts (spec §6.16).
        by_side: dict[str, ExportStoredFile] = {
            info.side.value: ExportStoredFile(
                uuid=info.uuid,
                original_filename=info.original_filename,
                content_type=info.content_type,
                byte_size=info.byte_size,
                sha256=digest,
                archive_path=(
                    None if paths is None else paths.certification_files.get((certification.id, info.side.value))
                ),
            )
            for info in bundle.cert_files_by_cert.get(certification.id, [])
            # Same race as a dive's export - see `_dive`.
            if (digest := bundle.cert_file_sha256.get((certification.id, info.side.value))) is not None
        }
        # REQUIRED, and the certification carries nothing that survives without it.
        if not _speakable(certification.agency, CertificationAgency):
            continue
        course = bundle.course_for(certification)
        exported.append(
            ExportCertification(
                uuid=certification.uuid,
                agency=certification.agency,
                agency_other=certification.agency_other,
                name=certification.name,
                number=certification.certification_number,
                certified_on=certification.certified_on,
                expires_on=certification.expires_on,
                instructor_name=certification.instructor_name,
                instructor_number=certification.instructor_number,
                training_center=certification.training_center,
                course_uuid=None if course is None else course.uuid,
                notes=_text(certification.notes),
                front_file=by_side.get(CertificationSide.FRONT.value),
                back_file=by_side.get(CertificationSide.BACK.value),
                created_at=certification.created_at,
            )
        )
    return exported


def _collections(bundle: ExportBundle, paths: ArchivePaths | None) -> list[tuple[str, list[Any]]]:
    """Everything after `dives`, in the order `ExportEnvelope` declares it.

    All of it is small enough to encode in one go - the biggest is a few hundred gear
    items - so only `dives` is loaded and encoded a record at a time. They are still
    *written* a record to a line, by `_encode_collection`, which is a layout question
    rather than a memory one.
    """
    return [
        (
            "trips",
            [_trip(trip, bundle.parts_by_trip[trip.id]) for trip in bundle.trips],
        ),
        (
            "courses",
            # Every course the diver has, with no filter: `agency` and `status` are both
            # OPTIONAL (spec §6.17), so neither can cost the record - see `_speakable`.
            [_export_course(course) for course in bundle.courses],
        ),
        (
            "sites",
            [
                ExportDiveSite(
                    uuid=site.uuid,
                    name=site.name,
                    # The locality's own centre and box, never the site's pin - which goes
                    # in `position` below and is a different fact (spec §6.10).
                    location=_location(location_from_row(site, DIVE_SITE_LOCATION_PREFIX)),
                    position=_position(site.latitude, site.longitude),
                    notes=_text(site.notes),
                    created_at=site.created_at,
                )
                for site in bundle.dive_sites
            ],
        ),
        (
            "species",
            [
                ExportSpecies(
                    uuid=species.uuid,
                    aphia_id=species.aphia_id,
                    scientific_name=species.scientific_name,
                    common_name=species.common_name,
                    rank=species.rank,
                    wikidata_qid=species.wikidata_qid,
                    created_at=species.created_at,
                )
                for species in bundle.species
            ],
        ),
        (
            "gear",
            [
                ExportGearItem(
                    uuid=item.uuid,
                    name=item.name,
                    brand=item.brand,
                    type=_sayable(item.type, GearType),
                    notes=_text(item.notes),
                    rented=item.rented,
                    archived=item.is_archived,
                    archived_at=item.archived_at,
                    dive_count=item.dive_count,
                    created_at=item.created_at,
                )
                for item in bundle.gear_items
            ],
        ),
        (
            "gear_sets",
            [
                ExportGearSet(
                    uuid=gear_set.uuid,
                    name=gear_set.name,
                    weight=gear_set.weight,
                    gear_uuids=[
                        item.uuid
                        for item_id in bundle.item_ids_by_set[gear_set.id]
                        if (item := bundle.gear_item_by_id.get(item_id))
                    ],
                    created_at=gear_set.created_at,
                )
                for gear_set in bundle.gear_sets
            ],
        ),
        (
            "gear_service_schedules",
            [
                ExportGearServiceSchedule(
                    uuid=schedule.uuid,
                    gear_uuid=item.uuid,
                    type=schedule.kind,
                    label=schedule.label,
                    starts_on=schedule.starts_on,
                    interval_months=schedule.interval_months,
                    interval_dives=schedule.interval_dives,
                    dive_count_at_start=schedule.dive_count_at_start,
                    active=schedule.is_active,
                    last_service_on=schedule.last_service_on,
                    next_due_on=schedule.next_due_on,
                    next_due_at_dive_count=schedule.next_due_at_dive_count,
                    created_at=schedule.created_at,
                )
                for schedule in bundle.schedules
                # `.get()`-and-skip rather than indexing, for the reason spelled out on
                # `ExportBundle.gear_for`: a miss here can only be hand-edited data, and a
                # 500 on the export is the worst answer to a row nobody can see.
                if (item := bundle.gear_item_by_id.get(schedule.gear_item_id))
                and _speakable(schedule.kind, ServiceKind)
            ],
        ),
        (
            "gear_service_records",
            [
                ExportGearServiceRecord(
                    uuid=record.uuid,
                    gear_uuid=item.uuid,
                    gear_service_schedule_uuid=(
                        None
                        if record.gear_service_schedule_id is None
                        else _schedule_uuid(bundle, record.gear_service_schedule_id)
                    ),
                    type=record.kind,
                    serviced_on=record.serviced_on,
                    dive_count_at_service=record.dive_count_at_service,
                    label=record.label,
                    performed_by=record.performed_by,
                    notes=_text(record.notes),
                    created_at=record.created_at,
                )
                for record in bundle.service_records
                if (item := bundle.gear_item_by_id.get(record.gear_item_id)) and _speakable(record.kind, ServiceKind)
            ],
        ),
        ("certifications", _certifications(bundle, paths)),
    ]


def _schedule_uuid(bundle: ExportBundle, schedule_id: int) -> uuid_pkg.UUID | None:
    """A record's schedule, or `None` when the rule it was logged against is gone.

    History outlives the rule by design (see `models/gear_service_record.py`), and the FK
    is `ON DELETE SET NULL` for exactly that: deleting a schedule nulls
    `gear_service_record.gear_service_schedule_id` at the source, so the id never reaches
    this function and the null comes from the caller rather than from here. The member is
    then simply absent, which is what the format says about a record whose rule was
    deleted (spec §6.15).

    The `.get()` is still what stands between a stale id and a `KeyError` - it is reachable
    only through hand-edited data now, and skipping is the right answer there for the reason
    `ExportBundle.gear_for` gives.

    A schedule *omitted* for an unspeakable `kind` answers `None` here too, and for the
    same reason the deleted one does: it is not in the document, so nothing in the document
    may point at it (see `_speakable`).
    """
    schedule = bundle.schedule_by_id.get(schedule_id)
    if schedule is None or not _speakable(schedule.kind, ServiceKind):
        return None
    return schedule.uuid


async def write_divejson(
    db: AsyncSession,
    bundle: ExportBundle,
    *,
    exported_at: datetime,
    paths: ArchivePaths | None = None,
) -> AsyncIterator[bytes]:
    """Stream the whole logbook as a DiveJSON document.

    One writer, two surfaces: `GET /export/divejson` serves these bytes directly and the
    archive stores them as its `logbook.divejson` member, the way `GET /export/uddf` and
    `dives.uddf` already work.

    `paths` is the archive's member layout, which fills in each stored file's
    `archive_path`, the portrait's included. `None` leaves those *absent*; only `archive.py`
    passes a layout today, and without one there is no zip for a path to point into.
    """
    yield b'{"format": ' + _encode(DIVEJSON_FORMAT) + b",\n"
    yield b'"version": ' + _encode(DIVEJSON_VERSION) + b",\n"
    yield b'"exported_at": ' + _encode(exported_at) + b",\n"
    yield b'"generator": ' + _encode(ExportGenerator(name=settings.APP_NAME, version=settings.APP_VERSION)) + b",\n"
    yield b'"diver": ' + _encode(_diver(bundle, paths)) + b",\n"

    yield b'"dives": ['
    for index, dive in enumerate(bundle.dives):
        # One dive's profiles at a time, which is what keeps peak memory to a dive rather
        # than a logbook - the same contract as before, now over however many recordings the
        # dive has instead of exactly one.
        profiles = {
            row.id: await load_profile(db, recording_id=row.id) if row.has_profile else None
            for row in bundle.recordings_by_dive.get(dive.id, [])
        }
        separator = b",\n" if index else b"\n"
        yield separator + _encode(_dive(bundle, dive, profiles=profiles, paths=paths))
    yield b"\n],\n" if bundle.dives else b"],\n"

    for key, records in _collections(bundle, paths):
        yield _encode(key) + b": " + _encode_collection(records) + b",\n"
    # Last, where the format's own member order puts it: the marker `read_as_written` keys on.
    yield b'"extensions": ' + _encode(EXPORT_EXTENSIONS) + b"\n}\n"
