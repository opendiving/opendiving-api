"""Unit tests for user API endpoints."""

from unittest.mock import AsyncMock, Mock, patch

import pytest
from pydantic import ValidationError
from uuid6 import uuid7

from src.app.api.v1.users import patch_user, read_current_user
from src.app.schemas.user import UnitSystem, UserRead, UserUpdate

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
        result = await read_current_user(Mock(), current_user_dict)

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
            mock_crud.update.assert_called_once_with(db=mock_db, object=user_update, uuid=current_user_dict["uuid"])

    @pytest.mark.asyncio
    async def test_patch_user_duplicate_username(self, mock_db, current_user_dict):
        """Test user update when the requested username is already taken."""
        user_update = UserUpdate(username="taken")

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.exists = AsyncMock(return_value=True)

            from src.app.core.exceptions.http_exceptions import DuplicateValueException

            with pytest.raises(DuplicateValueException):
                await patch_user(Mock(), user_update, current_user_dict, mock_db)


class TestIsSuperuserOnTheCallersOwnRecord:
    """`UserRead` carries `is_superuser` so a client can decide whether to offer the
    operator's surface at all.

    Not a disclosure about anybody else: `GET /user` returns the caller's own row, and no
    route in this app returns another account's. It is also not the gate - the three
    `/api/v1/admin/*` routes take `get_current_superuser`, and a client that lied about this
    field to itself would still be refused there.
    """

    def test_it_defaults_to_false(self):
        """The same reasoning as `units` beside it: a row read back without the key must
        still answer, and the safe answer is "not an operator"."""
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
    """`units` - the account-level metric-or-imperial toggle.

    Nothing the API serves is converted by it (see DECISIONS.md's *"Measurements are
    metric in the database and on the wire; `units` is who's looking"*), so the whole
    server-side surface is the two schemas these cover: it is read on `GET /user` and
    written on `PATCH /user`, and that is all.

    `test_update_explicit_nulls.py` covers the `NON_NULLABLE_FIELDS` half structurally,
    off the SQLAlchemy metadata; the null case here is the same guard seen from the
    caller's side.
    """

    def test_an_untouched_account_reads_as_metric(self):
        """The Python-side default, and the reason it exists: a row read back from a
        database where the hand-written `ALTER TABLE` hasn't run yet has no `units` key
        at all, and `GET /user` still has to answer.
        """
        values = UserRead.model_validate(
            {
                "uuid": uuid7(),
                "name": "Ada Lovelace",
                "username": "ada",
                "email": "ada@example.com",
            }
        )

        assert values.units is UnitSystem.METRIC

    @pytest.mark.asyncio
    async def test_patch_user_saves_the_units_preference(self, mock_db, current_user_dict):
        """The field has to be on `UserUpdate` as well as `UserRead`: the schema is
        `extra="forbid"`, so an omission here would 422 the settings toggle rather than
        save it.
        """
        user_update = UserUpdate(units=UnitSystem.IMPERIAL)

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.exists = AsyncMock(return_value=False)
            mock_crud.update = AsyncMock(return_value=None)

            result = await patch_user(Mock(), user_update, current_user_dict, mock_db)

            assert result == {"message": "User updated"}
            written = mock_crud.update.call_args.kwargs["object"]
            assert written.model_dump(exclude_unset=True) == {"units": UnitSystem.IMPERIAL}

    def test_a_value_outside_the_vocabulary_is_rejected(self):
        """`UnitSystem` is the only place the vocabulary is written down - there is no DB
        `CHECK` mirroring it (the `GearItem.type` decision), so this rejection is the
        whole enforcement.
        """
        with pytest.raises(ValidationError) as exc_info:
            UserUpdate(units="cubits")  # type: ignore[arg-type]

        assert "units" in str(exc_info.value)

    def test_an_explicit_null_is_rejected(self):
        """The column is `NOT NULL`, so `{"units": null}` is a 422 rather than an UPDATE
        that reaches Postgres and comes back a 500 - see `RejectsExplicitNulls`.
        """
        with pytest.raises(ValidationError) as exc_info:
            UserUpdate.model_validate({"units": None})

        assert "cannot be null" in str(exc_info.value)
