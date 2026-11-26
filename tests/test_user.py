"""Unit tests for user API endpoints."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from starlette.requests import Request
from uuid6 import uuid7

from src.app.api.v1.users import patch_user, read_current_user
from src.app.core.exceptions.http_exceptions import DuplicateValueException
from src.app.crud.crud_users import account_update_values
from src.app.schemas.user import UNIT_FAMILIES, UserRead, UserUpdate

# Note: there is no `POST /user` endpoint to test here anymore - account creation only
# happens via `POST /auth/complete` (see `tests/test_auth.py`).

# Note: there is no `GET /user/{uuid}` or `GET /users` endpoint to test here (yet) -
# looking up *other* users (individually or as a list) will be added later as a
# separate public-profile endpoint.

# `DELETE /user` lives in `tests/test_account_deletion.py`, with the purge job that
# finishes what it starts - the endpoint's only lasting effect is state that job acts on.


class TestReadCurrentUser:
    """Test current-user retrieval endpoint."""

    @pytest.mark.asyncio
    async def test_read_current_user_success(self, current_user_dict):
        """`GET /user` just returns whatever `get_current_user` resolved from the token."""
        result = await read_current_user(Request({"type": "http"}), current_user_dict)

        assert result == current_user_dict


class TestPatchUser:
    """Test user update endpoint."""

    @pytest.mark.asyncio
    async def test_patch_user_success(self, mock_db, current_user_dict):
        """Test successful user update - always operates on the caller's own account."""
        user_update = UserUpdate(name="New Name")

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.exists = AsyncMock(return_value=False)
            mock_crud.update = AsyncMock(return_value=None)

            result = await patch_user(Mock(), user_update, current_user_dict, mock_db)

            assert result == {"message": "User updated"}
            mock_crud.update.assert_called_once_with(
                db=mock_db, object={"name": "New Name"}, uuid=current_user_dict["uuid"]
            )

    @pytest.mark.asyncio
    async def test_patch_user_duplicate_username(self, mock_db, current_user_dict):
        """Test user update when the requested username is already taken."""
        user_update = UserUpdate(username="taken")

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.exists = AsyncMock(return_value=True)

            from src.app.core.exceptions.http_exceptions import DuplicateValueException

            with pytest.raises(DuplicateValueException):
                await patch_user(Mock(), user_update, current_user_dict, mock_db)

    @pytest.mark.asyncio
    async def test_a_name_claimed_between_the_check_and_the_write_is_a_duplicate(self, mock_db, current_user_dict):
        """Two divers claiming the same free username both pass `exists`; the unique index
        decides, and the loser gets the same answer the check would have given."""
        mock_db.rollback = AsyncMock()

        with (
            patch("src.app.api.v1.users.enforce_rate_limit", new_callable=AsyncMock),
            patch("src.app.api.v1.users.crud_users") as mock_crud,
        ):
            mock_crud.exists = AsyncMock(return_value=False)
            mock_crud.update = AsyncMock(side_effect=IntegrityError("update", {}, Exception("duplicate key")))

            with pytest.raises(DuplicateValueException, match="Username not available"):
                await patch_user(Mock(), UserUpdate(username="raced"), current_user_dict, mock_db)

        mock_db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_conflict_with_no_username_claimed_is_not_reported_as_one(self, mock_db, current_user_dict):
        mock_db.rollback = AsyncMock()
        values = UserUpdate.model_validate({"dive_form_preset_uuid": str(uuid7())})

        with (
            patch("src.app.api.v1.users.crud_users") as mock_crud,
            patch("src.app.api.v1.users.crud_dive_form_presets") as presets,
        ):
            presets.exists = AsyncMock(return_value=True)
            mock_crud.update = AsyncMock(side_effect=IntegrityError("update", {}, Exception("foreign key")))

            with pytest.raises(IntegrityError):
                await patch_user(Mock(), values, current_user_dict, mock_db)

        mock_db.rollback.assert_awaited_once()


class TestIsSuperuserOnTheCallersOwnRecord:
    """`UserRead` carries `is_superuser` so a client can decide whether to offer the
    operator's surface at all.

    Not a disclosure about anybody else: `GET /user` returns the caller's own row, and of
    another account no route returns more than the exceptions `DECISIONS.md` names under
    *Current-user routes live at a bare `/user`* - never this field. It is also not the
    gate - the `/api/v1/admin/*` routes take `get_current_superuser`, and a client
    that lied about this field to itself would still be refused there.
    """

    def test_it_defaults_to_false(self):
        """A row read back without the key must still answer, and the safe answer is "not an
        operator"."""
        values = UserRead.model_validate(
            {"uuid": uuid7(), "name": "Ada Lovelace", "username": "ada", "email": "ada@example.com"}
        )

        assert values.is_superuser is False

    def test_it_is_published_when_the_row_carries_it(self):
        values = UserRead.model_validate(
            {
                "uuid": uuid7(),
                "name": "Ada Lovelace",
                "username": "ada",
                "email": "ada@example.com",
                "is_superuser": True,
            }
        )

        assert values.is_superuser is True

    def test_patch_user_cannot_set_it(self):
        """`UserUpdate` is `extra="forbid"`, so an attempt to grant yourself the operator's
        surface is a 422 naming the field rather than a silently ignored no-op. The panel's
        `UserAdminUpdate` cannot set it either - promotion is SQL or the bootstrap."""
        with pytest.raises(ValidationError):
            UserUpdate(is_superuser=True)


class TestUnitsPreference:
    """`unit_preferences` - the account's system for each measurement family.

    Nothing the API serves is converted by it (see DECISIONS.md's *"Measurements are
    metric in the database and on the wire; `unit_preferences` is who's looking"*), so the
    server-side surface is the schemas these cover and the write `PATCH /user` makes.
    The merge itself runs in Postgres, and `test_check_in_links.py` holds it there.

    `test_update_explicit_nulls.py` covers the `NON_NULLABLE_FIELDS` half structurally,
    off the SQLAlchemy metadata; the null cases here are the same guard seen from the
    caller's side.
    """

    def test_an_account_read_without_the_record_reads_as_metric_everywhere(self):
        values = UserRead.model_validate(
            {"uuid": uuid7(), "name": "Ada Lovelace", "username": "ada", "email": "ada@example.com"}
        )

        assert values.unit_preferences.model_dump() == dict.fromkeys(UNIT_FAMILIES, "metric")
        assert values.model_dump()["units"] == "metric"

    def test_a_family_the_stored_record_lacks_reads_as_metric_and_units_follows_depth(self):
        values = UserRead.model_validate(
            {
                "uuid": uuid7(),
                "name": "Ada Lovelace",
                "username": "ada",
                "email": "ada@example.com",
                "unit_preferences": {"depth": "imperial", "pressure": "fathoms"},
            }
        )

        assert values.model_dump(include={"units", "unit_preferences"}) == {
            "units": "imperial",
            "unit_preferences": {
                "depth": "imperial",
                "pressure": "fathoms",
                "volume": "metric",
                "weight": "metric",
                "temperature": "metric",
            },
        }

    @pytest.mark.parametrize(
        "value", [None, {"depth": "cubits"}, {"unknown": "metric"}, {"pressure": None}, [], "imperial"]
    )
    def test_an_invalid_record_is_rejected(self, value):
        with pytest.raises(ValidationError):
            UserUpdate.model_validate({"unit_preferences": value})

    @pytest.mark.parametrize("value", [None, "cubits"])
    def test_an_invalid_units_is_rejected(self, value):
        with pytest.raises(ValidationError) as exc_info:
            UserUpdate.model_validate({"units": value})

        assert "units" in str(exc_info.value)

    def test_the_families_named_are_merged_into_the_stored_record(self):
        written, families = _unit_write(
            UserUpdate.model_validate({"unit_preferences": {"pressure": "imperial"}, "name": "Ada Lovelace"})
        )

        assert written == {"name": "Ada Lovelace"}
        assert families == {"pressure": "imperial"}

    def test_units_sets_every_family_before_the_body_s_own(self):
        _, families = _unit_write(
            UserUpdate.model_validate({"units": "imperial", "unit_preferences": {"temperature": "metric"}})
        )

        assert families == dict.fromkeys(UNIT_FAMILIES, "imperial") | {"temperature": "metric"}

    def test_an_empty_record_writes_nothing(self):
        assert account_update_values(UserUpdate.model_validate({"unit_preferences": {}})) == {}

    @pytest.mark.asyncio
    async def test_patch_user_writes_the_merge(self, mock_db, current_user_dict):
        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.update = AsyncMock(return_value=None)

            result = await patch_user(
                Mock(), UserUpdate.model_validate({"units": "imperial"}), current_user_dict, mock_db
            )

        assert result == {"message": "User updated"}
        written = mock_crud.update.call_args.kwargs["object"]
        assert set(written) == {"unit_preferences"}


def _unit_write(values: UserUpdate) -> tuple[dict, dict]:
    """The column values `PATCH /user` writes, with the families its merge binds."""
    written = account_update_values(values)
    compiled = written.pop("unit_preferences").compile(dialect=postgresql.dialect())
    assert "||" in str(compiled)
    [families] = compiled.params.values()
    return written, families
