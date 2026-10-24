"""A dive's surface conditions, fetched by the worker and written beside the dive.

**A cron, not a queue, and no writer hooked.** `fetch_dive_conditions` runs every minute and
finds its work in one query: the dives with no `dive_conditions` row, the dives whose row no
longer describes them, the `failed` rows due for a retry and the rows an older derivation
wrote. A row stores the inputs it was computed from - the start time, its offset and
date-only flag, the rounded position and the effective water type - and a row whose inputs
are `IS DISTINCT FROM` the dive's current ones is answered again. That covers every way a
dive's time, place or water can change - the form, a site's pin moved under every dive logged
there, a site swapped or deleted, a merge, a recording attached, a backfill, the admin panel -
without naming any of them; the import's restore of a deleted dive is the one write that
touches a row directly (`drop_conditions`).

**The fill.** Where the answer has a value for `air_temperature`, `weather` or `waves` and the
dive's field is empty at the moment of the write, the field takes it and the row records the
value written (`filled_*`). That record is the field's "fetched" mark, and the rules around it:

- a diver's different, non-empty value replaces the fill and clears the mark, while an empty
  value for a marked field leaves the fill standing (`apply_diver_edits`) - the web's forms
  submit every field they loaded, and a form loaded before the fill arrived carries nulls the
  diver never chose;
- a row that no longer describes its dive takes its written values with it, but only those
  the field still holds, by a conditional update, so a diver's own value is never touched;
- a mark whose field no longer holds its written value, whoever changed it, is cleared
  (`_clear_stale_marks`), so the mark never claims a value the fetch did not write.

`current` is never filled: no model resolves the current at a dive site.

**Failures are charged to whoever caused them** (`services.open_meteo`): a refused request is
the dive's, and backs off; anything instance-wide leaves the dive pending with no row, bounded
by a cool-down after a 429 or a refused key and by a per-dive strike count that defers a dive
whose own request keeps failing.
"""

import logging
import math
import typing
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from enum import StrEnum
from time import monotonic
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import anyio
from redis.exceptions import RedisError
from sqlalchemy import Float, Numeric, and_, case, cast, delete, func, insert, literal, or_, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.db.database import local_session, release_read_transaction
from ..core.utils import cache
from ..core.utils.datetime_offset import split_dive_start_time
from ..models.dive import Dive
from ..models.dive_conditions import DiveConditions
from ..models.dive_dive_site import DiveDiveSite
from ..models.dive_site import DiveSite
from ..models.user import User
from ..schemas.dive import WaterType
from ..schemas.dive_conditions import (
    ConditionsExtreme,
    ConditionsFilled,
    ConditionsPosition,
    ConditionsSea,
    ConditionsWeather,
    DiveConditionsLookupRead,
    DiveConditionsRead,
    ExtremeKindName,
    TideStateName,
)
from . import open_meteo
from .cache_invalidation import invalidate_dive_caches
from .conditions_derivations import (
    DERIVATION_VERSION,
    Extreme,
    nearest_sample,
    tide,
    waves_from_height,
    weather_from_code,
)

logger = logging.getLogger(__name__)

FETCHED = "fetched"
SKIPPED = "skipped"
FAILED = "failed"

# The dive's own fields a fetch may write. `current` is not one of them.
FILLABLE = ("air_temperature", "weather", "waves")

# How many dives one run takes. A warm answer is well under a second and a dive costs at most
# two, so this ends inside the minute; the stop below bounds a slow provider.
BATCH_SIZE = 30
# A run takes no new dive after this, so a cold provider cannot carry it past arq's five-minute
# job timeout, where it would be cancelled mid-dive.
RUN_SECONDS = 45.0
# The run's own lock: held for the longest a run may last - the stop above plus the two calls
# the last dive may still make, each to its deadline - with margin, and lapsing by itself after
# a kill, so a deploy that interrupts a run costs one lifetime.
LOCK_KEY = "conditions:run-lock"
LOCK_LIFETIME_SECONDS = int(RUN_SECONDS + 2 * open_meteo.DEADLINE_SECONDS + 15)
# Deletes the lock only if it is still this run's, in one step: a run that outlived its lock
# must not release the next run's.
_RELEASE_LOCK = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"

# How long a limiting provider or a refused key is left alone before anything is asked again.
COOLDOWN_KEY = f"conditions:cooldown:{open_meteo.PROVIDER}"
RATE_LIMITED_COOLDOWN_SECONDS = 15 * 60
KEY_REFUSED_COOLDOWN_SECONDS = 60 * 60

# Consecutive instance-wide failures on one dive's own request - a place whose answer always
# times out or 5xxes - counted in Redis and reset by an answer. The third defers the dive.
STRIKES_TO_DEFER = 3
STRIKES_TTL_SECONDS = 24 * 60 * 60

# A request refused for the dive's own inputs is retried after an hour, doubling, and given up
# after the cap until an input changes. A deferral doubles from an hour up to a day and is
# never given up on.
ATTEMPTS_CAP = 5
RETRY_AFTER = timedelta(hours=1)
LONGEST_DEFERRAL = timedelta(days=1)


class Handled(StrEnum):
    FETCHED = "fetched"
    SKIPPED = "skipped"
    FAILED = "failed"
    DEFERRED = "deferred"
    CLEARED = "cleared"
    MOVED = "moved"
    STOPPED = "stopped"
    STRUCK = "struck"


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A dive as the candidate query saw it: detached values only, so the session can be
    released before each outbound call."""

    dive_id: int
    user_id: int
    start_time: datetime
    utc_offset_minutes: int | None
    start_date_only: bool
    latitude: float | None
    longitude: float | None
    water_type: str | None
    attempts: int
    deferrals: int

    @property
    def inputs(self) -> tuple[Any, ...]:
        return (
            self.start_time,
            self.utc_offset_minutes,
            self.start_date_only,
            self.latitude,
            self.longitude,
            self.water_type,
        )


def _rounded(coordinate: Any) -> Any:
    """Two decimals, about 1.1 km, in SQL: the one rounding of a position everywhere it is
    compared, stored or asked about. Python's `round` and Postgres's `round(numeric)` disagree
    on a half-way tie, so the value the request and the row use is always read back from here."""
    return cast(func.round(cast(coordinate, Numeric), 2), Float)


def _current_inputs() -> tuple[Any, Any, Any, Any]:
    """The primary site as a lateral join, and the dive's rounded position and effective water
    type as expressions over it.

    The primary site is the lowest `position`, not position 0 - it is a sort key, and deleting
    the first site promotes the next without renumbering. The position is the dive's own entry
    fix where an import wrote one, else that site's pin, never the geocoded locality.
    """
    site = (
        select(DiveSite.latitude.label("lat"), DiveSite.longitude.label("lon"), DiveSite.water_type)
        .join(DiveDiveSite, DiveDiveSite.dive_site_id == DiveSite.id)
        .where(DiveDiveSite.dive_id == Dive.id)
        .order_by(DiveDiveSite.position, DiveDiveSite.id)
        .limit(1)
        .lateral("primary_site")
    )
    pinned = and_(site.c.lat.is_not(None), site.c.lon.is_not(None))
    fixed = Dive.entry_latitude.is_not(None)
    latitude = _rounded(case((fixed, Dive.entry_latitude), (pinned, site.c.lat), else_=None))
    longitude = _rounded(case((fixed, Dive.entry_longitude), (pinned, site.c.lon), else_=None))
    water = func.coalesce(Dive.water_type, site.c.water_type)
    return site, latitude, longitude, water


def _inputs_differ(latitude: Any, longitude: Any, water: Any) -> Any:
    return or_(
        DiveConditions.input_start_time.is_distinct_from(Dive.start_time),
        DiveConditions.input_utc_offset_minutes.is_distinct_from(Dive.utc_offset_minutes),
        DiveConditions.input_start_date_only.is_distinct_from(Dive.start_date_only),
        DiveConditions.input_latitude.is_distinct_from(latitude),
        DiveConditions.input_longitude.is_distinct_from(longitude),
        DiveConditions.input_water_type.is_distinct_from(water),
    )


async def _candidates(db: AsyncSession, now: datetime, limit: int) -> list[_Candidate]:
    """The run's dives, in three tiers, each newest-saved first by `dive.id`.

    First the dives whose row no longer describes them, since each shows a wrong place's or
    day's values until it is answered. Then the dives with no row and the `failed` rows due for
    a retry, so the dive a diver just logged is answered before the backlog. Last the rows an
    older derivation wrote, whose values describe the right place and day.

    A dive of a deleted account is passed over with no row written, so a reversed deletion
    brings it back. So is a dive whose day - UTC, or the wall clock's where the offset is
    unknown - is after today's, until it arrives; the one exception is a row that no longer
    describes such a dive, which is taken so its values can go.
    """
    tomorrow = datetime.combine(now.astimezone(UTC).date() + timedelta(days=1), time.min, tzinfo=UTC)
    site, latitude, longitude, water = _current_inputs()
    stale = and_(DiveConditions.id.is_not(None), _inputs_differ(latitude, longitude, water))
    due = and_(DiveConditions.state == FAILED, DiveConditions.next_attempt_at <= now)
    outdated = and_(DiveConditions.state == FETCHED, DiveConditions.derivation_version < DERIVATION_VERSION)
    tier = case((stale, 0), (outdated, 2), else_=1)
    rows = await db.execute(
        select(
            Dive.id,
            Dive.user_id,
            Dive.start_time,
            Dive.utc_offset_minutes,
            Dive.start_date_only,
            latitude,
            longitude,
            water,
            # A count earned by inputs the dive no longer has starts over with the new ones.
            case((stale, 0), else_=func.coalesce(DiveConditions.attempts, 0)),
            case((stale, 0), else_=func.coalesce(DiveConditions.deferrals, 0)),
        )
        .select_from(Dive)
        .join(User, User.id == Dive.user_id)
        .outerjoin(site, true())
        .outerjoin(DiveConditions, DiveConditions.dive_id == Dive.id)
        .where(
            Dive.is_deleted.is_(False),
            User.is_deleted.is_(False),
            or_(stale, and_(Dive.start_time < tomorrow, or_(DiveConditions.id.is_(None), due, outdated))),
        )
        .order_by(tier, Dive.id.desc())
        .limit(limit)
    )
    return [_Candidate(*row) for row in rows]


async def _drop_record(db: AsyncSession, dive_id: int) -> None:
    """Delete the dive's row and empty every field still holding the value it wrote there.

    Conditional per field, so a value the diver typed since - including in the seconds between
    the run's read and this write - is kept. An unmarked field is never touched.
    """
    for field in FILLABLE:
        written = select(getattr(DiveConditions, f"filled_{field}")).where(DiveConditions.dive_id == dive_id)
        await db.execute(
            update(Dive)
            .where(Dive.id == dive_id, getattr(Dive, field) == written.scalar_subquery())
            .values({field: None})
        )
    await db.execute(delete(DiveConditions).where(DiveConditions.dive_id == dive_id))


async def drop_conditions(db: AsyncSession, dive_id: int) -> None:
    """Delete a dive's row and empty nothing - for a write that replaces the dive's own values
    wholesale, as the import's restore of a deleted dive does. Not committed."""
    await db.execute(delete(DiveConditions).where(DiveConditions.dive_id == dive_id))


async def _invalidate(user_id: int) -> None:
    """Drop the diver's cached dive reads after a committed write. A Redis that is down costs
    only this: the diver sees the change when the detail cache's hour runs out."""
    try:
        await invalidate_dive_caches(user_id)
    except RedisError as exc:
        logger.warning("Dive caches not dropped after a conditions write (%s).", type(exc).__name__)


def _input_values(candidate: _Candidate) -> dict[str, Any]:
    return {
        "input_start_time": candidate.start_time,
        "input_utc_offset_minutes": candidate.utc_offset_minutes,
        "input_start_date_only": candidate.start_date_only,
        "input_latitude": candidate.latitude,
        "input_longitude": candidate.longitude,
        "input_water_type": candidate.water_type,
    }


async def _settle(
    db: AsyncSession, candidate: _Candidate, row: dict[str, Any] | None, fills: dict[str, Any] | None = None
) -> bool:
    """Replace the dive's row with `row`, or with nothing, filling empty fields from `fills` -
    in one transaction, and only if the dive's inputs still equal the ones the run read.

    The snapshot decided what to ask; it never decides what to overwrite. Every fill is a
    conditional update evaluated at the write, so a weather typed while the provider was
    answering is kept, and the written value is recorded only where a field changed. `False`
    when the dive moved, was deleted or another run wrote it first; the next run sees it again.
    """
    site, latitude, longitude, water = _current_inputs()
    current = (
        await db.execute(
            select(
                Dive.start_time,
                Dive.utc_offset_minutes,
                Dive.start_date_only,
                latitude,
                longitude,
                water,
                Dive.is_deleted,
            )
            .select_from(Dive)
            .outerjoin(site, true())
            .where(Dive.id == candidate.dive_id)
            .with_for_update(of=Dive)
        )
    ).one_or_none()
    if current is None or current.is_deleted or tuple(current)[:6] != candidate.inputs:
        await db.rollback()
        return False

    await _drop_record(db, candidate.dive_id)
    if row is not None:
        written: dict[str, Any] = {}
        for field, value in (fills or {}).items():
            if value is None:
                continue
            column = getattr(Dive, field)
            changed = await db.execute(
                update(Dive)
                .where(Dive.id == candidate.dive_id, column.is_(None))
                .values({field: value})
                .returning(Dive.id)
            )
            if changed.first() is not None:
                written[f"filled_{field}"] = value
        try:
            await db.execute(
                insert(DiveConditions).values(dive_id=candidate.dive_id, **_input_values(candidate), **row, **written)
            )
        except IntegrityError:
            await db.rollback()
            return False
    await db.commit()
    await _invalidate(candidate.user_id)
    return True


async def _clear_stale_marks(db: AsyncSession) -> None:
    """Clear every mark whose field no longer holds the value the fetch wrote there, whichever
    route changed it - compared with the stored written value, never re-derived."""
    users: set[int] = set()
    for field in FILLABLE:
        written = getattr(DiveConditions, f"filled_{field}")
        result = await db.execute(
            update(DiveConditions)
            .where(
                DiveConditions.dive_id == Dive.id,
                written.is_not(None),
                getattr(Dive, field).is_distinct_from(written),
            )
            .values({f"filled_{field}": None})
            .returning(Dive.user_id)
        )
        users.update(result.scalars())
    await db.commit()
    for user_id in users:
        await _invalidate(user_id)


async def apply_diver_edits(
    db: AsyncSession, dive_id: int, stored: dict[str, Any], update_data: dict[str, Any]
) -> None:
    """Hold `PATCH /dive/{uuid}` to the mark's rule, editing `update_data` in place.

    For each marked field the request names: a different, non-empty value is the diver's and
    clears the mark; an empty one is dropped from the update, leaving the fill standing. The
    web's edit page and Environment dialog submit every field they loaded, so a form opened
    before the fill arrived and saved after it sends nulls for values the diver never saw -
    taking those as edits would erase the fill for good. The cost is that a fetched value can be
    replaced and never blanked. Not committed: it joins the PATCH's transaction.
    """
    if not any(field in update_data for field in FILLABLE):
        return
    marked = (
        await db.execute(
            select(
                DiveConditions.filled_air_temperature, DiveConditions.filled_weather, DiveConditions.filled_waves
            ).where(DiveConditions.dive_id == dive_id)
        )
    ).one_or_none()
    if marked is None:
        return
    cleared: dict[str, None] = {}
    for field, written in zip(FILLABLE, marked, strict=True):
        if written is None or field not in update_data:
            continue
        if update_data[field] is None:
            del update_data[field]
        elif update_data[field] != stored[field]:
            cleared[f"filled_{field}"] = None
    if cleared:
        # The dive's row before the conditions row, the order `_settle` takes them in, so a PATCH
        # and the worker settling the same dive cannot deadlock.
        await db.execute(select(Dive.id).where(Dive.id == dive_id).with_for_update())
        await db.execute(update(DiveConditions).where(DiveConditions.dive_id == dive_id).values(cleared))


# -------- the record as read --------


def _zone(offset_minutes: int) -> timezone:
    return timezone(timedelta(minutes=offset_minutes))


def to_read(row: DiveConditions) -> DiveConditionsRead:
    """A `fetched` row as the API returns it, every instant at the offset it was resolved at."""
    if (
        row.provider is None
        or row.fetched_at is None
        or row.at is None
        or row.utc_offset_minutes is None
        or row.latitude is None
        or row.longitude is None
    ):
        raise ValueError("only a fetched row has a record to read")
    offset = row.utc_offset_minutes

    def extreme(kind: str | None, at: datetime | None, height: float | None) -> ConditionsExtreme | None:
        if kind is None or at is None or height is None:
            return None
        return ConditionsExtreme(
            kind=typing.cast(ExtremeKindName, kind), at=at.astimezone(_zone(offset)), height=height
        )

    weather = None
    if row.weather_model is not None:
        weather = ConditionsWeather(
            model=row.weather_model,
            air_temperature=row.air_temperature,
            relative_humidity=row.relative_humidity,
            wind_speed=row.wind_speed,
            wind_direction=row.wind_direction,
            wind_gusts=row.wind_gusts,
            cloud_cover=row.cloud_cover,
            precipitation=row.precipitation,
            pressure=row.pressure,
            weather_code=row.weather_code,
        )
    sea = None
    if row.sea_model is not None:
        sea = ConditionsSea(
            model=row.sea_model,
            sea_level_height=row.sea_level_height,
            tide_state=typing.cast(TideStateName | None, row.tide_state),
            previous_extreme=extreme(row.previous_extreme_kind, row.previous_extreme_at, row.previous_extreme_height),
            next_extreme=extreme(row.next_extreme_kind, row.next_extreme_at, row.next_extreme_height),
            sea_surface_temperature=row.sea_surface_temperature,
            wave_height=row.wave_height,
            wave_period=row.wave_period,
            swell_wave_height=row.swell_wave_height,
        )
    return DiveConditionsRead(
        provider=row.provider,
        fetched_at=row.fetched_at,
        at=row.at.astimezone(_zone(offset)),
        utc_offset_minutes=offset,
        position=ConditionsPosition(latitude=row.latitude, longitude=row.longitude),
        attribution=open_meteo.ATTRIBUTION,
        weather=weather,
        sea=sea,
        filled=ConditionsFilled(
            air_temperature=row.filled_air_temperature, weather=row.filled_weather, waves=row.filled_waves
        ),
    )


async def get_conditions(db: AsyncSession, dive_id: int) -> DiveConditionsRead | None:
    """The dive's record for its detail read, or `None` unless a provider answered for it."""
    row = (
        await db.execute(
            select(DiveConditions).where(DiveConditions.dive_id == dive_id, DiveConditions.state == FETCHED)
        )
    ).scalar_one_or_none()
    return None if row is None else to_read(row)


async def get_conditions_for_dives(db: AsyncSession, dive_ids: list[int]) -> dict[int, DiveConditionsRead]:
    """`get_conditions` for many dives in one query, keyed by dive id, for the export."""
    if not dive_ids:
        return {}
    rows = await db.execute(
        select(DiveConditions).where(DiveConditions.dive_id.in_(dive_ids), DiveConditions.state == FETCHED)
    )
    return {row.dive_id: to_read(row) for row in rows.scalars()}


# -------- the record as fetched --------


def _number(hourly: dict[str, list[Any]], name: str, index: int) -> float | None:
    series = hourly.get(name)
    if not isinstance(series, list) or index >= len(series):
        return None
    value = series[index]
    if not isinstance(value, int | float) or isinstance(value, bool) or not math.isfinite(value):
        return None
    return float(value)


def _numbers(hourly: dict[str, list[Any]], name: str, length: int) -> list[float | None]:
    return [_number(hourly, name, index) for index in range(length)]


def _times(hourly: dict[str, list[Any]]) -> list[int] | None:
    times = hourly.get("time")
    if not isinstance(times, list) or not all(isinstance(t, int) and not isinstance(t, bool) for t in times):
        return None
    return times


def _weather_values(answer: open_meteo.Answered, instant: datetime) -> dict[str, Any]:
    """The weather block at the hour nearest `instant`, or none where the answer has no hours."""
    times = _times(answer.hourly)
    index = None if times is None else nearest_sample(times, int(instant.timestamp()))
    if index is None:
        return {}
    code = _number(answer.hourly, "weather_code", index)
    return {
        "weather_model": open_meteo.MODEL,
        "air_temperature": _number(answer.hourly, "temperature_2m", index),
        "relative_humidity": _number(answer.hourly, "relative_humidity_2m", index),
        "wind_speed": _number(answer.hourly, "wind_speed_10m", index),
        "wind_direction": _number(answer.hourly, "wind_direction_10m", index),
        "wind_gusts": _number(answer.hourly, "wind_gusts_10m", index),
        "cloud_cover": _number(answer.hourly, "cloud_cover", index),
        "precipitation": _number(answer.hourly, "precipitation", index),
        "pressure": _number(answer.hourly, "pressure_msl", index),
        # WMO 4677's codes are 0 to 99; anything else is no code, not a row the read refuses.
        "weather_code": int(code) if code is not None and 0 <= code <= 99 else None,
    }


def _extreme_values(prefix: str, extreme: Extreme | None) -> dict[str, Any]:
    if extreme is None:
        return {}
    return {
        f"{prefix}_kind": extreme.kind.value,
        f"{prefix}_at": datetime.fromtimestamp(extreme.at, UTC),
        f"{prefix}_height": extreme.height,
    }


def _sea_values(answer: open_meteo.Answered, instant: datetime) -> dict[str, Any]:
    """The sea block at `instant`, or none where every series is null - a cell the model does
    not cover, a day before its start, one of its gaps."""
    times = _times(answer.hourly)
    if not times:
        return {}
    if all(value is None for name in open_meteo.SEA_VARIABLES for value in _numbers(answer.hourly, name, len(times))):
        return {}
    at = int(instant.timestamp())
    index = nearest_sample(times, at)
    if index is None:
        return {}
    reading = tide(times, _numbers(answer.hourly, "sea_level_height_msl", len(times)), at)
    return {
        "sea_model": open_meteo.MODEL,
        "sea_level_height": None if reading is None else reading.height,
        "tide_state": None if reading is None or reading.state is None else reading.state.value,
        **_extreme_values("previous_extreme", None if reading is None else reading.previous),
        **_extreme_values("next_extreme", None if reading is None else reading.next),
        "sea_surface_temperature": _number(answer.hourly, "sea_surface_temperature", index),
        "wave_height": _number(answer.hourly, "wave_height", index),
        "wave_period": _number(answer.hourly, "wave_period", index),
        "swell_wave_height": _number(answer.hourly, "swell_wave_height", index),
    }


# -------- the run --------


async def _take_lock(token: str) -> bool:
    """Whether this run may go ahead. Fails open, like the budget: without Redis two runs may
    overlap, and the row's uniqueness on the dive makes that a lost race, not a double row."""
    if cache.client is None:
        return True
    try:
        return bool(await cache.client.set(LOCK_KEY, token, nx=True, px=LOCK_LIFETIME_SECONDS * 1000))
    except RedisError:
        return True


async def _release_lock(token: str) -> None:
    if cache.client is None:
        return
    with anyio.CancelScope(shield=True):
        try:
            await cache.client.eval(_RELEASE_LOCK, 1, LOCK_KEY, token)  # type: ignore[misc]
        except RedisError:
            logger.warning("The dive conditions run lock could not be released; it lapses by itself.")


async def _cooling_down() -> bool:
    if cache.client is None:
        return False
    try:
        return await cache.client.get(COOLDOWN_KEY) is not None
    except RedisError:
        return False


async def _cool_down(seconds: int) -> None:
    if cache.client is None:
        return
    try:
        await cache.client.set(COOLDOWN_KEY, "1", ex=seconds)
    except RedisError:
        pass


def _strikes_key(dive_id: int) -> str:
    return f"conditions:strikes:{dive_id}"


async def _strike(dive_id: int) -> int:
    """Count one more instance-wide failure on this dive's request; `0` without Redis, which
    then defers nothing."""
    if cache.client is None:
        return 0
    try:
        strikes = int(await cache.client.incr(_strikes_key(dive_id)))
        await cache.client.expire(_strikes_key(dive_id), STRIKES_TTL_SECONDS)
    except RedisError:
        return 0
    return strikes


async def _forgive(dive_id: int) -> None:
    if cache.client is None:
        return
    try:
        await cache.client.delete(_strikes_key(dive_id))
    except RedisError:
        pass


def _day(candidate: _Candidate) -> date:
    """The dive's UTC day, or its wall clock's day where the offset is unknown - which is what
    `start_time` holds either way."""
    return candidate.start_time.astimezone(UTC).date()


async def _skip(db: AsyncSession, candidate: _Candidate, reason: str) -> Handled:
    logger.info("Dive %d skipped: %s.", candidate.dive_id, reason.replace("_", " "))
    row = {"state": SKIPPED, "reason": reason, "derivation_version": DERIVATION_VERSION}
    return Handled.SKIPPED if await _settle(db, candidate, row) else Handled.MOVED


async def _refused(db: AsyncSession, candidate: _Candidate, now: datetime, reason: str) -> Handled:
    """A request the provider would not answer for this dive's inputs: retried after an hour,
    doubling, and left alone after the cap until an input changes. Sets no mark, so a row like
    this is always safe to delete."""
    attempts = candidate.attempts + 1
    row = {
        "state": FAILED,
        "reason": reason,
        "derivation_version": DERIVATION_VERSION,
        "attempts": attempts,
        "deferrals": candidate.deferrals,
        "next_attempt_at": now + RETRY_AFTER * 2 ** (attempts - 1) if attempts < ATTEMPTS_CAP else None,
    }
    return Handled.FAILED if await _settle(db, candidate, row) else Handled.MOVED


async def _stop_asking(why: open_meteo.CouldNotAsk) -> bool:
    """Whether `why` is the instance's, so nothing more should be asked now: over budget, or a
    rate limit or a refused key, which also cool down - the latter an hour, since a key on a plan
    without the archive is refused there with a 401."""
    match why:
        case open_meteo.CouldNotAsk.BUDGET:
            return True
        case open_meteo.CouldNotAsk.RATE_LIMITED:
            await _cool_down(RATE_LIMITED_COOLDOWN_SECONDS)
            return True
        case open_meteo.CouldNotAsk.KEY_REFUSED:
            logger.error(
                "%s refused this instance's key; check DIVE_CONDITIONS_OPEN_METEO_API_KEY and that its plan "
                "includes the Historical Weather API.",
                open_meteo.PROVIDER,
            )
            await _cool_down(KEY_REFUSED_COOLDOWN_SECONDS)
            return True
    return False


async def _could_not_ask(
    db: AsyncSession, candidate: _Candidate, now: datetime, why: open_meteo.CouldNotAsk
) -> Handled:
    """Nothing about the dive: it stays pending with no row, and the run decides whether to go on.

    Over budget, rate-limited or refused its key, it stops (`_stop_asking`). A timeout, a 5xx or
    a transport failure is struck against the dive, and the third in a row defers it - a
    `failed` row due an hour later, doubling to a day, with no attempt counted - so a place
    whose request always fails is not asked every minute at the head of the queue.
    """
    if await _stop_asking(why):
        return Handled.STOPPED
    if await _strike(candidate.dive_id) >= STRIKES_TO_DEFER:
        deferrals = candidate.deferrals + 1
        row = {
            "state": FAILED,
            "reason": "deferred",
            "derivation_version": DERIVATION_VERSION,
            "attempts": candidate.attempts,
            "deferrals": deferrals,
            # The exponent is capped before it multiplies: a dive deferred daily for weeks would
            # otherwise overflow `timedelta` long after the delay had stopped growing.
            "next_attempt_at": now + min(RETRY_AFTER * 2 ** min(deferrals - 1, 5), LONGEST_DEFERRAL),
        }
        await _settle(db, candidate, row)
    return Handled.STRUCK


def _resolve_instant(
    start_time: datetime, utc_offset_minutes: int | None, zone_name: str | None
) -> tuple[datetime, int] | None:
    """The dive's instant and the offset it is spelled at.

    The dive's own where it has one. Where only the wall clock is known, that clock placed in the
    zone the provider named for the position, at the offset that zone had on the dive's date -
    never from the answer's hour labels, which carry today's offset for every date.
    """
    if utc_offset_minutes is not None:
        return start_time.astimezone(UTC), utc_offset_minutes
    if zone_name is None:
        return None
    try:
        zone = ZoneInfo(zone_name)
    except ZoneInfoNotFoundError, ValueError:
        return None
    local = start_time.replace(tzinfo=None).replace(tzinfo=zone)
    offset = local.utcoffset()
    if offset is None:
        return None
    return local.astimezone(UTC), int(offset.total_seconds() // 60)


@dataclass(frozen=True, slots=True)
class _Answer:
    """What the provider answered for one time and place: the instant read, the offset it is
    spelled at, and the weather and sea columns at it."""

    instant: datetime
    offset: int
    weather: dict[str, Any]
    sea: dict[str, Any]

    @property
    def fills(self) -> dict[str, Any]:
        """The dive's own fields the answer would fill, each `None` where it has no value."""
        sky = weather_from_code(self.weather.get("weather_code"))
        sea_state = waves_from_height(self.sea.get("wave_height"))
        return {
            "air_temperature": self.weather.get("air_temperature"),
            "weather": None if sky is None else sky.value,
            "waves": None if sea_state is None else sea_state.value,
        }


@dataclass(frozen=True, slots=True)
class _Refusal:
    """A request refused for these inputs, `reason` naming which."""

    reason: str


async def _answer(
    start_time: datetime,
    utc_offset_minutes: int | None,
    latitude: float,
    longitude: float,
    water_type: str | None,
    today: date,
    *,
    timeout: float = open_meteo.TIMEOUT_SECONDS,
    deadline: float = open_meteo.DEADLINE_SECONDS,
) -> _Answer | _Refusal | open_meteo.CouldNotAsk:
    """Ask the provider about a start time as stored - an instant, or a wall clock labelled UTC
    where the offset is unknown - at a rounded position. The worker and the form's preview both
    ask through here, so the same dive asks the same question and reads one cached answer."""
    day = start_time.astimezone(UTC).date()
    known_offset = utc_offset_minutes is not None
    # The archive refuses an end date after today and already answers every hour of today. A
    # wall clock with no offset spans a day either side until its zone says which UTC day it is.
    start = day if known_offset else day - timedelta(days=1)
    weather = await open_meteo.fetch_weather(
        latitude,
        longitude,
        start,
        min(day + timedelta(days=1), today),
        auto_zone=not known_offset,
        timeout=timeout,
        deadline=deadline,
    )
    if isinstance(weather, open_meteo.Refused):
        return _Refusal(f"weather_{weather.status}")
    if isinstance(weather, open_meteo.CouldNotAsk):
        return weather

    resolved = _resolve_instant(start_time, utc_offset_minutes, weather.timezone)
    if resolved is None:
        return _Refusal("no_zone")
    instant, offset = resolved

    sea_values: dict[str, Any] = {}
    # Unknown water is asked about; only fresh water is not. A lake near a coast must not
    # inherit the coast's tide.
    if water_type != WaterType.FRESH:
        sea_day = instant.date()
        sea = await open_meteo.fetch_sea(
            latitude,
            longitude,
            sea_day - timedelta(days=1),
            sea_day + timedelta(days=1),
            timeout=timeout,
            deadline=deadline,
        )
        if isinstance(sea, open_meteo.Refused):
            return _Refusal(f"sea_{sea.status}")
        if isinstance(sea, open_meteo.CouldNotAsk):
            # Left unanswered as a whole: the weather answer is cached, so the retry costs only
            # this call.
            return sea
        sea_values = _sea_values(sea, instant)

    return _Answer(instant=instant, offset=offset, weather=_weather_values(weather, instant), sea=sea_values)


async def _handle(db: AsyncSession, candidate: _Candidate, now: datetime) -> Handled:
    today = now.astimezone(UTC).date()
    if _day(candidate) > today:
        # Only a row that no longer describes a dive moved into the future reaches here.
        return Handled.CLEARED if await _settle(db, candidate, None) else Handled.MOVED
    if candidate.start_date_only:
        return await _skip(db, candidate, "no_time")
    if candidate.latitude is None or candidate.longitude is None:
        return await _skip(db, candidate, "no_position")
    latitude, longitude = candidate.latitude, candidate.longitude

    answer = await _answer(
        candidate.start_time, candidate.utc_offset_minutes, latitude, longitude, candidate.water_type, today
    )
    if isinstance(answer, _Refusal):
        return await _refused(db, candidate, now, answer.reason)
    if isinstance(answer, open_meteo.CouldNotAsk):
        return await _could_not_ask(db, candidate, now, answer)

    await _forgive(candidate.dive_id)
    row = {
        "state": FETCHED,
        "derivation_version": DERIVATION_VERSION,
        "provider": open_meteo.PROVIDER,
        "fetched_at": now,
        "at": answer.instant,
        "utc_offset_minutes": answer.offset,
        "latitude": latitude,
        "longitude": longitude,
        **answer.weather,
        **answer.sea,
    }
    return Handled.FETCHED if await _settle(db, candidate, row, answer.fills) else Handled.MOVED


async def _run(now: datetime) -> str:
    if await _cooling_down():
        return "Cooling down after the provider refused a request"
    started = monotonic()
    handled: Counter[Handled] = Counter()
    async with local_session() as db:
        await _clear_stale_marks(db)
        candidates = await _candidates(db, now, BATCH_SIZE)
        await release_read_transaction(db)
        failures_in_a_row = 0
        for candidate in candidates:
            if monotonic() - started >= RUN_SECONDS:
                break
            outcome = await _handle(db, candidate, now)
            handled[outcome] += 1
            if outcome is Handled.STOPPED:
                break
            if outcome is Handled.STRUCK:
                failures_in_a_row += 1
                # One request that always fails cannot hold every older dive behind it; two in
                # a row is the provider, not the dive.
                if failures_in_a_row >= 2:
                    break
            else:
                failures_in_a_row = 0
    summary = ", ".join(f"{handled[outcome]} {outcome.value}" for outcome in Handled if handled[outcome]) or "nothing"
    return f"{len(candidates)} candidate(s): {summary}"


async def run_dive_conditions(now: datetime | None = None) -> str:
    """One run of the worker's `fetch_dive_conditions`: nothing at all while
    `DIVE_CONDITIONS_PROVIDER` is unset, so every dive is still pending the day an operator
    names the provider; otherwise one batch, under the run's own lock."""
    if not settings.dive_conditions:
        return "Dive conditions are off"
    token = uuid4().hex
    if not await _take_lock(token):
        return "A run is already in progress"
    try:
        summary = await _run(now or datetime.now(UTC))
    finally:
        await _release_lock(token)
    logger.info("fetch_dive_conditions: %s", summary)
    return summary


# -------- the form's preview --------


async def preview_conditions(
    db: AsyncSession,
    *,
    start_time: datetime | date,
    latitude: float | None,
    longitude: float | None,
    water_type: str | None,
    now: datetime,
) -> DiveConditionsLookupRead | None:
    """What the worker would fill for a dive with these inputs, for the dive form to show; `None`
    when the provider could not be asked, which says nothing about the inputs.

    The inputs go through the worker's own steps - its rounding, its question, its derivations -
    so a dive saved with them is filled with exactly these values, and its fetch reads the answer
    this one cached. Writes nothing. Inputs the worker would not ask about - a day after today, a
    date alone, no position - and a request refused for them answer with every value null.
    A cool-down after the provider limited or refused the instance holds the preview as it holds
    the run, cached answers included.
    """
    stored, offset, date_only = split_dive_start_time(start_time)
    today = now.astimezone(UTC).date()
    nothing = DiveConditionsLookupRead(
        provider=open_meteo.PROVIDER,
        attribution=open_meteo.ATTRIBUTION,
        filled=ConditionsFilled(air_temperature=None, weather=None, waves=None),
    )
    if stored.astimezone(UTC).date() > today or date_only or latitude is None or longitude is None:
        return nothing
    rounded_latitude, rounded_longitude = (
        await db.execute(select(_rounded(literal(latitude, Float)), _rounded(literal(longitude, Float))))
    ).one()
    await release_read_transaction(db)

    if await _cooling_down():
        return None
    answer = await _answer(
        stored,
        offset,
        rounded_latitude,
        rounded_longitude,
        water_type,
        today,
        timeout=open_meteo.PREVIEW_TIMEOUT_SECONDS,
        deadline=open_meteo.PREVIEW_DEADLINE_SECONDS,
    )
    if isinstance(answer, open_meteo.CouldNotAsk):
        await _stop_asking(answer)
        return None
    if isinstance(answer, _Refusal):
        return nothing
    return DiveConditionsLookupRead(
        provider=open_meteo.PROVIDER,
        attribution=open_meteo.ATTRIBUTION,
        at=answer.instant.astimezone(_zone(answer.offset)),
        utc_offset_minutes=answer.offset,
        filled=ConditionsFilled(**answer.fills),
    )


# -------- the import's restore --------


async def restore_conditions(db: AsyncSession, dive_id: int, entry: DiveConditionsRead, values: dict[str, Any]) -> None:
    """Write the record a DiveJSON document carried for a dive the import just wrote, without
    asking the provider. Not committed: it joins the import's transaction.

    A field is marked as fetched only where the document's own member still equals the value the
    record says was written - a document whose weather was changed in another tool that kept the
    record imports the change as the diver's. The row's inputs are the dive's only where the
    record's instant and position equal the dive's; otherwise they are left null, so the worker
    finds the row stale and answers the dive afresh rather than restoring a record of another
    time or place as current.
    """
    site, latitude, longitude, water = _current_inputs()
    dive = (
        await db.execute(
            select(Dive.start_time, Dive.utc_offset_minutes, Dive.start_date_only, latitude, longitude, water)
            .select_from(Dive)
            .outerjoin(site, true())
            .where(Dive.id == dive_id)
        )
    ).one()
    start_time, offset, date_only, dive_latitude, dive_longitude, dive_water = dive
    # The dive's instant, where it has one: its own, or its wall clock read at the offset the
    # record resolved, which is what a dive with no offset of its own was answered at.
    instant: datetime | None = None
    if not date_only:
        instant = start_time - timedelta(minutes=entry.utc_offset_minutes if offset is None else 0)
    describes = (
        instant == entry.at and dive_latitude == entry.position.latitude and dive_longitude == entry.position.longitude
    )
    inputs = (
        {
            "input_start_time": start_time,
            "input_utc_offset_minutes": offset,
            "input_start_date_only": date_only,
            "input_latitude": dive_latitude,
            "input_longitude": dive_longitude,
            "input_water_type": dive_water,
        }
        if describes
        else {}
    )
    filled = {
        f"filled_{field}": written
        for field in FILLABLE
        if (written := getattr(entry.filled, field)) is not None and values.get(field) == written
    }
    weather = entry.weather
    sea = entry.sea
    await db.execute(
        insert(DiveConditions).values(
            dive_id=dive_id,
            state=FETCHED,
            derivation_version=DERIVATION_VERSION,
            provider=entry.provider,
            fetched_at=entry.fetched_at,
            at=entry.at,
            utc_offset_minutes=entry.utc_offset_minutes,
            latitude=entry.position.latitude,
            longitude=entry.position.longitude,
            **inputs,
            **({} if weather is None else _weather_columns(weather)),
            **({} if sea is None else _sea_columns(sea)),
            **filled,
        )
    )


def _weather_columns(weather: ConditionsWeather) -> dict[str, Any]:
    return {
        "weather_model": weather.model,
        "air_temperature": weather.air_temperature,
        "relative_humidity": weather.relative_humidity,
        "wind_speed": weather.wind_speed,
        "wind_direction": weather.wind_direction,
        "wind_gusts": weather.wind_gusts,
        "cloud_cover": weather.cloud_cover,
        "precipitation": weather.precipitation,
        "pressure": weather.pressure,
        "weather_code": weather.weather_code,
    }


def _sea_columns(sea: ConditionsSea) -> dict[str, Any]:
    columns: dict[str, Any] = {
        "sea_model": sea.model,
        "sea_level_height": sea.sea_level_height,
        "tide_state": sea.tide_state,
        "sea_surface_temperature": sea.sea_surface_temperature,
        "wave_height": sea.wave_height,
        "wave_period": sea.wave_period,
        "swell_wave_height": sea.swell_wave_height,
    }
    for prefix, extreme in (("previous_extreme", sea.previous_extreme), ("next_extreme", sea.next_extreme)):
        if extreme is not None:
            columns.update(
                {f"{prefix}_kind": extreme.kind, f"{prefix}_at": extreme.at, f"{prefix}_height": extreme.height}
            )
    return columns
