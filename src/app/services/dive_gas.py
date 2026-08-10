"""Gas-consumption arithmetic: how much gas a dive used, normalized to the surface.

`compute_gas_use` - everything above `gas_use_history` - is deliberately pure: no
session, no ORM, no clock of its own, so the rules can be tested exhaustively without a
database (see `tests/test_dive_gas.py`). Same split as `services/gear_service.py`.

Unlike that module's `service_status`, this has *no* twin in the web app, and shouldn't
grow one. Service status depends on today's date, which is why the browser has to derive
it; a dive's gas use is derived entirely from columns already on the dive and its
mixtures, so it can never go stale and is safe to compute once here and cache alongside
the dive (see `_cached_read_dive` in `api/v1/dives.py`). The browser renders what it's
given, and only owns the phrasing of why a figure is *absent* (`lib/dive-gas.ts`).
"""

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.datetime_offset import combine_start_time
from ..crud.crud_dive_mixtures import get_mixtures_for_dives
from ..models.dive import Dive
from ..schemas.dive import DiveGasUse, DiveGasUsePoint
from ..schemas.dive_mixture import DiveMixtureRead

# Metres of water per bar of ambient pressure. Salt water is nearer 10.06 m/bar and
# fresh water 10.33, but a dive has no water-type field and every dive log in
# circulation uses the round number. The resulting error is under 3% - comfortably
# inside the error of a hand-entered (or computer-averaged) average depth, and it
# applies equally to every dive, so the trend the dashboard graphs is unaffected.
#
# Two further simplifications are baked in for the same reason: surface pressure is
# assumed to be 1 bar (wrong at an altitude lake) and air is treated as an ideal gas
# (optimistic by roughly 5% at a 230 bar fill). Both are what every other dive log
# does, and correcting either would need data the app doesn't collect.
METERS_PER_BAR = 10.0


def compute_gas_use(
    *,
    duration: int,
    avg_depth: float | None,
    mixtures: Sequence[DiveMixtureRead],
) -> DiveGasUse | None:
    """Surface-normalized gas consumption for a dive, or `None` if it can't be known.

    The arithmetic is only meaningful when every litre breathed can be attributed to a
    known depth over a known time, so this returns `None` rather than a best guess
    whenever that doesn't hold:

    - **Not exactly one mixture.** A dive with several cylinders records no clue as to
      *how* they were breathed - a stage bottle emptied over five minutes at 6 m and a
      back gas breathed for forty at 30 m would both be divided by the whole dive's
      average depth, badly misattributing the shallow gas. Manifolded twins are the
      common case and already work here, because they're logged as one mixture with the
      pair's combined water capacity (see `VOLUME_OPTIONS` in the web app) and a single
      shared pressure, which is exactly right. Unlocking genuinely staged cylinders
      needs per-mixture time-on-gas and depth-on-gas, which don't exist yet.
    - **No average depth**, or a nonsensical one. Max depth is not a substitute: a dive
      spends only a moment there, so it would understate consumption by a wide margin.
    - **Either cylinder pressure missing.** Half a pressure pair says nothing.
    - **No pressure drop at all.** A cylinder that came up as full as it went down
      wasn't breathed - that's an unused pony bottle or a typo, not a diver with a
      0 L/min consumption rate.

    `duration` and `volume` are already `CHECK`-constrained positive at the database
    level (`ck_dive_duration_positive`, `ck_dive_mixture_volume_positive`), as is the
    pressure ordering (`ck_dive_mixture_pressure_order`, so the drop can never come out
    negative). They're re-checked here anyway - this is a pure function that also gets
    called from tests and could one day be called on unsaved input, and a zero slipping
    into the denominator would raise rather than return a wrong number.
    """
    if len(mixtures) != 1:
        return None

    mixture = mixtures[0]
    if avg_depth is None or avg_depth <= 0:
        return None
    if mixture.start_pressure is None or mixture.end_pressure is None:
        return None
    if duration <= 0 or mixture.volume <= 0:
        return None

    pressure_used = mixture.start_pressure - mixture.end_pressure
    if pressure_used <= 0:
        return None

    # Gas that came out of the cylinder, expressed as the volume it would occupy at the
    # surface: that's what makes the figure comparable between a 10 L and a 15 L tank.
    gas_used = pressure_used * mixture.volume

    # Dividing by ambient pressure is what turns "gas breathed at depth" into "gas the
    # diver would have breathed doing the same thing at the surface" - the whole point
    # of the metric, and why a deep dive and a shallow one become comparable.
    ambient_pressure = 1 + avg_depth / METERS_PER_BAR
    surface_minutes = ambient_pressure * (duration / 60)

    return DiveGasUse(
        gas_used=round(gas_used, 2),
        rmv=round(gas_used / surface_minutes, 2),
        sac_bar_per_min=round(pressure_used / surface_minutes, 2),
    )


async def gas_use_history(db: AsyncSession, user_id: int) -> list[DiveGasUsePoint]:
    """Every dive of a user's that yields a gas-use figure, oldest first.

    Deliberately *not* a paginated list endpoint's job: the point of the series is the
    trend across a diving career, so a page of ten would be meaningless, and `GET /dives`
    doesn't carry mixtures anyway.

    Two queries - the dives, then their mixtures batched - and `compute_gas_use` decides
    per dive. The obvious optimization is to push its conditions into SQL (a `HAVING
    count(*) = 1`, `avg_depth IS NOT NULL`, both pressures present) so the un-derivable
    majority never comes back; it's left out on purpose. That would be a second,
    silent copy of the rules in a second language, and the failure mode when the two
    drift is a dive quietly missing from a graph, which nobody notices. The cost of
    doing it in Python is a few hundred discarded rows per user, on a cached endpoint,
    at a scale this project has already sized elsewhere (see DECISIONS.md on
    `recalculate_dive_stats` being an O(n) rescan on every write). Revisit it if per-user
    dive counts ever justify it - and then move `compute_gas_use`'s conditions into a
    single place both can read.

    Ordered oldest-first because that's chart order; the caller doesn't re-sort.
    """
    result = await db.execute(
        select(
            Dive.id,
            Dive.uuid,
            Dive.dive_number,
            Dive.start_time,
            Dive.utc_offset_minutes,
            Dive.duration,
            Dive.avg_depth,
        )
        .where(Dive.user_id == user_id, Dive.is_deleted.is_(False))
        .order_by(Dive.start_time)
    )
    dives = list(result)

    mixtures_by_dive = await get_mixtures_for_dives(db=db, dive_ids=[dive.id for dive in dives])

    points = []
    for dive in dives:
        gas_use = compute_gas_use(duration=dive.duration, avg_depth=dive.avg_depth, mixtures=mixtures_by_dive[dive.id])
        if gas_use is None:
            continue

        points.append(
            DiveGasUsePoint(
                dive_uuid=dive.uuid,
                dive_number=dive.dive_number,
                # Re-attached to the dive's own offset, exactly as `_to_public_dive` does
                # it - a point on this graph has to be the same instant, labeled the same
                # way, as the dive page it links to.
                start_time=combine_start_time(dive.start_time, dive.utc_offset_minutes),
                # Non-null by construction: `compute_gas_use` returned a figure, which it
                # only does for a usable average depth.
                avg_depth=dive.avg_depth,
                gas_use=gas_use,
            )
        )

    return points
