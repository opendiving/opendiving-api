"""Unit tests for gas-consumption arithmetic (`services/dive_gas.py`).

Same convention as `test_gear_service.py`: both derivations are pure, so the whole
truth table - the worked numbers and every reason a dive can't produce one - is
covered here without a database. Endpoint behaviour on top of a live Postgres/Redis
is exercised by hand (see DECISIONS.md), not here.
"""

from src.app.schemas.dive_mixture import DiveMixtureRead
from src.app.schemas.dive_profile import GasAttribution
from src.app.services.dive_gas import (
    METERS_PER_BAR,
    compute_gas_use,
    compute_multi_tank_gas_use,
    resolve_gas_use,
)
from src.app.services.dive_profiles import ProfileGasAttribution


def _mixture(
    *,
    volume: float = 12.0,
    start_pressure: float | None = 200.0,
    end_pressure: float | None = 50.0,
    gas_number: int | None = None,
) -> DiveMixtureRead:
    """A single air cylinder. `id`/`oxygen`/`helium` are required by the schema but
    irrelevant to consumption - RMV is a volume rate, so what's *in* the cylinder
    doesn't enter the arithmetic at all (see `test_gas_mix_does_not_affect_the_result`).
    """
    return DiveMixtureRead(
        id=1,
        name="Back Gas",
        volume=volume,
        start_pressure=start_pressure,
        end_pressure=end_pressure,
        oxygen=21.0,
        helium=0.0,
        gas_number=gas_number,
    )


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


def _attribution(*entries: GasAttribution, duration_seconds: int = 6000) -> ProfileGasAttribution:
    """The attribution as it comes off a profile row, span included - the denominator
    `attributed_seconds` is a fraction of."""
    return ProfileGasAttribution(duration_seconds=duration_seconds, entries=list(entries))


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
            duration_seconds=4682,
        )

        result = compute_multi_tank_gas_use(mixtures=mixtures, attribution=attribution)

        assert result is not None
        assert [tank.gas_number for tank in result.tanks] == [1]
        assert result.gas_used == result.tanks[0].gas_used
        # Half the dive, and both halves of the fraction come off the same profile row -
        # the client renders "these figures cover 39 of the 78 minutes recorded" from it.
        assert (result.attributed_seconds, result.duration_seconds) == (2355, 4682)

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
            attribution=_attribution(_attributed(1, seconds=4300, mean_depth_cm=1779), duration_seconds=4300),
        )

        assert result is None

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


class TestResolveGasUse:
    """The one entry point every caller uses, so the choice between the two derivations
    lives in a single place."""

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
        a file that records no gas switches after it."""
        assert (
            resolve_gas_use(duration=45 * 60, avg_depth=18.0, mixtures=[_mixture(gas_number=1), _mixture(gas_number=2)])
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
