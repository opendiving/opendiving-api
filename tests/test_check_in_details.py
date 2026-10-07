"""The check-in details - what a dive shop's desk asks for, held once as their own object.

`GET`/`PATCH /user/checkin-details`. What is pinned is the object's invariants: an account
that saved nothing reads as the empty object; a key present replaces its member whole and a
key absent leaves it; lists come back in the order sent; a blank anchor is a 422 naming the row
and a blank optional member reads back null; the sign-in address is never the object's email;
and saving the policies keeps a reminder already sent for an unchanged policy. The widths are
checked against DiveJSON's own schema, since an import writes whatever a conforming document
carries.
"""

import uuid as uuid_pkg
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import divejson
import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import String, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.setup import create_application
from src.app.crud.crud_users import read_account
from src.app.models.checkin_details import CheckinDetails, CheckinEmergencyContact, CheckinInsurancePolicy
from src.app.models.user import User
from src.app.schemas.checkin_details import (
    MAX_EMERGENCY_CONTACTS,
    MAX_INSURANCE_POLICIES,
    CheckinDetailsRead,
    CheckinDetailsUpdate,
)
from src.app.schemas.user import UserRead, UserUpdate
from tests.conftest import db_available
from tests.helpers.generators import create_user

# The eight members `user` used to carry, none of which the account reads or takes any more.
FORMER_USER_COLUMNS = (
    "date_of_birth",
    "phone",
    "emergency_contact_name",
    "emergency_contact_phone",
    "emergency_contact_relationship",
    "insurance_provider",
    "insurance_policy_number",
    "insurance_expires_on",
)

FILLED: dict[str, Any] = {
    "email": "desk@example.org",
    "phone": "+20 100 123 4567",
    "date_of_birth": "1988-04-12",
    "emergency_contacts": [
        {"name": "Grace Hopper", "phone": "+1 202 555 0143", "relationship": "Partner"},
        {"name": "Alan Turing", "phone": None, "relationship": "Father"},
    ],
    "insurance_policies": [
        {"provider": "DAN Europe", "number": "DE-4471902", "expires_on": "2027-06-30"},
        {"provider": "DiveAssure", "number": None, "expires_on": None},
    ],
}


@pytest.fixture(scope="module")
def check_in_app() -> Any:
    """Its own app with `apply_migrations_on_start=False`: the shared `client` fixture's
    startup connects to Postgres, and the refusals below need none."""
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@pytest.fixture
def signed_in_client(check_in_app: Any) -> Generator[TestClient]:
    check_in_app.dependency_overrides[get_current_user] = lambda: {"id": 7, "uuid": uuid_pkg.uuid4()}
    try:
        with TestClient(check_in_app) as test_client:
            yield test_client
    finally:
        check_in_app.dependency_overrides = {}


class TestTheAccountNoLongerCarriesThem:
    @pytest.mark.parametrize("field", FORMER_USER_COLUMNS)
    def test_neither_the_row_nor_its_schemas_name_it(self, field: str) -> None:
        """`get_current_user` selects every mapped column, so what stays on `user` rides every
        signed-in request; and `PATCH /user` is `extra="forbid"`, so a client still sending one
        gets a 422 rather than a silent no-op."""
        assert field not in User.__table__.columns
        assert field not in UserRead.model_fields
        assert field not in UserUpdate.model_fields

    def test_the_reminder_pair_left_with_them(self) -> None:
        assert not {"insurance_notified_stage", "insurance_notified_for"} & set(User.__table__.columns.keys())


# Each bounded column, and the member DiveJSON §6.1 carries it as: `$defs` name, property.
FORMAT_MEMBERS: dict[tuple[str, str], tuple[str, str]] = {
    ("checkin_details", "email"): ("diver", "email"),
    ("checkin_details", "phone"): ("diver", "phone"),
    ("checkin_emergency_contact", "name"): ("emergency_contact", "name"),
    ("checkin_emergency_contact", "phone"): ("emergency_contact", "phone"),
    ("checkin_emergency_contact", "relationship"): ("emergency_contact", "relationship"),
    ("checkin_insurance_policy", "provider"): ("insurance", "provider"),
    ("checkin_insurance_policy", "number"): ("insurance", "number"),
}
_TABLES: dict[str, Any] = {
    "checkin_details": CheckinDetails.__table__,
    "checkin_emergency_contact": CheckinEmergencyContact.__table__,
    "checkin_insurance_policy": CheckinInsurancePolicy.__table__,
}


def _width(table: str, column: str) -> int:
    column_type = _TABLES[table].columns[column].type
    assert isinstance(column_type, String) and column_type.length is not None
    return column_type.length


def _body(table: str, column: str, value: str) -> dict[str, Any]:
    """A body carrying `value` in `column`'s member, beside whatever its row requires."""
    if table == "checkin_emergency_contact":
        return {"emergency_contacts": [{"name": "Ann", column: value}]}
    if table == "checkin_insurance_policy":
        return {"insurance_policies": [{"provider": "DAN", column: value}]}
    return {column: value}


class TestTheWidthsAreTheFormats:
    """An import writes what a conforming document carries, so a column narrower than the
    member it travels as would refuse a value the document is entitled to hold, and one
    wider would let the settings form save what the export then cannot write."""

    @pytest.mark.parametrize(("table", "column"), sorted(FORMAT_MEMBERS))
    def test_the_column_is_as_wide_as_the_member(self, table: str, column: str) -> None:
        definition, member = FORMAT_MEMBERS[table, column]
        bound = divejson.load_schema()["$defs"][definition]["properties"][member]["maxLength"]

        assert _width(table, column) == bound

    @pytest.mark.parametrize(("table", "column"), sorted(key for key in FORMAT_MEMBERS if key[1] != "email"))
    def test_the_update_schema_takes_exactly_the_column_s_width(self, table: str, column: str) -> None:
        width = _width(table, column)

        CheckinDetailsUpdate.model_validate(_body(table, column, "x" * width))
        with pytest.raises(ValidationError) as exc_info:
            CheckinDetailsUpdate.model_validate(_body(table, column, "x" * (width + 1)))

        assert column in str(exc_info.value)

    def test_an_email_past_the_column_is_refused(self) -> None:
        local = "x" * 64
        CheckinDetailsUpdate.model_validate({"email": f"{local}@example.org"})
        with pytest.raises(ValidationError):
            CheckinDetailsUpdate.model_validate({"email": f"{local}@{'d' * 63}.{'d' * 63}.{'d' * 63}.example"})


class TestTheBody:
    def test_a_key_absent_is_not_set(self) -> None:
        assert CheckinDetailsUpdate.model_validate({"phone": "+1"}).model_fields_set == {"phone"}

    @pytest.mark.parametrize("member", ["email", "phone", "date_of_birth"])
    def test_a_scalar_takes_a_null(self, member: str) -> None:
        values = CheckinDetailsUpdate.model_validate({member: None})

        assert member in values.model_fields_set
        assert getattr(values, member) is None

    @pytest.mark.parametrize("member", ["emergency_contacts", "insurance_policies"])
    def test_a_list_is_cleared_with_an_empty_one_and_refuses_a_null(self, member: str) -> None:
        assert getattr(CheckinDetailsUpdate.model_validate({member: []}), member) == []
        with pytest.raises(ValidationError, match="cannot be null"):
            CheckinDetailsUpdate.model_validate({member: None})

    def test_an_optional_text_sent_blank_is_null_and_one_with_padding_is_trimmed(self) -> None:
        values = CheckinDetailsUpdate.model_validate(
            {
                "email": "  ",
                "phone": " +1 202 ",
                "emergency_contacts": [{"name": " Grace ", "relationship": "  "}],
                "insurance_policies": [{"provider": "DAN", "number": ""}],
            }
        )

        assert values.email is None
        assert values.phone == "+1 202"
        assert values.emergency_contacts is not None and values.insurance_policies is not None
        assert values.emergency_contacts[0].model_dump() == {"name": "Grace", "phone": None, "relationship": None}
        assert values.insurance_policies[0].number is None

    def test_an_email_is_an_address(self) -> None:
        with pytest.raises(ValidationError):
            CheckinDetailsUpdate.model_validate({"email": "not an address"})

    def test_today_is_a_date_of_birth(self) -> None:
        today = datetime.now(UTC).date()

        assert CheckinDetailsUpdate.model_validate({"date_of_birth": today.isoformat()}).date_of_birth == today


class TestTheRefusals:
    """At the wire, where a client reads the `loc`. None of these reaches the database."""

    @pytest.mark.parametrize(
        ("body", "loc"),
        [
            ({"emergency_contacts": [{"name": "  ", "phone": "+1"}]}, ["body", "emergency_contacts", 0, "name"]),
            (
                {"insurance_policies": [{"provider": "DAN"}, {"provider": "", "number": "X"}]},
                ["body", "insurance_policies", 1, "provider"],
            ),
            ({"emergency_contacts": [{"phone": "+1"}]}, ["body", "emergency_contacts", 0, "name"]),
            ({"emergency_contacts": [{"name": "A"}] * (MAX_EMERGENCY_CONTACTS + 1)}, ["body", "emergency_contacts"]),
            (
                {"insurance_policies": [{"provider": "A"}] * (MAX_INSURANCE_POLICIES + 1)},
                ["body", "insurance_policies"],
            ),
            ({"date_of_birth": (datetime.now(UTC).date() + timedelta(days=2)).isoformat()}, ["body", "date_of_birth"]),
            ({"insurance_policies": [{"provider": "DAN", "expires_on": "soon"}]}, None),
            ({"emergency_contact_name": "Grace"}, ["body", "emergency_contact_name"]),
        ],
    )
    def test_it_is_a_422_naming_the_member(
        self, signed_in_client: TestClient, body: dict[str, Any], loc: list[Any] | None
    ) -> None:
        response = signed_in_client.patch("/api/v1/user/checkin-details", json=body)

        assert response.status_code == 422
        if loc is not None:
            assert loc in [error["loc"] for error in response.json()["detail"]]

    def test_the_old_keys_are_a_422_on_patch_user(self, signed_in_client: TestClient) -> None:
        response = signed_in_client.patch("/api/v1/user", json={"date_of_birth": "1988-04-12"})

        assert response.status_code == 422


class TestTheReadShape:
    def test_an_account_with_nothing_saved_is_the_empty_object(self) -> None:
        assert CheckinDetailsRead().model_dump() == {
            "email": None,
            "phone": None,
            "date_of_birth": None,
            "emergency_contacts": [],
            "insurance_policies": [],
        }


# -------------- against Postgres --------------


@pytest_asyncio.fixture
async def http(check_in_app: Any, async_db: AsyncSession) -> AsyncGenerator[httpx.AsyncClient]:
    async def the_tests_session() -> AsyncGenerator[AsyncSession]:
        yield async_db

    check_in_app.dependency_overrides[async_get_db] = the_tests_session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=check_in_app), base_url="http://test") as client:
        yield client
    check_in_app.dependency_overrides = {}


@pytest_asyncio.fixture
async def diver(db: Session, check_in_app: Any, async_db: AsyncSession) -> User:
    """A fresh account, signed in."""
    user = create_user(db)
    account = await read_account(async_db, uuid=user.uuid)
    check_in_app.dependency_overrides[get_current_user] = lambda: account
    return user


async def _patch(http: httpx.AsyncClient, body: dict[str, Any]) -> dict[str, Any]:
    response = await http.patch("/api/v1/user/checkin-details", json=body)
    assert response.status_code == 200, response.text
    result: dict[str, Any] = response.json()
    return result


def _pairs(db: Session, diver: User) -> list[tuple[Any, ...]]:
    db.expire_all()
    rows = db.execute(
        select(
            CheckinInsurancePolicy.provider, CheckinInsurancePolicy.notified_stage, CheckinInsurancePolicy.notified_for
        )
        .where(CheckinInsurancePolicy.user_id == diver.id)
        .order_by(CheckinInsurancePolicy.position)
    ).all()
    return [tuple(row) for row in rows]


def _mark_every_policy_sent(db: Session, diver: User) -> None:
    """What `send_renewal_reminders` leaves on a policy it emailed about: its stage and date."""
    db.execute(
        update(CheckinInsurancePolicy)
        .where(CheckinInsurancePolicy.user_id == diver.id)
        .values(notified_stage="expiring_soon", notified_for=CheckinInsurancePolicy.expires_on)
    )
    db.commit()


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestAgainstPostgres:
    @pytest.mark.asyncio
    async def test_a_fresh_account_reads_the_empty_object_without_its_sign_in_address(
        self, http: httpx.AsyncClient, diver: User
    ) -> None:
        response = await http.get("/api/v1/user/checkin-details")

        assert response.status_code == 200
        assert response.json() == CheckinDetailsRead().model_dump(mode="json")
        assert diver.email not in response.text

    @pytest.mark.asyncio
    async def test_every_member_reads_back_in_the_order_sent(self, http: httpx.AsyncClient, diver: User) -> None:
        written = await _patch(http, FILLED)

        assert written == FILLED
        assert (await http.get("/api/v1/user/checkin-details")).json() == FILLED

    @pytest.mark.asyncio
    async def test_a_list_sent_alone_replaces_that_list_and_nothing_else(
        self, http: httpx.AsyncClient, diver: User
    ) -> None:
        await _patch(http, FILLED)
        first, second = FILLED["emergency_contacts"]

        after = await _patch(http, {"emergency_contacts": [second, first]})

        assert after == FILLED | {"emergency_contacts": [second, first]}

    @pytest.mark.asyncio
    async def test_an_empty_list_clears_it_and_a_null_clears_a_scalar(
        self, http: httpx.AsyncClient, diver: User
    ) -> None:
        await _patch(http, FILLED)

        after = await _patch(http, {"insurance_policies": [], "email": None})

        assert after == FILLED | {"insurance_policies": [], "email": None}

    @pytest.mark.asyncio
    async def test_a_blank_optional_member_reads_back_null(self, http: httpx.AsyncClient, diver: User) -> None:
        after = await _patch(http, {"phone": "  ", "emergency_contacts": [{"name": "Grace", "relationship": "  "}]})

        assert after["phone"] is None
        assert after["emergency_contacts"] == [{"name": "Grace", "phone": None, "relationship": None}]

    @pytest.mark.asyncio
    async def test_get_user_carries_none_of_them(self, http: httpx.AsyncClient, diver: User) -> None:
        await _patch(http, FILLED)

        body = (await http.get("/api/v1/user")).json()

        assert not set(FORMER_USER_COLUMNS) & body.keys()
        assert body["email"] == diver.email

    @pytest.mark.asyncio
    async def test_a_policy_saved_unchanged_keeps_its_reminder_and_a_new_expiry_rearms_it(
        self, db: Session, http: httpx.AsyncClient, diver: User
    ) -> None:
        await _patch(http, FILLED)
        _mark_every_policy_sent(db, diver)
        dan, assure = FILLED["insurance_policies"]

        # Reordered, the first one's number corrected: the same two policies.
        await _patch(http, {"insurance_policies": [assure, dan | {"number": "DE-4471903"}]})

        assert _pairs(db, diver) == [
            ("DiveAssure", "expiring_soon", None),
            ("DAN Europe", "expiring_soon", date(2027, 6, 30)),
        ]

        await _patch(http, {"insurance_policies": [assure, dan | {"expires_on": "2028-06-30"}]})

        assert _pairs(db, diver) == [("DiveAssure", "expiring_soon", None), ("DAN Europe", None, None)]

    @pytest.mark.asyncio
    async def test_a_save_of_another_member_leaves_the_policies_rows_alone(
        self, db: Session, http: httpx.AsyncClient, diver: User
    ) -> None:
        await _patch(http, FILLED)
        _mark_every_policy_sent(db, diver)

        await _patch(http, {"email": "front@example.org"})

        assert [stage for _, stage, _ in _pairs(db, diver)] == ["expiring_soon", "expiring_soon"]

    @pytest.mark.asyncio
    async def test_one_diver_s_write_leaves_another_s_object_alone(
        self, db: Session, check_in_app: Any, async_db: AsyncSession, http: httpx.AsyncClient, diver: User
    ) -> None:
        await _patch(http, FILLED)
        other = create_user(db)
        account = await read_account(async_db, uuid=other.uuid)
        check_in_app.dependency_overrides[get_current_user] = lambda: account

        await _patch(http, {"emergency_contacts": [], "insurance_policies": [], "phone": None})

        db.expire_all()
        assert (
            db.execute(select(CheckinDetails.phone).where(CheckinDetails.user_id == diver.id)).scalar_one()
            == (FILLED["phone"])
        )
        assert len(_pairs(db, diver)) == 2
