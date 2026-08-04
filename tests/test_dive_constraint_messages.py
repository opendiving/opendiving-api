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

    def test_unknown_violation_falls_back_to_generic_message(self):
        exc = IntegrityError("INSERT ...", {}, Exception("some other constraint"))
        assert _fk_error_detail(exc) == "Invalid reference: a related record does not exist."


class TestMixtureErrorDetail:
    def test_volume_must_be_positive(self):
        assert (
            _mixture_error_detail(_integrity_error("ck_dive_mixture_volume_positive")) == "Volume must be positive."
        )

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

    def test_unknown_violation_falls_back_to_generic_message(self):
        exc = IntegrityError("INSERT ...", {}, Exception("some other constraint"))
        assert _mixture_error_detail(exc) == "Invalid gas mixture."
