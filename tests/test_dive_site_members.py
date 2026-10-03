"""A dive site's other names, registry entries, depth range, water type, altitude, entry
types and tags (`models/dive_site.py`, `schemas/dive_site.py`), the summary of the dives
there (`crud/crud_dive_sites.py`), and the routes over them (`api/v1/dive_sites.py`).

House style per `test_tags.py`: the schemas and the routes with their collaborators stubbed,
and a Postgres-guarded tail for what only the database settles - the constraints, the
search over a JSON list, the summary's aggregates and the list's orders.
"""

import ast
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1 import dive_sites as dive_sites_module
from src.app.api.v1 import dives as dives_module
from src.app.api.v1 import users as users_module
from src.app.core.exceptions.http_exceptions import UnprocessableEntityException
from src.app.crud.crud_dive_sites import get_summaries_for_dive_sites, sites_by_external_id
from src.app.crud.crud_tags import get_tags_page, replace_tags_for_dive_site
from src.app.models.dive import Dive
from src.app.models.dive_dive_site import DiveDiveSite
from src.app.models.dive_site import DiveSite
from src.app.models.dive_species import DiveSpecies
from src.app.models.user import User
from src.app.schemas.dive_site import (
    DiveSiteCreate,
    DiveSiteListSort,
    DiveSiteRead,
    DiveSiteReadInternal,
    DiveSiteUpdate,
    DiveSiteUpdateRequest,
    ExternalId,
)
from src.app.services.cache_invalidation import invalidate_dive_site_caches
from src.app.services.species_life_list import species_life_list
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_dive_site, create_species, create_tag

USER_ID = 1
API_DIR = Path(__file__).resolve().parents[1] / "src" / "app" / "api"


def _site_body(**members: Any) -> dict[str, Any]:
    return {"name": "Sunabe Seawall", **members}


class TestTheWriteSchema:
    def test_every_member_is_optional_and_a_bare_site_reads_empty(self) -> None:
        body = DiveSiteCreate.model_validate(_site_body())

        assert (body.other_names, body.external_ids, body.entry_types, body.tags) == ([], [], [], [])
        assert (body.depth_from, body.depth_to, body.water_type, body.altitude) == (None, None, None, None)

    def test_another_name_is_trimmed_and_one_the_name_or_an_earlier_one_says_is_dropped(self) -> None:
        """§3 rule 8's comparison, trimmed and case-folded: `SUNABE SEAWALL` is the name, and
        `Großes Riff` and `GROSSES RIFF` are one name."""
        body = DiveSiteCreate.model_validate(
            _site_body(other_names=["  砂辺 ", "SUNABE SEAWALL", "Großes Riff", "GROSSES RIFF", "砂辺"])
        )

        assert body.other_names == ["砂辺", "Großes Riff"]

    @pytest.mark.parametrize("other_name", ["", "   ", "x" * 256])
    def test_a_blank_or_over_long_other_name_is_a_422(self, other_name: str) -> None:
        with pytest.raises(ValidationError):
            DiveSiteCreate.model_validate(_site_body(other_names=[other_name]))

    @pytest.mark.parametrize(
        ("registry", "identifier"),
        [
            ("wikidata", "Q1047347"),
            ("openstreetmap", "node/313862678"),
            ("openstreetmap", "way/1"),
            ("openstreetmap", "relation/42"),
            # A registry the format names no form for is carried as written.
            ("wrecksite.eu", "10021"),
            ("x", "anything at all"),
        ],
    )
    def test_a_registry_entry_in_its_form_is_taken(self, registry: str, identifier: str) -> None:
        body = DiveSiteCreate.model_validate(
            _site_body(external_ids=[{"registry": registry, "identifier": identifier}])
        )

        assert body.external_ids == [ExternalId(registry=registry, identifier=identifier)]

    @pytest.mark.parametrize(
        ("registry", "identifier"),
        [
            ("wikidata", "q1047347"),
            ("wikidata", "Q0123"),
            ("wikidata", "1047347"),
            ("openstreetmap", "313862678"),
            ("openstreetmap", "node/0"),
            ("openstreetmap", "area/12"),
            ("OpenStreetMap", "node/1"),
            ("-osm", "node/1"),
            ("osm", ""),
            ("osm", "x" * 256),
            ("o" * 256, "1"),
        ],
    )
    def test_a_registry_entry_out_of_its_form_is_a_422(self, registry: str, identifier: str) -> None:
        with pytest.raises(ValidationError):
            DiveSiteCreate.model_validate(_site_body(external_ids=[{"registry": registry, "identifier": identifier}]))

    def test_one_registry_entry_twice_is_kept_once(self) -> None:
        """Compared exactly, so a different identifier under one registry is a second
        entry: a site may carry several of one registry."""
        entries = [
            {"registry": "openstreetmap", "identifier": "node/1"},
            {"registry": "openstreetmap", "identifier": "node/2"},
            {"registry": "openstreetmap", "identifier": "node/1"},
        ]

        body = DiveSiteCreate.model_validate(_site_body(external_ids=entries))

        assert [entry.identifier for entry in body.external_ids] == ["node/1", "node/2"]

    def test_the_entry_types_are_a_set_in_vocabulary_order(self) -> None:
        body = DiveSiteCreate.model_validate(_site_body(entry_types=["pool", "shore", "boat", "shore"]))

        assert body.entry_types == ["shore", "boat", "pool"]

    @pytest.mark.parametrize(("member", "value"), [("entry_types", ["sky"]), ("water_type", "lava")])
    def test_a_vocabulary_value_outside_the_set_is_a_422(self, member: str, value: Any) -> None:
        with pytest.raises(ValidationError):
            DiveSiteCreate.model_validate(_site_body(**{member: value}))

    @pytest.mark.parametrize(
        "members",
        [
            {"depth_from": -1},
            {"depth_to": -0.5},
            {"depth_from": 30, "depth_to": 10},
            {"depth_to": float("nan")},
            {"altitude": -451},
            {"altitude": 6501},
        ],
    )
    def test_a_number_out_of_the_format_s_range_is_a_422(self, members: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            DiveSiteCreate.model_validate(_site_body(**members))

    def test_the_two_ends_may_be_one_depth(self) -> None:
        body = DiveSiteCreate.model_validate(_site_body(depth_from=12, depth_to=12, altitude=-450))

        assert (body.depth_from, body.depth_to, body.altitude) == (12, 12, -450)

    def test_the_tags_follow_a_dive_s_rules(self) -> None:
        body = DiveSiteCreate.model_validate(_site_body(tags=[" wreck ", "Wreck", "night"]))

        assert body.tags == ["wreck", "night"]


class TestTheUpdateSchemas:
    @pytest.mark.parametrize("member", ["other_names", "external_ids", "entry_types"])
    @pytest.mark.parametrize("schema", [DiveSiteUpdate, DiveSiteUpdateRequest])
    def test_a_list_is_cleared_with_an_empty_one_never_a_null(self, schema: Any, member: str) -> None:
        with pytest.raises(ValidationError, match="cannot be null"):
            schema.model_validate({member: None})

    @pytest.mark.parametrize("member", ["depth_from", "depth_to", "water_type", "altitude"])
    def test_a_scalar_is_cleared_with_a_null(self, member: str) -> None:
        assert DiveSiteUpdateRequest.model_validate({member: None}).model_dump(exclude_unset=True) == {member: None}

    def test_an_omitted_member_is_left_out_of_the_write(self) -> None:
        """What keeps a site's new members through a save from a client that never sends
        them: the patch writes what the body names and nothing else."""
        body = DiveSiteUpdateRequest.model_validate({"name": "Blue Hole", "notes": "", "latitude": 1, "longitude": 2})

        assert set(body.model_dump(exclude_unset=True)) == {"name", "notes", "latitude", "longitude"}
        assert body.tags is None

    def test_another_name_the_sent_name_says_is_dropped(self) -> None:
        """The admin panel's flat form sends every field it shows, so a rename there drops a
        colliding other name in the schema itself."""
        body = DiveSiteUpdate.model_validate({"name": "Blue Hole", "other_names": ["BLUE HOLE", "El Bells"]})

        assert body.other_names == ["El Bells"]

    def test_a_pair_sent_whole_in_the_wrong_order_is_a_422(self) -> None:
        with pytest.raises(ValidationError):
            DiveSiteUpdateRequest.model_validate({"depth_from": 30, "depth_to": 10})


class TestTheReadSchema:
    def test_a_stored_vocabulary_value_outside_the_set_reads_back_as_itself(self) -> None:
        read = DiveSiteReadInternal.model_validate(
            {
                "id": 1,
                "user_id": 1,
                "uuid": uuid7(),
                "name": "Blue Hole",
                "created_at": datetime(2026, 6, 1, tzinfo=UTC),
                "water_type": "lava",
                "entry_types": ["shore", "zipline"],
            }
        )

        assert (read.water_type, read.entry_types) == ("lava", ["shore", "zipline"])

    def test_a_site_no_dive_names_reads_an_empty_summary(self) -> None:
        read = DiveSiteRead.model_validate(
            {"uuid": uuid7(), "user_uuid": uuid7(), "name": "Blue Hole", "created_at": datetime(2026, 6, 1, tzinfo=UTC)}
        )

        assert (read.dive_count, read.last_dived_on, read.max_dive_depth) == (0, None, None)
        assert (read.species_count, read.average_rating, read.tags) == (0, None, [])


# ------------------------------------------------------------------ the routes, stubbed


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """`patch_dive_site` with a stored site carrying other names and a depth range, and
    every collaborator recorded."""
    seen: dict[str, Any] = {}
    site = DiveSiteReadInternal(
        id=11,
        user_id=USER_ID,
        uuid=uuid7(),
        name="Blue Hole",
        other_names=["El Bells", "The Arch"],
        depth_from=5.0,
        depth_to=30.0,
        location_name="Dahab, Egypt",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    seen["site"] = site

    async def fake_update(*, db: Any, object: dict, uuid: Any) -> None:
        seen["update_data"] = object

    seen["replace_tags"] = AsyncMock()
    seen["resolve_tags"] = AsyncMock(return_value=[3, 4])
    seen["invalidate_sites"] = AsyncMock()
    seen["invalidate_dives"] = AsyncMock()
    monkeypatch.setattr(dive_sites_module, "_get_owned_dive_site", AsyncMock(return_value=site))
    monkeypatch.setattr(dive_sites_module, "dive_site_name_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(dive_sites_module.crud_dive_sites, "update", fake_update)
    monkeypatch.setattr(dive_sites_module, "replace_tags_for_dive_site", seen["replace_tags"])
    monkeypatch.setattr(dive_sites_module, "resolve_tag_ids", seen["resolve_tags"])
    monkeypatch.setattr(dive_sites_module, "invalidate_dive_site_caches", seen["invalidate_sites"])
    monkeypatch.setattr(dive_sites_module, "invalidate_dive_caches", seen["invalidate_dives"])
    return seen


async def _patch(body: dict[str, Any]) -> None:
    await dive_sites_module.patch_dive_site(
        request=MagicMock(),
        uuid=uuid7(),
        values=DiveSiteUpdateRequest.model_validate(body),
        current_user={"id": USER_ID, "uuid": uuid7()},
        db=MagicMock(),
    )


class TestThePatch:
    @pytest.mark.asyncio
    async def test_a_rename_onto_another_name_drops_that_one(self, stored: dict[str, Any]) -> None:
        """A rename is a rename, never a 422: the other name it collides with goes."""
        await _patch({"name": "el bells"})

        assert stored["update_data"] == {"name": "el bells", "other_names": ["The Arch"]}

    @pytest.mark.asyncio
    async def test_a_rename_that_collides_with_nothing_leaves_the_other_names_alone(
        self, stored: dict[str, Any]
    ) -> None:
        await _patch({"name": "Blue Hole (Dahab)"})

        assert stored["update_data"] == {"name": "Blue Hole (Dahab)"}

    @pytest.mark.asyncio
    async def test_other_names_alone_are_held_to_the_stored_name(self, stored: dict[str, Any]) -> None:
        await _patch({"other_names": ["BLUE HOLE", "Bells"]})

        assert stored["update_data"] == {"other_names": ["Bells"]}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [{"depth_from": 31}, {"depth_to": 4}])
    async def test_half_a_depth_range_against_the_stored_half_is_a_422(
        self, stored: dict[str, Any], body: dict[str, Any]
    ) -> None:
        with pytest.raises(UnprocessableEntityException):
            await _patch(body)

        assert "update_data" not in stored

    @pytest.mark.asyncio
    async def test_half_a_depth_range_that_fits_is_written(self, stored: dict[str, Any]) -> None:
        await _patch({"depth_to": None})

        assert stored["update_data"] == {"depth_to": None}

    @pytest.mark.asyncio
    async def test_the_tags_are_replaced_whole_and_the_site_reads_dropped(self, stored: dict[str, Any]) -> None:
        await _patch({"tags": ["wreck", "night"]})

        stored["replace_tags"].assert_awaited_once()
        assert stored["replace_tags"].await_args.args[1:] == (11, [3, 4])
        stored["invalidate_sites"].assert_awaited_once_with(USER_ID)
        assert "update_data" not in stored

    @pytest.mark.asyncio
    async def test_the_new_members_alone_leave_the_dive_reads_cached(self, stored: dict[str, Any]) -> None:
        """A dive read embeds a site's name, place and pin and nothing else of it, so a
        patch of the new members reshapes no dive read."""
        await _patch(
            {
                "other_names": ["Bells"],
                "external_ids": [{"registry": "wikidata", "identifier": "Q1"}],
                "depth_to": 40,
                "water_type": "salt",
                "altitude": 0,
                "entry_types": ["shore"],
                "tags": ["wreck"],
            }
        )

        stored["invalidate_sites"].assert_awaited_once_with(USER_ID)
        stored["invalidate_dives"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_empty_patch_drops_nothing(self, stored: dict[str, Any]) -> None:
        await _patch({})

        stored["invalidate_sites"].assert_not_awaited()


class TestTheReads:
    @pytest.mark.asyncio
    async def test_a_tag_that_is_not_the_caller_s_answers_an_empty_page(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Not a 404: the uuid names a resource whose existence must stay unprobeable."""
        cached = AsyncMock(return_value={"data": []})
        monkeypatch.setattr(dive_sites_module, "resolve_tag_id_for_user", AsyncMock(return_value=None))
        monkeypatch.setattr(dive_sites_module, "_cached_read_dive_sites", cached)

        await dive_sites_module.read_dive_sites(
            request=MagicMock(),
            current_user={"id": USER_ID, "uuid": uuid7()},
            db=MagicMock(),
            search="  Sunabe ",
            tag_uuid=uuid7(),
            sort=DiveSiteListSort.LAST_DIVED_ON,
        )

        assert cached.await_args is not None
        kwargs = cached.await_args.kwargs
        assert (kwargs["tag_id"], kwargs["search"], kwargs["sort"]) == (-1, "sunabe", DiveSiteListSort.LAST_DIVED_ON)

    @pytest.mark.asyncio
    async def test_the_single_read_authorizes_before_the_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def read(*_: Any, **__: Any) -> dict[str, Any]:
            calls.append("read")
            return {}

        monkeypatch.setattr(
            dive_sites_module, "_get_owned_dive_site", AsyncMock(side_effect=lambda *_: calls.append("owned"))
        )
        monkeypatch.setattr(dive_sites_module, "_cached_read_dive_site", AsyncMock(side_effect=read))

        await dive_sites_module.read_dive_site(
            request=MagicMock(), uuid=uuid7(), current_user={"id": USER_ID, "uuid": uuid7()}, db=MagicMock()
        )

        assert calls == ["owned", "read"]

    @pytest.mark.asyncio
    async def test_a_tag_deleted_mid_write_is_a_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        error = IntegrityError("INSERT", {}, Exception('violates foreign key constraint "dive_site_tag_tag_id_fkey"'))
        monkeypatch.setattr(dive_sites_module, "replace_tags_for_dive_site", AsyncMock(side_effect=error))
        db = MagicMock()
        db.rollback = AsyncMock()

        with pytest.raises(UnprocessableEntityException):
            await dive_sites_module._replace_tags(db, dive_site_id=11, tag_ids=[3])

        db.rollback.assert_awaited_once()


class TestEveryWriteThatMovesASummaryDropsTheSiteReads:
    """A site's read summarises the dives naming it, as a trip's counts the dives on it, so
    every route that drops the trip reads over what a write did to a trip's dives drops the
    site reads beside them. Read off the routes rather than listed, so a new dive write that
    remembers the trip and forgets the site fails here.

    The writes named below change a trip's own members - its parts, its people, an
    accommodation - and touch no dive's site, depth, rating, date or sightings.
    """

    TRIP_MEMBERS_ONLY = frozenset(
        {
            ("contacts.py", "erase_contact"),
            ("people.py", "erase_person"),
            ("trips.py", "write_trip"),
            ("trips.py", "patch_trip"),
            ("trips.py", "erase_trip"),
        }
    )

    @staticmethod
    def _calls(function: ast.AST) -> set[str]:
        return {
            node.func.id
            for node in ast.walk(function)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

    def test_each_one_drops_both(self) -> None:
        dropping_trips: set[tuple[str, str]] = set()
        forgetting_sites: list[str] = []
        for path in sorted((API_DIR / "v1").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text())):
                if not isinstance(node, ast.AsyncFunctionDef):
                    continue
                calls = self._calls(node)
                if "invalidate_trip_caches" not in calls:
                    continue
                dropping_trips.add((path.name, node.name))
                if (path.name, node.name) not in self.TRIP_MEMBERS_ONLY and "invalidate_dive_site_caches" not in calls:
                    forgetting_sites.append(f"{path.name}::{node.name}")

        assert not forgetting_sites
        # Every exclusion still names a route that drops the trip reads, so none goes stale.
        assert self.TRIP_MEMBERS_ONLY <= dropping_trips
        # And the floor: the four dive writes, the import and the site delete.
        assert {
            ("dives.py", "write_dive"),
            ("dives.py", "patch_dive"),
            ("dives.py", "erase_dive"),
            ("dives.py", "merge_two_dives"),
            ("logbook_import.py", "apply_logbook_import"),
            ("dive_sites.py", "erase_dive_site"),
        } <= dropping_trips - self.TRIP_MEMBERS_ONLY


class TestTheCacheKeys:
    def test_the_list_key_carries_every_query_parameter(self) -> None:
        """`@cache` keys on its prefix's placeholders alone."""
        prefix = dive_sites_module._LIST_CACHE_KEY_PREFIX
        for segment in ("page_{page}", "items_per_page:{items_per_page}", "search:{search}", "tag_{tag_id}"):
            assert segment in prefix
        assert "sort_{sort}" in prefix
        assert prefix.startswith("user_{user_id}_dive_sites:")

    @pytest.mark.asyncio
    async def test_the_invalidator_sweeps_the_list_and_every_single_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The single read sits under the owner, so a write that names no site can still
        reach it - the key the hand-written read is cached under, and the pattern dropping
        it, agree."""
        swept = AsyncMock()
        monkeypatch.setattr("src.app.services.cache_invalidation.delete_keys_by_pattern", swept)

        await invalidate_dive_site_caches(7)

        patterns = [call.args[0] for call in swept.await_args_list]
        assert patterns == ["user_7_dive_sites:*", "user_7_dive_site:*"]
        source = Path(dive_sites_module.__file__).read_text()
        assert '@cache(key_prefix="user_{user_id}_dive_site", resource_id_name="uuid"' in source


# ------------------------------------------------------------------ against Postgres


def _as(user: User) -> dict[str, Any]:
    return {"id": user.id, "uuid": user.uuid, "username": user.username, "is_superuser": False}


def _no_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("invalidate_dive_site_caches", "invalidate_dive_caches", "invalidate_trip_caches"):
        monkeypatch.setattr(dive_sites_module, name, AsyncMock())


def _dive(db: Session, user: User, start: datetime, **columns: Any) -> Dive:
    dive = Dive(user_id=user.id, dive_number=1, start_time=start, duration=1800, notes="", **columns)
    db.add(dive)
    db.commit()
    return dive


def _at(db: Session, dive: Dive, *sites: DiveSite) -> None:
    db.add_all(DiveDiveSite(dive_id=dive.id, dive_site_id=site.id, position=n) for n, site in enumerate(sites))
    db.commit()


async def _read(async_db: AsyncSession, user: User, uuid: Any) -> DiveSiteRead:
    read = await dive_sites_module._cached_read_dive_site.__wrapped__(  # type: ignore[attr-defined]
        None, user_id=user.id, uuid=uuid, owner_uuid=user.uuid, db=async_db
    )
    return DiveSiteRead.model_validate(read)


async def _page(async_db: AsyncSession, user: User, **arguments: Any) -> list[dict[str, Any]]:
    query: dict[str, Any] = {"search": None, "tag_id": None, "sort": DiveSiteListSort.NAME}
    query.update(arguments)
    result = await dive_sites_module._cached_read_dive_sites.__wrapped__(  # type: ignore[attr-defined]
        None, user_id=user.id, user_uuid=user.uuid, db=async_db, page=1, items_per_page=50, **query
    )
    rows: list[dict[str, Any]] = result["data"]
    return rows


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestTheDatabase:
    @pytest.mark.parametrize(
        "columns",
        [
            {"depth_from": -1.0},
            {"depth_to": -1.0},
            {"depth_from": 30.0, "depth_to": 10.0},
            {"altitude": 6501},
            {"altitude": -451},
        ],
    )
    def test_the_constraints_hold_the_format_s_bounds(self, db: Session, diver: User, columns: dict[str, Any]) -> None:
        """The backstop for a writer behind no schema - a hand-run `UPDATE`."""
        site = create_dive_site(db, diver)
        with pytest.raises(IntegrityError, match="ck_dive_site_"):
            db.execute(update(DiveSite).where(DiveSite.id == site.id).values(**columns))
            db.commit()
        db.rollback()

    def test_an_existing_site_reads_empty_new_members(self, db: Session, diver: User) -> None:
        """What every row on the flagship reads as once the revision runs: lists empty, the
        rest null."""
        site = create_dive_site(db, diver)
        db.refresh(site)

        assert (site.other_names, site.external_ids, site.entry_types) == ([], [], [])
        assert (site.depth_from, site.depth_to, site.water_type, site.altitude) == (None, None, None, None)

    @pytest.mark.asyncio
    async def test_a_site_is_written_with_every_member_and_read_back(
        self, db: Session, async_db: AsyncSession, diver: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_caches(monkeypatch)
        existing = create_tag(db, diver, name=f"wreck {uuid7().hex[-6:]}")

        created = await dive_sites_module.write_dive_site(
            request=MagicMock(),
            dive_site=DiveSiteCreate.model_validate(
                {
                    "name": f"Sunabe {uuid7().hex[-6:]}",
                    "other_names": ["砂辺", "Seawall"],
                    "external_ids": [{"registry": "openstreetmap", "identifier": "node/313862678"}],
                    "depth_from": 3,
                    "depth_to": 18.5,
                    "water_type": "salt",
                    "altitude": 0,
                    "entry_types": ["boat", "shore"],
                    "tags": [existing.name.upper(), "night"],
                }
            ),
            current_user=_as(diver),
            db=async_db,
        )
        read = await _read(async_db, diver, created.uuid)

        for site in (created, read):
            assert site.other_names == ["砂辺", "Seawall"]
            assert [entry.model_dump() for entry in site.external_ids] == [
                {"registry": "openstreetmap", "identifier": "node/313862678"}
            ]
            assert (site.depth_from, site.depth_to, site.water_type, site.altitude) == (3, 18.5, "salt", 0)
            assert site.entry_types == ["shore", "boat"]
            assert site.tags == [existing.name, "night"]
            assert site.dive_count == 0

    @pytest.mark.asyncio
    async def test_a_save_that_names_none_of_the_new_members_keeps_them(
        self, db: Session, async_db: AsyncSession, diver: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The deploy-skew case: the build before this one edits a site by sending the
        members it always sent, and the patch leaves every member it omits as it was."""
        _no_caches(monkeypatch)
        site = create_dive_site(db, diver)
        tag = create_tag(db, diver)
        site.other_names, site.water_type, site.depth_to = ["Pescador Wall"], "salt", 40.0
        site.external_ids = [{"registry": "wikidata", "identifier": "Q12"}]
        db.commit()
        await replace_tags_for_dive_site(async_db, site.id, [tag.id])

        await dive_sites_module.patch_dive_site(
            request=MagicMock(),
            uuid=site.uuid,
            values=DiveSiteUpdateRequest.model_validate(
                {"name": site.name, "notes": "Turtles", "latitude": 9.9, "longitude": 123.3, "location": None}
            ),
            current_user=_as(diver),
            db=async_db,
        )
        read = await _read(async_db, diver, site.uuid)

        assert (read.notes, read.other_names, read.water_type, read.depth_to) == (
            "Turtles",
            ["Pescador Wall"],
            "salt",
            40.0,
        )
        assert [entry.identifier for entry in read.external_ids] == ["Q12"]
        assert read.tags == [tag.name]

    @pytest.mark.asyncio
    async def test_the_summary_counts_live_dives_at_any_position(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        """The second site of a drift dive was dived as much as the first, and a deleted dive
        and another diver's say nothing of this site."""
        reef, wall = create_dive_site(db, diver), create_dive_site(db, diver)
        start = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
        moray, turtle = create_species(db), create_species(db)
        first = _dive(db, diver, start, max_depth=18.0, rating=4)
        second = _dive(db, diver, start + timedelta(days=2), max_depth=31.5, rating=None)
        third = _dive(db, diver, start + timedelta(days=4), max_depth=12.0, rating=2)
        deleted = _dive(db, diver, start + timedelta(days=9), max_depth=60.0, rating=5, is_deleted=True)
        _at(db, first, reef)
        _at(db, second, wall, reef)
        _at(db, third, wall)
        _at(db, deleted, reef)
        db.add_all(
            [
                DiveSpecies(dive_id=first.id, species_id=moray.id, position=0),
                DiveSpecies(dive_id=first.id, species_id=turtle.id, position=1),
                DiveSpecies(dive_id=second.id, species_id=moray.id, position=0),
                DiveSpecies(dive_id=deleted.id, species_id=create_species(db).id, position=0),
            ]
        )
        db.commit()
        # Another diver's dive at this diver's site id is not this diver's: scoped by owner.
        _at(db, _dive(db, other_diver, start + timedelta(days=20), max_depth=99.0), reef)

        summaries = await get_summaries_for_dive_sites(async_db, dive_site_ids=[reef.id, wall.id], user_id=diver.id)

        assert summaries[reef.id].model_dump() == {
            "dive_count": 2,
            "last_dived_on": date(2026, 6, 3),
            "max_dive_depth": 31.5,
            "species_count": 2,
            "average_rating": 4.0,
        }
        assert summaries[wall.id].model_dump() == {
            "dive_count": 2,
            "last_dived_on": date(2026, 6, 5),
            "max_dive_depth": 31.5,
            "species_count": 1,
            "average_rating": 2.0,
        }

    @pytest.mark.asyncio
    async def test_the_last_dive_s_date_is_its_own_local_date(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """An evening dive in Honolulu is the next day in UTC; the diver logged it on the
        day they dived, and that is the date shown. A date-only dive reads as its day."""
        evening, dated = create_dive_site(db, diver), create_dive_site(db, diver)
        _at(db, _dive(db, diver, datetime(2026, 6, 2, 5, 30, tzinfo=UTC), utc_offset_minutes=-600), evening)
        date_only = _dive(db, diver, datetime(2026, 6, 9, tzinfo=UTC))
        db.execute(update(Dive).where(Dive.id == date_only.id).values(utc_offset_minutes=None, start_date_only=True))
        db.commit()
        _at(db, date_only, dated)

        summaries = await get_summaries_for_dive_sites(async_db, dive_site_ids=[evening.id, dated.id], user_id=diver.id)

        assert summaries[evening.id].last_dived_on == date(2026, 6, 1)
        assert summaries[dated.id].last_dived_on == date(2026, 6, 9)

    @pytest.mark.asyncio
    async def test_the_list_sorts_on_the_summary_with_never_dived_last(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        sites = {name: create_dive_site(db, diver) for name in ("a", "b", "c")}
        for name, site in sites.items():
            site.name = f"{name} {uuid7().hex[-6:]}"
        db.commit()
        start = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
        for offset in (0, 1):
            _at(db, _dive(db, diver, start + timedelta(days=offset)), sites["b"])
        _at(db, _dive(db, diver, start + timedelta(days=5)), sites["c"])

        def names(rows: list[dict[str, Any]]) -> list[str]:
            return [row["name"][0] for row in rows]

        assert names(await _page(async_db, diver)) == ["a", "b", "c"]
        assert names(await _page(async_db, diver, sort=DiveSiteListSort.DIVE_COUNT)) == ["b", "c", "a"]
        assert names(await _page(async_db, diver, sort=DiveSiteListSort.LAST_DIVED_ON)) == ["c", "b", "a"]
        by_name = {row["name"][0]: row for row in await _page(async_db, diver)}
        assert (by_name["b"]["dive_count"], by_name["b"]["last_dived_on"]) == (2, date(2026, 6, 2))

    @pytest.mark.asyncio
    async def test_the_list_filters_on_a_tag(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        tagged, plain = create_dive_site(db, diver), create_dive_site(db, diver)
        tag = create_tag(db, diver)
        await replace_tags_for_dive_site(async_db, tagged.id, [tag.id])

        assert [row["uuid"] for row in await _page(async_db, diver, tag_id=tag.id)] == [tagged.uuid]
        assert await _page(async_db, diver, tag_id=-1) == []
        assert plain.uuid in {row["uuid"] for row in await _page(async_db, diver)}

    @pytest.mark.asyncio
    async def test_the_search_finds_another_name_in_any_script(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """The trap: the JSON is stored ASCII-escaped, so a match on the column's own text
        would never find `砂辺`. A substring, case-insensitive, as every picker search is."""
        site, other = create_dive_site(db, diver), create_dive_site(db, diver)
        site.other_names = ["砂辺", "Sunabe Seawall"]
        other.other_names = ["Elsewhere"]
        db.commit()

        for term in ("砂辺", "砂", "seaWALL", "sunabe"):
            assert [row["uuid"] for row in await _page(async_db, diver, search=term)] == [site.uuid], term
        # A JSON spelling of the list is not a name either.
        assert await _page(async_db, diver, search='", "') == []

    @pytest.mark.asyncio
    async def test_a_registry_entry_names_the_caller_s_sites_first_by_name(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        entry = {"registry": "openstreetmap", "identifier": f"node/{uuid7().int % 10**9 + 1}"}
        later, earlier, foreign = (
            create_dive_site(db, diver),
            create_dive_site(db, diver),
            create_dive_site(db, other_diver),
        )
        later.name, earlier.name = f"Z {later.name}", f"A {earlier.name}"
        for site in (later, earlier, foreign):
            site.external_ids = [entry]
        db.commit()

        held = await sites_by_external_id(async_db, user_id=diver.id)

        assert [site.uuid for site in held[(entry["registry"], entry["identifier"])]] == [earlier.uuid, later.uuid]

    @pytest.mark.asyncio
    async def test_a_tag_counts_the_sites_carrying_it(self, db: Session, async_db: AsyncSession, diver: User) -> None:
        """A tag on sites alone is not unused."""
        tag = create_tag(db, diver, name=f"cave {uuid7().hex[-6:]}")
        site = create_dive_site(db, diver)
        await replace_tags_for_dive_site(async_db, site.id, [tag.id])

        page = await get_tags_page(async_db, user_id=diver.id, offset=0, limit=10, search=tag.name)

        assert [(row.name, row.dive_count, row.site_count) for row in page["data"]] == [(tag.name, 0, 1)]

    @pytest.mark.asyncio
    async def test_the_life_list_takes_the_site_at_any_position(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        """So a site page's species list is as long as its summary's species count."""
        reef, wall = create_dive_site(db, diver), create_dive_site(db, diver)
        moray, turtle = create_species(db), create_species(db)
        first = create_dive(db, diver)
        second = create_dive(db, diver)
        _at(db, first, reef)
        _at(db, second, wall, reef)
        db.add_all(
            [
                DiveSpecies(dive_id=first.id, species_id=moray.id, position=0),
                DiveSpecies(dive_id=second.id, species_id=turtle.id, position=0),
            ]
        )
        db.commit()

        at_reef = await species_life_list(async_db, user_id=diver.id, offset=0, limit=10, dive_site_id=reef.id)
        at_wall = await species_life_list(async_db, user_id=diver.id, offset=0, limit=10, dive_site_id=wall.id)
        summary = (await get_summaries_for_dive_sites(async_db, dive_site_ids=[reef.id], user_id=diver.id))[reef.id]

        assert at_reef["total_count"] == summary.species_count == 2
        assert [row["uuid"] for row in at_wall["data"]] == [turtle.uuid]
        assert (await species_life_list(async_db, user_id=diver.id, offset=0, limit=10, dive_site_id=-1))[
            "total_count"
        ] == 0

    @pytest.mark.asyncio
    async def test_a_dive_write_is_read_at_once_through_the_site(
        self, db: Session, async_db: AsyncSession, diver: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The summary is never stale: the dive routes drop the site reads with the trip
        reads, and the next read says what the database says."""
        swept = AsyncMock()
        monkeypatch.setattr(dives_module, "invalidate_dive_site_caches", swept)
        for name in ("invalidate_dive_caches", "invalidate_gear_caches", "invalidate_trip_caches"):
            monkeypatch.setattr(dives_module, name, AsyncMock())
        site = create_dive_site(db, diver)
        dive = create_dive(db, diver)

        await dives_module.patch_dive(
            request=MagicMock(),
            uuid=dive.uuid,
            values=dives_module.DiveUpdateRequest.model_validate({"dive_site_uuids": [str(site.uuid)]}),
            current_user=_as(diver),
            db=async_db,
        )

        swept.assert_awaited_once_with(diver.id)
        assert (await _read(async_db, diver, site.uuid)).dive_count == 1

    def test_the_life_list_route_resolves_the_site_against_the_caller(self) -> None:
        """A foreign uuid reads as -1, the dives list's empty page, never another diver's."""
        source = Path(users_module.__file__).read_text()

        assert "resolve_dive_site_ids_for_user(" in source
        assert "site_{dive_site_id}" in users_module.SPECIES_LIFE_LIST_CACHE_KEY_PREFIX
