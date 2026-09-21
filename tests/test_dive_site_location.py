"""A dive site's locality is the same place object a trip part carries.

The member used to be a string. It is now `schemas/location.py`'s object on both hosts, so
what needs pinning here is the half a schema cannot state on its own:

- **What reaches the columns.** The wire nests one `location`; the table holds eight
  prefixed columns beside the site's own pin. A write that only mapped the name would drop
  a picked locality's fuller name, its centre and its box on every edit of an existing
  site, and the migration behind it has no rollback.
- **That a write replaces the place whole.** It is a value object with no identity, so
  there is nothing to merge a partial one into - and clearing it has to stay possible,
  because that is how a site entered with the wrong place is corrected.
- **That uniqueness keys on the locality's name and on nothing else about it.** Against
  Postgres, so the index and `dive_site_name_exists` are held to the same rule rather than
  to each other's description of it.

The coordinate-pair rule and the dive summary's staleness gates are
`test_dive_site_coordinates.py`'s; the geocoder's own wire members are
`test_geocoding.py`'s and are not renamed by any of this.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1 import dive_sites as dive_sites_module
from src.app.core.exceptions.http_exceptions import DuplicateValueException
from src.app.core.utils import cache as cache_module
from src.app.crud.crud_dive_sites import dive_site_name_exists
from src.app.models.dive_site import DiveSite
from src.app.models.user import User
from src.app.schemas.dive_site import (
    DiveSiteCreate,
    DiveSiteCreateInternal,
    DiveSiteRead,
    DiveSiteUpdate,
    DiveSiteUpdateRequest,
)
from src.app.schemas.location import DIVE_SITE_LOCATION_PREFIX, LOCATION_FIELDS, location_from_row
from tests.conftest import db_available

USER_UUID = uuid7()

# A place a geocoder answered for in full: both names, its own centre, its own box.
DAHAB = {
    "name": "Dahab, Egypt",
    "full_name": "Dahab, South Sinai Governorate, Egypt",
    "latitude": 28.4949,
    "longitude": 34.5136,
    "bbox_south": 28.44,
    "bbox_north": 28.54,
    "bbox_west": 34.46,
    "bbox_east": 34.56,
}

# The site's own pin, deliberately a different point from the locality's centre above.
BLUE_HOLE = (28.5717, 34.5372)


def _columns() -> dict[str, Any]:
    """`DAHAB` as the eight columns behind it."""
    return {f"{DIVE_SITE_LOCATION_PREFIX}{field}": DAHAB[field] for field in LOCATION_FIELDS}


class TestWhatTheWireCarries:
    def test_a_created_site_takes_the_whole_place(self) -> None:
        site = DiveSiteCreate.model_validate({"name": "Blue Hole", "location": DAHAB})

        assert site.location is not None
        assert site.location.model_dump() == DAHAB

    def test_a_typed_place_is_a_name_and_nothing_else(self) -> None:
        """The free-text escape hatch, on the terms a trip part already has it: a
        throttled geocoder must never stop a save."""
        site = DiveSiteCreate.model_validate({"name": "Blue Hole", "location": {"name": "Uncle Bert's reef"}})

        assert site.location is not None
        assert (site.location.full_name, site.location.latitude, site.location.bbox_south) == (None, None, None)

    def test_a_site_with_no_place_omits_the_member(self) -> None:
        assert DiveSiteCreate.model_validate({"name": "Blue Hole"}).location is None

    def test_a_locality_box_still_needs_its_centre(self) -> None:
        """`LocationInput`'s rules reach this host unchanged - one validator, both hosts."""
        with pytest.raises(ValueError, match="a bounding box needs latitude and longitude"):
            DiveSiteCreate.model_validate(
                {"name": "Blue Hole", "location": {**DAHAB, "latitude": None, "longitude": None}}
            )

    def test_a_read_nests_the_place_beside_the_sites_own_pin(self) -> None:
        """The two positions are different facts (spec §6.10), and the read is where a
        client meets them: one under `location`, one on the site."""
        site = DiveSiteRead(
            uuid=uuid7(),
            user_uuid=USER_UUID,
            name="Blue Hole",
            latitude=BLUE_HOLE[0],
            longitude=BLUE_HOLE[1],
            location=DAHAB,  # type: ignore[arg-type]
            created_at=datetime.now(UTC),
        )
        dumped = site.model_dump()

        assert dumped["location"] == DAHAB
        assert (dumped["latitude"], dumped["longitude"]) == BLUE_HOLE
        assert dumped["location"]["latitude"] != dumped["latitude"]


class TestAnEmptyLocalityNameIsRefusedOnTheWayInOnly:
    """`""` is not a short name, it is a place with none - §6.9 makes `name` 1-255, so an
    empty one exports a `location.name` the format's schema rejects and reads back as a
    nameless place. Every way in refuses it; nothing on the way out does.

    The asymmetry is deliberate and is `WholeCoordinatePair`'s: a row only raw SQL could
    have written should read back as the odd thing it is rather than turn every read of
    that site into a 500. The migration's `nullif` clears the rows the old unbounded field
    left behind, and these schemas are what stop a new one arriving.
    """

    def test_the_api_refuses_it(self) -> None:
        with pytest.raises(ValueError):
            DiveSiteCreate.model_validate({"name": "Blue Hole", "location": {"name": ""}})

    @pytest.mark.parametrize(
        ("schema", "body"),
        [
            (DiveSiteCreateInternal, {"name": "Blue Hole", "user_id": 1}),
            (DiveSiteUpdate, {"name": "Blue Hole"}),
        ],
        ids=["DiveSiteCreateInternal", "DiveSiteUpdate"],
    )
    def test_the_admin_form_refuses_it_too(self, schema: Any, body: dict[str, Any]) -> None:
        """Those two are what `admin/views.py` registers for this table, and they write the
        columns directly rather than through `LocationInput`."""
        with pytest.raises(ValueError):
            schema.model_validate({**body, "location_name": ""})

    @pytest.mark.parametrize(
        ("schema", "body"),
        [
            (DiveSiteCreateInternal, {"name": "Blue Hole", "user_id": 1}),
            (DiveSiteUpdate, {"name": "Blue Hole"}),
        ],
        ids=["DiveSiteCreateInternal", "DiveSiteUpdate"],
    )
    def test_clearing_the_locality_is_untouched(self, schema: Any, body: dict[str, Any]) -> None:
        """A minimum length says nothing about absence, and clearing is how a site entered
        with the wrong place is corrected."""
        assert schema.model_validate({**body, "location_name": None}).location_name is None

    def test_a_row_that_already_holds_one_still_reads_back(self) -> None:
        row = SimpleNamespace(**{f"{DIVE_SITE_LOCATION_PREFIX}{field}": None for field in LOCATION_FIELDS})
        row.location_name = ""

        place = location_from_row(row, DIVE_SITE_LOCATION_PREFIX)

        assert place is not None
        assert place.name == ""


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stubs out everything `patch_dive_site` touches and records the `update_data` it
    builds - the `test_dive_site_coordinates.py` fixture, with a locality on the row."""
    seen: dict[str, Any] = {}

    db_dive_site = MagicMock()
    db_dive_site.id = 11
    db_dive_site.user_id = 1
    db_dive_site.name = "Blue Hole"
    db_dive_site.location_name = "Dahab, Egypt"
    db_dive_site.latitude = None
    db_dive_site.longitude = None
    seen["db_dive_site"] = db_dive_site

    async def fake_update(*, db: Any, object: dict, uuid: uuid_pkg.UUID) -> None:
        seen["update_data"] = object

    seen["name_exists"] = AsyncMock(return_value=False)
    monkeypatch.setattr(dive_sites_module, "_get_owned_dive_site", AsyncMock(return_value=db_dive_site))
    monkeypatch.setattr(dive_sites_module, "dive_site_name_exists", seen["name_exists"])
    monkeypatch.setattr(dive_sites_module.crud_dive_sites, "update", fake_update)
    monkeypatch.setattr(dive_sites_module._dive_site_cache, "invalidate_list", AsyncMock())
    seen["invalidate_dive_caches"] = AsyncMock()
    monkeypatch.setattr(dive_sites_module, "invalidate_dive_caches", seen["invalidate_dive_caches"])

    return seen


async def _patch(values: dict[str, Any], mock_redis: Any) -> None:
    request = MagicMock()
    request.method = "PATCH"
    with patch.object(cache_module, "client", mock_redis):
        await dive_sites_module.patch_dive_site(
            request=request,
            uuid=uuid7(),
            values=DiveSiteUpdateRequest.model_validate(values),
            current_user={"id": 1, "uuid": USER_UUID},
            db=MagicMock(),
        )


class TestWhatReachesTheColumns:
    """The wire nests, the table does not, and nothing else pins the mapping."""

    @pytest.mark.asyncio
    async def test_a_picked_place_writes_every_column_of_it(self, captured: dict[str, Any], mock_redis: Any) -> None:
        await _patch({"location": DAHAB}, mock_redis)

        assert captured["update_data"] == _columns()

    @pytest.mark.asyncio
    async def test_a_place_with_only_a_name_clears_the_rest_rather_than_leaving_it(
        self, captured: dict[str, Any], mock_redis: Any
    ) -> None:
        """The whole reason the write replaces rather than merges: a diver who picks a
        locality and then types a different one must not keep the first one's centre and
        box, which would put the new place's name on the old place's map."""
        await _patch({"location": {"name": "Nuweiba, Egypt"}}, mock_redis)

        assert captured["update_data"] == {
            f"{DIVE_SITE_LOCATION_PREFIX}{field}": ("Nuweiba, Egypt" if field == "name" else None)
            for field in LOCATION_FIELDS
        }

    @pytest.mark.asyncio
    async def test_an_explicit_null_clears_the_whole_place(self, captured: dict[str, Any], mock_redis: Any) -> None:
        """How a site entered with the wrong locality is corrected back to "not
        recorded" - behaviour the string had and the object keeps."""
        await _patch({"location": None}, mock_redis)

        assert captured["update_data"] == {f"{DIVE_SITE_LOCATION_PREFIX}{field}": None for field in LOCATION_FIELDS}

    @pytest.mark.asyncio
    async def test_an_unrelated_edit_leaves_the_place_alone(self, captured: dict[str, Any], mock_redis: Any) -> None:
        """Omitted means unchanged. A dialog that seeds its fields from the record and
        PATCHes them back would otherwise strip a picked place on every rename."""
        await _patch({"name": "Blue Hole (Dahab)"}, mock_redis)

        assert captured["update_data"] == {"name": "Blue Hole (Dahab)"}


class TestUniquenessKeysOnTheLocalitysName:
    @pytest.mark.asyncio
    async def test_the_recheck_uses_the_new_localitys_name(self, captured: dict[str, Any], mock_redis: Any) -> None:
        await _patch({"location": DAHAB}, mock_redis)

        assert captured["name_exists"].await_args.kwargs["location_name"] == "Dahab, Egypt"

    @pytest.mark.asyncio
    async def test_the_recheck_falls_back_to_the_stored_name_when_the_place_is_untouched(
        self, captured: dict[str, Any], mock_redis: Any
    ) -> None:
        await _patch({"name": "Blue Hole (Dahab)"}, mock_redis)

        assert captured["name_exists"].await_args.kwargs["location_name"] == "Dahab, Egypt"

    @pytest.mark.asyncio
    async def test_clearing_the_place_rechecks_against_no_locality(
        self, captured: dict[str, Any], mock_redis: Any
    ) -> None:
        await _patch({"location": None}, mock_redis)

        assert captured["name_exists"].await_args.kwargs["location_name"] is None

    @pytest.mark.asyncio
    async def test_a_repeat_is_refused(self, captured: dict[str, Any], mock_redis: Any) -> None:
        captured["name_exists"].return_value = True

        with pytest.raises(DuplicateValueException):
            await _patch({"location": DAHAB}, mock_redis)

    @pytest.mark.asyncio
    async def test_moving_a_place_drops_the_cached_dives(self, captured: dict[str, Any], mock_redis: Any) -> None:
        """A dive's cached body embeds its sites' localities, so a body that only moves the
        place's centre still reshapes what those dives carry."""
        await _patch({"location": {**DAHAB, "latitude": 28.5, "longitude": 34.5}}, mock_redis)

        captured["invalidate_dive_caches"].assert_awaited_once_with(1)


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestUniquenessAgainstPostgres:
    """The index and the pre-insert check, executed rather than described.

    `dive_site_name_exists` exists to turn the index's `IntegrityError` into a 422, so the
    two have to agree about what a duplicate is. Only running both against the same rows
    settles that - and the rule moved columns in this change, which is exactly when a
    described agreement stops being one.
    """

    @staticmethod
    def _site(db: Session, diver: User, name: str, location_name: str | None) -> DiveSite:
        site = DiveSite(user_id=diver.id, name=name, notes="", location_name=location_name)
        db.add(site)
        db.commit()
        return site

    @pytest.mark.asyncio
    async def test_the_same_name_in_the_same_place_is_a_duplicate(self, db: Session, diver: User, async_db) -> None:
        name = f"Blue Hole {uuid7().hex[-8:]}"
        self._site(db, diver, name, "Dahab, Egypt")

        assert await dive_site_name_exists(async_db, diver.id, name, "Dahab, Egypt") is True

    @pytest.mark.asyncio
    async def test_the_same_name_somewhere_else_is_not(self, db: Session, diver: User, async_db) -> None:
        name = f"Blue Hole {uuid7().hex[-8:]}"
        self._site(db, diver, name, "Dahab, Egypt")

        assert await dive_site_name_exists(async_db, diver.id, name, "Gozo, Malta") is False

    @pytest.mark.asyncio
    async def test_the_rest_of_the_place_does_not_enter_the_key(self, db: Session, diver: User, async_db) -> None:
        """Two sites in "Dahab, Egypt" collide whether or not a geocoder filled in a centre
        for one of them: a place is identified by what it is called."""
        name = f"Blue Hole {uuid7().hex[-8:]}"
        db.add(
            DiveSite(
                user_id=diver.id,
                name=name,
                notes="",
                location_name="Dahab, Egypt",
                location_full_name="Dahab, South Sinai Governorate, Egypt",
                location_latitude=28.4949,
                location_longitude=34.5136,
                location_bbox_south=28.44,
                location_bbox_north=28.54,
                location_bbox_west=34.46,
                location_bbox_east=34.56,
            )
        )
        db.commit()

        assert await dive_site_name_exists(async_db, diver.id, name, "Dahab, Egypt") is True

    def test_the_index_refuses_the_row_the_check_would_have(self, db: Session, diver: User) -> None:
        name = f"Blue Hole {uuid7().hex[-8:]}"
        self._site(db, diver, name, "Dahab, Egypt")

        with pytest.raises(IntegrityError):
            self._site(db, diver, name, "dahab, egypt")
        db.rollback()

    def test_two_sites_with_no_locality_still_collide(self, db: Session, diver: User) -> None:
        """`coalesce(..., '')` in the index, and `IS NULL` in the check: an unrecorded
        locality is one key, not a hole that lets every name through twice."""
        name = f"Blue Hole {uuid7().hex[-8:]}"
        self._site(db, diver, name, None)

        with pytest.raises(IntegrityError):
            self._site(db, diver, name, None)
        db.rollback()
