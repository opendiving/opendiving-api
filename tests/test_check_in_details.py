"""The check-in details - what a dive shop's desk asks for, held once on the account.

Eight nullable columns and nothing else on this side: no route of their own, no model of
their own, no conversion. So what is worth pinning is the shape. `CHECK_IN_FIELDS` is the
list the model, `UserRead` and `UserUpdate` are each checked against here, because all
three spell the fields out and one added to a single declaration is invisible until a
client asks for it. The widths are checked against DiveJSON's own schema, since an import
writes whatever a conforming document carries.

`test_update_explicit_nulls.py` covers the explicit-null half structurally, off the
SQLAlchemy metadata. The contact-clearing case below is the same guard at the wire, which
is where the difference between a 200 and a 500 lives - and clearing a contact is the
operation the columns are nullable for.
"""

import uuid as uuid_pkg
from collections.abc import Generator, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

import divejson
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import String

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.api.v1 import users as users_module
from src.app.core.config import settings
from src.app.core.setup import create_application
from src.app.models.user import User
from src.app.schemas.user import CHECK_IN_FIELDS, UserRead, UserUpdate

OWNER = {
    "id": 7,
    "uuid": uuid_pkg.uuid4(),
    "name": "Ada Lovelace",
    "username": "ada",
    "email": "ada@example.com",
    "is_superuser": False,
}

# One filled-in account, every detail set. `test_the_filled_account_covers_every_field`
# keeps it in step with `CHECK_IN_FIELDS` rather than trusting it.
FILLED: dict[str, Any] = {
    "date_of_birth": date(1988, 4, 12),
    "phone": "+20 100 123 4567",
    "emergency_contact_name": "Grace Hopper",
    "emergency_contact_phone": "+1 202 555 0143",
    "emergency_contact_relationship": "Partner",
    "insurance_provider": "DAN Europe",
    "insurance_policy_number": "DE-4471902",
    "insurance_expires_on": date(2027, 6, 30),
}


@pytest.fixture(scope="module")
def check_in_app() -> Any:
    """Its own app with `apply_migrations_on_start=False`, as in `test_ownership.py` - the
    shared `client` fixture opens one whose startup hook connects to Postgres, and nothing
    here needs a database.
    """
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@contextmanager
def _signed_in(app: Any, **columns: Any) -> Iterator[TestClient]:
    """A client for a caller whose row is `OWNER` plus whatever columns the test names.

    `GET /user` answers with exactly what `get_current_user` resolved, which is every
    mapped column of the row - so "what does a filled-in account read back as" is a
    question about this dict rather than about a query.
    """
    app.dependency_overrides[get_current_user] = lambda: {**OWNER, **columns}
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides = {}


@pytest.fixture
def signed_in_client(check_in_app: Any) -> Generator[TestClient]:
    with _signed_in(check_in_app) as test_client:
        yield test_client


class TestTheColumnsAndTheSchemasAgree:
    """The drift guard. Three declarations, one list."""

    def test_the_filled_account_covers_every_field(self) -> None:
        assert set(FILLED) == set(CHECK_IN_FIELDS)

    @pytest.mark.parametrize("field", CHECK_IN_FIELDS)
    def test_the_column_is_nullable_and_carries_no_server_default(self, field: str) -> None:
        """Nullable is the whole design: an absent detail is the ordinary state, and an
        explicit `null` is how a diver removes one. A `server_default` would be the pair a
        `NOT NULL` column needs to be added over existing rows, and none of these is one.
        """
        column = User.__table__.columns[field]

        assert column.nullable
        assert column.server_default is None

    @pytest.mark.parametrize("field", CHECK_IN_FIELDS)
    def test_both_schemas_declare_it_and_neither_guards_it(self, field: str) -> None:
        """`UserUpdate` is `extra="forbid"`, so a field missing from it is a 422 for the
        settings card rather than a silent no-op - and a field listed in
        `NON_NULLABLE_FIELDS` could never be cleared.
        """
        assert field in UserRead.model_fields
        assert field in UserUpdate.model_fields
        assert field not in UserUpdate.NON_NULLABLE_FIELDS


# Each bounded column, and the member DiveJSON §6.1 carries it as: `$defs` name, property.
FORMAT_MEMBERS: dict[str, tuple[str, str]] = {
    "phone": ("diver", "phone"),
    "emergency_contact_name": ("emergency_contact", "name"),
    "emergency_contact_phone": ("emergency_contact", "phone"),
    "emergency_contact_relationship": ("emergency_contact", "relationship"),
    "insurance_provider": ("insurance", "provider"),
    "insurance_policy_number": ("insurance", "number"),
}


def _width(column: str) -> int:
    column_type = User.__table__.columns[column].type
    assert isinstance(column_type, String) and column_type.length is not None
    return column_type.length


class TestTheWidthsAreTheFormats:
    """An import writes what a conforming document carries, so a column narrower than the
    member it travels as would refuse a value the document is entitled to hold, and one
    wider would let the settings form save what the export then cannot write."""

    @pytest.mark.parametrize("column", sorted(FORMAT_MEMBERS))
    def test_the_column_is_as_wide_as_the_member(self, column: str) -> None:
        definition, member = FORMAT_MEMBERS[column]
        bound = divejson.load_schema()["$defs"][definition]["properties"][member]["maxLength"]

        assert _width(column) == bound

    @pytest.mark.parametrize("column", sorted(FORMAT_MEMBERS))
    def test_the_update_schema_takes_exactly_the_column_s_width(self, column: str) -> None:
        width = _width(column)

        UserUpdate.model_validate({column: "x" * width})
        with pytest.raises(ValidationError) as exc_info:
            UserUpdate.model_validate({column: "x" * (width + 1)})

        assert column in str(exc_info.value)


class TestReadingThem:
    def test_an_account_that_filled_in_nothing_reads_them_all_as_null(self, signed_in_client: TestClient) -> None:
        body = signed_in_client.get("/api/v1/user").json()

        assert {field: body[field] for field in CHECK_IN_FIELDS} == dict.fromkeys(CHECK_IN_FIELDS)

    def test_a_filled_in_account_reads_every_detail_back(self, check_in_app: Any) -> None:
        with _signed_in(check_in_app, **FILLED) as client:
            body = client.get("/api/v1/user").json()

        assert {field: body[field] for field in CHECK_IN_FIELDS} == {
            field: value.isoformat() if isinstance(value, date) else value for field, value in FILLED.items()
        }

    def test_a_row_without_the_columns_still_validates(self) -> None:
        """The same reasoning as `units` beside them: `get_current_user` validates the whole
        row through `UserRead` on every authenticated request, so a row read back without
        these keys has to answer rather than 500 the account.
        """
        values = UserRead.model_validate(
            {"uuid": uuid_pkg.uuid4(), "name": "Ada Lovelace", "username": "ada", "email": "ada@example.com"}
        )

        assert all(getattr(values, field) is None for field in CHECK_IN_FIELDS)


class TestWritingThem:
    def test_clearing_the_emergency_contact_is_a_200(
        self, signed_in_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Three fields cleared together, which is what "remove my emergency contact"
        means. The columns are nullable for exactly this, so the null has to reach the
        UPDATE rather than be refused on the way in.
        """
        update = AsyncMock()
        monkeypatch.setattr(users_module.crud_users, "update", update)

        response = signed_in_client.patch(
            "/api/v1/user",
            json={
                "emergency_contact_name": None,
                "emergency_contact_phone": None,
                "emergency_contact_relationship": None,
            },
        )

        assert response.status_code == 200
        written = update.await_args_list[0].kwargs["object"]
        assert written.model_dump(exclude_unset=True) == {
            "emergency_contact_name": None,
            "emergency_contact_phone": None,
            "emergency_contact_relationship": None,
        }

    def test_every_detail_saves_in_one_patch(
        self, signed_in_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        update = AsyncMock()
        monkeypatch.setattr(users_module.crud_users, "update", update)

        response = signed_in_client.patch(
            "/api/v1/user",
            json={field: value.isoformat() if isinstance(value, date) else value for field, value in FILLED.items()},
        )

        assert response.status_code == 200
        written = update.await_args_list[0].kwargs["object"]
        assert written.model_dump(exclude_unset=True) == FILLED

    def test_a_birth_date_in_the_future_is_a_422(
        self, signed_in_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mistyped year, refused before it reaches a dive shop as a diver who is not
        born yet."""
        update = AsyncMock()
        monkeypatch.setattr(users_module.crud_users, "update", update)
        tomorrow = datetime.now(UTC).date() + timedelta(days=1)

        response = signed_in_client.patch("/api/v1/user", json={"date_of_birth": tomorrow.isoformat()})

        assert response.status_code == 422
        assert "date_of_birth" in str(response.json()["detail"])
        update.assert_not_awaited()

    def test_today_is_accepted(self) -> None:
        """The boundary, and deliberately inclusive - a newborn is not this rule's problem,
        and an off-by-one here would refuse a legitimate value on one day of the year."""
        values = UserUpdate(date_of_birth=datetime.now(UTC).date())

        assert values.date_of_birth == datetime.now(UTC).date()

    def test_an_insurance_expiry_that_is_not_a_date_is_a_422(
        self, signed_in_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        update = AsyncMock()
        monkeypatch.setattr(users_module.crud_users, "update", update)

        response = signed_in_client.patch("/api/v1/user", json={"insurance_expires_on": "next summer"})

        assert response.status_code == 422
        update.assert_not_awaited()


class TestTheAnchor:
    """A contact nobody is named in, or a policy number with no insurer, is not a detail the
    format admits, so `PATCH /user` refuses to leave one behind. Checked against the row as
    it will be, and only when the patch touches that object's fields."""

    def _patch(self, app: Any, monkeypatch: pytest.MonkeyPatch, body: dict[str, Any], **row: Any) -> Any:
        update = AsyncMock()
        monkeypatch.setattr(users_module.crud_users, "update", update)
        with _signed_in(app, **{**dict.fromkeys(CHECK_IN_FIELDS), **row}) as client:
            response = client.patch("/api/v1/user", json=body)
        return response, update

    def test_a_contact_phone_without_a_name_is_a_422_naming_the_name(
        self, check_in_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response, update = self._patch(check_in_app, monkeypatch, {"emergency_contact_phone": "+1 202 555 0143"})

        assert response.status_code == 422
        assert [error["loc"] for error in response.json()["detail"]] == [["body", "emergency_contact_name"]]
        update.assert_not_awaited()

    def test_clearing_the_name_while_the_phone_stands_is_a_422(
        self, check_in_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response, update = self._patch(
            check_in_app,
            monkeypatch,
            {"emergency_contact_name": None},
            emergency_contact_name="Grace Hopper",
            emergency_contact_phone="+1 202 555 0143",
        )

        assert response.status_code == 422
        update.assert_not_awaited()

    def test_a_blank_name_is_no_name(self, check_in_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        response, _ = self._patch(
            check_in_app, monkeypatch, {"emergency_contact_name": "  ", "emergency_contact_relationship": "Partner"}
        )

        assert response.status_code == 422

    def test_a_policy_number_without_a_provider_is_a_422_naming_the_provider(
        self, check_in_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response, _ = self._patch(check_in_app, monkeypatch, {"insurance_policy_number": "DE-4471902"})

        assert response.status_code == 422
        assert [error["loc"] for error in response.json()["detail"]] == [["body", "insurance_provider"]]

    def test_an_expiry_without_a_provider_is_a_422(self, check_in_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        response, _ = self._patch(check_in_app, monkeypatch, {"insurance_expires_on": "2027-06-30"})

        assert response.status_code == 422

    def test_a_phone_beside_a_stored_name_saves(self, check_in_app: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        response, update = self._patch(
            check_in_app,
            monkeypatch,
            {"emergency_contact_phone": "+1 202 555 0143"},
            emergency_contact_name="Grace Hopper",
        )

        assert response.status_code == 200
        update.assert_awaited_once()

    def test_clearing_the_whole_contact_from_an_anchorless_row_saves(
        self, check_in_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        response, _ = self._patch(
            check_in_app,
            monkeypatch,
            {"emergency_contact_phone": None},
            emergency_contact_phone="+1 202 555 0143",
        )

        assert response.status_code == 200

    def test_an_unrelated_save_on_an_anchorless_row_is_untouched(
        self, check_in_app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A row saved before the rule existed can hold a phone and no name. Refusing every
        later save until the diver fixes it would lock the settings page for a units toggle."""
        response, _ = self._patch(
            check_in_app,
            monkeypatch,
            {"units": "imperial"},
            emergency_contact_phone="+1 202 555 0143",
            insurance_policy_number="DE-4471902",
        )

        assert response.status_code == 200
