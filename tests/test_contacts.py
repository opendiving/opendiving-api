"""Contacts: the record (`models/contact.py`), its routes (`api/v1/contacts.py`), the five
references to it, and the training-center shim on the course and certification writes.

House style per `test_courses.py`: the routes with their collaborators stubbed and the
assertions on what they hand them, plus a Postgres-guarded tail for what only the database
settles - the case-insensitive name index, the address constraint, and the `ON DELETE SET
NULL` on every reference.
"""

from datetime import UTC, date, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1 import certifications as certifications_module
from src.app.api.v1 import contacts as contacts_module
from src.app.api.v1 import courses as courses_module
from src.app.api.v1 import dives as dives_module
from src.app.api.v1 import gear_service as gear_service_module
from src.app.api.v1 import trips as trips_module
from src.app.core.exceptions.http_exceptions import DuplicateValueException, UnprocessableEntityException
from src.app.crud.crud_contacts import (
    ContactRef,
    contact_name_exists,
    crud_contacts,
    get_contact_refs_by_ids,
    resolve_contact_id_for_user,
    resolve_contact_ids_for_user,
    resolve_or_create_contact,
)
from src.app.crud.crud_trip_parts import get_parts_for_trip, get_trip_uuids_staying_at, replace_parts_for_trip
from src.app.models.certification import Certification
from src.app.models.contact import Contact
from src.app.models.course import Course
from src.app.models.dive import Dive
from src.app.models.gear_service_record import GearServiceRecord
from src.app.models.trip_part import TripPart
from src.app.models.user import User
from src.app.schemas.certification import CertificationCreate, CertificationUpdateRequest
from src.app.schemas.contact import (
    ContactCreate,
    ContactReadInternal,
    ContactRole,
    ContactUpdateRequest,
    address_columns,
)
from src.app.schemas.course import CourseCreate, CourseReadInternal, CourseStatus, CourseUpdateRequest
from src.app.schemas.dive import DiveUpdateRequest
from src.app.schemas.gear_service import GearServiceRecordUpdateRequest
from src.app.schemas.trip import TripCreate, TripPartInput
from src.app.services import contact_links
from tests.conftest import db_available
from tests.helpers.generators import (
    create_certification,
    create_contact,
    create_course,
    create_dive,
    create_gear_item,
    create_gear_service_record,
    create_trip,
)

USER_ID = 1
USER_UUID = uuid7()


def _current_user() -> dict[str, Any]:
    return {"id": USER_ID, "uuid": USER_UUID, "username": "ada", "is_superuser": False}


def _internal_contact(**overrides: Any) -> ContactReadInternal:
    values: dict[str, Any] = {
        "id": 5,
        "user_id": USER_ID,
        "uuid": uuid7(),
        "name": "Blue Ocean",
        "roles": ["dive_center"],
        "notes": "",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(overrides)
    return ContactReadInternal(**values)


class TestTheWriteSchema:
    def test_roles_are_a_set_in_vocabulary_order(self) -> None:
        contact = ContactCreate.model_validate(
            {"name": "Blue Ocean", "roles": ["accommodation", "dive_center", "accommodation"]}
        )

        assert contact.roles == [ContactRole.DIVE_CENTER, ContactRole.ACCOMMODATION]

    def test_no_role_at_all_is_a_contact_too(self) -> None:
        assert ContactCreate.model_validate({"name": "Grandma's house"}).roles == []

    def test_a_role_outside_the_vocabulary_is_a_422(self) -> None:
        with pytest.raises(ValidationError):
            ContactCreate.model_validate({"name": "Blue Ocean", "roles": ["resort"]})

    @pytest.mark.parametrize("website", ["https://blueocean.example", "http://blueocean.example/dahab"])
    def test_a_website_is_an_absolute_http_url(self, website: str) -> None:
        assert ContactCreate.model_validate({"name": "Blue Ocean", "website": website}).website == website

    @pytest.mark.parametrize("website", ["blueocean.example", "ftp://blueocean.example", "https://"])
    def test_anything_a_link_cannot_open_is_refused(self, website: str) -> None:
        """A bare host is the client's to complete with `https://`; a guess here would be a
        second copy of that rule."""
        with pytest.raises(ValidationError, match="absolute http or https URL"):
            ContactCreate.model_validate({"name": "Blue Ocean", "website": website})

    def test_an_email_has_to_be_one(self) -> None:
        with pytest.raises(ValidationError):
            ContactCreate.model_validate({"name": "Blue Ocean", "email": "not an address"})

    def test_an_address_needs_its_country(self) -> None:
        with pytest.raises(ValidationError):
            ContactCreate.model_validate({"name": "Blue Ocean", "address": {"city": "Dahab"}})

    def test_an_address_spreads_across_every_column(self) -> None:
        """All five named, `None` where empty, so a replacement clears the old street."""
        contact = ContactCreate.model_validate({"name": "Blue Ocean", "address": {"country": "Egypt"}})

        assert address_columns(contact.address) == {
            "address_street": None,
            "address_city": None,
            "address_postcode": None,
            "address_region": None,
            "address_country": "Egypt",
        }
        assert set(address_columns(None).values()) == {None}

    def test_the_create_body_refuses_what_it_does_not_know(self) -> None:
        with pytest.raises(ValidationError):
            ContactCreate.model_validate({"name": "Blue Ocean", "address_country": "Egypt"})

    @pytest.mark.parametrize("field", ["name", "roles", "notes"])
    def test_a_patch_may_not_null_a_not_null_column(self, field: str) -> None:
        with pytest.raises(ValidationError, match="cannot be null"):
            ContactUpdateRequest.model_validate({field: None})

    @pytest.mark.parametrize("field", ["phone", "email", "website", "address"])
    def test_a_patch_clears_the_nullable_members(self, field: str) -> None:
        values = ContactUpdateRequest.model_validate({field: None})

        assert field in values.model_fields_set


class TestTheReadShape:
    def test_the_address_is_nested_and_absent_without_a_country(self) -> None:
        with_address = contacts_module._to_public_contact(
            _internal_contact(address_city="Dahab", address_country="Egypt"), user_uuid=USER_UUID
        )
        without = contacts_module._to_public_contact(_internal_contact(), user_uuid=USER_UUID)

        assert with_address.address is not None
        assert (with_address.address.city, with_address.address.country) == ("Dahab", "Egypt")
        assert without.address is None

    def test_a_stored_role_outside_the_vocabulary_reads_back_as_itself(self) -> None:
        """The column has no `CHECK`, so a read that typed the enum would 500 on a row a
        direct write left - *"A stored vocabulary is read back as a string"*."""
        read = contacts_module._to_public_contact(_internal_contact(roles=["resort"]), user_uuid=USER_UUID)

        assert read.roles == ["resort"]


@pytest.fixture
def route_collaborators(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    stubs: dict[str, Any] = {
        "owned": AsyncMock(return_value=_internal_contact()),
        "exists": AsyncMock(return_value=False),
        "create": AsyncMock(return_value=_internal_contact()),
        "update": AsyncMock(),
        "delete": AsyncMock(),
        "stayed_on": AsyncMock(return_value=[]),
    }
    monkeypatch.setattr(contacts_module, "_get_owned_contact", stubs["owned"])
    monkeypatch.setattr(contacts_module, "contact_name_exists", stubs["exists"])
    monkeypatch.setattr(contacts_module.crud_contacts, "create", stubs["create"])
    monkeypatch.setattr(contacts_module.crud_contacts, "update", stubs["update"])
    monkeypatch.setattr(contacts_module.crud_contacts, "delete", stubs["delete"])
    monkeypatch.setattr(contacts_module, "get_trip_uuids_staying_at", stubs["stayed_on"])
    for name in (
        "invalidate_contact_caches",
        "invalidate_dive_caches",
        "invalidate_course_caches",
        "invalidate_certification_caches",
        "invalidate_gear_caches",
        "invalidate_trip_caches",
        "invalidate_trip_items",
    ):
        stubs[name] = AsyncMock()
        monkeypatch.setattr(contacts_module, name, stubs[name])
    return stubs


class TestTheRoutes:
    @pytest.mark.asyncio
    async def test_a_repeated_name_is_a_422(self, route_collaborators: dict[str, Any]) -> None:
        route_collaborators["exists"].return_value = True

        with pytest.raises(DuplicateValueException):
            await contacts_module.write_contact(
                request=MagicMock(),
                contact=ContactCreate.model_validate({"name": "blue ocean"}),
                current_user=_current_user(),
                db=MagicMock(),
            )

        route_collaborators["create"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_create_stores_the_address_flat(self, route_collaborators: dict[str, Any]) -> None:
        await contacts_module.write_contact(
            request=MagicMock(),
            contact=ContactCreate.model_validate(
                {"name": "Blue Ocean", "roles": ["school"], "address": {"city": "Dahab", "country": "Egypt"}}
            ),
            current_user=_current_user(),
            db=MagicMock(),
        )

        stored = route_collaborators["create"].await_args.kwargs["object"]
        assert (stored.address_city, stored.address_country, stored.user_id) == ("Dahab", "Egypt", USER_ID)
        assert stored.roles == [ContactRole.SCHOOL]
        route_collaborators["invalidate_contact_caches"].assert_awaited_once_with(USER_ID)

    @pytest.mark.asyncio
    async def test_naming_the_address_replaces_it_whole(self, route_collaborators: dict[str, Any]) -> None:
        await contacts_module.patch_contact(
            request=MagicMock(),
            uuid=uuid7(),
            values=ContactUpdateRequest.model_validate({"address": {"country": "Egypt"}}),
            current_user=_current_user(),
            db=MagicMock(),
        )

        written = route_collaborators["update"].await_args.kwargs["object"]
        assert written == {
            "address_street": None,
            "address_city": None,
            "address_postcode": None,
            "address_region": None,
            "address_country": "Egypt",
        }

    @pytest.mark.asyncio
    async def test_a_rename_reaches_the_reads_that_print_the_name(self, route_collaborators: dict[str, Any]) -> None:
        """The course and certification reads carry the contact's name for the web build
        that prints a training center, so a rename drops those two families as well."""
        await contacts_module.patch_contact(
            request=MagicMock(),
            uuid=uuid7(),
            values=ContactUpdateRequest.model_validate({"name": "Blue Ocean Dahab"}),
            current_user=_current_user(),
            db=MagicMock(),
        )

        for name in ("invalidate_contact_caches", "invalidate_course_caches", "invalidate_certification_caches"):
            route_collaborators[name].assert_awaited_once_with(USER_ID)
        route_collaborators["invalidate_dive_caches"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_renaming_onto_another_contact_is_a_422(self, route_collaborators: dict[str, Any]) -> None:
        route_collaborators["exists"].return_value = True

        with pytest.raises(DuplicateValueException):
            await contacts_module.patch_contact(
                request=MagicMock(),
                uuid=uuid7(),
                values=ContactUpdateRequest.model_validate({"name": "Gear Hub"}),
                current_user=_current_user(),
                db=MagicMock(),
            )

        assert route_collaborators["exists"].await_args.kwargs["exclude_id"] == 5
        route_collaborators["update"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_empty_patch_writes_and_drops_nothing(self, route_collaborators: dict[str, Any]) -> None:
        await contacts_module.patch_contact(
            request=MagicMock(),
            uuid=uuid7(),
            values=ContactUpdateRequest.model_validate({}),
            current_user=_current_user(),
            db=MagicMock(),
        )

        route_collaborators["update"].assert_not_awaited()
        route_collaborators["invalidate_contact_caches"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_delete_drops_every_family_whose_reads_it_changes(
        self, route_collaborators: dict[str, Any]
    ) -> None:
        """Five hosts read back a null now, and a single trip's key names no user - so the
        trips a part stayed at are collected before the row goes and dropped one by one."""
        stayed_on = [uuid7(), uuid7()]
        route_collaborators["stayed_on"].return_value = stayed_on

        await contacts_module.erase_contact(
            request=MagicMock(), uuid=uuid7(), current_user=_current_user(), db=MagicMock()
        )

        route_collaborators["delete"].assert_awaited_once()
        for name in (
            "invalidate_contact_caches",
            "invalidate_dive_caches",
            "invalidate_course_caches",
            "invalidate_certification_caches",
            "invalidate_gear_caches",
            "invalidate_trip_caches",
        ):
            route_collaborators[name].assert_awaited_once_with(USER_ID)
        route_collaborators["invalidate_trip_items"].assert_awaited_once_with(stayed_on)


# ------------------------------------------------------------------ the references


class TestTheCourseWrite:
    """`contact_uuid`, and the shim that honours a training-center string beside it."""

    @pytest.fixture
    def stubs(self, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        course = CourseReadInternal(
            id=11,
            user_id=USER_ID,
            uuid=uuid7(),
            name="Advanced Nitrox",
            status=CourseStatus.COMPLETED,
            notes="",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
        values: dict[str, Any] = {
            "owned": AsyncMock(return_value=course),
            "create": AsyncMock(return_value=course),
            "update": AsyncMock(),
            "resolve": AsyncMock(return_value=42),
            "resolve_or_create": AsyncMock(return_value=(43, True)),
            "invalidate_courses": AsyncMock(),
            "invalidate_contacts": AsyncMock(),
        }
        monkeypatch.setattr(courses_module, "_get_owned_course", values["owned"])
        monkeypatch.setattr(courses_module.crud_courses, "create", values["create"])
        monkeypatch.setattr(courses_module.crud_courses, "update", values["update"])
        monkeypatch.setattr(contact_links, "resolve_contact_id_for_user", values["resolve"])
        monkeypatch.setattr(contact_links, "resolve_or_create_contact", values["resolve_or_create"])
        monkeypatch.setattr(courses_module, "invalidate_course_caches", values["invalidate_courses"])
        monkeypatch.setattr(courses_module, "invalidate_contact_caches", values["invalidate_contacts"])
        monkeypatch.setattr(courses_module, "_contact_of", AsyncMock(return_value=None))
        return values

    async def _create(self, body: dict[str, Any]) -> None:
        await courses_module.write_course(
            request=MagicMock(),
            course=CourseCreate.model_validate({"name": "Advanced Nitrox", **body}),
            current_user=_current_user(),
            db=MagicMock(),
        )

    async def _patch(self, body: dict[str, Any]) -> None:
        await courses_module.patch_course(
            request=MagicMock(),
            uuid=uuid7(),
            values=CourseUpdateRequest.model_validate(body),
            current_user=_current_user(),
            db=MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_a_contact_uuid_is_resolved_against_the_caller(self, stubs: dict[str, Any]) -> None:
        await self._create({"contact_uuid": str(uuid7())})

        assert stubs["resolve"].await_args.kwargs["user_id"] == USER_ID
        assert stubs["create"].await_args.kwargs["object"].contact_id == 42
        stubs["resolve_or_create"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_someone_elses_contact_is_a_422(self, stubs: dict[str, Any]) -> None:
        stubs["resolve"].return_value = None

        with pytest.raises(UnprocessableEntityException, match="Contact not found"):
            await self._create({"contact_uuid": str(uuid7())})

        stubs["create"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_previous_build_s_training_center_becomes_a_contact(self, stubs: dict[str, Any]) -> None:
        """The deploy-skew shim: what a diver types into the old build's field during the
        window is kept, as the contact of that name."""
        await self._create({"training_center": "  Blue Ocean  "})

        assert stubs["resolve_or_create"].await_args.kwargs == {"user_id": USER_ID, "name": "Blue Ocean"}
        assert stubs["create"].await_args.kwargs["object"].contact_id == 43
        stubs["invalidate_contacts"].assert_awaited_once_with(USER_ID)

    @pytest.mark.asyncio
    async def test_the_uuid_wins_over_a_training_center_beside_it(self, stubs: dict[str, Any]) -> None:
        await self._create({"contact_uuid": str(uuid7()), "training_center": "Blue Ocean"})

        stubs["resolve_or_create"].assert_not_awaited()
        assert stubs["create"].await_args.kwargs["object"].contact_id == 42

    @pytest.mark.asyncio
    @pytest.mark.parametrize("training_center", [None, "", "   "])
    async def test_a_blank_training_center_changes_nothing(
        self, stubs: dict[str, Any], training_center: str | None
    ) -> None:
        """So an edit from the previous build can set a contact and never clear one."""
        await self._patch({"name": "Advanced Nitrox", "training_center": training_center})

        assert "contact_id" not in stubs["update"].await_args.kwargs["object"]
        stubs["resolve_or_create"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_null_contact_uuid_unlinks(self, stubs: dict[str, Any]) -> None:
        await self._patch({"contact_uuid": None})

        assert stubs["update"].await_args.kwargs["object"] == {"contact_id": None}

    def test_the_read_serves_the_linked_contact_s_name_as_the_training_center(self) -> None:
        """The shim's read half, for the build that prints the field and echoes it back."""
        contact = ContactRef(uuid=uuid7(), name="Blue Ocean")
        course = CourseReadInternal(
            id=11,
            user_id=USER_ID,
            uuid=uuid7(),
            name="Advanced Nitrox",
            status=CourseStatus.COMPLETED,
            contact_id=3,
            notes="",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

        read = courses_module._to_public_course(course, user_uuid=USER_UUID, contact=contact)
        unlinked = courses_module._to_public_course(course, user_uuid=USER_UUID)

        assert (read.contact_uuid, read.training_center) == (contact.uuid, "Blue Ocean")
        assert (unlinked.contact_uuid, unlinked.training_center) == (None, None)
        assert "contact_id" not in read.model_dump()


class TestTheCertificationWrite:
    def test_both_write_shapes_take_the_reference_and_the_shim(self) -> None:
        contact_uuid = uuid7()
        created = CertificationCreate.model_validate(
            {"agency": "padi", "name": "Open Water", "contact_uuid": str(contact_uuid), "training_center": "Blue"}
        )
        patched = CertificationUpdateRequest.model_validate({"contact_uuid": None})

        assert (created.contact_uuid, created.training_center) == (contact_uuid, "Blue")
        assert "contact_uuid" in patched.model_fields_set

    def test_the_read_serves_the_linked_contact_s_name_too(self) -> None:
        contact = ContactRef(uuid=uuid7(), name="Blue Ocean")
        internal = certifications_module.CertificationReadInternal(
            id=1,
            user_id=USER_ID,
            uuid=uuid7(),
            agency="padi",
            name="Open Water",
            contact_id=3,
            notes="",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

        read = certifications_module._to_public_certification(internal, user_uuid=USER_UUID, contact=contact)

        assert (read.contact_uuid, read.training_center) == (contact.uuid, "Blue Ocean")

    def test_a_vanished_contact_is_named_in_the_integrity_message(self) -> None:
        assert (
            certifications_module._FK_CONSTRAINT_MESSAGES["certification_contact_id_fkey"]
            == contact_links.CONTACT_NOT_FOUND
        )


class TestTheOtherHosts:
    @pytest.mark.asyncio
    async def test_a_dive_patch_resolves_and_clears_the_contact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        resolve = AsyncMock(return_value=7)
        monkeypatch.setattr(dives_module, "resolve_contact_reference", resolve)

        linked = await dives_module._link_updates(
            MagicMock(), DiveUpdateRequest.model_validate({"contact_uuid": str(uuid7())}), USER_ID
        )
        cleared = await dives_module._link_updates(
            MagicMock(), DiveUpdateRequest.model_validate({"contact_uuid": None}), USER_ID
        )
        untouched = await dives_module._link_updates(MagicMock(), DiveUpdateRequest.model_validate({}), USER_ID)

        assert (linked, cleared, untouched) == ({"contact_id": 7}, {"contact_id": None}, {})

    def test_a_vanished_contact_is_named_in_the_dives_integrity_message(self) -> None:
        error = IntegrityError("insert", {}, Exception('violates foreign key constraint "dive_contact_id_fkey"'))

        assert dives_module._fk_error_detail(error) == "Contact not found."

    def test_a_service_record_patch_takes_the_reference(self) -> None:
        values = GearServiceRecordUpdateRequest.model_validate({"contact_uuid": None, "performed_by": "Ahmed"})

        assert values.model_dump(exclude_unset=True, exclude={"contact_uuid"}) == {"performed_by": "Ahmed"}
        assert "contact_uuid" in values.model_fields_set

    @pytest.mark.asyncio
    async def test_a_part_staying_at_someone_elses_contact_is_refused_before_the_trip_exists(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Resolved before the trip row is written, so a refused part leaves no trip behind
        without its parts."""
        create = AsyncMock()
        monkeypatch.setattr(trips_module, "trip_name_exists", AsyncMock(return_value=False))
        monkeypatch.setattr(trips_module, "resolve_contact_ids_for_user", AsyncMock(return_value=None))
        monkeypatch.setattr(trips_module.crud_trips, "create", create)

        with pytest.raises(UnprocessableEntityException, match="Contact not found"):
            await trips_module.write_trip(
                request=MagicMock(),
                trip=TripCreate.model_validate({"name": "Dahab", "parts": [{"accommodation_uuid": str(uuid7())}]}),
                current_user=_current_user(),
                db=MagicMock(),
            )

        create.assert_not_awaited()


# ------------------------------------------------------------------ against Postgres


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheDatabase:
    @pytest.mark.asyncio
    async def test_a_name_is_unique_per_diver_case_insensitively(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        mine = create_contact(db, diver)

        assert await contact_name_exists(async_db, user_id=diver.id, name=f"  {mine.name.upper()} ")
        assert not await contact_name_exists(async_db, user_id=other_diver.id, name=mine.name)
        assert not await contact_name_exists(async_db, user_id=diver.id, name=mine.name, exclude_id=mine.id)
        db.add(Contact(user_id=diver.id, name=mine.name.lower()))
        with pytest.raises(IntegrityError, match="ux_contact_user_id_name_lower"):
            db.commit()
        db.rollback()

    def test_a_street_without_a_country_is_refused(self, db: Session, diver: User) -> None:
        """The admin form writes the columns flat, past the request shape's rule."""
        db.add(Contact(user_id=diver.id, name=f"Nowhere {uuid7().hex[-8:]}", address_street="Mashraba"))
        with pytest.raises(IntegrityError, match="ck_contact_address_has_country"):
            db.commit()
        db.rollback()

    @pytest.mark.asyncio
    async def test_the_shim_reuses_a_contact_by_name_and_otherwise_makes_a_school(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        mine = create_contact(db, diver)
        fresh_name = f"Koh Tao Divers {uuid7().hex[-8:]}"

        reused = await resolve_or_create_contact(async_db, user_id=diver.id, name=mine.name.lower())
        created_id, created = await resolve_or_create_contact(async_db, user_id=diver.id, name=f" {fresh_name} ")
        await async_db.commit()

        assert reused == (mine.id, False)
        assert created
        row = await async_db.get(Contact, created_id)
        assert row is not None and (row.name, row.roles) == (fresh_name, ["school"])

    @pytest.mark.asyncio
    async def test_someone_elses_contact_never_resolves(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        theirs = create_contact(db, other_diver)
        mine = create_contact(db, diver)

        assert await resolve_contact_id_for_user(async_db, contact_uuid=theirs.uuid, user_id=diver.id) is None
        assert (
            await resolve_contact_ids_for_user(async_db, contact_uuids=[mine.uuid, theirs.uuid], user_id=diver.id)
            is None
        )
        assert await resolve_contact_ids_for_user(async_db, contact_uuids=[mine.uuid], user_id=diver.id) == {
            mine.uuid: mine.id
        }
        assert await get_contact_refs_by_ids(async_db, contact_ids=[theirs.id, None], user_id=diver.id) == {}

    @pytest.mark.asyncio
    async def test_a_part_reads_its_accommodation_back_by_uuid(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        trip = create_trip(db, diver)
        contact = create_contact(db, diver, roles=["accommodation"])
        trip_id, contact_uuid, contact_id, trip_uuid = trip.id, contact.uuid, contact.id, trip.uuid

        await replace_parts_for_trip(
            db=async_db,
            trip_id=trip_id,
            parts=[TripPartInput(start_date=date(2026, 6, 1), accommodation_uuid=contact_uuid), TripPartInput()],
            accommodation_ids={contact_uuid: contact_id},
        )

        first, second = await get_parts_for_trip(async_db, trip_id)
        assert (first.accommodation_uuid, second.accommodation_uuid) == (contact_uuid, None)
        assert await get_trip_uuids_staying_at(async_db, contact_id) == [trip_uuid]

    @pytest.mark.asyncio
    async def test_deleting_a_contact_unlinks_all_five_references(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """`ON DELETE SET NULL` on every one, a hidden card's included: the rows survive and
        keep everything else."""
        contact = create_contact(db, diver)
        contact_id, contact_uuid = contact.id, contact.uuid
        dive = create_dive(db, diver)
        course = create_course(db, diver)
        live_card = create_certification(db, diver)
        hidden_card = create_certification(db, diver)
        record = create_gear_service_record(db, diver, create_gear_item(db, diver))
        trip = create_trip(db, diver)
        for row in (dive, course, live_card, hidden_card, record):
            row.contact_id = contact_id
        hidden_card.is_deleted = True
        db.commit()
        db.query(TripPart).filter(TripPart.trip_id == trip.id).update({"accommodation_contact_id": contact_id})
        db.commit()
        ids: list[tuple[type[Dive | Course | Certification | GearServiceRecord], int]] = [
            (Dive, dive.id),
            (Course, course.id),
            (Certification, live_card.id),
            (GearServiceRecord, record.id),
        ]
        hidden_id, trip_id = hidden_card.id, trip.id

        await crud_contacts.delete(db=async_db, uuid=contact_uuid)

        db.expunge_all()
        assert db.get(Contact, contact_id) is None
        for model, row_id in ids:
            stored: Any = db.get(model, row_id)
            assert stored is not None and stored.contact_id is None, model.__name__
        hidden = db.get(Certification, hidden_id)
        assert hidden is not None and hidden.contact_id is None
        (part,) = db.query(TripPart).filter(TripPart.trip_id == trip_id).all()
        assert part.accommodation_contact_id is None


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheReadsCarryTheReference:
    """Through the real read bodies and a real session: the list path is separate from the
    single read on every host, and a client resolves the name from the uuid."""

    @pytest.mark.asyncio
    async def test_the_dive_list_and_the_single_dive(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        contact = create_contact(db, diver)
        dive = create_dive(db, diver)
        dive.contact_id = contact.id
        db.commit()
        dive_uuid, contact_uuid, user_id, user_uuid = dive.uuid, contact.uuid, diver.id, diver.uuid

        page = await dives_module._cached_read_dives.__wrapped__(  # type: ignore[attr-defined]
            request=None,
            user_id=user_id,
            user_uuid=user_uuid,
            db=async_db,
            page=1,
            items_per_page=10,
            trip_id=None,
            course_id=None,
            dive_site_id=None,
            gear_item_id=None,
            species_id=None,
        )
        single = await dives_module._cached_read_dive.__wrapped__(  # type: ignore[attr-defined]
            request=None, user_id=user_id, uuid=dive_uuid, owner_uuid=user_uuid, db=async_db
        )

        assert [row["contact_uuid"] for row in page["data"]] == [contact_uuid]
        assert single.contact_uuid == contact_uuid

    @pytest.mark.asyncio
    async def test_a_course_and_a_card_carry_the_uuid_and_the_shim_s_name(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        contact = create_contact(db, diver)
        course = create_course(db, diver)
        card = create_certification(db, diver)
        course.contact_id = card.contact_id = contact.id
        db.commit()
        expected = (contact.uuid, contact.name)
        course_uuid, card_uuid, user_id, user_uuid = course.uuid, card.uuid, diver.id, diver.uuid

        courses = await courses_module._cached_read_courses.__wrapped__(  # type: ignore[attr-defined]
            request=None,
            user_id=user_id,
            user_uuid=user_uuid,
            db=async_db,
            page=1,
            items_per_page=10,
            search=None,
            date_from=None,
            date_to=None,
            agency=None,
            status=None,
        )
        one_course = await courses_module._cached_read_course.__wrapped__(  # type: ignore[attr-defined]
            request=None, user_id=user_id, uuid=course_uuid, owner_uuid=user_uuid, db=async_db
        )
        cards = await certifications_module._cached_read_certifications.__wrapped__(  # type: ignore[attr-defined]
            request=None, user_id=user_id, user_uuid=user_uuid, db=async_db, page=1, items_per_page=10, course_id=None
        )
        one_card = await certifications_module._cached_read_certification.__wrapped__(  # type: ignore[attr-defined]
            request=None, user_id=user_id, uuid=card_uuid, owner_uuid=user_uuid, db=async_db
        )

        assert [(row["contact_uuid"], row["training_center"]) for row in courses["data"]] == [expected]
        assert (one_course.contact_uuid, one_course.training_center) == expected
        assert [(row["contact_uuid"], row["training_center"]) for row in cards["data"]] == [expected]
        assert (one_card.contact_uuid, one_card.training_center) == expected

    @pytest.mark.asyncio
    async def test_a_service_record_names_its_shop_on_both_paths(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        contact = create_contact(db, diver, roles=["shop"])
        item = create_gear_item(db, diver)
        record = create_gear_service_record(db, diver, item)
        record.contact_id = contact.id
        db.commit()
        contact_uuid, record_uuid, item_uuid, user_id, user_uuid = (
            contact.uuid,
            record.uuid,
            item.uuid,
            diver.id,
            diver.uuid,
        )

        page = await gear_service_module._cached_read_records.__wrapped__(  # type: ignore[attr-defined]
            request=None,
            user_id=user_id,
            user_uuid=user_uuid,
            db=async_db,
            page=1,
            items_per_page=10,
            gear_item_id=None,
            gear_service_schedule_id=None,
        )
        single = await gear_service_module._cached_read_record.__wrapped__(  # type: ignore[attr-defined]
            request=None,
            user_id=user_id,
            uuid=record_uuid,
            owner_uuid=user_uuid,
            gear_item_uuid=item_uuid,
            gear_service_schedule_uuid=None,
            db=async_db,
        )

        assert [row["contact_uuid"] for row in page["data"]] == [contact_uuid]
        assert single.contact_uuid == contact_uuid


def test_every_role_is_the_format_s() -> None:
    """Value for value and in order, DiveJSON §6.18's vocabulary - the web mirrors it by hand
    and the export writes it through."""
    import divejson.converter

    assert [role.value for role in ContactRole] == list(divejson.converter.contact_roles())
