"""Gas-consumption arithmetic: how much gas a dive used, normalized to the surface.

Everything above `gas_use_history` is deliberately pure: no session, no ORM, no clock of
its own, so the rules can be tested exhaustively without a database (see
`tests/test_dive_gas.py`). Same split as `services/gear_service.py`.

Three derivations, and `resolve_gas_use` is the one entry point that picks between them.
`compute_gas_use` handles a single cylinder off the dive's own duration and average depth.
`compute_multi_tank_gas_use` handles several, off `dive_profile.gas_attribution` - which
gas was breathed for how long and how deep, read out of the dive computer's own gas
switches. The second is what the first's multi-cylinder refusal was always waiting for:
"there's nothing to say which of them were breathed at what depth" stopped being true once
the profile extractor started recording it. `compute_parallel_gas_use` is the third and the
narrowest: when every cylinder is flagged `parallel` - a sidemount pair or independent
doubles - the diver has answered that question themselves, and litres breathed alternately
at one depth simply add up, with no attribution needed. It is a fallback, tried only where
the second declines.

Unlike `gear_service`'s `service_status`, this has *no* twin in the web app, and shouldn't
grow one. Service status depends on today's date, which is why the browser has to derive
it; a dive's gas use is derived entirely from columns already on the dive, its mixtures
and its profile, so it can never go stale and is safe to compute once here and cache
alongside the dive (see `_cached_read_dive` in `api/v1/dives.py`). The browser renders
what it's given, and only owns the phrasing of why a figure is *absent* (`lib/dive-gas.ts`).
"""

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.datetime_offset import combine_start_time
from ..crud.crud_dive_mixtures import get_mixtures_for_dives
from ..models.dive import Dive
from ..schemas.dive import DiveGasUse, DiveGasUsePoint, DiveTankGasUse
from ..schemas.dive_mixture import DiveMixtureRead, TankUsage
from ..schemas.dive_profile import DEPTH_SCALE, GasAttribution
from .dive_profiles import ProfileGasAttribution, get_gas_attribution_for_dives

# Metres of water per bar of ambient pressure. Salt water is nearer 10.06 m/bar and
# fresh water 10.33, but every dive log in circulation uses the round number and this one
# does too. The resulting error is under 3% - comfortably inside the error of a
# hand-entered (or computer-averaged) average depth, and it applies equally to every dive,
# so the trend the dashboard graphs is unaffected.
#
# `dive.water_type` exists now, and this deliberately ignores it. Reading it would put a
# 3% step between two dives of the same diver on the strength of a field that is null on
# most rows, which is a worse artefact than the flat 3% it removes - the graph would show
# a change in the diver rather than a change in what they wrote down. If it ever becomes
# water-type-aware, the null rows are the whole problem to solve first. See DECISIONS.md.
#
# Two further simplifications are baked in for the same reason: surface pressure is
# assumed to be 1 bar (wrong at an altitude lake, and `dive.altitude` does not change that
# either) and air is treated as an ideal gas (optimistic by roughly 5% at a 230 bar fill).
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

    - **Not exactly one mixture.** A dive with several cylinders records no clue *here* as
      to how they were breathed - a stage bottle emptied over five minutes at 6 m and a
      back gas breathed for forty at 30 m would both be divided by the whole dive's
      average depth, badly misattributing the shallow gas. Manifolded twins are the
      common case and already work here, because they're logged as one mixture with the
      pair's combined water capacity (see `VOLUME_OPTIONS` in the web app) and a single
      shared pressure, which is exactly right. Genuinely staged cylinders need per-mixture
      time-on-gas and depth-on-gas, which is what `compute_multi_tank_gas_use` takes and
      this function is deliberately not given; a sidemount pair needs neither, and is
      `compute_parallel_gas_use`'s. All three derivations stay separable, so a dive with
      one cylinder is computed the same way it was before either of the others existed.
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
        # One cylinder has nothing to break down into, and the figures cover the whole
        # dive by construction - there is no stretch of it breathed off something else,
        # so there is no fraction to report either.
        tanks=[],
        attributed_seconds=None,
        duration=None,
    )


@dataclass(frozen=True, slots=True)
class _TankArithmetic:
    """One cylinder's figures before rounding.

    Kept unrounded because the dive's totals are sums of these: rounding each tank first
    and adding the results would fold every tank's rounding error into the total, which is
    the same mistake `compute_gas_use` avoids by deriving its rates before rounding.
    """

    gas_number: int
    seconds: int
    mean_depth: float
    pressure_used: float
    gas_used: float
    surface_minutes: float


def _pressure_used(mixture: DiveMixtureRead) -> float | None:
    """What this cylinder's own pressures say came out of it, or `None` if they say nothing.

    Deliberately narrow: pressures only. It is used twice, and the second use is what makes
    the narrowness matter - a cylinder that was demonstrably breathed and that the profile
    never attributed any time to is evidence the attribution is incomplete, not a cylinder
    to pass over. Volume does not belong in that judgement. A cylinder that went 200 -> 100
    bar *was* breathed whatever its recorded capacity says; a capacity that can't be turned
    into litres is a reason it produces no figure, checked where the multiplication happens
    (`_tank_arithmetic`), not a reason to decide it was never breathed and quietly report
    the rest of the dive as fully accounted for.

    Equal pressures are the unused pony bottle `compute_gas_use` documents - not breathed,
    and evidence of nothing.
    """
    if mixture.start_pressure is None or mixture.end_pressure is None:
        return None
    pressure_used = mixture.start_pressure - mixture.end_pressure
    return pressure_used if pressure_used > 0 else None


def _tank_arithmetic(mixture: DiveMixtureRead, attributed: GasAttribution) -> _TankArithmetic | None:
    """One cylinder's consumption, or `None` when this cylinder can't produce one.

    Every condition `compute_gas_use` applies to a whole dive, applied to one tank - the
    two must agree, or a dive would gain or lose a figure by growing a second cylinder.
    The difference is only where the two inputs come from: the time and the depth are this
    cylinder's own (from the profile), not the dive's.
    """
    pressure_used = _pressure_used(mixture)
    if pressure_used is None:
        return None
    # `ck_dive_mixture_volume_positive` makes this unreachable from a stored row; it is
    # here for the same reason `compute_gas_use` re-checks it, and because this is the
    # line that would otherwise multiply by it.
    if mixture.volume <= 0:
        return None
    if attributed.seconds <= 0 or attributed.mean_depth_cm <= 0:
        return None

    mean_depth = attributed.mean_depth_cm / DEPTH_SCALE
    gas_used = pressure_used * mixture.volume
    return _TankArithmetic(
        gas_number=attributed.gas_number,
        seconds=attributed.seconds,
        mean_depth=mean_depth,
        pressure_used=pressure_used,
        gas_used=gas_used,
        surface_minutes=(1 + mean_depth / METERS_PER_BAR) * (attributed.seconds / 60),
    )


# A per-tank RMV above this is a segmentation artefact, not a diver. A whole cylinder's
# pressure drop divided by a stretch of seconds is arithmetically valid and physiologically
# impossible: what it really means is that the device recorded the switch long after the
# cylinder was actually breathed, so the time it was breathed for is sitting inside another
# tank's stretch - the same fault the unattributed-but-breathed branch refuses, arriving
# through the one door that branch cannot watch.
#
# 100 L/min is deliberately far above any figure a dive produces rather than near one. A
# working diver peaks around 40 and a frightened one might touch 60-80 over a short stretch;
# maximal human ventilation is higher still but not for the length of time a gas is breathed
# for. The artefacts this catches are not near the line - the case that prompted it computes
# 2 062 L/min - so the threshold is set where a real if extreme dive cannot reach it, because
# a false positive costs a dive every figure it had, including the honest ones.
#
# Deliberately not applied to `compute_gas_use`: a single cylinder is divided by the dive's
# own duration, so there is no segmentation to go wrong, and adding a ceiling there would
# change a long-standing figure - which is exactly what the split between the derivations
# exists to prevent.
#
# **And not to `compute_parallel_gas_use` either**, for the same reason rather than a new
# one: a flagged pair is divided by the whole dive's duration and the whole dive's average
# depth, so it has no per-tank stretches to have segmented wrongly. The ceiling would also
# break the equivalence that path is built on - the same physical pair logged as one
# manifolded row would return a figure where two honest flagged rows returned `None`.
#
# **And deliberately one-sided.** The mirror fault exists - a switch recorded *early* gives
# a cylinder more time than it was breathed, and its rate comes out too low - but there is
# no floor to catch it with, because the low side has no wall the high side has. Nothing
# resembles 2 062 L/min except an artefact; 2 L/min is what a stage bottle that was cracked
# open for a couple of breaths and logged with a 10 bar drop honestly computes to, and a
# floor set anywhere near real dives would refuse them. So this catches the direction that
# can be caught, and the other is left to the coverage fraction and the diver's own eyes.
MAX_PLAUSIBLE_RMV = 100.0


def compute_multi_tank_gas_use(
    *, mixtures: Sequence[DiveMixtureRead], attribution: ProfileGasAttribution
) -> DiveGasUse | None:
    """Consumption for a dive with several cylinders, or `None` if it can't be known.

    This is what `compute_gas_use`'s multi-cylinder refusal was waiting for. That refusal
    stands on there being "nothing to say which cylinders were breathed at what depth";
    `dive_profile.gas_attribution` is now that something, derived from the gas switches the
    dive computer recorded (see `services/dive_profiles.py::derive_gas_attribution`), and
    each tank is normalized against *its own* time and mean depth instead of the dive's.

    Takes no `duration` or `avg_depth` for exactly that reason - neither enters the
    arithmetic. What it takes instead is the join, on `gas_number`:

    - **A duplicate `gas_number` among the mixtures refuses the whole dive.** The number is
      a label a device chose, not an index this code assigned (see DECISIONS.md), so two
      cylinders claiming one label make every join ambiguous, not just theirs - and picking
      the first would silently attribute a back gas's time to a deco bottle.
    - **A cylinder the attribution doesn't mention is left out** when its own pressures say
      it was never breathed, which is the commonest case in the corpus by far: a diver
      carries one transmitter, so the deco bottle has no pressures and could produce no
      figure anyway. **But one that was demonstrably breathed refuses the whole dive**, for
      the reason spelled out at that branch: the time it was breathed for is inside another
      tank's stretch, so the surviving figures are wrong rather than incomplete, and no
      coverage fraction can say so.
    - **A tank that fails `_tank_arithmetic` is left out** on the same terms. Unlike the
      case above, the attribution knew about it, so its seconds are excluded from every
      other tank's and the shortfall is real and reported.
    - **A tank whose figures come out physiologically impossible refuses the dive**, which
      is the same fault as the second case arriving by a different route - a switch
      recorded late rather than not at all. See `MAX_PLAUSIBLE_RMV`.

    The dive-level figures are then the totals over the tanks that survived, and
    `attributed_seconds` against `duration` is what makes that honest: the time
    those tanks cover, over the span the profile recorded, so a caller can see how much of
    the dive is missing rather than reading the totals as the whole story.

    `sac_bar_per_min` is **null** here rather than summed. Bar/min is a rate only against a
    known cylinder volume - 10 bar out of an 11 L stage and 10 bar out of a 22 L twinset
    are different amounts of gas - so there is no dive-wide figure to give, only the
    per-tank ones, each of which has exactly one volume behind it. Litres and RMV do sum,
    because both are already volumes at the surface.
    """
    if len(mixtures) < 2 or not attribution.entries:
        return None

    numbered = [mixture.gas_number for mixture in mixtures if mixture.gas_number is not None]
    if len(set(numbered)) != len(numbered):
        return None

    attributed_by_number = {attributed.gas_number: attributed for attributed in attribution.entries}
    if len(attributed_by_number) != len(attribution.entries):
        return None

    tanks = []
    for mixture in mixtures:
        attributed = attributed_by_number.get(mixture.gas_number) if mixture.gas_number is not None else None
        if attributed is None:
            # A cylinder the attribution never mentions, whose own pressures say gas came
            # out of it, refuses the dive. Its litres are missing from the totals *and*
            # the time it was breathed for is sitting inside some other tank's stretch,
            # inflating that tank's seconds and understating its rate - so the figures
            # that survive are wrong, not merely partial, and the coverage fraction cannot
            # show it: both halves would agree and read as the whole dive. This is the
            # sidemount pair the device sees as one gas, and the deco bottle whose switch
            # the diver never confirmed on the computer. A cylinder with *no* pressure
            # drop is passed over instead, because there is nothing to have misplaced.
            if _pressure_used(mixture) is not None:
                return None
            continue
        tank = _tank_arithmetic(mixture, attributed)
        if tank is None:
            continue
        if tank.gas_used / tank.surface_minutes > MAX_PLAUSIBLE_RMV:
            # See `MAX_PLAUSIBLE_RMV`. Refuses the dive rather than dropping the tank,
            # because the time this cylinder was really breathed for is inside another
            # tank's stretch: the figures that would survive are wrong too, and dropping
            # this one would leave a coverage fraction reading as very nearly the whole
            # dive - the shortfall being precisely the few seconds that caused the fault.
            return None
        tanks.append(tank)

    if not tanks:
        return None

    gas_used = sum(tank.gas_used for tank in tanks)
    surface_minutes = sum(tank.surface_minutes for tank in tanks)

    return DiveGasUse(
        gas_used=round(gas_used, 2),
        rmv=round(gas_used / surface_minutes, 2),
        sac_bar_per_min=None,
        tanks=[
            DiveTankGasUse(
                gas_number=tank.gas_number,
                gas_used=round(tank.gas_used, 2),
                rmv=round(tank.gas_used / tank.surface_minutes, 2),
                sac_bar_per_min=round(tank.pressure_used / tank.surface_minutes, 2),
                seconds_on_gas=tank.seconds,
                mean_depth=round(tank.mean_depth, 2),
            )
            for tank in tanks
        ],
        attributed_seconds=sum(tank.seconds for tank in tanks),
        duration=attribution.duration,
    )


def compute_parallel_gas_use(
    *,
    duration: int,
    avg_depth: float | None,
    mixtures: Sequence[DiveMixtureRead],
) -> DiveGasUse | None:
    """Consumption for a dive breathed off a flagged parallel set, or `None`.

    A sidemount pair or independent doubles is the one multi-cylinder shape whose litres
    are simply additive: the cylinders are breathed alternately at the *same* depth over
    the *same* dive, so the misattribution `compute_gas_use`'s multi-cylinder refusal
    exists to prevent cannot happen here. `2 x 11.1 L x 92.5 bar` and `11.1 L x 185 bar`
    describe the same 2 054 L, which is why the two honest rows and the one dishonest row
    a diver might have written instead agree to the litre.

    What makes that safe is the diver's own answer, not a guess: every mixture has to
    carry `usage == TankUsage.PARALLEL`. The conditions, all of which must hold:

    - **At least two cylinders, every one of them flagged `parallel`.** A mixed set - a
      pair plus an unflagged bottle, or plus one explicitly `staged` - returns `None`. A
      partial sum is worse than nothing: the bottle's litres would be missing from the
      numerator while the whole dive stayed in the denominator, quietly reporting an RMV
      that is too *low*. Same honesty rule the multi-tank path applies, and the one
      Subsurface applies to its own airuse sum.
    - **An average depth and a duration**, both of them the dive's own. This path has no
      profile behind it and wants none - it is the rescue for dives that have no usable
      attribution at all.
    - **Both pressures on every cylinder.** One missing pressure anywhere refuses the
      whole dive, for the reason above: the cylinder was carried and probably breathed,
      and there is no way to leave it out honestly.
    - **A total drop above zero.** Per-row drops are summed rather than each being
      required positive, because a zero-drop row is the unused pony bottle `_pressure_used`
      documents - it contributes zero litres, and the denominator is still right, since the
      diver breathed the other cylinder for the whole dive. Only a set that says *nothing*
      came out of any cylinder has no figure to give. Individual drops cannot come out
      negative on a stored row (`ck_dive_mixture_pressure_order`).

    `sac_bar_per_min` is pooled - the mean drop across the cylinders per surface-minute -
    **only when every volume is exactly equal**, and `None` otherwise. That is the
    constraint Shearwater imposes for its own pooled sidemount SAC and Garmin imposes at
    pairing time, and it is what makes the figure honest: bar/min is a rate against a known
    volume, so pooling drops across a 11.1 L and a 12 L would be averaging two different
    amounts of gas. Equal volumes make it exactly the figure the same pair logged as one
    manifolded row would report, since `sum(V * d_i) = nV * (sum(d_i) / n)`. Equality is
    tested exactly rather than within a tolerance: volumes come from the app's own presets
    or from one diver's hand, and a tolerance would invent a convention no agency defines.

    `tanks` is empty. Per-tank rates need time-on-gas, which is exactly what a dive with no
    attribution does not have, and per-tank *litres* alone would be a half-populated
    `DiveTankGasUse` - the whole-object-or-nothing rule `DiveGasUse` is built on.

    `MAX_PLAUSIBLE_RMV` is deliberately not applied; see the comment on it.
    """
    if len(mixtures) < 2:
        return None
    if any(mixture.usage is not TankUsage.PARALLEL for mixture in mixtures):
        return None
    if avg_depth is None or avg_depth <= 0 or duration <= 0:
        return None
    if any(mixture.volume <= 0 for mixture in mixtures):
        return None
    if any(mixture.start_pressure is None or mixture.end_pressure is None for mixture in mixtures):
        return None

    # Narrowed by the guard above; re-derived rather than carried so the types stay honest.
    drops = [
        mixture.start_pressure - mixture.end_pressure
        for mixture in mixtures
        if mixture.start_pressure is not None and mixture.end_pressure is not None
    ]
    if sum(drops) <= 0:
        return None

    gas_used = sum(drop * mixture.volume for drop, mixture in zip(drops, mixtures, strict=True))
    ambient_pressure = 1 + avg_depth / METERS_PER_BAR
    surface_minutes = ambient_pressure * (duration / 60)

    volumes = {mixture.volume for mixture in mixtures}
    pooled_sac = (sum(drops) / len(drops)) / surface_minutes if len(volumes) == 1 else None

    return DiveGasUse(
        gas_used=round(gas_used, 2),
        rmv=round(gas_used / surface_minutes, 2),
        sac_bar_per_min=None if pooled_sac is None else round(pooled_sac, 2),
        tanks=[],
        attributed_seconds=None,
        duration=None,
    )


def resolve_gas_use(
    *,
    duration: int,
    avg_depth: float | None,
    mixtures: Sequence[DiveMixtureRead],
    attribution: ProfileGasAttribution | None = None,
) -> DiveGasUse | None:
    """Whichever of the three derivations this dive supports.

    Cylinder count picks the first candidate - one is `compute_gas_use`'s, exactly as
    before Phase 4, and several are `compute_multi_tank_gas_use`'s - and count alone is no
    longer the whole rule, as it was until the parallel flag existed. Where the multi-tank
    path declines *and* every mixture is flagged `parallel`, `compute_parallel_gas_use`
    gets a turn.

    **Attribution wins over the flag**, which is why the fallback is second rather than
    gated ahead of it. A dive whose profile attributed time and depth per cylinder yields
    a strictly richer answer - per-tank litres, rates, seconds and mean depths - and a
    diver flagging a pair that the device *did* record switches for must not lose it. What
    the fallback rescues is the set that returns `None` today: no profile, no switches, or
    the refusal a cylinder with real pressures and no attribution entry triggers - the
    sidemount pair the computer saw as one gas.

    No existing path changed what it returns. `compute_gas_use` and
    `compute_multi_tank_gas_use` are untouched, and an unflagged multi-cylinder dive still
    says nothing. In particular a single-cylinder dive with a profile is *not* re-derived
    from the profile's mean depth - the dive's own `avg_depth` is the diver's record and
    may have been edited, and swapping which number a long-standing figure comes from is
    not a change to make in passing.

    Every caller that turns a dive into a response goes through here rather than choosing,
    so the rule lives in one place.
    """
    if len(mixtures) == 1:
        return compute_gas_use(duration=duration, avg_depth=avg_depth, mixtures=mixtures)
    attributed = compute_multi_tank_gas_use(mixtures=mixtures, attribution=attribution or ProfileGasAttribution())
    if attributed is not None:
        return attributed
    return compute_parallel_gas_use(duration=duration, avg_depth=avg_depth, mixtures=mixtures)


async def gas_use_history(db: AsyncSession, user_id: int) -> list[DiveGasUsePoint]:
    """Every dive of a user's that yields a gas-use figure, oldest first.

    Deliberately *not* a paginated list endpoint's job: the point of the series is the
    trend across a diving career, so a page of ten would be meaningless, and `GET /dives`
    doesn't carry mixtures anyway.

    Three queries - the dives, then their mixtures batched, then the gas attribution of the
    multi-cylinder subset those mixtures identify - and `resolve_gas_use` decides per dive.
    The obvious optimization is to push its conditions into SQL (a `HAVING
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

    dive_ids = [dive.id for dive in dives]
    mixtures_by_dive = await get_mixtures_for_dives(db=db, dive_ids=dive_ids)
    # Only a dive with several cylinders can use an attribution, and the mixtures above
    # already say which those are - so the JSONB column is fetched for those alone rather
    # than for a recreational diver's whole log. `>= 2` rather than the dispatcher's
    # `!= 1`: the two select the same dives among those that can produce a figure, but a
    # dive logged with no cylinders at all is common in a long log and can no more use an
    # attribution than a single-cylinder one - `compute_multi_tank_gas_use` discards it on
    # its first line either way.
    attribution_by_dive = await get_gas_attribution_for_dives(
        db=db, dive_ids=[dive_id for dive_id in dive_ids if len(mixtures_by_dive[dive_id]) >= 2]
    )

    points = []
    for dive in dives:
        if dive.avg_depth is None:
            # Not a condition of the *arithmetic* - the multi-tank path normalizes each
            # cylinder against its own mean depth and never consults this - but of the
            # point: `DiveGasUsePoint.avg_depth` is what the tooltip reads, and inventing
            # one from the attributed depths would put a number on the chart that the dive
            # does not claim. Tested first rather than after the figure is computed, so
            # that this reads as "this dive cannot be plotted" rather than as a figure
            # derived and thrown away. Unreachable for an imported dive: every export in
            # the corpus records an average depth.
            continue

        gas_use = resolve_gas_use(
            duration=dive.duration,
            avg_depth=dive.avg_depth,
            mixtures=mixtures_by_dive[dive.id],
            attribution=attribution_by_dive.get(dive.id),
        )
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
                # Non-null because the guard at the top of the loop skipped every dive
                # without one. Not, as it once was, because the arithmetic demanded it:
                # `compute_multi_tank_gas_use` can return a figure for a dive whose
                # `avg_depth` is null, so removing that guard as redundant would put a
                # `None` into a required `float` here.
                avg_depth=dive.avg_depth,
                gas_use=gas_use,
            )
        )

    return points
