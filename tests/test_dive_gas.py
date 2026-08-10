"""Unit tests for gas-consumption arithmetic (`services/dive_gas.py`).

Same convention as `test_gear_service.py`: `compute_gas_use` is pure, so the whole
truth table - the worked numbers and every reason a dive can't produce one - is
covered here without a database. Endpoint behaviour on top of a live Postgres/Redis
is exercised by hand (see DECISIONS.md), not here.
"""

from src.app.schemas.dive_mixture import DiveMixtureRead
from src.app.services.dive_gas import METERS_PER_BAR, compute_gas_use


def _mixture(
    *,
    volume: float = 12.0,
    start_pressure: float | None = 200.0,
    end_pressure: float | None = 50.0,
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
