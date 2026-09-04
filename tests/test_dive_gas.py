"""Unit tests for gas-consumption arithmetic (`services/dive_gas.py`).

Same convention as `test_gear_service.py`: all three derivations are pure, so the whole
truth table - the worked numbers and every reason a dive can't produce one - is
covered here without a database. Endpoint behaviour on top of a live Postgres/Redis
is exercised by hand (see DECISIONS.md), not here.
"""

from typing import Any

from src.app.schemas.dive_mixture import DiveMixtureRead, TankUsage
from src.app.schemas.dive_profile import GasAttribution
from src.app.services.dive_gas import (
    MAX_PLAUSIBLE_RMV,
    METERS_PER_BAR,
    compute_gas_use,
    compute_multi_tank_gas_use,
    compute_parallel_gas_use,
    resolve_gas_use,
)
from src.app.services.dive_profiles import ProfileGasAttribution


def _mixture(
    *,
    volume: float = 12.0,
    start_pressure: float | None = 200.0,
    end_pressure: float | None = 50.0,
    gas_number: int | None = None,
    usage: TankUsage | None = None,
) -> DiveMixtureRead:
    """A single air cylinder. `id`/`oxygen`/`helium` are required by the schema but
    irrelevant to consumption - RMV is a volume rate, so what's *in* the cylinder
    doesn't enter the arithmetic at all (see `test_gas_mix_does_not_affect_the_result`).

    `usage` defaults to `None` - "not recorded", which is what every row says unless a
    diver answered - so the existing cases keep testing the unflagged world they were
    written for.
    """
    return DiveMixtureRead(
        id=1,
        volume=volume,
        start_pressure=start_pressure,
        end_pressure=end_pressure,
        oxygen=21.0,
        helium=0.0,
        gas_number=gas_number,
        usage=usage,
    )


def _parallel(**overrides: Any) -> DiveMixtureRead:
    """One cylinder of a flagged parallel set - the sidemount pair's own shape."""
    return _mixture(usage=TankUsage.PARALLEL, **overrides)


class TestComputeGasUse:
    """The worked numbers. 12 L, 200 -> 50 bar, 18 m average, 45 minutes:
    150 bar x 12 L = 1800 L at the surface, over 2.8 bar ambient x 45 min = 126
    surface-minutes, so 14.29 L/min.
    """

    def test_computes_the_worked_example(self) -> None:
        result = compute_gas_use(duration=45 * 60, avg_depth=18.0, mixtures=[_mixture()])

        assert result is not None
        assert result.gas_used == 1800.0
        assert result.rmv == 14.29
        assert result.sac_bar_per_min == 1.19

    def test_computes_a_second_independent_example(self) -> None:
        # 11.1 L (S80), 210 -> 70 bar over 40 min at 12 m: 1554 L / (2.2 x 40) = 17.66.
        result = compute_gas_use(
            duration=40 * 60,
            avg_depth=12.0,
            mixtures=[_mixture(volume=11.1, start_pressure=210.0, end_pressure=70.0)],
        )

        assert result is not None
        assert result.gas_used == 1554.0
        assert result.rmv == 17.66

    def test_deeper_dive_normalizes_to_a_lower_rate(self) -> None:
        """The whole point of the metric: the same gas, in the same time, at twice the
        ambient pressure is half the surface-equivalent rate. 10 m is 2 bar ambient,
        30 m is 4 bar.
        """
        shallow = compute_gas_use(duration=30 * 60, avg_depth=10.0, mixtures=[_mixture()])
        deep = compute_gas_use(duration=30 * 60, avg_depth=30.0, mixtures=[_mixture()])

        assert shallow is not None and deep is not None
        assert deep.rmv == round(shallow.rmv / 2, 2)

    def test_cylinder_size_cancels_out_of_rmv_but_not_bar_per_min(self) -> None:
        """Two divers breathing the same amount of gas from different cylinders have the
        same RMV and different bar/min - which is exactly why RMV is the comparable one,
        and why both are returned.
        """
        small = compute_gas_use(
            duration=40 * 60, avg_depth=20.0, mixtures=[_mixture(volume=10.0, start_pressure=200.0, end_pressure=80.0)]
        )
        large = compute_gas_use(
            duration=40 * 60, avg_depth=20.0, mixtures=[_mixture(volume=15.0, start_pressure=200.0, end_pressure=120.0)]
        )

        assert small is not None and large is not None
        assert small.gas_used == large.gas_used == 1200.0
        assert small.rmv == large.rmv
        assert small.sac_bar_per_min != large.sac_bar_per_min

    def test_gas_mix_does_not_affect_the_result(self) -> None:
        """RMV is a volume rate, so nitrox and trimix consume identically. If this ever
        starts failing, someone has confused it with gas *density* or narcotic depth.
        """
        air = compute_gas_use(duration=45 * 60, avg_depth=18.0, mixtures=[_mixture()])

        trimix = _mixture()
        trimix.oxygen = 21.0
        trimix.helium = 35.0
        mixed = compute_gas_use(duration=45 * 60, avg_depth=18.0, mixtures=[trimix])

        assert air is not None and mixed is not None
        assert air.rmv == mixed.rmv

    def test_rounds_to_two_decimal_places(self) -> None:
        """Matches the precision the rest of the dive API already exposes (see the web
        app's DECISIONS.md on 2-decimal mixture values).
        """
        result = compute_gas_use(duration=37 * 60, avg_depth=13.7, mixtures=[_mixture()])

        assert result is not None
        # Non-null on this path by construction - only the multi-cylinder one has no
        # cylinder volume to express a pressure rate against.
        assert result.sac_bar_per_min is not None
        assert result.rmv == round(result.rmv, 2)
        assert result.sac_bar_per_min == round(result.sac_bar_per_min, 2)

    def test_rates_are_derived_before_rounding(self) -> None:
        """`gas_used` is rounded for display, but the rates must not be computed *from*
        the rounded figure - a rounding error in the numerator would propagate.
        """
        result = compute_gas_use(
            duration=33 * 60,
            avg_depth=17.3,
            mixtures=[_mixture(volume=11.1, start_pressure=203.0, end_pressure=61.0)],
        )

        assert result is not None
        expected = (203.0 - 61.0) * 11.1 / ((1 + 17.3 / METERS_PER_BAR) * 33)
        assert result.rmv == round(expected, 2)


class TestComputeGasUseReturnsNone:
    """Every reason a dive can't produce a figure. Each returns `None` rather than a
    partial or best-guess number - see `compute_gas_use`'s docstring.
    """

    def test_when_there_are_no_mixtures(self) -> None:
        assert compute_gas_use(duration=45 * 60, avg_depth=18.0, mixtures=[]) is None

    def test_when_there_is_more_than_one_mixture(self) -> None:
        """Multi-tank is unsupported until a dive records how the cylinders were
        breathed - not because the litres can't be summed, but because there's nothing
        to say which of them were breathed at what depth.
        """
        back_gas = _mixture()
        deco = _mixture(volume=11.1, start_pressure=200.0, end_pressure=140.0)

        assert compute_gas_use(duration=45 * 60, avg_depth=18.0, mixtures=[back_gas, deco]) is None

    def test_when_average_depth_is_missing(self) -> None:
        assert compute_gas_use(duration=45 * 60, avg_depth=None, mixtures=[_mixture()]) is None

    def test_when_average_depth_is_zero_or_negative(self) -> None:
        assert compute_gas_use(duration=45 * 60, avg_depth=0.0, mixtures=[_mixture()]) is None
        assert compute_gas_use(duration=45 * 60, avg_depth=-3.0, mixtures=[_mixture()]) is None

    def test_when_either_pressure_is_missing(self) -> None:
        assert compute_gas_use(duration=45 * 60, avg_depth=18.0, mixtures=[_mixture(start_pressure=None)]) is None
        assert compute_gas_use(duration=45 * 60, avg_depth=18.0, mixtures=[_mixture(end_pressure=None)]) is None
        assert (
            compute_gas_use(
                duration=45 * 60, avg_depth=18.0, mixtures=[_mixture(start_pressure=None, end_pressure=None)]
            )
            is None
        )

    def test_when_the_cylinder_was_not_breathed(self) -> None:
        """Equal pressures mean an unused pony/stage bottle or a typo. A 0 L/min diver
        is not a thing, and reporting one would quietly drag a dashboard average down.
        """
        assert (
            compute_gas_use(
                duration=45 * 60, avg_depth=18.0, mixtures=[_mixture(start_pressure=200.0, end_pressure=200.0)]
            )
            is None
        )

    def test_when_duration_or_volume_is_non_positive(self) -> None:
        """Both are `CHECK`-constrained positive in the database, so this is belt and
        braces - but it's the branch that keeps a zero out of the denominator.
        """
        assert compute_gas_use(duration=0, avg_depth=18.0, mixtures=[_mixture()]) is None
        assert compute_gas_use(duration=-60, avg_depth=18.0, mixtures=[_mixture()]) is None
        assert compute_gas_use(duration=45 * 60, avg_depth=18.0, mixtures=[_mixture(volume=0.0)]) is None


def _attributed(gas_number: int, *, seconds: int, mean_depth_cm: int) -> GasAttribution:
    """One cylinder's stretch of the dive, as the profile extractor derived it from the
    device's own gas switches (see `services/dive_profiles.py::derive_gas_attribution`)."""
    return GasAttribution(gas_number=gas_number, seconds=seconds, mean_depth_cm=mean_depth_cm)


def _attribution(*entries: GasAttribution, duration: int = 6000) -> ProfileGasAttribution:
    """The attribution as it comes off a profile row, span included - the denominator
    `attributed_seconds` is a fraction of."""
    return ProfileGasAttribution(duration=duration, entries=list(entries))


class TestComputeMultiTankGasUse:
    """The two cylinders are deliberately the two dives `TestComputeGasUse` already works
    out on its own - a 12 L at 200 -> 50 bar for 45 min at 18 m (1800 L, RMV 14.29) and an
    11.1 L at 210 -> 70 bar for 40 min at 12 m (1554 L, RMV 17.66). Every number below is
    therefore checkable against a figure that was verified single-tank first, which is the
    property that matters: a cylinder must not be worth a different amount of gas for
    having been logged next to another one.
    """

    def _tanks(self) -> tuple[list[DiveMixtureRead], ProfileGasAttribution]:
        mixtures = [
            _mixture(gas_number=1),
            _mixture(gas_number=2, volume=11.1, start_pressure=210.0, end_pressure=70.0),
        ]
        attribution = _attribution(
            _attributed(1, seconds=45 * 60, mean_depth_cm=1800),
            _attributed(2, seconds=40 * 60, mean_depth_cm=1200),
        )
        return mixtures, attribution

    def test_gives_each_cylinder_its_own_figures(self):
        mixtures, attribution = self._tanks()

        result = compute_multi_tank_gas_use(mixtures=mixtures, attribution=attribution)

        assert result is not None
        assert [(tank.gas_number, tank.gas_used, tank.rmv) for tank in result.tanks] == [
            (1, 1800.0, 14.29),
            (2, 1554.0, 17.66),
        ]
        assert [(tank.seconds_on_gas, tank.mean_depth) for tank in result.tanks] == [(2700, 18.0), (2400, 12.0)]

    def test_a_tank_is_normalized_against_its_own_depth_not_the_dive_s(self):
        """The reason the whole feature exists. The same deco bottle emptied at 6 m and at
        30 m is a wildly different consumption rate, and before attribution both were
        divided by whatever the dive averaged.
        """
        mixtures = [_mixture(gas_number=1), _mixture(gas_number=2)]
        shallow = compute_multi_tank_gas_use(
            mixtures=mixtures,
            attribution=_attribution(
                _attributed(1, seconds=1800, mean_depth_cm=3000),
                _attributed(2, seconds=1800, mean_depth_cm=1000),
            ),
        )

        assert shallow is not None
        # 4 bar ambient against 2 bar: the same litres over the same time is half the rate.
        assert shallow.tanks[0].rmv == round(shallow.tanks[1].rmv / 2, 2)

    def test_the_dive_figures_are_the_totals_over_the_tanks(self):
        mixtures, attribution = self._tanks()

        result = compute_multi_tank_gas_use(mixtures=mixtures, attribution=attribution)

        assert result.gas_used == 1800.0 + 1554.0
        # 126 surface-minutes on the first cylinder and 88 on the second.
        assert result.rmv == round(3354 / 214, 2)
        # Litres and RMV sum; bar/min does not, because the two cylinders are different
        # sizes and a rate against no particular volume is a rate of nothing.
        assert result.sac_bar_per_min is None
        # Each tank's own: 150 bar over 126 surface-minutes, and 140 over 88.
        assert [tank.sac_bar_per_min for tank in result.tanks] == [1.19, 1.59]
        assert result.attributed_seconds == 2700 + 2400

    def test_a_cylinder_that_recorded_no_pressures_is_left_out_and_the_shortfall_shows(self):
        """The commonest tech shape in the corpus by a distance: one transmitter on the
        back gas, a staged deco bottle with nothing logged. The back gas still yields a
        figure, and `attributed_seconds` is what says the dive was longer than the figure
        covers.
        """
        mixtures = [
            _mixture(gas_number=1),
            _mixture(gas_number=2, volume=11.0, start_pressure=None, end_pressure=None),
        ]
        attribution = _attribution(
            _attributed(1, seconds=2355, mean_depth_cm=2837),
            _attributed(2, seconds=2327, mean_depth_cm=655),
            duration=4682,
        )

        result = compute_multi_tank_gas_use(mixtures=mixtures, attribution=attribution)

        assert result is not None
        assert [tank.gas_number for tank in result.tanks] == [1]
        assert result.gas_used == result.tanks[0].gas_used
        # Half the dive, and both halves of the fraction come off the same profile row -
        # the client renders "these figures cover 39 of the 78 minutes recorded" from it.
        assert (result.attributed_seconds, result.duration) == (2355, 4682)

    def test_an_unmentioned_cylinder_that_was_never_breathed_is_left_out(self):
        """A hand-added cylinder has no `gas_number` to join on, and one the device never
        recorded a switch to was never attributed any time. Neither says anything is wrong
        as long as the cylinder's own pressures agree it was not breathed - an unused pony
        bottle, or a stage with nothing logged."""
        mixtures = [
            _mixture(gas_number=1),
            _mixture(gas_number=7, start_pressure=200.0, end_pressure=200.0),
            _mixture(gas_number=None, start_pressure=None, end_pressure=None),
        ]

        result = compute_multi_tank_gas_use(
            mixtures=mixtures, attribution=_attribution(_attributed(1, seconds=2700, mean_depth_cm=1800))
        )

        assert [tank.gas_number for tank in result.tanks] == [1]

    def test_the_tanks_are_ordered_as_the_mixtures_are(self):
        """Cylinder order on the dive is the diver's, and the client joins each tank back
        to the mixture row it sits beside."""
        mixtures = [_mixture(gas_number=2), _mixture(gas_number=1)]
        attribution = _attribution(
            _attributed(1, seconds=2700, mean_depth_cm=1800),
            _attributed(2, seconds=2400, mean_depth_cm=1200),
        )

        result = compute_multi_tank_gas_use(mixtures=mixtures, attribution=attribution)

        assert [tank.gas_number for tank in result.tanks] == [2, 1]


class TestComputeMultiTankGasUseReturnsNone:
    def test_when_a_cylinder_was_breathed_and_the_attribution_never_mentions_it(self):
        """The one case where a missing cylinder makes the *surviving* figures wrong rather
        than merely partial, so it cannot be reported as partial either.

        The deco bottle here has a real 100 bar drop and no attribution entry, which means
        the file's switches never accounted for the time it was breathed - so that time is
        sitting inside gas 1's stretch, inflating its seconds and understating its rate.
        Both halves of the coverage fraction would agree and read as the whole dive, which
        is precisely the claim `attributed_seconds` exists to stop anyone making.
        """
        mixtures = [
            _mixture(gas_number=1, volume=22.0, start_pressure=220.0, end_pressure=60.0),
            _mixture(gas_number=2, volume=11.0, start_pressure=200.0, end_pressure=100.0),
        ]

        result = compute_multi_tank_gas_use(
            mixtures=mixtures,
            attribution=_attribution(_attributed(1, seconds=4300, mean_depth_cm=1779), duration=4300),
        )

        assert result is None

    def test_when_a_cylinder_s_whole_drop_lands_in_a_stretch_too_short_to_breathe_it(self):
        """The same fault as the case above, arriving by the other door: the switch was
        recorded, but late. The deco bottle here is credited with 50 bar out of 11 L over
        ten seconds - 2 062 L/min, which is not a diver - and the time it was really
        breathed for is inside gas 1's stretch, dragging that tank's rate down too. Both
        halves of the coverage fraction would agree and read as the whole dive.
        """
        mixtures = [
            _mixture(gas_number=1, volume=22.0, start_pressure=220.0, end_pressure=90.0),
            _mixture(gas_number=2, volume=11.0, start_pressure=200.0, end_pressure=150.0),
        ]

        result = compute_multi_tank_gas_use(
            mixtures=mixtures,
            attribution=_attribution(
                _attributed(1, seconds=2690, mean_depth_cm=2750),
                _attributed(2, seconds=10, mean_depth_cm=600),
                duration=2700,
            ),
        )

        assert result is None

    def test_but_not_when_a_hard_working_diver_merely_breathes_fast(self):
        """The ceiling is set where a real dive cannot reach it, because a false positive
        costs the dive every figure it had. 40 L/min on the deco bottle is a diver working
        hard, and it must still produce numbers.
        """
        mixtures = [
            _mixture(gas_number=1, volume=22.0, start_pressure=220.0, end_pressure=90.0),
            _mixture(gas_number=2, volume=11.0, start_pressure=200.0, end_pressure=150.0),
        ]

        result = compute_multi_tank_gas_use(
            mixtures=mixtures,
            attribution=_attribution(
                _attributed(1, seconds=2100, mean_depth_cm=2750),
                _attributed(2, seconds=600, mean_depth_cm=600),
                duration=2700,
            ),
        )

        assert result is not None
        assert result.tanks[1].rmv == 34.38
        assert max(tank.rmv for tank in result.tanks) < MAX_PLAUSIBLE_RMV

    def test_when_the_dive_has_fewer_than_two_cylinders(self):
        """One cylinder is `compute_gas_use`'s, and the split is what keeps a long-standing
        figure from changing which number it comes from."""
        assert (
            compute_multi_tank_gas_use(
                mixtures=[_mixture(gas_number=1)],
                attribution=_attribution(_attributed(1, seconds=2700, mean_depth_cm=1800)),
            )
            is None
        )

    def test_when_nothing_records_which_gas_was_breathed_when(self):
        """A FIT export with two gases and no `dive_gas_switched` event is exactly this,
        and it is where the feature has to keep saying nothing."""
        assert (
            compute_multi_tank_gas_use(
                mixtures=[_mixture(gas_number=1), _mixture(gas_number=2)], attribution=_attribution()
            )
            is None
        )

    def test_when_two_cylinders_claim_the_same_gas_number(self):
        """The number is a label a device chose, not an index this code assigned, so a
        duplicate makes every join ambiguous rather than only its own - and picking the
        first would attribute a back gas's time to a deco bottle in silence.
        """
        assert (
            compute_multi_tank_gas_use(
                mixtures=[_mixture(gas_number=1), _mixture(gas_number=1)],
                attribution=_attribution(_attributed(1, seconds=2700, mean_depth_cm=1800)),
            )
            is None
        )

    def test_when_no_cylinder_can_produce_a_figure(self):
        mixtures = [
            _mixture(gas_number=1, start_pressure=None),
            _mixture(gas_number=2, start_pressure=200.0, end_pressure=200.0),
        ]
        attribution = _attribution(
            _attributed(1, seconds=2700, mean_depth_cm=1800),
            _attributed(2, seconds=2400, mean_depth_cm=1200),
        )

        assert compute_multi_tank_gas_use(mixtures=mixtures, attribution=attribution) is None

    def test_when_a_cylinder_has_no_time_or_no_depth(self):
        """The same guards `compute_gas_use` applies to a dive, applied per tank: a zero in
        either would be a division by zero or a rate normalized against the surface.
        """
        mixtures = [_mixture(gas_number=1), _mixture(gas_number=2)]

        assert (
            compute_multi_tank_gas_use(
                mixtures=mixtures,
                attribution=_attribution(
                    _attributed(1, seconds=0, mean_depth_cm=1800),
                    _attributed(2, seconds=2400, mean_depth_cm=0),
                ),
            )
            is None
        )


class TestComputeParallelGasUse:
    """Dive 276's arithmetic, which is where this branch came from: a sidemount pair of
    two 11.1 L cylinders, 22.6 m average over 59 minutes, first written down as one
    cylinder with its pressures summed (`415 -> 230`) and later re-encoded as the two
    honest rows it always was. `11.1 x (415-230) = 2 x 11.1 x 92.5 = 2053.5 L`, so the
    dishonest encoding and this branch agree to the litre - which is the whole claim.

    3.26 bar ambient x 59 min = 192.34 surface-minutes throughout.
    """

    def test_computes_dive_276(self) -> None:
        result = compute_parallel_gas_use(
            duration=59 * 60,
            avg_depth=22.6,
            mixtures=[
                _parallel(volume=11.1, start_pressure=200.0, end_pressure=110.0),
                _parallel(volume=11.1, start_pressure=210.0, end_pressure=115.0),
            ],
        )

        assert result is not None
        assert result.gas_used == 2053.5
        assert result.rmv == 10.68
        assert result.sac_bar_per_min == 0.48

    def test_reports_no_per_tank_breakdown(self) -> None:
        """Per-tank *rates* need time-on-gas, which is exactly what a dive with no
        attribution hasn't got, and per-tank litres alone would be a half-populated
        `DiveTankGasUse`. The empty list is the sentinel a client already reads.
        """
        result = compute_parallel_gas_use(
            duration=59 * 60, avg_depth=22.6, mixtures=[_parallel(volume=11.1), _parallel(volume=11.1)]
        )

        assert result is not None
        assert result.tanks == []
        assert result.attributed_seconds is None
        assert result.duration is None

    def test_the_manifolded_encoding_of_the_same_dive_agrees(self) -> None:
        """The identity the pooled-SAC definition exists for. One 22.2 L row at the pair's
        mean drop is how a diver would (wrongly, but understandably) log the same physical
        dive, and every figure has to match - otherwise flagging the honest encoding would
        cost the diver a number they already had.
        """
        pair = compute_parallel_gas_use(
            duration=59 * 60,
            avg_depth=22.6,
            mixtures=[
                _parallel(volume=11.1, start_pressure=200.0, end_pressure=110.0),
                _parallel(volume=11.1, start_pressure=210.0, end_pressure=115.0),
            ],
        )
        manifolded = compute_gas_use(
            duration=59 * 60,
            avg_depth=22.6,
            mixtures=[_mixture(volume=22.2, start_pressure=200.0, end_pressure=107.5)],
        )

        assert pair is not None and manifolded is not None
        assert (pair.gas_used, pair.rmv, pair.sac_bar_per_min) == (
            manifolded.gas_used,
            manifolded.rmv,
            manifolded.sac_bar_per_min,
        )

    def test_unequal_volumes_keep_the_litres_and_drop_the_pooled_sac(self) -> None:
        """Litres sum without restriction; a pressure-domain rate does not. Averaging a
        drop out of an 11.1 L against one out of a 12 L would be averaging two different
        amounts of gas - the equal-size constraint Shearwater and Garmin both impose.
        """
        result = compute_parallel_gas_use(
            duration=59 * 60,
            avg_depth=22.6,
            mixtures=[
                _parallel(volume=11.1, start_pressure=200.0, end_pressure=110.0),
                _parallel(volume=12.0, start_pressure=210.0, end_pressure=115.0),
            ],
        )

        assert result is not None
        assert result.gas_used == 2139.0
        assert result.rmv == 11.12
        assert result.sac_bar_per_min is None

    def test_a_zero_drop_cylinder_contributes_nothing_and_refuses_nothing(self) -> None:
        """A carried-but-untouched cylinder of the pair adds zero litres, and the
        denominator is still the whole dive - the diver breathed the other one throughout.
        The pooled SAC is the *mean* drop, so the untouched cylinder halves it, which is
        exactly what a manifolded pair sharing one gauge would have shown.
        """
        result = compute_parallel_gas_use(
            duration=59 * 60,
            avg_depth=22.6,
            mixtures=[
                _parallel(volume=11.1, start_pressure=200.0, end_pressure=15.0),
                _parallel(volume=11.1, start_pressure=200.0, end_pressure=200.0),
            ],
        )

        assert result is not None
        assert result.gas_used == 2053.5
        assert result.sac_bar_per_min == 0.48

    def test_no_plausibility_ceiling_is_applied(self) -> None:
        """`MAX_PLAUSIBLE_RMV` guards the per-tank path's segmentation errors. This path
        divides by the dive's own duration and depth, so there is nothing to segment
        wrongly - and applying it here would let one 24 L manifolded row return a figure
        where the same dive as two flagged 12 L rows returned `None`.
        """
        result = compute_parallel_gas_use(
            duration=60,
            avg_depth=10.0,
            mixtures=[
                _parallel(volume=12.0, start_pressure=200.0, end_pressure=0.0),
                _parallel(volume=12.0, start_pressure=200.0, end_pressure=0.0),
            ],
        )

        assert result is not None
        assert result.rmv > MAX_PLAUSIBLE_RMV
        assert result.gas_used == 4800.0


class TestComputeParallelGasUseReturnsNone:
    """Every reason a flagged set still can't produce a figure. The flag says the litres
    are additive; it does not conjure the numbers to add.
    """

    def test_when_there_is_only_one_cylinder(self) -> None:
        """A lone cylinder flagged parallel is `compute_gas_use`'s dive, not this one -
        the dispatcher never routes it here, and the function refuses it on its own too.
        """
        assert compute_parallel_gas_use(duration=59 * 60, avg_depth=22.6, mixtures=[_parallel()]) is None

    def test_when_there_are_no_mixtures(self) -> None:
        assert compute_parallel_gas_use(duration=59 * 60, avg_depth=22.6, mixtures=[]) is None

    def test_when_a_cylinder_is_unflagged(self) -> None:
        """A pair plus a bottle nobody has answered for. Summing the flagged rows alone
        would leave the numerator short against a full-dive denominator and quietly report
        an RMV that is too *low* - worse than saying nothing.
        """
        assert (
            compute_parallel_gas_use(
                duration=59 * 60, avg_depth=22.6, mixtures=[_parallel(volume=11.1), _parallel(volume=11.1), _mixture()]
            )
            is None
        )

    def test_when_a_cylinder_is_explicitly_staged(self) -> None:
        """The deliberately-answered mixed set: a sidemount pair plus a deco bottle
        breathed at its own depth. Refused by design, not for want of data.
        """
        assert (
            compute_parallel_gas_use(
                duration=59 * 60,
                avg_depth=22.6,
                mixtures=[
                    _parallel(volume=11.1),
                    _parallel(volume=11.1),
                    _mixture(volume=11.1, usage=TankUsage.STAGED),
                ],
            )
            is None
        )

    def test_when_any_cylinder_is_missing_a_pressure(self) -> None:
        """One null pressure refuses the whole dive - the cylinder was carried and very
        likely breathed, and there is no honest way to leave it out of the sum.
        """
        for partial in (
            _parallel(volume=11.1, start_pressure=None),
            _parallel(volume=11.1, end_pressure=None),
        ):
            assert compute_parallel_gas_use(duration=59 * 60, avg_depth=22.6, mixtures=[_parallel(), partial]) is None

    def test_when_nothing_came_out_of_any_cylinder(self) -> None:
        """Every cylinder as full as it went down: an unbreathed set or a typo, not a
        diver with a 0 L/min consumption rate.
        """
        untouched = [
            _parallel(start_pressure=200.0, end_pressure=200.0),
            _parallel(start_pressure=210.0, end_pressure=210.0),
        ]

        assert compute_parallel_gas_use(duration=59 * 60, avg_depth=22.6, mixtures=untouched) is None

    def test_when_average_depth_is_missing_or_nonsensical(self) -> None:
        """`avg_depth` is this path's only depth - there is no profile behind it to fall
        back on, which is the whole point of the branch.
        """
        pair = [_parallel(volume=11.1), _parallel(volume=11.1)]

        assert compute_parallel_gas_use(duration=59 * 60, avg_depth=None, mixtures=pair) is None
        assert compute_parallel_gas_use(duration=59 * 60, avg_depth=0.0, mixtures=pair) is None
        assert compute_parallel_gas_use(duration=59 * 60, avg_depth=-1.0, mixtures=pair) is None

    def test_when_the_duration_or_a_volume_is_not_positive(self) -> None:
        """Re-checked here for the same reason `compute_gas_use` re-checks them: this is a
        pure function that tests and future callers can reach with unsaved input, and a
        zero in the denominator would raise rather than return a wrong number.
        """
        assert compute_parallel_gas_use(duration=0, avg_depth=22.6, mixtures=[_parallel(), _parallel()]) is None
        assert (
            compute_parallel_gas_use(duration=59 * 60, avg_depth=22.6, mixtures=[_parallel(), _parallel(volume=0.0)])
            is None
        )


class TestResolveGasUse:
    """The one entry point every caller uses, so the choice between the three derivations
    lives in a single place. Cylinder count picks the first candidate and no longer decides
    on its own: a multi-cylinder dive the attribution declines falls through to the
    additive path when - and only when - every mixture is flagged parallel."""

    def test_a_single_cylinder_dive_is_computed_exactly_as_before(self):
        attribution = _attribution(_attributed(1, seconds=600, mean_depth_cm=3000))

        result = resolve_gas_use(
            duration=45 * 60, avg_depth=18.0, mixtures=[_mixture(gas_number=1)], attribution=attribution
        )

        assert result == compute_gas_use(duration=45 * 60, avg_depth=18.0, mixtures=[_mixture(gas_number=1)])
        # In particular the profile's own mean depth does not get to override the dive's
        # `avg_depth`, which is the diver's record and may have been edited.
        assert result.rmv == 14.29
        assert result.tanks == []
        assert result.attributed_seconds is None

    def test_a_multi_cylinder_dive_without_attribution_still_says_nothing(self):
        """Every multi-cylinder dive in the log before Phase 4, and every one imported from
        a file that records no gas switches after it. Unflagged is still unflagged: the
        parallel fallback rescues a dive only where the diver said it was a parallel set.
        """
        assert (
            resolve_gas_use(duration=45 * 60, avg_depth=18.0, mixtures=[_mixture(gas_number=1), _mixture(gas_number=2)])
            is None
        )

    def test_a_flagged_parallel_pair_is_computed_without_any_attribution(self):
        """The dive the fallback exists for: a hand-logged sidemount pair with full
        pressures, no profile and therefore no gas switches to attribute anything from.
        """
        result = resolve_gas_use(
            duration=59 * 60,
            avg_depth=22.6,
            mixtures=[
                _parallel(volume=11.1, start_pressure=200.0, end_pressure=110.0),
                _parallel(volume=11.1, start_pressure=210.0, end_pressure=115.0),
            ],
        )

        assert result is not None
        assert result.gas_used == 2053.5
        assert result.tanks == []

    def test_attribution_wins_over_the_flag(self):
        """A per-tank answer is strictly richer - time and mean depth per cylinder - so a
        diver who flags a pair their computer *did* record switches for must not lose it.
        The flag is a fallback, never an override.
        """
        mixtures = [_parallel(gas_number=1), _parallel(gas_number=2)]
        attribution = _attribution(
            _attributed(1, seconds=2700, mean_depth_cm=1800),
            _attributed(2, seconds=2400, mean_depth_cm=1200),
        )

        result = resolve_gas_use(duration=45 * 60, avg_depth=18.0, mixtures=mixtures, attribution=attribution)

        assert result == compute_multi_tank_gas_use(mixtures=mixtures, attribution=attribution)
        assert result is not None
        assert len(result.tanks) == 2

    def test_a_flagged_pair_whose_attribution_refuses_falls_through(self):
        """The recorded refusal this branch was written to rescue: a cylinder with a real
        pressure drop that the attribution never mentions - the sidemount pair the computer
        saw as one gas. `compute_multi_tank_gas_use` declines, and the flag picks it up.
        """
        mixtures = [
            _parallel(volume=11.1, start_pressure=200.0, end_pressure=110.0, gas_number=1),
            _parallel(volume=11.1, start_pressure=210.0, end_pressure=115.0, gas_number=2),
        ]
        # Only cylinder 1 is attributed; cylinder 2 was demonstrably breathed.
        attribution = _attribution(_attributed(1, seconds=3540, mean_depth_cm=2260))

        assert compute_multi_tank_gas_use(mixtures=mixtures, attribution=attribution) is None

        result = resolve_gas_use(duration=59 * 60, avg_depth=22.6, mixtures=mixtures, attribution=attribution)

        assert result is not None
        assert result.gas_used == 2053.5

    def test_a_mixed_flag_set_says_nothing_from_either_path(self):
        """Neither derivation applies: no attribution for the per-tank one, and a staged
        bottle in the set for the additive one.
        """
        assert (
            resolve_gas_use(
                duration=59 * 60,
                avg_depth=22.6,
                mixtures=[_parallel(volume=11.1), _mixture(volume=11.1, usage=TankUsage.STAGED)],
            )
            is None
        )

    def test_a_multi_cylinder_dive_with_attribution_is_computed_per_tank(self):
        result = resolve_gas_use(
            duration=45 * 60,
            avg_depth=18.0,
            mixtures=[_mixture(gas_number=1), _mixture(gas_number=2)],
            attribution=_attribution(
                _attributed(1, seconds=2700, mean_depth_cm=1800),
                _attributed(2, seconds=2400, mean_depth_cm=1200),
            ),
        )

        assert result is not None
        assert len(result.tanks) == 2
        assert result.attributed_seconds == 5100
