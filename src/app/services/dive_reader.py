"""The one place the api reads a dive-computer file, and the dive form's projection of it.

`divejson` is the reader, on every path. The head decides the format - `divejson.sniff` over
`divejson.SNIFF_BYTES`, and the filename decides nothing - and the whole file converts as that
format into a DiveJSON document, the same conversion logbook import runs. What the form and
the attach path want is one dive, so a document of none, of several, or of one dive recorded
by two computers is refused here with a sentence saying where such a file goes instead.

The document is then read two ways, and neither is a second reader. **`prefill`** is the dive
form's projection: `ParsedDiveSchema`, with the app's own rounding and a bottom temperature
derived where the file states none, because those are what the form shows. **The recording**
is shaped by `services/recording_shape.py`, the functions logbook import shapes a document's
recording with, which is what makes an attached file and an imported one the same recording.

**How a format is named, served back and ordered is the application's**, not the package's:
the label a diver reads, the content type a stored file is downloaded as, and whether a file
of the format is a diver's logbook or one computer's recording all live in the tables below,
keyed by format id. A pin bump that adds a reader owes all three, and
`test_every_read_format_has_a_label` fails the build until it has them.

Pure CPU and never on the event loop: every caller on a request path hands these to
`run_in_threadpool` (*"Uploaded files are parsed in a thread, not on the event loop"* in
`DECISIONS.md`).
"""

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Context, Decimal

import divejson
from divejson import ConverterError, NonConformingOutputError, SourceTooLargeError
from pydantic import ValidationError

from ..core.utils.datetime_offset import split_local_start_time
from ..schemas.dive import DiveMode, Salinity
from ..schemas.dive_profile import TEMPERATURE_SCALE
from ..schemas.logbook_import import ImportCylinder, ImportDive, ImportDocument, ImportRecording
from ..schemas.parsed_dive import DiveMixtureSchema, ParsedDecoModel, ParsedDevice, ParsedDiveSchema
from .dive_profiles import NormalizedProfile
from .dive_recordings import DECO_MODEL_COLUMNS, DEVICE_COLUMNS
from .recording_shape import Drop, ShapedRecording, recording_start, shape_recording

logger = logging.getLogger(__name__)

# How each registered format is named to a diver. An id the library grows past this table
# falls back to the id itself, so a sentence stays true and only gets terser - but that
# tolerance is a floor, not the plan: `test_every_read_format_has_a_label` fails the build
# when the pin moves past this table, because the fallback is invisible in every other
# guard. A whole reader (`suunto_xml`, added in 0.4.0) arrived unnoticed that way, offered
# by the API and greyed out by the picker, with nothing on either side able to see it.
FORMAT_LABELS = {
    "uddf": "UDDF (.uddf)",
    "ssrf": "Subsurface (.ssrf)",
    "fit": "FIT (.fit)",
    "suunto_json": "Suunto app JSON (.json)",
    "suunto_xml": "Suunto DM5 XML (.xml)",
}

# What a stored file of each format is served back as. From here rather than from the
# uploader's claimed `Content-Type` or a sniff of the stored bytes: the parse token binds the
# format id the reader answered, so the value in the download's response header is always one
# of a closed set this application declares. The same test as the labels keeps it complete.
FORMAT_CONTENT_TYPES = {
    "uddf": "application/xml",
    "ssrf": "application/xml",
    "fit": "application/vnd.ant.fit",
    "suunto_json": "application/json",
    "suunto_xml": "application/xml",
}

# What a stored file is served as when nothing here names its format: a restored archive
# file the document does not say how it was read, or one this build no longer reads.
# `dive_file.content_type` is `String(32)`.
FALLBACK_CONTENT_TYPE = "application/octet-stream"

# Whether a file of each format is a diver's own logbook, which may hold many dives and
# states the diver's numbering, notes and sites, or one computer's recording of one dive.
# An import reads a logbook's files ahead of a computer's, so where the two describe one
# dive it is the logbook's dive that the computer's recording joins. Kept by the same test
# as the labels.
LOGBOOK = "logbook"
COMPUTER = "computer"
FORMAT_KINDS = {
    "uddf": LOGBOOK,
    "ssrf": LOGBOOK,
    "fit": COMPUTER,
    "suunto_json": COMPUTER,
    "suunto_xml": COMPUTER,
}


def is_logbook_format(fmt: str) -> bool:
    """Whether `fmt` is a logbook's format rather than one computer's - a format this table
    has not heard of reads as a computer's, the kind that states the least."""
    return FORMAT_KINDS.get(fmt) == LOGBOOK


# The formats whose files an import attaches ahead of the rest of their kind, in this order;
# every other format follows, by name. A dive's first file decides what its recording states
# - its fixes above all, which a later file only fills - and the Suunto app's JSON states each
# fix's error where the same dive's FIT states none.
ATTACHES_FIRST = ("suunto_json",)


def attach_rank(fmt: str | None) -> int:
    """Where a file of `fmt` attaches among its kind's in one import."""
    return ATTACHES_FIRST.index(fmt) if fmt in ATTACHES_FIRST else len(ATTACHES_FIRST)


_HUNDREDTHS = Decimal("0.01")
# Digits enough for any finite float at two places: the default context's 28 signal
# `InvalidOperation` from 1e26 up, and an imported document may state a cylinder that large.
_ANY_FLOAT = Context(prec=320)

# A `NonConformingOutputError`, or a converted document this app's own envelope refuses.
CONVERTER_BUG = (
    "This file was recognised, and reading it produced a dive this app cannot read. That is a bug in the "
    "converter rather than anything wrong with your file - please report it."
)


class UnsupportedDiveFileError(Exception):
    """Bytes nothing here reads as one dive-computer file - a 415."""


class DiveFileReadError(Exception):
    """A file this build reads, and cannot take as one dive - a 422."""


def formats_this_build_reads() -> str:
    """The registry's read formats, as a diver would name them.

    Derived from `divejson.read_formats()` on every call rather than written out: the pin
    moves on its own, and a sentence listing four formats while the build reads five is the
    one failure a message like this can have.
    """
    return ", ".join(FORMAT_LABELS.get(fmt, fmt) for fmt in divejson.read_formats())


def reads(fmt: str | None) -> bool:
    """Whether `fmt` is a format this build's reader converts."""
    return fmt is not None and fmt in divejson.read_formats()


def content_type_of(fmt: str | None) -> str:
    """What a stored file of `fmt` is served back as."""
    return FORMAT_CONTENT_TYPES.get(fmt or "", FALLBACK_CONTENT_TYPE)


@dataclass(frozen=True, slots=True)
class ReadDive:
    """One file read as one dive: the format that read it, the dive, and its recording.

    `recording` is `None` for a dive the document gives no recording - a logbook entry with no
    computer behind it, which the form still prefills. `started_at` is the dive's start as the
    document spells it, which is what the form is handed.
    """

    format: str
    dive: ImportDive
    recording: ImportRecording | None
    started_at: str | None


def _conversion_moment() -> datetime:
    """The `exported_at` a conversion stamps on its output - UTC, as the import stamps it.

    The library's default is *now, in the local zone*; nothing downstream reads the member,
    so the only thing that matters is that it is not a function of the server's zone.
    """
    return datetime.now(UTC)


def read_dive_file(content: bytes, *, format: str | None = None) -> ReadDive:
    """One file as one dive, or a refusal saying why not.

    `format` is a format id the caller already holds - the key a stored file was recorded
    under, or the one its parse token names - and skips the sniff: a stored file is re-read
    by the format it was admitted as rather than sniffed again. Left `None`, the head decides.

    **The dive's `uuid` is discarded by every caller**: a bare file carries no identity, so
    the converter derives one from its position, and it is the same for every file of a
    format.
    """
    if format is None:
        claimed = divejson.sniff(content[: divejson.SNIFF_BYTES])
        if claimed == "zip":
            raise UnsupportedDiveFileError(
                "This is an archive, and the dive form takes one dive-computer file. Import the archive as a "
                "logbook, or pick one file from inside it."
            )
        if not reads(claimed):
            raise UnsupportedDiveFileError(
                f"This is not a dive-computer file this app reads. It reads: {formats_this_build_reads()}."
            )
        format = str(claimed)

    try:
        conversion = divejson.convert(content, format=format, exported_at=_conversion_moment())
    except SourceTooLargeError as exc:
        # A 422 rather than the 413 the words suggest: the file is under the upload cap, and
        # past the reader's own bound on how much one file may ask of it - which no single
        # dive's record reaches, and a watch's whole activity log does.
        raise DiveFileReadError(
            f"This file records an activity log rather than a dive - {exc}. Export the dive on its own."
        ) from exc
    except NonConformingOutputError as exc:
        logger.exception("The converter produced a non-conforming document from a %s file", format)
        raise DiveFileReadError(CONVERTER_BUG) from exc
    except ConverterError as exc:
        raise DiveFileReadError(f"This file could not be read: {exc}.") from exc
    except Exception as exc:
        # The package promises every failure is a `ConverterError`, and it drives third-party
        # decoders over bytes a stranger supplied. A backstop, so the endpoint cannot 500 on a
        # corrupt upload whatever the decoder does with it.
        logger.exception("Unexpected error reading a %s file", format)
        raise DiveFileReadError(f"This file could not be read: {str(exc) or type(exc).__name__}.") from exc

    try:
        document = ImportDocument.model_validate(conversion.document)
    except ValidationError as exc:
        logger.exception("A converted %s document did not survive this app's own envelope", format)
        raise DiveFileReadError(CONVERTER_BUG) from exc

    if not document.dives:
        raise DiveFileReadError("This file records no dive - the activity it holds is not one the dive form can log.")
    if len(document.dives) > 1:
        raise DiveFileReadError(
            f"This file holds {len(document.dives)} dives, and the dive form takes one. Import it as a logbook to "
            "bring them all in."
        )
    dive = document.dives[0]
    if len(dive.recordings) > 1:
        raise DiveFileReadError(
            f"This file holds {len(dive.recordings)} computers' records of this dive, and the dive form takes one "
            "computer's record of one dive. Import it as a logbook to keep them all."
        )

    raw = conversion.document["dives"][0].get("started_at")
    return ReadDive(
        format=format,
        dive=dive,
        recording=dive.recordings[0] if dive.recordings else None,
        started_at=raw if isinstance(raw, str) else None,
    )


def _discard(_message: str) -> None:
    """Where a value the shaping drops goes when there is no report to note it in."""


def shape(read: ReadDive, drop: Drop = _discard) -> ShapedRecording | None:
    """The file's one recording as the import shapes it, or `None` where it has none."""
    return None if read.recording is None else shape_recording(read.dive, read.recording, drop)


def start_of(read: ReadDive, shaped: ShapedRecording | None) -> tuple[datetime, int | None] | None:
    """Where the file's samples count from, as the stored column pair: its recording's
    start, else its dive's.

    `None` for a dive whose start is a bare date, which is no recording's start.
    """
    if shaped is not None:
        return None if shaped.start_time is None else (shaped.start_time, shaped.utc_offset_minutes)
    started_at = recording_start(read.dive, None, _discard)
    return None if started_at is None else split_local_start_time(started_at)


def two_places(value: float | None) -> float | None:
    """The app's precision for a number the form shows beside a cylinder or a readout.

    A decimal quantize of the value's shortest spelling rather than `round`, which works on the
    binary float and lands `2.675` on `2.67`. The web renders these at two decimals, so a finer
    value would come back re-rounded on the first edit.
    """
    if value is None or not math.isfinite(value):
        return value
    return float(Decimal(str(value)).quantize(_HUNDREDTHS, context=_ANY_FLOAT))


def _six_places(value: float | None) -> float | None:
    """A coordinate at six places, about 11 cm at the equator - the precision a fix is stored at."""
    return None if value is None else round(value, 6)


def _mixture(cylinder: ImportCylinder) -> DiveMixtureSchema:
    return DiveMixtureSchema(
        end_pressure=two_places(cylinder.end_pressure),
        gas_number=cylinder.gas_number,
        helium=two_places(cylinder.helium),
        oxygen=two_places(cylinder.oxygen),
        po2_limit=two_places(cylinder.ppo2_limit),
        role=cylinder.role,
        start_pressure=two_places(cylinder.start_pressure),
        volume=two_places(cylinder.volume),
    )


def bottom_temperature(stated: float | None, profile: NormalizedProfile | None) -> float | None:
    """A dive's bottom temperature as this app takes it: the one its document states, else the
    coldest reading of its recording's temperature channel, in degrees.

    The dive form and logbook import both apply it, so a file lands with one value through
    either door. Every dive-computer format but DM5's XML leaves the value unstated, and the
    default is the app's arithmetic rather than the converter's: a format writer deriving it
    would be making a reading up.
    """
    if stated is not None:
        return stated
    if profile is None or profile.temperature is None:
        return None
    return min(profile.temperature.v) / TEMPERATURE_SCALE


def prefill(read: ReadDive, shaped: ShapedRecording | None) -> ParsedDiveSchema:
    """The dive form's values for one file: the document's dive, as this app shows it.

    A pure function of the document. The dive's `duration`, `max_depth`, `avg_depth` and
    start pass as the document writes them, and so does the rest of what it states of the
    dive - notes, conditions, rating, tags - with no bound applied (`ParsedDiveSchema` says
    why); its cylinders, readouts and ppO₂ limits are rounded to the two decimals the form
    shows; its positions to six places; and `dive_number` is the document's own `number`,
    which a logbook format states and no dive-computer format does - a device's counter is
    not the diver's dive number, and lands on the recording as `device.dive_number`.

    The recording's members come from `shaped`, the same shaping the attach path stores, so
    the form shows the device and settings the recording will carry.
    """
    dive = read.dive
    device = {} if shaped is None else shaped.device
    deco_model = {} if shaped is None else shaped.deco_model
    readouts = {} if shaped is None else shaped.readouts
    entry, exit_ = dive.entry_position, dive.exit_position
    return ParsedDiveSchema(
        avg_depth=dive.avg_depth,
        bottom_temperature=bottom_temperature(dive.bottom_temperature, None if shaped is None else shaped.profile),
        dive_number=dive.number,
        duration=dive.duration,
        max_depth=dive.max_depth,
        start_time=read.started_at,
        mixtures=[_mixture(cylinder) for cylinder in dive.cylinders],
        notes=dive.notes,
        visibility=dive.visibility,
        weight=dive.weight,
        water_type=dive.water_type,
        altitude=dive.altitude,
        type=dive.type,
        rating=dive.rating,
        air_temperature=dive.air_temperature,
        current=dive.current,
        waves=dive.waves,
        weather=dive.weather,
        entry_type=dive.entry_type,
        boat_name=dive.boat_name,
        tags=dive.tags,
        device=ParsedDevice(**{member: device.get(column) for member, column in DEVICE_COLUMNS.items()})
        if device
        else None,
        mode=None if shaped is None or shaped.mode is None else DiveMode(shaped.mode),
        deco_model=ParsedDecoModel(**{member: deco_model.get(column) for member, column in DECO_MODEL_COLUMNS.items()})
        if deco_model
        else None,
        salinity=None if shaped is None or shaped.salinity is None else Salinity(shaped.salinity),
        cns_start=two_places(readouts.get("cns_start")),
        cns_end=two_places(readouts.get("cns_end")),
        otu_start=two_places(readouts.get("otu_start")),
        otu_end=two_places(readouts.get("otu_end")),
        surface_pressure_bar=readouts.get("surface_pressure_bar"),
        entry_latitude=None if entry is None else _six_places(entry.latitude),
        entry_longitude=None if entry is None else _six_places(entry.longitude),
        exit_latitude=None if exit_ is None else _six_places(exit_.latitude),
        exit_longitude=None if exit_ is None else _six_places(exit_.longitude),
    )


def read_prefill(content: bytes) -> tuple[str, ParsedDiveSchema]:
    """`POST /dive/parse`'s whole read: the format the head claimed, and the form's values."""
    read = read_dive_file(content)
    return read.format, prefill(read, shape(read))
