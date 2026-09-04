"""Unit tests mapping DB `CheckConstraint` violations to API error messages.

`src.app.api.v1.dives` translates a Postgres `CheckConstraint` violation (surfaced
as a `sqlalchemy.exc.IntegrityError` wrapping a driver-specific error whose message
includes the constraint's name) into a human-readable 422 `detail` string via
`_fk_error_detail`/`_mixture_error_detail`. These tests build fake `IntegrityError`s
whose `.orig` message mimics what asyncpg/psycopg produce, without needing a real
database connection - see `test_dive_check_constraints.py` for tests that verify the
constraints themselves against a live Postgres database.
"""

from sqlalchemy.exc import IntegrityError

from src.app.api.v1.dives import _fk_error_detail, _mixture_error_detail


def _integrity_error(constraint_name: str) -> IntegrityError:
    """Build an `IntegrityError` whose message resembles a real driver error.

    Mirrors the shape of the message asyncpg/psycopg produce for a check
    constraint violation, e.g.:
    'new row for relation "dive" violates check constraint "ck_dive_duration_positive"'
    """
    orig = Exception(f'new row for relation "dive" violates check constraint "{constraint_name}"')
    return IntegrityError("INSERT ...", {}, orig)


class TestFkErrorDetail:
    def test_trip_fk_violation(self):
        exc = IntegrityError("INSERT ...", {}, Exception('violates foreign key constraint "dive_trip_id_fkey"'))
        assert _fk_error_detail(exc) == "Trip not found."

    def test_dive_site_fk_violation(self):
        exc = IntegrityError("INSERT ...", {}, Exception('violates foreign key constraint "dive_site_id_fkey"'))
        assert _fk_error_detail(exc) == "Dive site not found."

    def test_duration_must_be_positive(self):
        assert _fk_error_detail(_integrity_error("ck_dive_duration_positive")) == "Duration must be positive."

    def test_visibility_must_be_non_negative(self):
        assert (
            _fk_error_detail(_integrity_error("ck_dive_visibility_non_negative"))
            == "Visibility must be zero or positive."
        )

    def test_max_depth_must_be_positive(self):
        assert _fk_error_detail(_integrity_error("ck_dive_max_depth_positive")) == "Max depth must be positive."

    def test_avg_depth_must_be_positive(self):
        assert _fk_error_detail(_integrity_error("ck_dive_avg_depth_positive")) == "Average depth must be positive."

    def test_weight_must_be_non_negative(self):
        assert _fk_error_detail(_integrity_error("ck_dive_weight_non_negative")) == "Weight must be zero or positive."

    def test_altitude_range(self):
        assert (
            _fk_error_detail(_integrity_error("ck_dive_altitude_range"))
            == "Altitude must be between -450 and 6500 meters."
        )

    def test_avg_depth_within_max(self):
        """The backstop for the one route into this constraint `validate_depth_pair`
        cannot cover: a concurrent edit between `patch_dive`'s merged check and its
        UPDATE."""
        assert (
            _fk_error_detail(_integrity_error("ck_dive_avg_depth_within_max"))
            == "Average depth cannot be greater than max depth."
        )

    def test_coordinate_ranges(self):
        """Unreachable through the form - nothing sets these but the import - but a
        constraint with no message here is *worse* than a raw 500: both dive write paths
        already wrap `IntegrityError`, so it falls through to the generic fallback below
        and answers 422 with a sentence about a missing related record."""
        assert (
            _fk_error_detail(_integrity_error("ck_dive_entry_latitude_range"))
            == "Imported latitudes must be between -90 and 90."
        )
        assert (
            _fk_error_detail(_integrity_error("ck_dive_exit_longitude_range"))
            == "Imported longitudes must be between -180 and 180."
        )

    def test_half_a_position(self):
        assert (
            _fk_error_detail(_integrity_error("ck_dive_exit_position_pair"))
            == "An imported position needs both a latitude and a longitude."
        )

    def test_unknown_violation_falls_back_to_generic_message(self):
        exc = IntegrityError("INSERT ...", {}, Exception("some other constraint"))
        assert _fk_error_detail(exc) == "Invalid reference: a related record does not exist."


class TestMixtureErrorDetail:
    def test_volume_must_be_positive(self):
        assert _mixture_error_detail(_integrity_error("ck_dive_mixture_volume_positive")) == "Volume must be positive."

    def test_oxygen_range(self):
        assert (
            _mixture_error_detail(_integrity_error("ck_dive_mixture_oxygen_range"))
            == "Oxygen percentage must be between 0 and 100."
        )

    def test_helium_range(self):
        assert (
            _mixture_error_detail(_integrity_error("ck_dive_mixture_helium_range"))
            == "Helium percentage must be between 0 and 100."
        )

    def test_oxygen_helium_sum(self):
        assert (
            _mixture_error_detail(_integrity_error("ck_dive_mixture_oxygen_helium_sum"))
            == "Oxygen and helium percentages cannot sum to more than 100."
        )

    def test_pressure_order(self):
        assert (
            _mixture_error_detail(_integrity_error("ck_dive_mixture_pressure_order"))
            == "End pressure cannot be greater than start pressure."
        )

    def test_start_pressure_range(self):
        """ "above 0", not "between 0 and 350": the second phrasing tells a diver who just
        typed a 0 that the value they were rejected for is legal."""
        assert (
            _mixture_error_detail(_integrity_error("ck_dive_mixture_start_pressure_range"))
            == "Start pressure must be above 0 and at most 350 bar."
        )

    def test_end_pressure_range(self):
        """ "between" is correct here and not above, because 0 *is* legal at the end of a
        dive - the message has to carry the asymmetry the constraints do."""
        assert (
            _mixture_error_detail(_integrity_error("ck_dive_mixture_end_pressure_range"))
            == "End pressure must be between 0 and 350 bar."
        )

    def test_unknown_violation_falls_back_to_generic_message(self):
        exc = IntegrityError("INSERT ...", {}, Exception("some other constraint"))
        assert _mixture_error_detail(exc) == "Invalid gas mixture."
