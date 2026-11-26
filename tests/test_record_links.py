"""A record's links (`schemas/link.py`) on the five records that keep them, and a contact's
`website`, which is its first link's address on the wire and in the column the previous build
reads.

The write shape and the shim are pinned on their own; the routes and the triggers that keep the
column in step are run against Postgres.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from pydantic import BaseModel, ValidationError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1 import contacts as contacts_module
from src.app.api.v1 import courses as courses_module
from src.app.api.v1 import gear_items as gear_items_module
from src.app.api.v1 import people as people_module
from src.app.api.v1 import trips as trips_module
from src.app.core.utils import cache as cache_module
from src.app.crud.crud_contacts import crud_contacts
from src.app.crud.crud_users import read_account
from src.app.models.user import User
from src.app.schemas.contact import (
    ContactCreate,
    ContactCreateInternal,
    ContactRead,
    ContactUpdate,
    ContactUpdateRequest,
    links_after_website,
)
from src.app.schemas.course import CourseCreate, CourseUpdateRequest
from src.app.schemas.gear_item import GearItemCreate, GearItemUpdate
from src.app.schemas.link import LINK_WHITESPACE, LINKS_MAX, Link, LinkRead
from src.app.schemas.person import PersonCreate, PersonUpdateRequest
from src.app.schemas.trip import TripCreate, TripUpdateRequest
from tests.conftest import db_available
from tests.helpers.generators import create_contact

SITE = "https://blueocean.example/"
BOOKING = {"url": "https://blueocean.example/booking", "label": "Booking"}


class TestTheEntry:
    @pytest.mark.parametrize(
        "url", ["https://blueocean.example", "http://blueocean.example/dahab?lang=en", "HTTPS://blueocean.example"]
    )
    def test_an_absolute_http_address_with_a_host_is_kept_as_written(self, url: str) -> None:
        assert Link.model_validate({"url": url}).model_dump() == {"url": url, "label": None}

    @pytest.mark.parametrize(
        "url",
        [
            "blueocean.example",
            "ftp://blueocean.example",
            "mailto:info@blueocean.example",
            "https://",
            "https:///path",
            "https://[blueocean",
            # `urlsplit` strips these before it reads a scheme; the format's pattern opens on a letter.
            "\x01https://blueocean.example",
            "\x00https://blueocean.example",
        ],
    )
    def test_anything_else_is_refused(self, url: str) -> None:
        with pytest.raises(ValidationError, match="absolute http or https"):
            Link.model_validate({"url": url})

    @pytest.mark.parametrize("space", sorted(LINK_WHITESPACE), ids=lambda space: f"U+{ord(space):04X}")
    @pytest.mark.parametrize("where", ["lead", "inside", "trail"])
    def test_every_character_of_the_formats_whitespace_class_is_refused_anywhere(self, space: str, where: str) -> None:
        url = {"lead": f"{space}{SITE}", "inside": f"https://blue{space}ocean.example/", "trail": f"{SITE}{space}"}[
            where
        ]

        with pytest.raises(ValidationError, match=f"U\\+{ord(space):04X}"):
            Link.model_validate({"url": url})

    def test_the_class_is_the_schemas_code_point_for_code_point(self) -> None:
        """§6.18 lists it; the published schema spells it as one character class."""
        expected = {
            *range(0x09, 0x0E),
            *range(0x1C, 0x21),
            0x85,
            0xA0,
            0x1680,
            *range(0x2000, 0x200B),
            0x2028,
            0x2029,
            0x202F,
            0x205F,
            0x3000,
            0xFEFF,
        }

        assert {ord(space) for space in LINK_WHITESPACE} == expected

    def test_the_bounds_are_the_formats(self) -> None:
        Link.model_validate({"url": "https://a.example/" + "a" * 494, "label": "b" * 120})
        with pytest.raises(ValidationError, match="512"):
            Link.model_validate({"url": "https://a.example/" + "a" * 495})
        with pytest.raises(ValidationError, match="120"):
            Link.model_validate({"url": SITE, "label": "b" * 121})

    @pytest.mark.parametrize("label", ["", "   "])
    def test_a_blank_label_is_stored_as_none(self, label: str) -> None:
        assert Link.model_validate({"url": SITE, "label": label}).label is None

    def test_an_unknown_member_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="extra"):
            Link.model_validate({"url": SITE, "extensions": {}})

    def test_a_read_takes_whatever_is_stored(self) -> None:
        assert LinkRead.model_validate({"url": "https://blue ocean.example"}).url == "https://blue ocean.example"


# Each record's create and update shapes, and the smallest body each create takes.
CREATES: list[tuple[type[BaseModel], dict[str, Any]]] = [
    (ContactCreate, {"name": "Blue Ocean"}),
    (ContactCreateInternal, {"name": "Blue Ocean", "user_id": 1}),
    (PersonCreate, {"name": "Alex"}),
    (CourseCreate, {"name": "Advanced Nitrox"}),
    (GearItemCreate, {"name": "MK25"}),
    (TripCreate, {"name": "Red Sea"}),
]
UPDATES: list[type[BaseModel]] = [
    ContactUpdate,
    ContactUpdateRequest,
    PersonUpdateRequest,
    CourseUpdateRequest,
    GearItemUpdate,
    TripUpdateRequest,
]


CREATE_IDS = [schema.__name__ for schema, _ in CREATES]
UPDATE_IDS = [schema.__name__ for schema in UPDATES]


class TestTheList:
    @pytest.mark.parametrize(("schema", "body"), CREATES, ids=CREATE_IDS)
    def test_a_create_without_the_member_stores_none(self, schema: type[BaseModel], body: dict[str, Any]) -> None:
        assert schema.model_validate(body).model_dump()["links"] == []

    @pytest.mark.parametrize(("schema", "body"), CREATES, ids=CREATE_IDS)
    def test_the_order_is_the_divers_and_an_address_named_twice_is_kept_once(
        self, schema: type[BaseModel], body: dict[str, Any]
    ) -> None:
        links = [{"url": "https://b.example"}, BOOKING, {"url": "https://b.example", "label": "Again"}]

        stored = schema.model_validate({**body, "links": links}).model_dump()["links"]

        assert stored == [{"url": "https://b.example", "label": None}, BOOKING]

    @pytest.mark.parametrize(("schema", "body"), CREATES, ids=CREATE_IDS)
    def test_more_than_the_bound_is_refused(self, schema: type[BaseModel], body: dict[str, Any]) -> None:
        links = [{"url": f"https://blueocean.example/{n}"} for n in range(LINKS_MAX + 1)]

        schema.model_validate({**body, "links": links[:LINKS_MAX]})
        with pytest.raises(ValidationError, match=str(LINKS_MAX)):
            schema.model_validate({**body, "links": links})

    @pytest.mark.parametrize("schema", UPDATES, ids=UPDATE_IDS)
    def test_a_patch_replaces_the_list_whole_and_may_not_null_it(self, schema: type[BaseModel]) -> None:
        assert schema.model_validate({"links": []}).model_dump(exclude_unset=True) == {"links": []}
        with pytest.raises(ValidationError, match="links cannot be null"):
            schema.model_validate({"links": None})

    @pytest.mark.parametrize("schema", UPDATES, ids=UPDATE_IDS)
    def test_a_patch_folds_a_repeated_address(self, schema: type[BaseModel]) -> None:
        values = schema.model_validate({"links": [BOOKING, {"url": BOOKING["url"]}]})

        assert values.model_dump(exclude_unset=True)["links"] == [BOOKING]


def _stored(*urls: str) -> list[LinkRead]:
    return [LinkRead(url=url, label=f"label {url}") for url in urls]


class TestTheWebsiteShim:
    def test_a_create_naming_website_alone_stores_one_link_without_a_label(self) -> None:
        contact = ContactCreate.model_validate({"name": "Blue Ocean", "website": SITE})

        assert contact.model_dump(exclude={"website"})["links"] == [{"url": SITE, "label": None}]

    @pytest.mark.parametrize("body", [{"website": None}, {}])
    def test_a_create_with_no_website_stores_no_link(self, body: dict[str, Any]) -> None:
        assert ContactCreate.model_validate({"name": "Blue Ocean", **body}).links == []

    @pytest.mark.parametrize("schema", [ContactCreate, ContactUpdateRequest])
    def test_naming_both_is_refused(self, schema: type[BaseModel]) -> None:
        with pytest.raises(ValidationError, match="Send links or website, not both"):
            schema.model_validate({"name": "Blue Ocean", "website": SITE, "links": []})

    def test_the_website_takes_the_links_check(self) -> None:
        with pytest.raises(ValidationError, match="U\\+0020"):
            ContactCreate.model_validate({"name": "Blue Ocean", "website": "https://blue ocean.example"})

    def test_the_internal_and_admin_shapes_do_not_carry_it(self) -> None:
        assert "website" not in ContactCreateInternal.model_fields
        assert "website" not in ContactUpdate.model_fields

    def test_the_stored_first_address_changes_nothing(self) -> None:
        assert links_after_website(_stored(SITE, BOOKING["url"]), SITE) is None
        assert links_after_website([], None) is None

    def test_another_address_replaces_the_first_and_clears_its_label(self) -> None:
        stored = _stored(SITE, BOOKING["url"])

        assert links_after_website(stored, "https://new.example/") == [
            {"url": "https://new.example/", "label": None},
            {"url": BOOKING["url"], "label": f"label {BOOKING['url']}"},
        ]

    def test_an_address_on_a_contact_with_none_becomes_its_only_link(self) -> None:
        assert links_after_website([], SITE) == [{"url": SITE, "label": None}]

    def test_null_removes_the_first(self) -> None:
        assert links_after_website(_stored(SITE, BOOKING["url"]), None) == [
            {"url": BOOKING["url"], "label": f"label {BOOKING['url']}"}
        ]

    def test_the_second_address_written_into_the_first_folds_into_it(self) -> None:
        assert links_after_website(_stored(SITE, BOOKING["url"]), BOOKING["url"]) == [
            {"url": BOOKING["url"], "label": None}
        ]

    @pytest.mark.parametrize(("links", "website"), [([BOOKING, {"url": SITE}], BOOKING["url"]), ([], None)])
    def test_a_read_derives_it_from_the_first_link(self, links: list[dict[str, Any]], website: str | None) -> None:
        read = ContactRead.model_validate(
            {
                "uuid": uuid7(),
                "name": "Blue Ocean",
                "links": links,
                "website": "https://stale.example/",
                "user_uuid": uuid7(),
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
        )

        assert read.website == website


# -------------- against Postgres --------------


def _contact_row(db: Session, uuid: Any) -> tuple[Any, ...]:
    return db.execute(text("SELECT links, website FROM contact WHERE uuid = :uuid"), {"uuid": uuid}).one()._tuple()


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheRoutes:
    """Scenarios 1 and 2 of the shim, through the routes and the column behind them."""

    @pytest.fixture(autouse=True)
    def no_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The routes' invalidations reach whatever Redis client an earlier test left behind;
        none is what these assertions read."""
        monkeypatch.setattr(cache_module, "client", None)

    @pytest_asyncio.fixture
    async def account(self, diver: User, async_db: AsyncSession) -> dict[str, Any]:
        account = await read_account(async_db, uuid=diver.uuid)
        assert account is not None
        return account

    async def _create(self, async_db: AsyncSession, account: dict[str, Any], body: dict[str, Any]) -> ContactRead:
        return await contacts_module.write_contact(
            request=MagicMock(),
            contact=ContactCreate.model_validate({"name": f"Blue Ocean {uuid7().hex[-8:]}", **body}),
            current_user=account,
            db=async_db,
        )

    async def _patch(
        self, async_db: AsyncSession, account: dict[str, Any], read: ContactRead, body: dict[str, Any]
    ) -> None:
        await contacts_module.patch_contact(
            request=MagicMock(),
            uuid=read.uuid,
            values=ContactUpdateRequest.model_validate(body),
            current_user=account,
            db=async_db,
        )

    @pytest.mark.asyncio
    async def test_a_create_from_a_client_that_sends_website(
        self, db: Session, async_db: AsyncSession, account: dict[str, Any]
    ) -> None:
        read = await self._create(async_db, account, {"website": SITE})

        assert [link.model_dump() for link in read.links] == [{"url": SITE, "label": None}]
        assert read.website == SITE
        assert _contact_row(db, read.uuid) == ([{"url": SITE, "label": None}], SITE)

    @pytest.mark.asyncio
    async def test_the_shim_on_a_patch(self, db: Session, async_db: AsyncSession, account: dict[str, Any]) -> None:
        read = await self._create(async_db, account, {"links": [{"url": SITE}, BOOKING]})

        # The previous web build's save, the whole record with `website` unchanged.
        await self._patch(async_db, account, read, {"name": read.name, "website": SITE, "notes": ""})
        assert _contact_row(db, read.uuid) == ([{"url": SITE, "label": None}, BOOKING], SITE)

        await self._patch(async_db, account, read, {"links": [{"url": SITE, "label": "Site"}, BOOKING]})
        await self._patch(async_db, account, read, {"website": "https://new.example/"})
        assert _contact_row(db, read.uuid) == (
            [{"url": "https://new.example/", "label": None}, BOOKING],
            "https://new.example/",
        )

        await self._patch(async_db, account, read, {"website": None})
        assert _contact_row(db, read.uuid) == ([BOOKING], BOOKING["url"])

        await self._patch(async_db, account, read, {"links": []})
        assert _contact_row(db, read.uuid) == ([], None)

    @pytest.mark.asyncio
    async def test_every_record_stores_and_replaces_its_list(
        self, db: Session, async_db: AsyncSession, account: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        links = [BOOKING, {"url": SITE, "label": None}]
        suffix = uuid7().hex[-8:]
        # The table, the create route and its body's name and shape, the patch route and its shape.
        writes: list[tuple[str, Callable[..., Any], str, type[BaseModel], Callable[..., Any], type[BaseModel]]] = [
            (
                "person",
                people_module.write_person,
                "person",
                PersonCreate,
                people_module.patch_person,
                PersonUpdateRequest,
            ),
            (
                "course",
                courses_module.write_course,
                "course",
                CourseCreate,
                courses_module.patch_course,
                CourseUpdateRequest,
            ),
            (
                "gear_item",
                gear_items_module.write_gear_item,
                "gear_item",
                GearItemCreate,
                gear_items_module.patch_gear_item,
                GearItemUpdate,
            ),
            ("trip", trips_module.write_trip, "trip", TripCreate, trips_module.patch_trip, TripUpdateRequest),
        ]
        for table, write, argument, create_schema, patch, update_schema in writes:
            created = await write(
                request=MagicMock(),
                current_user=account,
                db=async_db,
                **{argument: create_schema.model_validate({"name": f"Links {suffix}", "links": links})},
            )
            assert [link.model_dump() for link in created.links] == links, table

            await patch(
                request=MagicMock(),
                uuid=created.uuid,
                values=update_schema.model_validate({"links": [{"url": SITE}]}),
                current_user=account,
                db=async_db,
            )
            stored = db.execute(
                text(f"SELECT links FROM {table} WHERE uuid = :uuid"),  # noqa: S608 - the table is the test's own
                {"uuid": created.uuid},
            ).scalar_one()
            assert stored == [{"url": SITE, "label": None}], table


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheColumnTheSiblingBuildReads:
    """The triggers the revision adds keep `website` the first link's address on every write
    that names `links` - the routes', the import's and the admin panel's alike - and leave alone
    what a build that writes `website` itself puts there."""

    @pytest.mark.asyncio
    async def test_an_update_naming_links_rewrites_it(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        contact = create_contact(db, diver)

        await crud_contacts.update(db=async_db, object={"links": [BOOKING]}, uuid=contact.uuid)
        assert _contact_row(db, contact.uuid)[1] == BOOKING["url"]

        await crud_contacts.update(db=async_db, object={"links": []}, uuid=contact.uuid)
        assert _contact_row(db, contact.uuid)[1] is None

    def test_an_insert_carrying_links_sets_it(self, db: Session, diver: User) -> None:
        uuid = uuid7()
        db.execute(
            text(
                "INSERT INTO contact (user_id, name, roles, links, notes, uuid, created_at) "
                "VALUES (:user_id, :name, '[]', CAST(:links AS json), '', :uuid, now())"
            ),
            {
                "user_id": diver.id,
                "name": f"Blue Ocean {uuid.hex[-8:]}",
                "links": f'[{{"url": "{SITE}"}}]',
                "uuid": uuid,
            },
        )
        db.commit()

        assert _contact_row(db, uuid)[1] == SITE

    def test_what_the_previous_build_writes_alone_stands(self, db: Session, diver: User) -> None:
        """It inserts with `website` and no links, and updates `website` alone."""
        uuid = uuid7()
        db.execute(
            text(
                "INSERT INTO contact (user_id, name, roles, website, notes, uuid, created_at) "
                "VALUES (:user_id, :name, '[]', :website, '', :uuid, now())"
            ),
            {"user_id": diver.id, "name": f"Blue Ocean {uuid.hex[-8:]}", "website": SITE, "uuid": uuid},
        )
        db.commit()
        assert _contact_row(db, uuid) == ([], SITE)

        db.execute(
            text("UPDATE contact SET website = :website WHERE uuid = :uuid"),
            {"website": "https://x.example", "uuid": uuid},
        )
        db.commit()
        assert _contact_row(db, uuid) == ([], "https://x.example")
