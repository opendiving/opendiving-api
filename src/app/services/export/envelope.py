"""Streams `logbook.divejson` - a complete DiveJSON 1.0 copy of a diver's logbook.

The shape, and why the app's own JSON format became the published one, is documented on
`schemas/export.py`. This module is only concerned with producing it without ever holding
it whole.

**Why it is streamed rather than serialized from `ExportEnvelope`.** Every dive embeds
its full profile, and a profile is a few thousand samples across up to four channels. A
model instance for a thousand-dive log, plus the encoded JSON of the same, is hundreds of
megabytes resident - for a file the caller is going to write straight to a socket or a
temp file. So the envelope's scalars are emitted once, and each dive is loaded, encoded
and dropped one at a time. Streaming is also what puts `format` and `version` first, which
the format requires of a writer (spec §4) and which nothing about a dict would guarantee.

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
from typing import Any

from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from ...core.config import settings
from ...core.utils.datetime_offset import combine_start_time
from ...models.dive import Dive
from ...schemas.certification import CertificationAgency, CertificationSide
from ...schemas.course import CourseStatus
from ...schemas.dive_mixture import DiveMixtureBase, DiveMixtureRead
from ...schemas.export import (
    DIVEJSON_FORMAT,
    DIVEJSON_PRODUCER_KEY,
    DIVEJSON_VERSION,
    ExportBoundingBox,
    ExportCertification,
    ExportCourse,
    ExportDevice,
    ExportDive,
    ExportDiver,
    ExportDiveSite,
    ExportGearItem,
    ExportGearServiceRecord,
    ExportGearServiceSchedule,
    ExportGearSet,
    ExportGenerator,
    ExportPosition,
    ExportRecording,
    ExportSpecies,
    ExportStoredFile,
    ExportTrip,
    ExportTripLocation,
)
from ...schemas.trip import TripLocationRead
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


def _mixture(mixture: DiveMixtureRead) -> DiveMixtureBase:
    return DiveMixtureBase(**mixture.model_dump(exclude={"id"}))


def _diver(bundle: ExportBundle) -> ExportDiver:
    user = bundle.user
    return ExportDiver(
        uuid=user.uuid,
        name=user.name,
        username=user.username,
        email=user.email,
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


def _trip_location(location: TripLocationRead) -> ExportTripLocation:
    position = _position(location.latitude, location.longitude)
    corners = (location.bbox_south, location.bbox_north, location.bbox_west, location.bbox_east)
    # A box needs its point: the spec makes `bbox` depend on `position`, because a rectangle
    # with no centre is a frame around nothing a reader can place.
    bbox = None
    if position is not None and all(corner is not None for corner in corners):
        south, north, west, east = corners
        bbox = ExportBoundingBox(south=south, north=north, west=west, east=east)  # type: ignore[arg-type]
    return ExportTripLocation(
        name=location.name,
        display_name=location.display_name,
        position=position,
        bbox=bbox,
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

    §3's rule 4 - a recording carries at least one of its device, its profile and its files -
    is satisfied here rather than asserted: a row with no device columns, no samples and no
    file left after the digest race above describes nothing, and a document is better without
    it than with an empty object a reader has to skip.

    **`started_at` is written only when it differs from the dive's**, §6.4a's absent-means-
    the-dive's rule. Compared on the stored column pair rather than on the combined string,
    because two recordings of one dive may legitimately carry different offsets and a
    string comparison would call `12:17:38Z` and `15:17:38+03:00` different starts.
    """
    files = [stored for file in row.files if (stored := _stored_file(bundle, file, paths)) is not None]
    device = ExportDevice(**row.device) if row.device else None
    if device is None and profile is None and not files:
        return None

    started_at = None
    if row.start_time is not None and (
        row.start_time != dive.start_time or row.utc_offset_minutes != dive.utc_offset_minutes
    ):
        started_at = combine_start_time(row.start_time, row.utc_offset_minutes)
    return ExportRecording(
        device=device,
        started_at=started_at,
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
        dive_number=dive.dive_number,
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
        water_type=dive.water_type,
        altitude=dive.altitude,
        cns_start=dive.cns_start,
        cns_end=dive.cns_end,
        otu_start=dive.otu_start,
        otu_end=dive.otu_end,
        surface_pressure=dive.surface_pressure_bar,
        entry_position=_position(dive.entry_latitude, dive.entry_longitude),
        exit_position=_position(dive.exit_latitude, dive.exit_longitude),
        trip_uuid=None if trip is None else trip.uuid,
        course_uuid=None if course is None else course.uuid,
        site_uuids=[site.uuid for site in bundle.sites_for(dive)],
        gear_uuids=[item.uuid for item in bundle.gear_for(dive)],
        species_uuids=[species.uuid for species in bundle.species_for(dive)],
        # `DiveMixtureBase`, not `DiveMixtureRead`: the latter carries the internal row
        # `id`, and nothing in this document references a cylinder by anything.
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
        course = bundle.course_for(certification)
        exported.append(
            ExportCertification(
                uuid=certification.uuid,
                agency=certification.agency,
                agency_other=certification.agency_other,
                name=certification.name,
                certification_number=certification.certification_number,
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
    items - so only `dives` gets the per-record treatment.
    """
    return [
        (
            "trips",
            [
                ExportTrip(
                    uuid=trip.uuid,
                    name=trip.name,
                    locations=[_trip_location(location) for location in bundle.locations_by_trip[trip.id]],
                    starts_on=trip.start_date,
                    ends_on=trip.end_date,
                    notes=_text(trip.notes),
                    created_at=trip.created_at,
                )
                for trip in bundle.trips
            ],
        ),
        (
            "courses",
            [
                ExportCourse(
                    uuid=course.uuid,
                    name=course.name,
                    agency=CertificationAgency(course.agency),
                    agency_other=course.agency_other,
                    status=CourseStatus(course.status),
                    starts_on=course.start_date,
                    ends_on=course.end_date,
                    instructor_name=course.instructor_name,
                    instructor_number=course.instructor_number,
                    training_center=course.training_center,
                    notes=_text(course.notes),
                    created_at=course.created_at,
                )
                for course in bundle.courses
            ],
        ),
        (
            "sites",
            [
                ExportDiveSite(
                    uuid=site.uuid,
                    name=site.name,
                    location=site.location,
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
                    type=item.type,
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
                if (item := bundle.gear_item_by_id.get(record.gear_item_id))
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
    """
    schedule = bundle.schedule_by_id.get(schedule_id)
    return None if schedule is None else schedule.uuid


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
    `archive_path`. `None` leaves those *absent*; only `archive.py` passes a layout today,
    and without one there is no zip for a path to point into.
    """
    yield b'{"format":' + _encode(DIVEJSON_FORMAT) + b',"version":' + _encode(DIVEJSON_VERSION) + b",\n"
    yield b'"exported_at":' + _encode(exported_at) + b",\n"
    yield b'"generator":' + _encode(ExportGenerator(name=settings.APP_NAME, version=settings.APP_VERSION)) + b",\n"
    yield b'"diver":' + _encode(_diver(bundle)) + b",\n"

    yield b'"dives":['
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

    collections = _collections(bundle, paths)
    for index, (key, records) in enumerate(collections):
        trailer = b",\n" if index < len(collections) - 1 else b"\n"
        yield _encode(key) + b":" + _encode(records) + trailer
    yield b"}\n"
