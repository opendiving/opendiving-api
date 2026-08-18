"""Streams `export.json` - the complete structured copy of a diver's logbook.

The shape, and why it exists next to the UDDF document, is documented on
`schemas/export.py`. This module is only concerned with producing it without ever
holding it whole.

**Why it is streamed rather than serialized from `ExportEnvelope`.** Every dive embeds
its full profile, and a profile is a few thousand samples across up to four channels. A
model instance for a thousand-dive log, plus the encoded JSON of the same, is hundreds of
megabytes resident - for a file the caller is going to write straight to a socket or a
temp file. So the envelope's scalars are emitted once, and each dive is loaded, encoded
and dropped one at a time.

That trade has a cost: the declared shape (`ExportEnvelope`) is not on the write path and
could drift from what is actually written. `tests/test_export_json.py` closes it by
validating this generator's output against that model, which is why the model is worth
declaring at all.
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
from ...schemas.dive_mixture import DiveMixtureBase, DiveMixtureRead
from ...schemas.export import (
    EXPORT_FORMAT,
    EXPORT_VERSION,
    ExportCertification,
    ExportCertificationFile,
    ExportDive,
    ExportDiveFile,
    ExportDiveSite,
    ExportGearItem,
    ExportGearServiceRecord,
    ExportGearServiceSchedule,
    ExportGearSet,
    ExportGenerator,
    ExportTrip,
    ExportTripLocation,
    ExportUser,
)
from ..dive_profiles import LoadedProfile, load_profile, to_read_schema
from .loader import ExportBundle
from .paths import ArchivePaths


def _encode(value: Any) -> bytes:
    """One JSON value, compact and as UTF-8.

    `ensure_ascii=False` because the file is declared UTF-8 and a diver's notes read
    better as themselves than as `\\u00e4`-escapes; compact because the payload is
    dominated by profile arrays, where indentation would multiply the size of the thing
    for no reader's benefit.
    """
    return json.dumps(jsonable_encoder(value), ensure_ascii=False).encode("utf-8")


def _mixture(mixture: DiveMixtureRead) -> DiveMixtureBase:
    return DiveMixtureBase(**mixture.model_dump(exclude={"id"}))


def _user(bundle: ExportBundle) -> ExportUser:
    user = bundle.user
    return ExportUser(
        uuid=user.uuid,
        name=user.name,
        username=user.username,
        email=user.email,
        gear_service_emails=user.gear_service_emails,
        created_at=user.created_at,
    )


def _dive(bundle: ExportBundle, dive: Dive, *, profile: LoadedProfile | None, paths: ArchivePaths | None) -> ExportDive:
    file_info = bundle.file_by_dive[dive.id]
    # The metadata and the digest are two statements of the same read transaction, so a
    # file deleted between them leaves one with a row and the other without a key. Narrow,
    # but `archive._write_blobs` already handles the same race for the bytes, and a
    # download that 500s because a file vanished mid-export is the wrong answer to it.
    digest = bundle.dive_file_sha256.get(dive.id)
    source_file = None
    if file_info is not None and digest is not None:
        source_file = ExportDiveFile(
            uuid=file_info.uuid,
            original_filename=file_info.original_filename,
            content_type=file_info.content_type,
            byte_size=file_info.byte_size,
            sha256=digest,
            parser_key=file_info.parser_key,
            archive_path=None if paths is None else paths.dive_files.get(dive.id),
        )

    trip = bundle.trip_for(dive)
    return ExportDive(
        uuid=dive.uuid,
        dive_number=dive.dive_number,
        # The API's one rule for this column everywhere: one combined offset-aware
        # string, never the stored UTC instant next to a separate offset.
        start_time=combine_start_time(dive.start_time, dive.utc_offset_minutes),
        duration=dive.duration,
        notes=dive.notes,
        max_depth=dive.max_depth,
        avg_depth=dive.avg_depth,
        bottom_temperature=dive.bottom_temperature,
        visibility=dive.visibility,
        weight=dive.weight,
        cns_start=dive.cns_start,
        cns_end=dive.cns_end,
        otu_start=dive.otu_start,
        otu_end=dive.otu_end,
        surface_pressure_bar=dive.surface_pressure_bar,
        entry_latitude=dive.entry_latitude,
        entry_longitude=dive.entry_longitude,
        exit_latitude=dive.exit_latitude,
        exit_longitude=dive.exit_longitude,
        trip_uuid=None if trip is None else trip.uuid,
        dive_site_uuids=[site.uuid for site in bundle.sites_for(dive)],
        gear_item_uuids=[item.uuid for item in bundle.gear_for(dive)],
        # `DiveMixtureBase`, not `DiveMixtureRead`: the latter carries the internal row
        # `id`, and nothing in this file references a cylinder by anything.
        mixtures=[_mixture(mixture) for mixture in bundle.mixtures_by_dive[dive.id]],
        source_file=source_file,
        profile=None if profile is None else to_read_schema(profile),
        created_at=dive.created_at,
    )


def _certifications(bundle: ExportBundle, paths: ArchivePaths | None) -> list[ExportCertification]:
    exported = []
    for certification in bundle.certifications:
        files = [
            ExportCertificationFile(
                uuid=info.uuid,
                side=info.side,
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
        ]
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
                notes=certification.notes,
                files=files,
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
                    locations=[
                        ExportTripLocation(**location.model_dump()) for location in bundle.locations_by_trip[trip.id]
                    ],
                    start_date=trip.start_date,
                    end_date=trip.end_date,
                    notes=trip.notes,
                    created_at=trip.created_at,
                )
                for trip in bundle.trips
            ],
        ),
        (
            "dive_sites",
            [
                ExportDiveSite(
                    uuid=site.uuid,
                    name=site.name,
                    location=site.location,
                    latitude=site.latitude,
                    longitude=site.longitude,
                    notes=site.notes,
                    created_at=site.created_at,
                )
                for site in bundle.dive_sites
            ],
        ),
        (
            "gear_items",
            [
                ExportGearItem(
                    uuid=item.uuid,
                    name=item.name,
                    brand=item.brand,
                    type=item.type,
                    notes=item.notes,
                    rented=item.rented,
                    is_archived=item.is_archived,
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
                    gear_item_uuids=[
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
                    gear_item_uuid=item.uuid,
                    kind=schedule.kind,
                    label=schedule.label,
                    starts_on=schedule.starts_on,
                    interval_months=schedule.interval_months,
                    interval_dives=schedule.interval_dives,
                    dive_count_at_start=schedule.dive_count_at_start,
                    is_active=schedule.is_active,
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
                    gear_item_uuid=item.uuid,
                    gear_service_schedule_uuid=(
                        None
                        if record.gear_service_schedule_id is None
                        else _schedule_uuid(bundle, record.gear_service_schedule_id)
                    ),
                    kind=record.kind,
                    serviced_on=record.serviced_on,
                    dive_count_at_service=record.dive_count_at_service,
                    label=record.label,
                    performed_by=record.performed_by,
                    notes=record.notes,
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
    is `ON DELETE SET NULL` for exactly that. `loader._owned` reads back a *soft*-deleted
    schedule a record still points at, so the usual answer here is a uuid the file also
    defines - but a hard-deleted one leaves nothing to resolve, and `.get()` is what keeps
    that a null rather than a `KeyError`.
    """
    schedule = bundle.schedule_by_id.get(schedule_id)
    return None if schedule is None else schedule.uuid


async def write_export_json(
    db: AsyncSession,
    bundle: ExportBundle,
    *,
    exported_at: datetime,
    paths: ArchivePaths | None = None,
) -> AsyncIterator[bytes]:
    """Stream the whole logbook as `export.json`.

    `paths` is the archive's member layout, which fills in each stored file's
    `archive_path`. `None` leaves those null; only `archive.py` passes a layout today, and
    without one there is no zip for a path to point into.
    """
    yield b'{"format":' + _encode(EXPORT_FORMAT) + b',"version":' + _encode(EXPORT_VERSION) + b",\n"
    yield b'"exported_at":' + _encode(exported_at) + b",\n"
    yield b'"generator":' + _encode(ExportGenerator(name=settings.APP_NAME, version=settings.APP_VERSION)) + b",\n"
    yield b'"user":' + _encode(_user(bundle)) + b",\n"

    yield b'"dives":['
    for index, dive in enumerate(bundle.dives):
        profile = await load_profile(db, dive_id=dive.id)
        separator = b",\n" if index else b"\n"
        yield separator + _encode(_dive(bundle, dive, profile=profile, paths=paths))
    yield b"\n],\n" if bundle.dives else b"],\n"

    collections = _collections(bundle, paths)
    for index, (key, records) in enumerate(collections):
        trailer = b",\n" if index < len(collections) - 1 else b"\n"
        yield _encode(key) + b":" + _encode(records) + trailer
    yield b"}\n"
