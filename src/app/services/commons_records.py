"""OpenDiving Commons on this instance: the bundle the image ships, and the rows it becomes.

A Commons record is a `dive_site` row with no owning account. `data/commons_dive_sites.geojson`
is the pool as `scripts/build_commons_bundle.py` read it from a named commit of the repository,
and at startup `sync_commons` brings the rows in line with it, keyed on the record's uuid.

**One instance is the primary, and there the table is the truth**: the sync inserts what the
table lacks and changes nothing, since a row may hold what a review wrote. Everywhere else the
rows are a read-only copy of the bundle, so a row whose bundled facts differ is updated. Neither
deletes: a dive may name the row, and no record is ever retired.

The notice block at the head of the file is the licence compliance artifact: ODbL 4.2 requires
the notice to travel within the data or its metadata. It is what credits each record, too, so a
record whose `source` it does not name is not loaded rather than served with no attribution.

`feature_of` and `columns_of` are one mapping read in two directions. A record written to the
bundle, synced and read back through `GET /commons/dive-sites` is the record that went in.
"""

import json
import logging
import uuid as uuid_pkg
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import Table, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from ..core.config import settings
from ..core.db.database import async_engine as engine
from ..models.dive_site import DiveSite

logger = logging.getLogger(__name__)

_TABLE = cast(Table, DiveSite.__table__)

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "commons_dive_sites.geojson"

# The pool's own registry token: the extension key its provenance sits under, and the registry
# an export names a record in.
COMMONS_REGISTRY = "opendivingcommons"

# Arbitrary, as `core/setup.py`'s neighbours are; every process running the sync picks the same.
_SYNC_LOCK_KEY = 8231907441002139

# `IN` lists are chunked: asyncpg binds at most 32,767 parameters to a statement.
_CHUNK = 10_000

# The members a Commons record publishes, as the columns that hold them. Both the public export
# and the sync read this list and no other, and nothing here reads `saved_site` or a tag: a
# diver's notes and tags cannot reach a record because neither is selected.
_FACT_COLUMNS = (
    "name",
    "other_names",
    "latitude",
    "longitude",
    "location_name",
    "external_ids",
    "depth_from",
    "depth_to",
    "water_type",
    "altitude",
    "entry_types",
    "commons_source",
    "commons_license",
    "commons_seed_registry",
    "commons_seed_identifier",
    "commons_retrieved",
)
_OPTIONAL_MEMBERS = ("depth_from", "depth_to", "water_type", "altitude")
_LIST_MEMBERS = ("other_names", "entry_types")


@dataclass(frozen=True, slots=True)
class CommonsBundle:
    """The file as read: its notice, the credit per source, and every creditable record."""

    commit: str
    licence: str
    licence_url: str
    notice: str
    attributions: Mapping[str, str]
    records: tuple[dict[str, Any], ...]


# Set on the first successful read, and only then: a bad read is retried by the next caller.
_loaded: CommonsBundle | None = None

# Whether a failed read has already been reported at WARNING. Every site read asks for a credit,
# so a broken deploy would otherwise write this line per request.
_warned = False


def columns_of(feature: Mapping[str, Any]) -> dict[str, Any]:
    """A repository record as `dive_site` columns, its uuid among them."""
    properties = feature["properties"]
    provenance = properties["extensions"][COMMONS_REGISTRY]
    longitude, latitude = feature["geometry"]["coordinates"]
    seeded_from = provenance.get("seeded_from") or {}
    retrieved = provenance.get("retrieved")
    return {
        "uuid": uuid_pkg.UUID(properties["uuid"]),
        "name": properties["name"],
        "other_names": properties.get("other_names", []),
        "latitude": latitude,
        "longitude": longitude,
        "location_name": (properties.get("location") or {}).get("name"),
        "external_ids": properties.get("external_ids", []),
        "depth_from": properties.get("depth_from"),
        "depth_to": properties.get("depth_to"),
        "water_type": properties.get("water_type"),
        "altitude": properties.get("altitude"),
        "entry_types": properties.get("entry_types", []),
        "commons_source": provenance["source"],
        "commons_license": provenance["license"],
        "commons_seed_registry": seeded_from.get("registry"),
        "commons_seed_identifier": seeded_from.get("identifier"),
        "commons_retrieved": None if retrieved is None else date.fromisoformat(retrieved),
    }


def feature_of(row: Mapping[str, Any]) -> dict[str, Any]:
    """A row as the repository's record: a member with nothing to say is absent, as it is there."""
    provenance: dict[str, Any] = {"source": row["commons_source"], "license": row["commons_license"]}
    if row["commons_retrieved"] is not None:
        provenance["retrieved"] = row["commons_retrieved"].isoformat()
    if row["commons_seed_registry"] is not None:
        provenance["seeded_from"] = {
            "registry": row["commons_seed_registry"],
            "identifier": row["commons_seed_identifier"],
        }
    properties: dict[str, Any] = {"uuid": str(row["uuid"]), "name": row["name"]}
    if row["location_name"] is not None:
        properties["location"] = {"name": row["location_name"]}
    if row["external_ids"]:
        properties["external_ids"] = row["external_ids"]
    properties |= {member: row[member] for member in _LIST_MEMBERS if row[member]}
    properties |= {member: row[member] for member in _OPTIONAL_MEMBERS if row[member] is not None}
    properties["extensions"] = {COMMONS_REGISTRY: provenance}
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [row["longitude"], row["latitude"]]},
        "properties": properties,
    }


def _read_bundle() -> CommonsBundle | None:
    """The file, or `None` if it could not be read. Reading it is the one step here that touches
    the world, and a broken file must not take a site read or a startup down with it."""
    global _warned
    try:
        document = json.loads(_DATA_PATH.read_text(encoding="utf-8"))
        attributions = {
            source["source"]: source["attribution"]
            for source in document["sources"]
            if source.get("source") and source.get("attribution")
        }
        records = []
        for feature in document["features"]:
            columns = columns_of(feature)
            # Not a parse failure: a record whose source the notice does not name has no credit
            # to travel with it, and serving it would be the breach the block exists to prevent.
            if columns["commons_source"] in attributions:
                records.append(columns)
        return CommonsBundle(
            commit=document["commit"],
            licence=document["licence"],
            licence_url=document["licence_url"],
            notice=document["notice"],
            attributions=attributions,
            records=tuple(records),
        )
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        logger.log(
            logging.DEBUG if _warned else logging.WARNING,
            "Could not read %s (%s); the instance serves its Commons rows uncredited and syncs nothing.",
            _DATA_PATH.name,
            exc,
        )
        _warned = True
        return None


def load_bundle() -> CommonsBundle | None:
    """The bundle, read the first time it is asked for and not at import: the worker imports
    this package and most of it never needs one."""
    global _loaded
    if _loaded is None:
        _loaded = _read_bundle()
    return _loaded


def attribution_for(source: str) -> str | None:
    """The credit a record of this source asks for, as the bundle's notice words it."""
    bundle = load_bundle()
    return None if bundle is None else bundle.attributions.get(source)


def _chunks(values: Sequence[Any]) -> Iterator[Sequence[Any]]:
    for start in range(0, len(values), _CHUNK):
        yield values[start : start + _CHUNK]


def _differing(row: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    return {column: record[column] for column in _FACT_COLUMNS if row[column] != record[column]}


async def _rows_named(conn: AsyncConnection, uuids: Iterable[uuid_pkg.UUID]) -> dict[uuid_pkg.UUID, dict[str, Any]]:
    selected = [_TABLE.c.id, _TABLE.c.uuid, _TABLE.c.user_id, *(_TABLE.c[column] for column in _FACT_COLUMNS)]
    rows: dict[uuid_pkg.UUID, dict[str, Any]] = {}
    for chunk in _chunks(list(uuids)):
        for row in (await conn.execute(select(*selected).where(_TABLE.c.uuid.in_(chunk)))).mappings():
            rows[row["uuid"]] = dict(row)
    return rows


async def _apply(conn: AsyncConnection, records: Sequence[Mapping[str, Any]], *, primary: bool) -> None:
    """Bring the rows in line with these records. Writes nothing where they already are."""
    existing = await _rows_named(conn, (record["uuid"] for record in records))
    now = datetime.now(UTC)

    lacking = [{**record, "created_at": now} for record in records if record["uuid"] not in existing]
    if lacking:
        # An import keeps a document's uuid when nothing claims it, so one can land between the
        # read above and this: that row is the diver's, and it stays.
        await conn.execute(insert(_TABLE).on_conflict_do_nothing(index_elements=["uuid"]), lacking)

    updated = skipped = 0
    for record in records:
        row = existing.get(record["uuid"])
        if row is None:
            continue
        if row["user_id"] is not None:
            skipped += 1
            logger.warning(
                "Commons record %s skipped: its uuid already names a private site on this instance.", record["uuid"]
            )
            continue
        if primary or not (changed := _differing(row, record)):
            continue
        await conn.execute(
            update(_TABLE).where(_TABLE.c.id == row["id"], _TABLE.c.user_id.is_(None)).values(**changed, updated_at=now)
        )
        updated += 1

    if lacking or updated:
        logger.info("Commons sync: %d records inserted, %d updated, %d skipped.", len(lacking), updated, skipped)


async def sync_commons() -> None:
    """Bring this instance's Commons rows in line with the bundle, after the migrations.

    Whether or not this process ran them, so `MIGRATE_ON_START=false` still syncs. One worker
    syncs and the rest move on: every gunicorn worker runs the lifespan, and a transaction-scoped
    try-lock lets whichever gets there first do it. Never a reason to fail startup: a schema
    behind this build or an unreadable bundle logs and leaves the instance serving what it has.
    """
    bundle = load_bundle()
    if bundle is None:
        return
    try:
        async with engine.begin() as conn:
            locked = await conn.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": _SYNC_LOCK_KEY})
            if locked.scalar_one():
                await _apply(conn, bundle.records, primary=settings.COMMONS_PRIMARY)
    except Exception:
        logger.warning("Could not sync the Commons; the next start will retry.", exc_info=True)


async def read_commons_features(db: AsyncSession) -> list[dict[str, Any]]:
    """Every Commons record as the repository's record, ordered by uuid, so two reads of an
    unchanged table are the same bytes."""
    selected = [_TABLE.c.uuid, *(_TABLE.c[column] for column in _FACT_COLUMNS)]
    rows = await db.execute(select(*selected).where(_TABLE.c.user_id.is_(None)).order_by(_TABLE.c.uuid))
    return [feature_of(dict(row)) for row in rows.mappings()]
