"""People: the record (`models/person.py`), its routes (`api/v1/people.py`), a person write
connecting one to an account (`services/person_connections.py`), and the four hosts that name
one. What a tie shows the other side - *Added you*, the pictures, Disconnect - is
`test_connections.py`'s.

House style per `test_contacts.py`: the schemas and the routes' cache calls with their
collaborators stubbed, and a Postgres-guarded tail for what only the database settles - the
name index, the one-tie-per-account index, the dive count, the search across the join, and
the references on each host.
"""

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import divejson.converter
import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.integrity import refuse_vanished_reference
from src.app.api.v1 import certifications as certifications_module
from src.app.api.v1 import courses as courses_module
from src.app.api.v1 import dives as dives_module
from src.app.api.v1 import people as people_module
from src.app.api.v1 import trips as trips_module
from src.app.core.exceptions.http_exceptions import DuplicateValueException, UnprocessableEntityException
from src.app.crud.crud_people import (
    get_people_for_dives,
    get_people_page,
    person_name_exists,
    replace_people_for_course,
    replace_people_for_dive,
    replace_people_for_trip,
    resolve_person_ids_for_user,
)
from src.app.models.account_disconnect import AccountDisconnect
from src.app.models.certification import Certification
from src.app.models.connect_link import ConnectLink
from src.app.models.course_person import CoursePerson
from src.app.models.dive_person import DivePerson
from src.app.models.person import Person
from src.app.models.user import User
from src.app.schemas.certification import CertificationCreate, CertificationUpdateRequest
from src.app.schemas.course import CourseCreate, CourseUpdateRequest
from src.app.schemas.dive import DiveCreateRequest, DiveListSort, DiveUpdateRequest
from src.app.schemas.person import (
    PersonCreate,
    PersonReadInternal,
    PersonReference,
    PersonReferenceRead,
    PersonRole,
    PersonUpdate,
    PersonUpdateRequest,
)
from src.app.services import person_connections
from src.app.services.connect_links import new_connect_link_token
from src.app.services.person_connections import resolve_people_references
from tests.conftest import db_available
from tests.helpers.generators import (
    create_course,
    create_dive,
    create_person,
    create_trip,
    create_user,
    create_user_picture,
)

USER_ID = 1
USER_UUID = uuid7()


def _current_user() -> dict[str, Any]:
    return {"id": USER_ID, "uuid": USER_UUID, "username": "ada", "is_superuser": False}


def _internal_person(**overrides: Any) -> PersonReadInternal:
    values: dict[str, Any] = {
        "id": 5,
        "user_id": USER_ID,
        "uuid": uuid7(),
        "name": "Alex M.",
        "notes": "",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(overrides)
    return PersonReadInternal(**values)


class TestTheWriteSchema:
    def test_a_name_is_stored_trimmed(self) -> None:
        assert PersonCreate.model_validate({"name": "  Alex M.  "}).name == "Alex M."

    @pytest.mark.parametrize("name", ["", "   "])
    def test_a_name_of_nothing_is_a_422(self, name: str) -> None:
        with pytest.raises(ValidationError):
            PersonCreate.model_validate({"name": name})

    def test_an_email_has_to_be_one(self) -> None:
        with pytest.raises(ValidationError):
            PersonCreate.model_validate({"name": "Alex", "email": "alex@"})

    def test_the_create_body_refuses_what_it_does_not_know(self) -> None:
        """`connected_user_id` in particular: a body names an account by a connect link or a
        public id, and only the route turns that into a row id."""
        with pytest.raises(ValidationError):
            PersonCreate.model_validate({"name": "Alex", "connected_user_id": 7})

    @pytest.mark.parametrize("field", ["name", "notes"])
    def test_a_patch_may_not_null_a_not_null_column(self, field: str) -> None:
        with pytest.raises(ValidationError, match="cannot be null"):
            PersonUpdate.model_validate({field: None})

    @pytest.mark.parametrize("field", ["email", "phone"])
    def test_a_patch_clears_the_nullable_members(self, field: str) -> None:
        values = PersonUpdateRequest.model_validate({field: None})

        assert field in values.model_fields_set

    def test_a_role_outside_the_vocabulary_is_a_422_on_a_write(self) -> None:
        with pytest.raises(ValidationError):
            PersonReference.model_validate({"person_uuid": str(uuid7()), "role": "divemaster"})

    def test_a_stored_role_outside_the_vocabulary_reads_back_as_itself(self) -> None:
        """No direct write can store one, and the read is still not where it would be refused
        - *"A stored vocabulary is read back as a string"*."""
        assert PersonReferenceRead(person_uuid=uuid7(), role="divemaster").role == "divemaster"

    @pytest.mark.parametrize(
        "schema",
        [DiveCreateRequest, DiveUpdateRequest, CourseCreate, CourseUpdateRequest],
        ids=lambda schema: schema.__name__,
    )
    def test_every_host_write_takes_a_list_of_references(self, schema: Any) -> None:
        assert "people" in schema.model_fields

    @pytest.mark.parametrize("schema", [CertificationCreate, CertificationUpdateRequest], ids=lambda s: s.__name__)
    def test_a_card_takes_its_instructor_by_uuid(self, schema: Any) -> None:
        assert "instructor_uuid" in schema.model_fields

    @pytest.mark.parametrize(
        ("schema", "body"),
        [
            (CourseCreate, {"name": "AN/DP"}),
            (CourseUpdateRequest, {}),
            (CertificationCreate, {"agency": "padi", "name": "Rescue Diver"}),
            (CertificationUpdateRequest, {}),
        ],
        ids=["CourseCreate", "CourseUpdateRequest", "CertificationCreate", "CertificationUpdateRequest"],
    )
    def test_an_instructor_is_never_named_by_a_string(self, schema: Any, body: dict[str, Any]) -> None:
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            schema.model_validate({**body, "instructor_name": "Jae Kim"})


def test_every_role_is_the_format_s() -> None:
    """Value for value and in order, DiveJSON §6.20's vocabulary - the web mirrors it by hand
    and the export writes it through."""
    assert [role.value for role in PersonRole] == list(divejson.converter.person_roles())


@pytest.fixture
def route_collaborators(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    stubs: dict[str, Any] = {
        "owned": AsyncMock(return_value=_internal_person()),
        "exists": AsyncMock(return_value=False),
        "update": AsyncMock(),
        "delete": AsyncMock(),
    }
    monkeypatch.setattr(people_module, "_get_owned_person", stubs["owned"])
    monkeypatch.setattr(people_module, "person_name_exists", stubs["exists"])
    monkeypatch.setattr(people_module.crud_people, "update", stubs["update"])
    monkeypatch.setattr(people_module.crud_people, "delete", stubs["delete"])
    for name in (
        "invalidate_dive_caches",
        "invalidate_trip_caches",
        "invalidate_course_caches",
        "invalidate_certification_caches",
    ):
        stubs[name] = AsyncMock()
        monkeypatch.setattr(people_module, name, stubs[name])
    return stubs


class TestTheRoutes:
    @pytest.mark.asyncio
    async def test_renaming_onto_another_person_is_a_422(self, route_collaborators: dict[str, Any]) -> None:
        route_collaborators["exists"].return_value = True

        with pytest.raises(DuplicateValueException):
            await people_module.patch_person(
                request=MagicMock(),
                uuid=uuid7(),
                values=PersonUpdateRequest.model_validate({"name": "Sam"}),
                current_user=_current_user(),
                db=AsyncMock(),
            )

        assert route_collaborators["exists"].await_args.kwargs["exclude_id"] == 5
        route_collaborators["update"].assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [{"name": "Alex Moreno"}, {"phone": "+34 600 000 000"}])
    async def test_an_edit_drops_nothing(self, route_collaborators: dict[str, Any], body: dict[str, str]) -> None:
        """Every host reads its people by uuid and role, never a name, and the check-in link
        that prints one is uncached - so not even a rename changes a cached body."""
        await people_module.patch_person(
            request=MagicMock(),
            uuid=uuid7(),
            values=PersonUpdateRequest.model_validate(body),
            current_user=_current_user(),
            db=AsyncMock(),
        )

        route_collaborators["update"].assert_awaited_once()
        for name in (
            "invalidate_dive_caches",
            "invalidate_trip_caches",
            "invalidate_course_caches",
            "invalidate_certification_caches",
        ):
            route_collaborators[name].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_delete_drops_every_family_whose_reads_it_changes(
        self, route_collaborators: dict[str, Any]
    ) -> None:
        """Four hosts read back without it. The trip family's sweep reaches the single trips
        as well as the list, so the trips it was on need no naming."""
        await people_module.erase_person(
            request=MagicMock(), uuid=uuid7(), current_user=_current_user(), db=MagicMock()
        )

        route_collaborators["delete"].assert_awaited_once()
        for name in (
            "invalidate_dive_caches",
            "invalidate_trip_caches",
            "invalidate_course_caches",
            "invalidate_certification_caches",
        ):
            route_collaborators[name].assert_awaited_once_with(USER_ID)


class TestTheIntegrityMessages:
    def test_a_vanished_person_on_a_dive_is_named(self) -> None:
        error = IntegrityError("INSERT", {}, Exception('violates foreign key constraint "dive_person_person_id_fkey"'))

        assert dives_module._fk_error_detail(error) == "Person not found."

    @pytest.mark.asyncio
    async def test_a_vanished_instructor_on_a_card_is_named(self) -> None:
        error = IntegrityError(
            "UPDATE", {}, Exception('violates foreign key constraint "certification_instructor_id_fkey"')
        )

        with pytest.raises(UnprocessableEntityException, match="Person not found."):
            await refuse_vanished_reference(AsyncMock(), error, certifications_module._FK_CONSTRAINT_MESSAGES)


# ------------------------------------------------------------------ against Postgres


def _as(user: User) -> dict[str, Any]:
    return {"id": user.id, "uuid": user.uuid, "username": user.username, "is_superuser": False}


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheDatabase:
    @pytest.mark.asyncio
    async def test_a_name_is_unique_per_diver_case_insensitively(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        mine = create_person(db, diver)

        assert await person_name_exists(async_db, user_id=diver.id, name=mine.name.upper())
        assert not await person_name_exists(async_db, user_id=other_diver.id, name=mine.name)
        assert not await person_name_exists(async_db, user_id=diver.id, name=mine.name, exclude_id=mine.id)
        db.add(Person(user_id=diver.id, name=mine.name.lower()))
        with pytest.raises(IntegrityError, match="ux_person_user_id_name_lower"):
            db.commit()
        db.rollback()

    def test_a_person_never_connects_to_its_owner(self, db: Session, diver: User) -> None:
        db.add(Person(user_id=diver.id, name=f"Me {uuid7().hex[-8:]}", connected_user_id=diver.id))
        with pytest.raises(IntegrityError, match="ck_person_not_connected_to_its_owner"):
            db.commit()
        db.rollback()

    def test_one_person_per_connected_account(self, db: Session, diver: User, other_diver: User) -> None:
        create_person(db, diver, connected_to=other_diver)
        db.add(Person(user_id=diver.id, name=f"Twice {uuid7().hex[-8:]}", connected_user_id=other_diver.id))
        with pytest.raises(IntegrityError, match="ux_person_user_id_connected_user_id"):
            db.commit()
        db.rollback()

    @pytest.mark.asyncio
    async def test_someone_elses_person_never_resolves(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        theirs = create_person(db, other_diver)
        mine = create_person(db, diver)

        assert await resolve_person_ids_for_user(async_db, [mine.uuid, theirs.uuid], diver.id) is None
        assert await resolve_person_ids_for_user(async_db, [mine.uuid], diver.id) == {mine.uuid: mine.id}
        with pytest.raises(UnprocessableEntityException, match="Person not found."):
            await resolve_people_references(async_db, [PersonReference(person_uuid=theirs.uuid)], user_id=diver.id)

    @pytest.mark.asyncio
    async def test_a_person_named_twice_keeps_the_first_place_and_role(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        dive = create_dive(db, diver)
        alex, sam = create_person(db, diver), create_person(db, diver)
        dive_id, alex_uuid, sam_uuid = dive.id, alex.uuid, sam.uuid

        await replace_people_for_dive(async_db, dive_id, [(sam.id, "guide"), (alex.id, None), (sam.id, "buddy")])

        (references,) = (await get_people_for_dives(async_db, [dive_id])).values()
        assert [(reference.person_uuid, reference.role) for reference in references] == [
            (sam_uuid, "guide"),
            (alex_uuid, None),
        ]

    @pytest.mark.asyncio
    async def test_the_dive_count_is_live_dives_only(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """What `GET /dives?person_uuid=` matches, so the list and the person's page agree. A
        soft-deleted dive keeps its join row and is not counted; a trip or a course naming
        the person is not a dive at all."""
        person = create_person(db, diver)
        live, hidden = create_dive(db, diver), create_dive(db, diver, is_deleted=True)
        trip, course = create_trip(db, diver), create_course(db, diver)
        person_id, user_id = person.id, diver.id
        await replace_people_for_dive(async_db, live.id, [(person_id, "buddy")])
        await replace_people_for_dive(async_db, hidden.id, [(person_id, "buddy")])
        await replace_people_for_trip(async_db, trip.id, [(person_id, "companion")])
        await replace_people_for_course(async_db, course.id, [(person_id, "instructor")])

        page = await get_people_page(async_db, user_id=user_id, offset=0, limit=10, search=None)

        assert [row.dive_count for row in page["data"]] == [1]

    @pytest.mark.asyncio
    async def test_the_list_finds_a_person_by_name_or_by_the_connected_username(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        connected = create_person(db, diver, name=f"Alex {uuid7().hex[-8:]}", connected_to=other_diver)
        create_person(db, diver, name=f"Sam {uuid7().hex[-8:]}")
        user_id, username, name = diver.id, other_diver.username, connected.name

        by_handle = await get_people_page(async_db, user_id=user_id, offset=0, limit=10, search=username[2:8])
        by_name = await get_people_page(async_db, user_id=user_id, offset=0, limit=10, search=name[:4].lower())

        assert [(row.name, row.username) for row in by_handle["data"]] == [(name, username)]
        assert [row.name for row in by_name["data"]] == [name]
        assert by_handle["total_count"] == 1

    @pytest.mark.asyncio
    async def test_the_list_route_clamps_and_folds_the_search(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        person = create_person(db, diver, name=f"Zed {uuid7().hex[-8:]}")
        name = person.name

        page = await people_module.read_people(
            request=MagicMock(),
            current_user=_as(diver),
            db=async_db,
            page=0,
            items_per_page=10_000,
            search=f"  {name.upper()} ",
        )

        assert [row["name"] for row in page["data"]] == [name]
        assert (page["page"], page["items_per_page"]) == (1, 100)

    @pytest.mark.asyncio
    async def test_a_card_naming_someone_elses_person_is_a_422(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        theirs = create_person(db, other_diver)

        with pytest.raises(UnprocessableEntityException, match="Person not found."):
            await certifications_module.write_certification(
                request=MagicMock(),
                certification=CertificationCreate.model_validate(
                    {"agency": "padi", "name": "Rescue Diver", "instructor_uuid": str(theirs.uuid)}
                ),
                current_user=_as(diver),
                db=async_db,
            )

    @pytest.mark.asyncio
    async def test_a_rename_of_the_connected_account_shows_through(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        """The account is stored, never the string."""
        person = create_person(db, diver, connected_to=other_diver)
        other_diver.username = f"r{uuid7().hex[-12:]}"
        db.commit()
        person_id, renamed = person.id, other_diver.username

        read = await people_module._read(async_db, person_id)

        assert read.username == renamed

    @pytest.mark.asyncio
    async def test_the_dive_list_filters_on_a_person(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        person = create_person(db, diver)
        named, other, hidden = create_dive(db, diver), create_dive(db, diver), create_dive(db, diver, is_deleted=True)
        await replace_people_for_dive(async_db, named.id, [(person.id, None)])
        await replace_people_for_dive(async_db, hidden.id, [(person.id, None)])
        named_uuid, other_uuid, person_id, user_id, user_uuid = named.uuid, other.uuid, person.id, diver.id, diver.uuid

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
            person_id=person_id,
            tag_id=None,
            dive_type=None,
            sort=DiveListSort.DATE,
        )

        assert [row["uuid"] for row in page["data"]] == [named_uuid]
        assert other_uuid not in {row["uuid"] for row in page["data"]}

    @pytest.mark.asyncio
    async def test_deleting_a_person_takes_its_references_and_unlinks_its_cards(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        person = create_person(db, diver)
        dive, course = create_dive(db, diver), create_course(db, diver)
        card = Certification(user_id=diver.id, agency="padi", name="Rescue Diver", notes="", instructor_id=person.id)
        db.add(card)
        db.commit()
        await replace_people_for_dive(async_db, dive.id, [(person.id, "buddy")])
        await replace_people_for_course(async_db, course.id, [(person.id, "instructor")])
        person_id, person_uuid, card_id = person.id, person.uuid, card.id

        await people_module.crud_people.delete(db=async_db, uuid=person_uuid)

        db.expunge_all()
        assert db.get(Person, person_id) is None
        assert db.execute(select(DivePerson).where(DivePerson.person_id == person_id)).first() is None
        assert db.execute(select(CoursePerson).where(CoursePerson.person_id == person_id)).first() is None
        stored = db.get(Certification, card_id)
        assert stored is not None and stored.instructor_id is None


def _link(db: Session, owner: User) -> str:
    token = new_connect_link_token()
    db.add(ConnectLink(user_id=owner.id, token=token))
    db.commit()
    return token


def _refusal(refused: pytest.ExceptionInfo[RequestValidationError]) -> tuple[Any, str]:
    (error,) = refused.value.errors()
    return error["loc"], error["msg"]


async def _create(async_db: AsyncSession, diver: User, **body: Any) -> Any:
    return await people_module.write_person(
        request=MagicMock(),
        person=PersonCreate.model_validate({"name": f"Alex {uuid7().hex[-6:]}", **body}),
        current_user=_as(diver),
        db=async_db,
    )


async def _patch(async_db: AsyncSession, diver: User, person_uuid: Any, **body: Any) -> None:
    await people_module.patch_person(
        request=MagicMock(),
        uuid=person_uuid,
        values=PersonUpdateRequest.model_validate(body),
        current_user=_as(diver),
        db=async_db,
    )


class TestTheConnectMembers:
    def test_a_body_names_one_way_at_most(self) -> None:
        with pytest.raises(ValidationError, match="not several"):
            PersonCreate.model_validate({"name": "A", "connect_link": "a" * 32, "connect_account": str(uuid7())})
        with pytest.raises(ValidationError, match="not several"):
            PersonUpdateRequest.model_validate({"connect_account": str(uuid7()), "connect_link": "a" * 32})

    @pytest.mark.parametrize("schema", [PersonCreate, PersonUpdateRequest])
    @pytest.mark.parametrize("username", [None, "alexm"])
    def test_a_username_is_refused_like_any_member_the_body_does_not_declare(
        self, schema: Any, username: str | None
    ) -> None:
        """Connecting by username is gone: a body naming one, even null, is a 422."""
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            schema.model_validate({"name": "A", "username": username})


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestConnecting:
    """A person write connects only from what the other account handed over: its live link,
    or its own tie to the caller."""

    @pytest.mark.asyncio
    async def test_a_link_connects_a_new_person_and_the_read_carries_username_and_picture(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        create_user_picture(db, other_diver)
        token = _link(db, other_diver)
        username = other_diver.username

        created = await _create(async_db, diver, connect_link=token)

        assert (created.username, created.avatar_sha256) == (username, "a" * 64)

    @pytest.mark.asyncio
    async def test_an_update_connects_a_person_logged_by_name_months_ago(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        person = create_person(db, diver)
        person_id, person_uuid, token, username = person.id, person.uuid, _link(db, other_diver), other_diver.username

        await _patch(async_db, diver, person_uuid, connect_link=token)

        assert (await people_module._read(async_db, person_id)).username == username

    @pytest.mark.asyncio
    async def test_a_person_already_connected_is_refused_on_the_member(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        person = create_person(db, diver, connected_to=create_user(db))
        person_uuid, token = person.uuid, _link(db, other_diver)

        with pytest.raises(RequestValidationError) as refused:
            await _patch(async_db, diver, person_uuid, connect_link=token)

        assert _refusal(refused) == (("body", "connect_link"), person_connections.ALREADY_CONNECTED)

    @pytest.mark.asyncio
    async def test_a_dead_link_your_own_and_one_another_person_holds_are_refused_on_the_member(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        holder = create_person(db, diver, connected_to=other_diver)
        holder_name, theirs, mine = holder.name, _link(db, other_diver), _link(db, diver)

        refusals = []
        for token in (new_connect_link_token(), mine, theirs):
            with pytest.raises(RequestValidationError) as refused:
                await _create(async_db, diver, connect_link=token)
            refusals.append(_refusal(refused))

        assert refusals == [
            (("body", "connect_link"), person_connections.NO_SUCH_LINK),
            (("body", "connect_link"), person_connections.OWN_ACCOUNT),
            (("body", "connect_link"), f"{holder_name} is already connected to that account."),
        ]

    @pytest.mark.asyncio
    async def test_a_deleted_owners_link_is_dead(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        leaving = create_user(db)
        token = _link(db, leaving)
        leaving.is_deleted = True
        leaving.deleted_at = datetime.now(UTC)
        db.commit()

        with pytest.raises(RequestValidationError) as refused:
            await _create(async_db, diver, connect_link=token)

        assert _refusal(refused)[1] == person_connections.NO_SUCH_LINK

    @pytest.mark.asyncio
    async def test_adding_back_needs_the_other_accounts_tie(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        """An id with no tie and an id naming nobody are one refusal."""
        stranger = create_user(db)
        create_person(db, other_diver, connected_to=diver)
        username, stranger_uuid, other_uuid = other_diver.username, stranger.uuid, other_diver.uuid

        refusals = []
        for account in (stranger_uuid, uuid7()):
            with pytest.raises(RequestValidationError) as refused:
                await _create(async_db, diver, connect_account=str(account))
            refusals.append(_refusal(refused))
        created = await _create(async_db, diver, connect_account=str(other_uuid))

        assert refusals == [(("body", "connect_account"), person_connections.NO_SUCH_TIE)] * 2
        assert created.username == username

    @pytest.mark.asyncio
    async def test_adding_back_your_own_account_is_refused_as_such(self, async_db: AsyncSession, diver: User) -> None:
        with pytest.raises(RequestValidationError) as refused:
            await _create(async_db, diver, connect_account=str(diver.uuid))

        assert _refusal(refused) == (("body", "connect_account"), person_connections.OWN_ACCOUNT)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("way", ["link", "add back"])
    async def test_connecting_again_forgets_the_accounts_disconnect(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User, way: str
    ) -> None:
        """Either way is that account's consent, so an import may connect the two again."""
        db.add(AccountDisconnect(user_id=other_diver.id, disconnected_user_id=diver.id))
        db.commit()
        body = (
            {"connect_link": _link(db, other_diver)}
            if way == "link"
            else {"connect_account": str(create_person(db, other_diver, connected_to=diver) and other_diver.uuid)}
        )
        other_id, diver_id = other_diver.id, diver.id

        await _create(async_db, diver, **body)

        db.expire_all()
        assert (
            db.scalar(
                select(AccountDisconnect.id).where(
                    AccountDisconnect.user_id == other_id, AccountDisconnect.disconnected_user_id == diver_id
                )
            )
            is None
        )

    @pytest.mark.asyncio
    async def test_a_name_already_taken_is_the_duplicate_refusal_whatever_connects_it(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        taken = create_person(db, diver).name
        token = _link(db, other_diver)

        with pytest.raises(DuplicateValueException):
            await _create(async_db, diver, name=taken.upper(), connect_link=token)


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheHostsCarryReferences:
    """Through the real read bodies: references only, in the diver's order, with roles."""

    @pytest.mark.asyncio
    async def test_the_single_dive(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        dive = create_dive(db, diver)
        alex, sam = create_person(db, diver), create_person(db, diver)
        await replace_people_for_dive(async_db, dive.id, [(sam.id, "guide"), (alex.id, None)])
        dive_uuid, user_id, user_uuid, sam_uuid, alex_uuid = dive.uuid, diver.id, diver.uuid, sam.uuid, alex.uuid

        read = await dives_module._cached_read_dive.__wrapped__(  # type: ignore[attr-defined]
            request=None, user_id=user_id, uuid=dive_uuid, owner_uuid=user_uuid, db=async_db
        )

        assert [(reference.person_uuid, reference.role) for reference in read.people] == [
            (sam_uuid, "guide"),
            (alex_uuid, None),
        ]

    @pytest.mark.asyncio
    async def test_a_trip_on_both_paths(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        trip, person = create_trip(db, diver), create_person(db, diver)
        await replace_people_for_trip(async_db, trip.id, [(person.id, "companion")])
        trip_uuid, user_id, user_uuid, person_uuid = trip.uuid, diver.id, diver.uuid, person.uuid

        page = await trips_module._cached_read_trips.__wrapped__(  # type: ignore[attr-defined]
            request=None, user_id=user_id, user_uuid=user_uuid, db=async_db, page=1, items_per_page=10, search=None
        )
        single = await trips_module._cached_read_trip.__wrapped__(  # type: ignore[attr-defined]
            request=None, user_id=user_id, uuid=trip_uuid, owner_uuid=user_uuid, db=async_db
        )

        expected = [{"person_uuid": person_uuid, "role": "companion"}]
        assert [row["people"] for row in page["data"] if row["uuid"] == trip_uuid] == [expected]
        assert single["people"] == expected

    @pytest.mark.asyncio
    async def test_a_dive_write_refuses_someone_elses_person_before_anything_is_written(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        theirs = create_person(db, other_diver)
        create_dive_stub = AsyncMock()
        monkeypatch.setattr(dives_module.crud_dives, "create", create_dive_stub)

        with pytest.raises(UnprocessableEntityException, match="Person not found."):
            await dives_module.write_dive(
                request=MagicMock(),
                dive=DiveCreateRequest.model_validate(
                    {
                        "dive_number": 1,
                        "start_time": "2026-06-01T09:00:00+02:00",
                        "submerged_time": 1800,
                        "people": [{"person_uuid": str(theirs.uuid), "role": "buddy"}],
                    }
                ),
                current_user=_as(diver),
                db=async_db,
            )

        create_dive_stub.assert_not_awaited()


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheCourseAndCardWrites:
    """A course's instructors are people by role and a card's is one by uuid, through the
    real write and read bodies."""

    @staticmethod
    def _no_caches(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(courses_module, "invalidate_course_caches", AsyncMock())
        monkeypatch.setattr(certifications_module, "invalidate_certification_caches", AsyncMock())

    @pytest.mark.asyncio
    async def test_a_course_keeps_its_people_until_a_patch_sends_them(
        self, db: Session, async_db: AsyncSession, diver: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._no_caches(monkeypatch)
        jae, sam = create_person(db, diver), create_person(db, diver)
        jae_uuid, sam_uuid = jae.uuid, sam.uuid

        created = await courses_module.write_course(
            request=MagicMock(),
            course=CourseCreate.model_validate(
                {
                    "name": "AN/DP",
                    "people": [
                        {"person_uuid": str(jae_uuid), "role": "instructor"},
                        {"person_uuid": str(sam_uuid), "role": "student"},
                    ],
                }
            ),
            current_user=_as(diver),
            db=async_db,
        )
        await courses_module.patch_course(
            request=MagicMock(),
            uuid=created.uuid,
            values=CourseUpdateRequest.model_validate({"notes": "passed"}),
            current_user=_as(diver),
            db=async_db,
        )
        kept = await courses_module._cached_read_course.__wrapped__(  # type: ignore[attr-defined]
            request=None, user_id=diver.id, uuid=created.uuid, owner_uuid=diver.uuid, db=async_db
        )
        await courses_module.patch_course(
            request=MagicMock(),
            uuid=created.uuid,
            values=CourseUpdateRequest.model_validate({"people": [{"person_uuid": str(sam_uuid)}]}),
            current_user=_as(diver),
            db=async_db,
        )
        replaced = await courses_module._cached_read_course.__wrapped__(  # type: ignore[attr-defined]
            request=None, user_id=diver.id, uuid=created.uuid, owner_uuid=diver.uuid, db=async_db
        )

        expected = [(jae_uuid, "instructor"), (sam_uuid, "student")]
        assert [(reference.person_uuid, reference.role) for reference in created.people] == expected
        assert [(reference.person_uuid, reference.role) for reference in kept.people] == expected
        assert [(reference.person_uuid, reference.role) for reference in replaced.people] == [(sam_uuid, None)]

    @pytest.mark.asyncio
    async def test_a_card_keeps_its_instructor_until_a_null_clears_it(
        self, db: Session, async_db: AsyncSession, diver: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._no_caches(monkeypatch)
        jae_uuid = create_person(db, diver).uuid

        created = await certifications_module.write_certification(
            request=MagicMock(),
            certification=CertificationCreate.model_validate(
                {"agency": "padi", "name": "Rescue Diver", "instructor_uuid": str(jae_uuid)}
            ),
            current_user=_as(diver),
            db=async_db,
        )
        await certifications_module.patch_certification(
            request=MagicMock(),
            uuid=created.uuid,
            values=CertificationUpdateRequest.model_validate({"notes": "renewed"}),
            current_user=_as(diver),
            db=async_db,
        )
        kept = await certifications_module._cached_read_certification.__wrapped__(  # type: ignore[attr-defined]
            request=None, user_id=diver.id, uuid=created.uuid, owner_uuid=diver.uuid, db=async_db
        )
        await certifications_module.patch_certification(
            request=MagicMock(),
            uuid=created.uuid,
            values=CertificationUpdateRequest.model_validate({"instructor_uuid": None}),
            current_user=_as(diver),
            db=async_db,
        )
        cleared = await certifications_module._cached_read_certification.__wrapped__(  # type: ignore[attr-defined]
            request=None, user_id=diver.id, uuid=created.uuid, owner_uuid=diver.uuid, db=async_db
        )

        assert created.instructor_uuid == kept.instructor_uuid == jae_uuid
        assert cleared.instructor_uuid is None
