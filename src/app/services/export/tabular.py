"""The CSV half of the export - the files that open in a spreadsheet.

Named `tabular` rather than `csv` so it cannot shadow the stdlib module it is built on.

`dives.csv` is the one a diver actually wants: one row per dive, flattened, with the
related records folded into readable cells (sites `;`-joined in visit order, cylinders as
`EAN32 12L 200->80bar`) rather than spread across a normalized set of tables nobody opens
in Excel. The archive ships the normalized files alongside it - `mixtures.csv` and the
rest - so the joins are still there for anyone who wants them.

Three things this is careful about:

- **Quoting.** Notes hold commas, quotes and newlines, and every one of those is a way to
  produce a file that opens wrong. `csv.writer` handles all of it; nothing here builds a
  row by joining strings.
- **The byte-order mark.** Every file here is written UTF-8 **with** a BOM, because the
  likely consumer is Excel, which reads a BOM-less UTF-8 CSV as the local ANSI codepage
  and turns every accented site name into mojibake. Python's `csv`, pandas and every other
  programmatic reader either strip it (`encoding="utf-8-sig"`) or tolerate it in the first
  header cell. It used to be on `dives.csv` alone, on the theory that the normalized files
  are read by scripts rather than spreadsheets - but `dive-sites.csv`, `trips.csv` and
  `certifications.csv` carry exactly the same free text, and a diver double-clicking one
  out of the archive hits precisely the failure the BOM exists to prevent. A mangled site
  name is a worse outcome than a `utf-8-sig` a script author has to pass.
- **`\\r\\n`.** RFC 4180's line ending, and what `csv.writer` emits by default. Left
  alone rather than normalized to `\\n`, since the spreadsheet is the audience.

Numbers are written exactly as stored - meters, bar, degrees Celsius, seconds - with no
rounding beyond what the derived SAC/RMV figures already carry.

**Formula injection is knowingly not neutralized**, and that is a decision rather than an
oversight. A cell beginning `=`, `+`, `-` or `@` is evaluated by Excel and LibreOffice, and
notes, site names and filenames all reach cells verbatim. Every value here is the caller's
own data handed back to the caller, so there is no cross-account vector; the only scenario
left is a diver deliberately typing a formula into their own logbook and sharing the file.
Against that, the usual mitigation - prefixing such cells with an apostrophe - would mangle
a great many real rows, because dive notes beginning with a dash are ordinary
("- 20 min at 30 m"). Corrupting the common case to guard the contrived one is the wrong
trade. Revisit if the export ever carries data a *second* party supplied.
"""

import csv
import io
from collections.abc import Iterable, Iterator
from typing import Any

from ...core.utils.datetime_offset import combine_start_time
from ...models.dive import Dive
from ...schemas.dive_mixture import DiveMixtureRead
from ...schemas.gear_service import ServiceKind
from ..dive_gas import resolve_gas_use
from .loader import ExportBundle
from .naming import gas_name, trip_location_names

# Excel's cue that the file is UTF-8. See the module docstring.
BOM = "\ufeff"

DIVES_HEADER = (
    "dive_number",
    "date",
    "time",
    "utc_offset",
    "duration_seconds",
    "max_depth_m",
    "avg_depth_m",
    "bottom_temperature_c",
    "visibility_m",
    "weight_kg",
    "trip",
    "dive_sites",
    "cylinders",
    "gas_used_l",
    "rmv_l_per_min",
    "sac_bar_per_min",
    "cns_end",
    "otu_end",
    "surface_pressure_bar",
    # Empty where the dive computer recorded no fix, rather than `0` - which a reader
    # would take for Null Island, the trap `dive-sites.csv` avoids the same way.
    "entry_latitude",
    "entry_longitude",
    "exit_latitude",
    "exit_longitude",
    "source_file",
    "notes",
    "dive_uuid",
)


def _rows_to_csv(header: tuple[str, ...], rows: Iterable[tuple[Any, ...]]) -> Iterator[str]:
    """Serialize a header and rows with `csv.writer`, one chunk per row.

    The buffer is truncated after every row so this stays O(1) in memory over a log of
    any size - the whole reason the callers below are generators rather than list
    builders.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    yield BOM
    for row in (header, *rows):
        writer.writerow(row)
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)


def _cylinder_summary(mixture: DiveMixtureRead) -> str:
    """One cylinder as a diver would read it out: `EAN32 12L 200->80bar`.

    Every part after the gas is dropped when it wasn't recorded, so a hand-entered
    cylinder with no pressures reads `Air 12L` rather than `Air 12L None->Nonebar`.
    """
    parts = [gas_name(mixture.oxygen, mixture.helium), f"{mixture.volume:g}L"]
    if mixture.start_pressure is not None and mixture.end_pressure is not None:
        parts.append(f"{mixture.start_pressure:g}->{mixture.end_pressure:g}bar")
    elif mixture.start_pressure is not None:
        parts.append(f"{mixture.start_pressure:g}bar")
    if mixture.role is not None:
        parts.append(f"({mixture.role.value})")
    return " ".join(parts)


def _utc_offset(minutes: int) -> str:
    """`+02:00` - the same spelling the combined `start_time` carries, in its own column
    so a spreadsheet can sort on local time without parsing an offset out of a string."""
    sign = "-" if minutes < 0 else "+"
    hours, remainder = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{remainder:02d}"


def _dive_row(bundle: ExportBundle, dive: Dive) -> tuple[Any, ...]:
    local = combine_start_time(dive.start_time, dive.utc_offset_minutes)
    mixtures = bundle.mixtures_by_dive[dive.id]
    gas_use = resolve_gas_use(
        duration=dive.duration,
        avg_depth=dive.avg_depth,
        mixtures=mixtures,
        attribution=bundle.attribution_by_dive.get(dive.id),
    )
    trip = bundle.trip_for(dive)
    source_file = bundle.file_by_dive[dive.id]
    return (
        dive.dive_number,
        local.date().isoformat(),
        local.strftime("%H:%M:%S"),
        _utc_offset(dive.utc_offset_minutes),
        dive.duration,
        dive.max_depth,
        dive.avg_depth,
        dive.bottom_temperature,
        dive.visibility,
        dive.weight,
        None if trip is None else trip.name,
        "; ".join(site.name for site in bundle.sites_for(dive)),
        "; ".join(_cylinder_summary(mixture) for mixture in mixtures),
        None if gas_use is None else gas_use.gas_used,
        None if gas_use is None else gas_use.rmv,
        None if gas_use is None else gas_use.sac_bar_per_min,
        dive.cns_end,
        dive.otu_end,
        dive.surface_pressure_bar,
        dive.entry_latitude,
        dive.entry_longitude,
        dive.exit_latitude,
        dive.exit_longitude,
        None if source_file is None else source_file.original_filename,
        dive.notes,
        str(dive.uuid),
    )


def write_dives_csv(bundle: ExportBundle) -> Iterator[str]:
    """The flat, human view: one row per dive, oldest first."""
    return _rows_to_csv(DIVES_HEADER, (_dive_row(bundle, dive) for dive in bundle.dives))


MIXTURES_HEADER = (
    "dive_number",
    "dive_uuid",
    "gas",
    "oxygen_percent",
    "helium_percent",
    "volume_l",
    "start_pressure_bar",
    "end_pressure_bar",
    "po2_limit_bar",
    "gas_number",
    "role",
)


def write_mixtures_csv(bundle: ExportBundle) -> Iterator[str]:
    """One row per cylinder - the normalized view `dives.csv` summarizes into a cell."""

    def rows() -> Iterator[tuple[Any, ...]]:
        for dive in bundle.dives:
            for mixture in bundle.mixtures_by_dive[dive.id]:
                yield (
                    dive.dive_number,
                    str(dive.uuid),
                    gas_name(mixture.oxygen, mixture.helium),
                    mixture.oxygen,
                    mixture.helium,
                    mixture.volume,
                    mixture.start_pressure,
                    mixture.end_pressure,
                    mixture.po2_limit,
                    mixture.gas_number,
                    None if mixture.role is None else mixture.role.value,
                )

    return _rows_to_csv(MIXTURES_HEADER, rows())


TRIPS_HEADER = ("name", "location", "start_date", "end_date", "dives", "notes", "trip_uuid")


def write_trips_csv(bundle: ExportBundle) -> Iterator[str]:
    counts: dict[int, int] = {}
    for dive in bundle.dives:
        if dive.trip_id is not None:
            counts[dive.trip_id] = counts.get(dive.trip_id, 0) + 1

    def rows() -> Iterator[tuple[Any, ...]]:
        for trip in bundle.trips:
            yield (
                trip.name,
                # One cell where the trip has a list of places, joined the way the app
                # shows them. A spreadsheet column is not a place to put a nested shape,
                # and `export.json` is where the structured locations are.
                trip_location_names(bundle.locations_by_trip[trip.id]),
                trip.start_date.isoformat(),
                None if trip.end_date is None else trip.end_date.isoformat(),
                counts.get(trip.id, 0),
                trip.notes,
                str(trip.uuid),
            )

    return _rows_to_csv(TRIPS_HEADER, rows())


DIVE_SITES_HEADER = ("name", "location", "latitude", "longitude", "dives", "notes", "dive_site_uuid")


def write_dive_sites_csv(bundle: ExportBundle) -> Iterator[str]:
    counts: dict[int, int] = {}
    for site_ids in bundle.site_ids_by_dive.values():
        for site_id in site_ids:
            counts[site_id] = counts.get(site_id, 0) + 1

    def rows() -> Iterator[tuple[Any, ...]]:
        for site in bundle.dive_sites:
            yield (
                site.name,
                site.location,
                site.latitude,
                site.longitude,
                counts.get(site.id, 0),
                site.notes,
                str(site.uuid),
            )

    return _rows_to_csv(DIVE_SITES_HEADER, rows())


GEAR_ITEMS_HEADER = (
    "name",
    "brand",
    "type",
    "rented",
    "archived",
    "dive_count",
    "sets",
    "notes",
    "gear_item_uuid",
)


def write_gear_items_csv(bundle: ExportBundle) -> Iterator[str]:
    """One row per item, with the sets it belongs to folded in - the gear-set membership
    has no file of its own, since a set is a shortcut for filling in a form rather than
    a record of anything that happened."""
    sets_by_item: dict[int, list[str]] = {}
    for gear_set in bundle.gear_sets:
        for item_id in bundle.item_ids_by_set[gear_set.id]:
            sets_by_item.setdefault(item_id, []).append(gear_set.name)

    def rows() -> Iterator[tuple[Any, ...]]:
        for item in bundle.gear_items:
            yield (
                item.name,
                item.brand,
                item.type,
                item.rented,
                item.is_archived,
                item.dive_count,
                "; ".join(sets_by_item.get(item.id, [])),
                item.notes,
                str(item.uuid),
            )

    return _rows_to_csv(GEAR_ITEMS_HEADER, rows())


GEAR_SERVICE_HEADER = (
    "gear_item",
    "gear_item_uuid",
    "row_type",
    "kind",
    "label",
    "serviced_on",
    "performed_by",
    "interval_months",
    "interval_dives",
    "last_service_on",
    "next_due_on",
    "next_due_at_dive_count",
    "active",
    "notes",
    "row_uuid",
)


def write_gear_service_csv(bundle: ExportBundle) -> Iterator[str]:
    """Schedules and records in one file, told apart by `row_type`.

    Two files would be more normalized and less useful: what a diver checks is "when is
    this regulator next due, and when was it last done", and that is one item's rows read
    together. The columns each kind doesn't use are left empty.
    """

    def item_columns(gear_item_id: int) -> tuple[str, str]:
        """Name *and* uuid: a name is not unique across a diver's history, and the uuid is
        what actually joins this file to `gear-items.csv`."""
        item = bundle.gear_item_by_id.get(gear_item_id)
        return ("", "") if item is None else (item.name, str(item.uuid))

    def rows() -> Iterator[tuple[Any, ...]]:
        for schedule in bundle.schedules:
            yield (
                *item_columns(schedule.gear_item_id),
                "schedule",
                ServiceKind(schedule.kind).value,
                schedule.label,
                None,
                None,
                schedule.interval_months,
                schedule.interval_dives,
                None if schedule.last_service_on is None else schedule.last_service_on.isoformat(),
                None if schedule.next_due_on is None else schedule.next_due_on.isoformat(),
                schedule.next_due_at_dive_count,
                schedule.is_active,
                None,
                str(schedule.uuid),
            )
        for record in bundle.service_records:
            yield (
                *item_columns(record.gear_item_id),
                "record",
                ServiceKind(record.kind).value,
                record.label,
                record.serviced_on.isoformat(),
                record.performed_by,
                None,
                None,
                None,
                None,
                None,
                None,
                record.notes,
                str(record.uuid),
            )

    return _rows_to_csv(GEAR_SERVICE_HEADER, rows())


CERTIFICATIONS_HEADER = (
    "agency",
    "name",
    "certification_number",
    "certified_on",
    "expires_on",
    "instructor_name",
    "instructor_number",
    "training_center",
    "notes",
    "certification_uuid",
)


def write_certifications_csv(bundle: ExportBundle) -> Iterator[str]:
    def rows() -> Iterator[tuple[Any, ...]]:
        for certification in bundle.certifications:
            yield (
                # `agency_other` is the whole point of `OTHER`, so the column shows the
                # agency the diver actually named rather than the enum's escape hatch.
                certification.agency_other or certification.agency,
                certification.name,
                certification.certification_number,
                None if certification.certified_on is None else certification.certified_on.isoformat(),
                None if certification.expires_on is None else certification.expires_on.isoformat(),
                certification.instructor_name,
                certification.instructor_number,
                certification.training_center,
                certification.notes,
                str(certification.uuid),
            )

    return _rows_to_csv(CERTIFICATIONS_HEADER, rows())


# The archive's `csv/` directory, in the order the files are added to it.
CSV_WRITERS = (
    ("dives.csv", write_dives_csv),
    ("mixtures.csv", write_mixtures_csv),
    ("trips.csv", write_trips_csv),
    ("dive-sites.csv", write_dive_sites_csv),
    ("gear-items.csv", write_gear_items_csv),
    ("gear-service.csv", write_gear_service_csv),
    ("certifications.csv", write_certifications_csv),
)
