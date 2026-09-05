"""Integration tests for the `CheckConstraint`s on `Dive`/`DiveMixture`.

Unlike the rest of this suite, these tests insert real rows through a sync SQLAlchemy
session against a live Postgres database (see the `db` fixture in `conftest.py`), so
they verify the actual constraints Postgres enforces - not just the Python-level
`CheckConstraint` declarations in `src/app/models/dive.py`/`dive_mixture.py`. See
`test_dive_constraint_messages.py` for DB-free tests of the corresponding API error
messages.

Automatically skipped if no database is reachable (e.g. running `pytest` outside the
project's docker compose setup).
"""

from collections.abc import Generator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from src.app.models.dive import Dive
from src.app.models.dive_mixture import DiveMixture
from src.app.models.user import User
from tests.conftest import db_available
from tests.helpers.generators import create_user

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")


@pytest.fixture
def dive_owner(db: Session) -> User:
    return create_user(db)


def _make_dive(user_id: int, **overrides: Any) -> Dive:
    defaults: dict[str, Any] = {
        "user_id": user_id,
        "dive_number": 1,
        "start_time": datetime.now(UTC),
        "duration": 1800,
        "notes": "",
    }
    defaults.update(overrides)
    return Dive(**defaults)


def _make_mixture(dive_id: int, **overrides: Any) -> DiveMixture:
    defaults: dict[str, Any] = {"dive_id": dive_id, "volume": 12.0, "oxygen": 21.0, "helium": 0.0}
    defaults.update(overrides)
    return DiveMixture(**defaults)


def _assert_violates(db: Session, obj: Any, constraint_name: str) -> None:
    db.add(obj)
    with pytest.raises(IntegrityError, match=constraint_name):
        db.commit()
    db.rollback()


def _assert_rejected(db: Session, obj: Any) -> None:
    """Like `_assert_violates`, but without asserting which constraint fired.

    Used where two constraints legitimately overlap - e.g. `oxygen > 100` with a
    valid (>= 0) `helium` also always pushes `oxygen + helium` over 100, so either
    `ck_dive_mixture_oxygen_range` or `ck_dive_mixture_oxygen_helium_sum` is a
    correct rejection reason.
    """
    db.add(obj)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


class TestDiveCheckConstraints:
    def test_zero_duration_is_rejected(self, db: Session, dive_owner: User) -> None:
        _assert_violates(db, _make_dive(dive_owner.id, duration=0), "ck_dive_duration_positive")

    def test_negative_duration_is_rejected(self, db: Session, dive_owner: User) -> None:
        _assert_violates(db, _make_dive(dive_owner.id, duration=-5), "ck_dive_duration_positive")

    def test_positive_duration_is_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id, duration=1))
        db.commit()

    def test_negative_visibility_is_rejected(self, db: Session, dive_owner: User) -> None:
        _assert_violates(db, _make_dive(dive_owner.id, visibility=-1), "ck_dive_visibility_non_negative")

    def test_zero_visibility_is_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id, visibility=0))
        db.commit()

    def test_null_visibility_is_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id, visibility=None))
        db.commit()

    def test_zero_max_depth_is_rejected(self, db: Session, dive_owner: User) -> None:
        _assert_violates(db, _make_dive(dive_owner.id, max_depth=0), "ck_dive_max_depth_positive")

    def test_negative_max_depth_is_rejected(self, db: Session, dive_owner: User) -> None:
        _assert_violates(db, _make_dive(dive_owner.id, max_depth=-10), "ck_dive_max_depth_positive")

    def test_positive_max_depth_is_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id, max_depth=18.5))
        db.commit()

    def test_null_max_depth_is_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id, max_depth=None))
        db.commit()

    def test_zero_avg_depth_is_rejected(self, db: Session, dive_owner: User) -> None:
        _assert_violates(db, _make_dive(dive_owner.id, avg_depth=0), "ck_dive_avg_depth_positive")

    def test_negative_avg_depth_is_rejected(self, db: Session, dive_owner: User) -> None:
        _assert_violates(db, _make_dive(dive_owner.id, avg_depth=-3), "ck_dive_avg_depth_positive")

    def test_positive_avg_depth_is_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id, avg_depth=12.0))
        db.commit()

    def test_null_avg_depth_is_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id, avg_depth=None))
        db.commit()

    def test_an_avg_depth_deeper_than_the_max_is_rejected(self, db: Session, dive_owner: User) -> None:
        """A mean cannot exceed a maximum, so the row records at least one wrong number.

        The only *pair* rule on this table, and the reason it is here rather than only in
        `schemas/dive.py`: the DiveJSON writer must not be able to emit a document the
        format's own validator rejects (spec §6.2), and a Pydantic validator would leave
        the admin panel and any direct write free to store one.
        """
        _assert_violates(db, _make_dive(dive_owner.id, avg_depth=30.0, max_depth=20.0), "ck_dive_avg_depth_within_max")

    def test_an_equal_pair_is_allowed(self, db: Session, dive_owner: User) -> None:
        """A perfectly square profile is unusual, not impossible - so `<=`, not `<`."""
        db.add(_make_dive(dive_owner.id, avg_depth=20.0, max_depth=20.0))
        db.commit()

    def test_either_depth_alone_is_allowed(self, db: Session, dive_owner: User) -> None:
        """The rule compares a pair, and a dive that recorded one of the two has no pair."""
        db.add(_make_dive(dive_owner.id, avg_depth=30.0, max_depth=None))
        db.add(_make_dive(dive_owner.id, dive_number=2, avg_depth=None, max_depth=20.0))
        db.commit()

    def test_negative_weight_is_rejected(self, db: Session, dive_owner: User) -> None:
        _assert_violates(db, _make_dive(dive_owner.id, weight=-1), "ck_dive_weight_non_negative")

    def test_zero_weight_is_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id, weight=0))
        db.commit()

    def test_positive_weight_is_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id, weight=6.5))
        db.commit()

    def test_null_weight_is_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id, weight=None))
        db.commit()

    def test_altitude_outside_the_diveable_band_is_rejected(self, db: Session, dive_owner: User) -> None:
        """The band catches a unit or typo error, not an unusual dive: below -450 m is
        under the Dead Sea, and above 6500 m is above the highest attested dive on the
        planet."""
        _assert_violates(db, _make_dive(dive_owner.id, altitude=-451), "ck_dive_altitude_range")
        _assert_violates(db, _make_dive(dive_owner.id, altitude=6501), "ck_dive_altitude_range")

    def test_the_altitude_bounds_themselves_are_allowed(self, db: Session, dive_owner: User) -> None:
        """Inclusive on both sides, and both ends are real places: the Dead Sea surface
        sits near -430 m and the Ojos del Salado summit pool near 6390 m."""
        db.add(_make_dive(dive_owner.id, altitude=-450))
        db.add(_make_dive(dive_owner.id, altitude=6500))
        db.commit()

    def test_sea_level_and_a_null_altitude_are_both_allowed(self, db: Session, dive_owner: User) -> None:
        """0 is a recorded reading - most dives happen at sea level - and telling it apart
        from "didn't record it" is why this column is nullable rather than defaulted."""
        db.add(_make_dive(dive_owner.id, altitude=0))
        db.add(_make_dive(dive_owner.id))
        db.commit()

    def test_any_water_type_string_is_accepted_by_the_database(self, db: Session, dive_owner: User) -> None:
        """Deliberately unconstrained, exactly like `gear_item.type`: `WaterType` is a
        Pydantic enum on every write path, so a DB copy of the vocabulary would buy
        nothing and cost a `DROP`/`ADD CONSTRAINT` per new member (see DECISIONS.md).
        This test is the record of that, not a gap - `test_dive_update.py` pins the 422
        the API answers with."""
        db.add(_make_dive(dive_owner.id, water_type="brackish"))
        db.add(_make_dive(dive_owner.id, water_type="soda"))
        db.commit()

    def test_negative_cns_is_rejected(self, db: Session, dive_owner: User) -> None:
        _assert_violates(db, _make_dive(dive_owner.id, cns_start=-1), "ck_dive_cns_start_non_negative")
        _assert_violates(db, _make_dive(dive_owner.id, cns_end=-1), "ck_dive_cns_end_non_negative")

    def test_negative_otu_is_rejected(self, db: Session, dive_owner: User) -> None:
        _assert_violates(db, _make_dive(dive_owner.id, otu_start=-1), "ck_dive_otu_start_non_negative")
        _assert_violates(db, _make_dive(dive_owner.id, otu_end=-1), "ck_dive_otu_end_non_negative")

    def test_zero_cns_and_otu_are_allowed(self, db: Session, dive_owner: User) -> None:
        """`>= 0`, not `> 0`: a dive that began with no oxygen loading records a real 0,
        and that is worth telling apart from having recorded nothing."""
        db.add(_make_dive(dive_owner.id, cns_start=0, cns_end=0, otu_start=0, otu_end=0))
        db.commit()

    def test_cns_over_one_hundred_percent_is_allowed(self, db: Session, dive_owner: User) -> None:
        """Deliberately unbounded above - a CNS clock past 100 % is precisely the reading
        a diver most needs to see, and clamping it would hide it."""
        db.add(_make_dive(dive_owner.id, cns_end=140.0))
        db.commit()

    def test_surface_pressure_outside_the_barometric_band_is_rejected(self, db: Session, dive_owner: User) -> None:
        """The band exists to catch a unit error, not an unusual dive site: both Suunto
        exports write this field in Pascal, so an unconverted 105700 is off by five
        orders of magnitude."""
        _assert_violates(db, _make_dive(dive_owner.id, surface_pressure_bar=105700.0), "ck_dive_surface_pressure_range")
        _assert_violates(db, _make_dive(dive_owner.id, surface_pressure_bar=0.1), "ck_dive_surface_pressure_range")

    def test_real_surface_pressures_are_allowed(self, db: Session, dive_owner: User) -> None:
        """1.057 bar is a real reading off a 2025 export; 0.55 is roughly a 5 000 m lake.

        0.44 is the floor's own reason to be 0.4 rather than 0.5: it is ambient pressure at
        `ck_dive_altitude_range`'s 6500 m ceiling, so the old floor refused a reading the
        altitude bound blesses - and it is DiveJSON's floor too (spec §6.2), which is where
        the contradiction was noticed.
        """
        db.add(_make_dive(dive_owner.id, surface_pressure_bar=1.057))
        db.add(_make_dive(dive_owner.id, surface_pressure_bar=0.55))
        db.add(_make_dive(dive_owner.id, surface_pressure_bar=0.44))
        db.commit()

    def test_coordinates_past_the_poles_or_the_antimeridian_are_rejected(self, db: Session, dive_owner: User) -> None:
        """The limits of the coordinate system, so a value outside them is a unit error -
        a Suunto radian or a FIT semicircle count that reached the column unconverted."""
        _assert_violates(
            db, _make_dive(dive_owner.id, entry_latitude=90.1, entry_longitude=34.0), "ck_dive_entry_latitude_range"
        )
        _assert_violates(
            db, _make_dive(dive_owner.id, entry_latitude=28.0, entry_longitude=-180.1), "ck_dive_entry_longitude_range"
        )
        _assert_violates(
            db, _make_dive(dive_owner.id, exit_latitude=-90.1, exit_longitude=34.0), "ck_dive_exit_latitude_range"
        )
        _assert_violates(
            db, _make_dive(dive_owner.id, exit_latitude=28.0, exit_longitude=180.1), "ck_dive_exit_longitude_range"
        )

    def test_the_bounds_themselves_are_allowed(self, db: Session, dive_owner: User) -> None:
        """Inclusive on both sides: the poles and the antimeridian are real places, and a
        dive at one of them would be the most interesting row in the table."""
        db.add(_make_dive(dive_owner.id, entry_latitude=90.0, entry_longitude=180.0))
        db.add(_make_dive(dive_owner.id, exit_latitude=-90.0, exit_longitude=-180.0))
        db.commit()

    def test_half_a_position_is_rejected(self, db: Session, dive_owner: User) -> None:
        """Either half alone pins the dive to the equator or the prime meridian, which is
        a claim no file made. `ParsedDiveSchema._drop_half_positions` is what keeps an
        import from ever reaching this; the constraint is what makes that a guarantee."""
        _assert_violates(db, _make_dive(dive_owner.id, entry_latitude=28.0), "ck_dive_entry_position_pair")
        _assert_violates(db, _make_dive(dive_owner.id, entry_longitude=34.0), "ck_dive_entry_position_pair")
        _assert_violates(db, _make_dive(dive_owner.id, exit_latitude=28.0), "ck_dive_exit_position_pair")
        _assert_violates(db, _make_dive(dive_owner.id, exit_longitude=34.0), "ck_dive_exit_position_pair")

    def test_an_exit_position_with_no_entry_one_is_allowed(self, db: Session, dive_owner: User) -> None:
        """The two pairs are independent, and this is the corpus's ordinary shape rather
        than a corner: a wrist computer gets no fix until the diver surfaces, so every
        GPS-carrying export in it records an exit position and no entry one."""
        db.add(_make_dive(dive_owner.id, exit_latitude=28.437455, exit_longitude=34.458997))
        db.commit()

    def test_null_positions_are_allowed(self, db: Session, dive_owner: User) -> None:
        db.add(_make_dive(dive_owner.id))
        db.commit()


class TestDiveMixtureCheckConstraints:
    @pytest.fixture
    def dive(self, db: Session, dive_owner: User) -> Generator[Dive, Any]:
        dive = _make_dive(dive_owner.id)
        db.add(dive)
        db.commit()
        db.refresh(dive)
        yield dive

    def test_zero_volume_is_rejected(self, db: Session, dive: Dive) -> None:
        _assert_violates(db, _make_mixture(dive.id, volume=0), "ck_dive_mixture_volume_positive")

    def test_negative_volume_is_rejected(self, db: Session, dive: Dive) -> None:
        _assert_violates(db, _make_mixture(dive.id, volume=-1), "ck_dive_mixture_volume_positive")

    def test_negative_oxygen_is_rejected(self, db: Session, dive: Dive) -> None:
        _assert_violates(db, _make_mixture(dive.id, oxygen=-1, helium=0), "ck_dive_mixture_oxygen_range")

    def test_oxygen_over_100_is_rejected(self, db: Session, dive: Dive) -> None:
        # oxygen=101 with a valid helium also always violates the oxygen+helium<=100
        # constraint, so we can't pin down which specific constraint fires here.
        _assert_rejected(db, _make_mixture(dive.id, oxygen=101, helium=0))

    def test_negative_helium_is_rejected(self, db: Session, dive: Dive) -> None:
        _assert_violates(db, _make_mixture(dive.id, oxygen=0, helium=-1), "ck_dive_mixture_helium_range")

    def test_helium_over_100_is_rejected(self, db: Session, dive: Dive) -> None:
        # Same overlap as test_oxygen_over_100_is_rejected, mirrored for helium.
        _assert_rejected(db, _make_mixture(dive.id, oxygen=0, helium=101))

    def test_oxygen_plus_helium_over_100_is_rejected(self, db: Session, dive: Dive) -> None:
        _assert_violates(db, _make_mixture(dive.id, oxygen=60, helium=50), "ck_dive_mixture_oxygen_helium_sum")

    def test_oxygen_plus_helium_equal_to_100_is_allowed(self, db: Session, dive: Dive) -> None:
        db.add(_make_mixture(dive.id, oxygen=60, helium=40))
        db.commit()

    def test_oxygen_and_helium_at_boundaries_are_allowed(self, db: Session, dive: Dive) -> None:
        db.add(_make_mixture(dive.id, oxygen=0, helium=0))
        db.commit()

    def test_valid_mixture_is_allowed(self, db: Session, dive: Dive) -> None:
        db.add(_make_mixture(dive.id, volume=12, oxygen=21, helium=0))
        db.commit()

    def test_a_cylinder_may_record_a_mix_without_a_vessel(self, db: Session, dive: Dive) -> None:
        """The row this whole change exists for: a gas link and no `<tankvolume>`."""
        db.add(_make_mixture(dive.id, volume=None, start_pressure=200, end_pressure=80))
        db.commit()

    def test_a_cylinder_may_record_a_vessel_without_a_mix(self, db: Session, dive: Dive) -> None:
        """The mirror, and the one §6.3 spells out: absent `oxygen` is not 21."""
        db.add(_make_mixture(dive.id, oxygen=None, helium=None))
        db.commit()

    def test_a_cylinder_may_record_none_of_the_three(self, db: Session, dive: Dive) -> None:
        db.add(_make_mixture(dive.id, volume=None, oxygen=None, helium=None))
        db.commit()

    def test_a_null_operand_does_not_switch_the_sum_constraint_off_for_the_other(self, db: Session, dive: Dive) -> None:
        """`oxygen + helium <= 100` passes on UNKNOWN, so a NULL half admits the row -
        and that has to stay true only for the *pair*. A recorded fraction on its own is
        still bounded by its own range constraint, which is what the restatement in
        revision `d3b1700eb489` had to preserve rather than accidentally relax."""
        db.add(_make_mixture(dive.id, oxygen=95, helium=None))
        db.commit()
        _assert_violates(db, _make_mixture(dive.id, oxygen=None, helium=101), "ck_dive_mixture_helium_range")

    def test_the_restated_constraints_still_reject_what_they_always_did(self, db: Session, dive: Dive) -> None:
        """Spelling the null case out is a restatement, not a relaxation: every recorded
        value is bounded exactly as it was before the columns became nullable."""
        _assert_violates(db, _make_mixture(dive.id, volume=0), "ck_dive_mixture_volume_positive")
        _assert_violates(db, _make_mixture(dive.id, oxygen=-1, helium=0), "ck_dive_mixture_oxygen_range")
        _assert_violates(db, _make_mixture(dive.id, oxygen=0, helium=-1), "ck_dive_mixture_helium_range")
        _assert_violates(db, _make_mixture(dive.id, oxygen=60, helium=50), "ck_dive_mixture_oxygen_helium_sum")

    def test_end_pressure_greater_than_start_pressure_is_rejected(self, db: Session, dive: Dive) -> None:
        _assert_violates(
            db,
            _make_mixture(dive.id, start_pressure=50, end_pressure=200),
            "ck_dive_mixture_pressure_order",
        )

    def test_end_pressure_equal_to_start_pressure_is_allowed(self, db: Session, dive: Dive) -> None:
        db.add(_make_mixture(dive.id, start_pressure=200, end_pressure=200))
        db.commit()

    def test_end_pressure_less_than_start_pressure_is_allowed(self, db: Session, dive: Dive) -> None:
        db.add(_make_mixture(dive.id, start_pressure=200, end_pressure=50))
        db.commit()

    def test_null_start_pressure_is_allowed(self, db: Session, dive: Dive) -> None:
        db.add(_make_mixture(dive.id, start_pressure=None, end_pressure=50))
        db.commit()

    def test_null_end_pressure_is_allowed(self, db: Session, dive: Dive) -> None:
        db.add(_make_mixture(dive.id, start_pressure=200, end_pressure=None))
        db.commit()

    def test_zero_start_pressure_is_rejected(self, db: Session, dive: Dive) -> None:
        """A cylinder at 0 bar delivers nothing, so no dive began on one. Every 0 that
        reaches this column is a file's absent-marker, a client bug or a typo - see
        `DiveMixtureSchema._drop_unpressurized` for the corpus behind the first."""
        _assert_violates(db, _make_mixture(dive.id, start_pressure=0), "ck_dive_mixture_start_pressure_range")

    def test_negative_start_pressure_is_rejected(self, db: Session, dive: Dive) -> None:
        _assert_violates(db, _make_mixture(dive.id, start_pressure=-1), "ck_dive_mixture_start_pressure_range")

    def test_zero_end_pressure_is_allowed(self, db: Session, dive: Dive) -> None:
        """The asymmetry, from the database's side: **you cannot start a dive on an empty
        cylinder, but you can finish one on an empty cylinder.** An out-of-gas ascent, a
        drained stage and an SPG pegged at zero are all real, and this is the boundary a
        later tidy-up of the two adjacent constraints would silently take away."""
        db.add(_make_mixture(dive.id, start_pressure=200, end_pressure=0))
        db.commit()

    def test_negative_end_pressure_is_rejected(self, db: Session, dive: Dive) -> None:
        _assert_violates(db, _make_mixture(dive.id, end_pressure=-1), "ck_dive_mixture_end_pressure_range")

    def test_pressures_above_the_band_are_rejected(self, db: Session, dive: Dive) -> None:
        """205203 is what reading DM5's millibar as bar used to store (see DECISIONS.md),
        and 415/230 is a sidemount pair summed against one cylinder's water capacity -
        every individual number positive, correctly ordered, and inside every constraint
        the table had before this one."""
        _assert_violates(db, _make_mixture(dive.id, start_pressure=205203), "ck_dive_mixture_start_pressure_range")
        _assert_violates(db, _make_mixture(dive.id, start_pressure=351), "ck_dive_mixture_start_pressure_range")
        _assert_violates(db, _make_mixture(dive.id, end_pressure=351), "ck_dive_mixture_end_pressure_range")

    def test_a_300_bar_din_fill_is_allowed(self, db: Session, dive: Dive) -> None:
        """The highest real fill there is, and the reason the band is 350 rather than
        tighter: it exists to catch a unit error, not to have an opinion about how hard
        someone fills a cylinder."""
        db.add(_make_mixture(dive.id, start_pressure=300, end_pressure=80))
        db.commit()

    def test_nan_pressures_are_rejected(self, db: Session, dive: Dive) -> None:
        """The case the *upper* half of each band exists for, and the one nobody would
        think to write. Postgres sorts `NaN` above every number, so `'NaN'::float8 > 0`
        is **true** - a one-sided `> 0` constraint would have admitted it while looking
        closed, and one stored `NaN` turns `GET /dives` into a 500 that only hand-written
        SQL clears (see `_ParserOutput`).
        """
        _assert_violates(
            db, _make_mixture(dive.id, start_pressure=float("nan")), "ck_dive_mixture_start_pressure_range"
        )
        _assert_violates(db, _make_mixture(dive.id, end_pressure=float("nan")), "ck_dive_mixture_end_pressure_range")

    def test_po2_limit_outside_the_diveable_band_is_rejected(self, db: Session, dive: Dive) -> None:
        """140000 is what a Suunto JSON export writes for 1.4 bar. Reaching the database
        unconverted is the failure this constraint exists for."""
        _assert_violates(db, _make_mixture(dive.id, po2_limit=140000.0), "ck_dive_mixture_po2_limit_range")
        _assert_violates(db, _make_mixture(dive.id, po2_limit=0.1), "ck_dive_mixture_po2_limit_range")

    def test_real_po2_limits_are_allowed(self, db: Session, dive: Dive) -> None:
        """1.4 on a back gas and 1.6 on a deco bottle - both real, on the same dive."""
        db.add(_make_mixture(dive.id, po2_limit=1.4))
        db.add(_make_mixture(dive.id, po2_limit=1.6))
        db.commit()

    def test_zero_gas_number_is_allowed(self, db: Session, dive: Dive) -> None:
        """A Suunto Ocean numbers its cylinders from 0, and the profiles already stored
        for those dives label their pressure channels `0` to match. This started out as a
        `>= 1` check and the backfill's first real run rejected the corpus on it - see
        DECISIONS.md."""
        db.add(_make_mixture(dive.id, gas_number=0))
        db.commit()

    def test_negative_gas_number_is_rejected(self, db: Session, dive: Dive) -> None:
        """No format produces one, so this is a sign bug rather than a convention."""
        _assert_violates(db, _make_mixture(dive.id, gas_number=-1), "ck_dive_mixture_gas_number_non_negative")

    def test_null_tech_fields_are_allowed(self, db: Session, dive: Dive) -> None:
        """A hand-entered cylinder has no position in any file, and most exports record
        no ppO2 or role."""
        db.add(_make_mixture(dive.id, po2_limit=None, gas_number=None, role=None))
        db.commit()
