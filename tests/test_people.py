"""People: the record (`models/person.py`), its routes (`api/v1/people.py`), the link to an
account (`services/person_links.py`), and the four hosts that name one.

House style per `test_contacts.py`: the schemas and the routes' cache calls with their
collaborators stubbed, and a Postgres-guarded tail for what only the database settles - the
name index, the one-link-per-account index, the dive count, the search across the join, and
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

from src.app.api.v1 import certifications as certifications_module
from src.app.api.v1 import courses as courses_module
from src.app.api.v1 import dives as dives_module
from src.app.api.v1 import people as people_module
from src.app.api.v1 import trips as trips_module
from src.app.core.exceptions.http_exceptions import (
    DuplicateValueException,
    RateLimitException,
    UnprocessableEntityException,
)
from src.app.core.utils import cache as cache_module
from src.app.crud.crud_people import (
    get_people_for_dives,
    get_people_page,
    person_name_exists,
    replace_people_for_course,
    replace_people_for_dive,
    replace_people_for_trip,
    resolve_person_ids_for_user,
)
from src.app.models.certification import Certification
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
from src.app.services import person_links
from src.app.services.person_links import resolve_linked_account, resolve_people_references
from tests.conftest import db_available
from tests.helpers.generators import create_course, create_dive, create_person, create_trip, create_user

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


class _CountingRedis:
    """Enough of a client for the fixed-window limiter: `incr` counts, and `get` reads."""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> None:
        return None

    async def ttl(self, key: str) -> int:
        return 60

    async def get(self, key: str) -> str | None:
        return None if key not in self.counts else str(self.counts[key])


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
        """`linked_user_id` in particular: a body names an account by username, and only the
        route turns that into a row id."""
        with pytest.raises(ValidationError):
            PersonCreate.model_validate({"name": "Alex", "linked_user_id": 7})

    @pytest.mark.parametrize("field", ["name", "notes"])
    def test_a_patch_may_not_null_a_not_null_column(self, field: str) -> None:
        with pytest.raises(ValidationError, match="cannot be null"):
            PersonUpdate.model_validate({field: None})

    @pytest.mark.parametrize("field", ["email", "phone", "username"])
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
        "spend": AsyncMock(),
        "resolve": AsyncMock(return_value=42),
    }
    monkeypatch.setattr(people_module, "_get_owned_person", stubs["owned"])
    monkeypatch.setattr(people_module, "person_name_exists", stubs["exists"])
    monkeypatch.setattr(people_module.crud_people, "update", stubs["update"])
    monkeypatch.setattr(people_module.crud_people, "delete", stubs["delete"])
    monkeypatch.setattr(people_module, "spend_link_attempt", stubs["spend"])
    monkeypatch.setattr(people_module, "resolve_linked_account", stubs["resolve"])
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
                db=MagicMock(),
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
            db=MagicMock(),
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
    async def test_unlinking_counts_nothing(self, route_collaborators: dict[str, Any]) -> None:
        await people_module.patch_person(
            request=MagicMock(),
            uuid=uuid7(),
            values=PersonUpdateRequest.model_validate({"username": None}),
            current_user=_current_user(),
            db=MagicMock(),
        )

        assert route_collaborators["update"].await_args.kwargs["object"] == {"linked_user_id": None}
        route_collaborators["spend"].assert_not_awaited()

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
            await certifications_module._refuse_a_vanished_reference(AsyncMock(), error)


# ------------------------------------------------------------------ against Postgres


def _as(user: User) -> dict[str, Any]:
    return {"id": user.id, "uuid": user.uuid, "username": user.username, "is_superuser": False}


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheDatabase:
    @pytest.mark.asyncio
    async def test_a_name_is_unique_per_diver_trimmed_and_case_insensitively(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        mine = create_person(db, diver)

        assert await person_name_exists(async_db, user_id=diver.id, name=f"  {mine.name.upper()} ")
        assert not await person_name_exists(async_db, user_id=other_diver.id, name=mine.name)
        assert not await person_name_exists(async_db, user_id=diver.id, name=mine.name, exclude_id=mine.id)
        db.add(Person(user_id=diver.id, name=mine.name.lower()))
        with pytest.raises(IntegrityError, match="ux_person_user_id_name_lower"):
            db.commit()
        db.rollback()

    def test_a_person_never_links_to_its_owner(self, db: Session, diver: User) -> None:
        db.add(Person(user_id=diver.id, name=f"Me {uuid7().hex[-8:]}", linked_user_id=diver.id))
        with pytest.raises(IntegrityError, match="ck_person_not_linked_to_its_owner"):
            db.commit()
        db.rollback()

    def test_one_person_per_linked_account(self, db: Session, diver: User, other_diver: User) -> None:
        create_person(db, diver, linked_to=other_diver)
        db.add(Person(user_id=diver.id, name=f"Twice {uuid7().hex[-8:]}", linked_user_id=other_diver.id))
        with pytest.raises(IntegrityError, match="ux_person_user_id_linked_user_id"):
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
    async def test_the_list_finds_a_person_by_name_or_by_the_linked_username(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        linked = create_person(db, diver, name=f"Alex {uuid7().hex[-8:]}", linked_to=other_diver)
        create_person(db, diver, name=f"Sam {uuid7().hex[-8:]}")
        user_id, username, name = diver.id, other_diver.username, linked.name

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
    async def test_a_rename_of_the_linked_account_shows_through(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        """The account is stored, never the string."""
        person = create_person(db, diver, linked_to=other_diver)
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


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheLink:
    """The username resolver answers exactly as the availability check does, and refuses on
    the field the form shows it on."""

    @pytest.fixture(autouse=True)
    def _redis(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The limiter runs for real against a counter of our own: the module-level client is
        bound to the loop that opened it, and each test runs on its own - *"The local suite
        has no Redis"* in DECISIONS.md."""
        monkeypatch.setattr(cache_module, "client", _CountingRedis())

    @pytest.mark.asyncio
    async def test_an_account_is_found_by_its_exact_username(
        self, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        assert await resolve_linked_account(async_db, owner_id=diver.id, username=other_diver.username) == (
            other_diver.id
        )

    @pytest.mark.asyncio
    async def test_an_account_in_its_grace_period_is_found_too(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The availability check calls its username taken; answering otherwise here would
        reveal the deletion to anyone comparing the two."""
        leaving = create_user(db)
        leaving.is_deleted = True
        leaving.deleted_at = datetime.now(UTC)
        db.commit()

        assert await resolve_linked_account(async_db, owner_id=diver.id, username=leaving.username) == leaving.id

    @pytest.mark.asyncio
    @pytest.mark.parametrize("case", ["nobody", "own"])
    async def test_nobody_and_your_own_account_are_refused_on_the_field(
        self, case: str, async_db: AsyncSession, diver: User
    ) -> None:
        username = f"n{uuid7().hex[-12:]}" if case == "nobody" else diver.username

        with pytest.raises(RequestValidationError) as refused:
            await resolve_linked_account(async_db, owner_id=diver.id, username=username)

        (error,) = refused.value.errors()
        assert error["loc"] == ("body", "username")
        assert error["msg"] == (person_links.NO_SUCH_ACCOUNT if case == "nobody" else person_links.OWN_ACCOUNT)

    @pytest.mark.asyncio
    async def test_an_account_another_person_links_is_refused_by_that_persons_name(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        holder = create_person(db, diver, linked_to=other_diver)
        holder_id, holder_name = holder.id, holder.name

        with pytest.raises(RequestValidationError) as refused:
            await resolve_linked_account(async_db, owner_id=diver.id, username=other_diver.username)
        # The holder itself may keep it.
        assert (
            await resolve_linked_account(
                async_db, owner_id=diver.id, username=other_diver.username, person_id=holder_id
            )
            == other_diver.id
        )

        assert holder_name in refused.value.errors()[0]["msg"]

    @pytest.mark.asyncio
    async def test_a_create_links_and_reads_back_the_username(
        self, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        created = await people_module.write_person(
            request=MagicMock(),
            person=PersonCreate.model_validate(
                {"name": f" Alex {uuid7().hex[-6:]} ", "username": other_diver.username}
            ),
            current_user=_as(diver),
            db=async_db,
        )

        assert created.username == other_diver.username
        assert created.dive_count == 0
        assert created.name == created.name.strip()

    @pytest.mark.asyncio
    async def test_only_a_change_of_account_counts_against_the_limit(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        person = create_person(db, diver, linked_to=other_diver)
        third = create_user(db)
        spend = AsyncMock()
        monkeypatch.setattr(people_module, "spend_link_attempt", spend)
        person_uuid, same, different = person.uuid, other_diver.username, third.username

        for username in (same, different):
            await people_module.patch_person(
                request=MagicMock(),
                uuid=person_uuid,
                values=PersonUpdateRequest.model_validate({"username": username}),
                current_user=_as(diver),
                db=async_db,
            )

        spend.assert_awaited_once_with(diver.id)
        assert (await people_module._read(async_db, person.id)).username == different

    @pytest.mark.asyncio
    async def test_past_the_limit_a_link_is_a_429(
        self, async_db: AsyncSession, diver: User, other_diver: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(person_links.settings, "PERSON_LINK_RATE_LIMIT_PER_USER", 1)
        body = {"username": f"n{uuid7().hex[-12:]}"}

        with pytest.raises(RequestValidationError):
            await people_module.write_person(
                request=MagicMock(),
                person=PersonCreate.model_validate({**body, "name": f"A {uuid7().hex[-6:]}"}),
                current_user=_as(diver),
                db=async_db,
            )
        with pytest.raises(RateLimitException):
            await people_module.write_person(
                request=MagicMock(),
                person=PersonCreate.model_validate({**body, "name": f"B {uuid7().hex[-6:]}"}),
                current_user=_as(diver),
                db=async_db,
            )


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
        assert [reference.model_dump() for reference in single.people] == expected

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
                        "duration": 1800,
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
