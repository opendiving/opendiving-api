"""Unit tests for dive site coordinates (`schemas/dive_site.py`,
`api/v1/dive_sites.py::patch_dive_site`).

One rule is worth this much test: **latitude and longitude are one value**. Half a pair is
not a partial position, it is a meaningless one - a site pinned on the equator or the prime
meridian by accident rather than by a diver.

Where the rule is enforced differs by verb, and that is the whole of what is covered here:

* On the way *in* (`DiveSiteCreate`) the body is all there is, so a model validator decides
  it.
* On a PATCH the body carries only what changed, so the rule has to be applied to the
  **effective** pair - what the row will hold afterwards - exactly as the neighbouring
  `effective_location` check already does for uniqueness. Applying it to the body alone
  would refuse a diver nudging one coordinate of a pair that is already whole.

No database: `patch_dive_site`'s collaborators are stubbed and the assertions are on the
`update_data` it hands to `crud_dive_sites.update`.
"""

import uuid as uuid_pkg
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from uuid6 import uuid7

from src.app.api.v1 import dive_sites as dive_sites_module
from src.app.core.exceptions.http_exceptions import UnprocessableEntityException
from src.app.core.utils import cache as cache_module
from src.app.schemas.dive_site import DiveSiteCreate, DiveSiteUpdate

USER_UUID = uuid7()
# The Blue Hole, Dahab - a real pair, so a transposed lat/lon is visible in a diff.
BLUE_HOLE = (28.5721, 34.5372)


def _create_body(**overrides: Any) -> dict[str, Any]:
    return {"name": "Blue Hole", "user_uuid": str(USER_UUID), **overrides}


class TestCreateSchema:
    def test_a_whole_pair_is_accepted(self) -> None:
        site = DiveSiteCreate.model_validate(_create_body(latitude=BLUE_HOLE[0], longitude=BLUE_HOLE[1]))

        assert (site.latitude, site.longitude) == BLUE_HOLE

    def test_no_coordinates_at_all_is_accepted(self) -> None:
        """The overwhelmingly common case: a site is a name and maybe a location."""
        site = DiveSiteCreate.model_validate(_create_body())

        assert (site.latitude, site.longitude) == (None, None)

    @pytest.mark.parametrize("half", ["latitude", "longitude"])
    def test_half_a_pair_is_refused(self, half: str) -> None:
        with pytest.raises(ValidationError, match="must be set together"):
            DiveSiteCreate.model_validate(_create_body(**{half: 12.5}))

    @pytest.mark.parametrize(
        ("latitude", "longitude"),
        [
            (90.0, 180.0),
            (-90.0, -180.0),
        ],
    )
    def test_the_extremes_of_the_globe_are_inside_the_range(self, latitude: float, longitude: float) -> None:
        site = DiveSiteCreate.model_validate(_create_body(latitude=latitude, longitude=longitude))

        assert (site.latitude, site.longitude) == (latitude, longitude)

    @pytest.mark.parametrize(
        ("latitude", "longitude"),
        [
            (90.1, 0.0),
            (-90.1, 0.0),
            # The classic swap: a longitude that is a perfectly good latitude, sent in the
            # wrong field. Only the out-of-range half can be caught, but that is the half
            # that matters.
            (34.5372, 190.0),
        ],
    )
    def test_a_coordinate_off_the_globe_is_refused(self, latitude: float, longitude: float) -> None:
        with pytest.raises(ValidationError):
            DiveSiteCreate.model_validate(_create_body(latitude=latitude, longitude=longitude))

    def test_not_a_number_is_refused(self) -> None:
        """`NaN` is a float and would sail through a bare `float | None`; it fails the
        range check instead, since no comparison against it is ever true."""
        with pytest.raises(ValidationError):
            DiveSiteCreate.model_validate(_create_body(latitude=float("nan"), longitude=float("nan")))


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stubs out everything `patch_dive_site` touches and records the `update_data` it
    builds. The stored row starts with no coordinates; tests that need one set them on
    `captured["db_dive_site"]` before patching."""
    seen: dict[str, Any] = {}

    db_dive_site = MagicMock()
    db_dive_site.id = 11
    db_dive_site.user_id = 1
    db_dive_site.name = "Blue Hole"
    db_dive_site.location = "Dahab, Egypt"
    db_dive_site.latitude = None
    db_dive_site.longitude = None
    seen["db_dive_site"] = db_dive_site

    async def fake_update(*, db: Any, object: dict, uuid: uuid_pkg.UUID) -> None:
        seen["update_data"] = object

    monkeypatch.setattr(dive_sites_module, "_get_owned_dive_site", AsyncMock(return_value=db_dive_site))
    monkeypatch.setattr(dive_sites_module, "dive_site_name_exists", AsyncMock(return_value=False))
    monkeypatch.setattr(dive_sites_module.crud_dive_sites, "update", fake_update)
    monkeypatch.setattr(dive_sites_module._dive_site_cache, "invalidate_list", AsyncMock())
    seen["invalidate_dive_caches"] = AsyncMock()
    monkeypatch.setattr(dive_sites_module, "invalidate_dive_caches", seen["invalidate_dive_caches"])

    return seen


async def _patch(values: dict[str, Any], mock_redis: Any) -> None:
    """`patch_dive_site` is `@cache`-decorated, so it needs a client even on a PATCH -
    the decorator's only job on a non-GET is dropping the item's cached entry."""
    request = MagicMock()
    request.method = "PATCH"
    with patch.object(cache_module, "client", mock_redis):
        await dive_sites_module.patch_dive_site(
            request=request,
            uuid=uuid7(),
            values=DiveSiteUpdate.model_validate(values),
            current_user={"id": 1, "uuid": USER_UUID},
            db=MagicMock(),
        )


class TestPatchKeepsThePairWhole:
    @pytest.mark.asyncio
    async def test_a_whole_pair_is_stored(self, captured: dict[str, Any], mock_redis: Any) -> None:
        await _patch({"latitude": BLUE_HOLE[0], "longitude": BLUE_HOLE[1]}, mock_redis)

        assert (captured["update_data"]["latitude"], captured["update_data"]["longitude"]) == BLUE_HOLE

    @pytest.mark.asyncio
    async def test_half_a_pair_on_a_site_with_no_position_is_refused(
        self, captured: dict[str, Any], mock_redis: Any
    ) -> None:
        """The half-set row the effective-pair check exists to prevent."""
        with pytest.raises(UnprocessableEntityException, match="must be set together"):
            await _patch({"latitude": BLUE_HOLE[0]}, mock_redis)

        assert "update_data" not in captured

    @pytest.mark.asyncio
    async def test_one_coordinate_of_an_existing_pair_can_be_nudged(
        self, captured: dict[str, Any], mock_redis: Any
    ) -> None:
        """Moving a marker a hundred metres north is a one-field edit, and the row it
        lands on is still a whole pair - so the rule has nothing to say about it."""
        captured["db_dive_site"].latitude, captured["db_dive_site"].longitude = BLUE_HOLE

        await _patch({"latitude": 28.58}, mock_redis)

        assert captured["update_data"] == {"latitude": 28.58}

    @pytest.mark.asyncio
    async def test_clearing_both_halves_removes_the_position(self, captured: dict[str, Any], mock_redis: Any) -> None:
        """An explicit null is the only way to unset a coordinate, and it survives
        `exclude_unset` because it was explicitly set."""
        captured["db_dive_site"].latitude, captured["db_dive_site"].longitude = BLUE_HOLE

        await _patch({"latitude": None, "longitude": None}, mock_redis)

        assert captured["update_data"] == {"latitude": None, "longitude": None}

    @pytest.mark.asyncio
    async def test_clearing_only_one_half_is_refused(self, captured: dict[str, Any], mock_redis: Any) -> None:
        """Not silently cleared for them: a null on one half asks for a row this API
        refuses to write, and guessing that they meant both would be a mutation they
        didn't ask for."""
        captured["db_dive_site"].latitude, captured["db_dive_site"].longitude = BLUE_HOLE

        with pytest.raises(UnprocessableEntityException, match="must be set together"):
            await _patch({"latitude": None}, mock_redis)

        assert "update_data" not in captured

    @pytest.mark.asyncio
    async def test_an_unrelated_edit_leaves_the_coordinates_alone(
        self, captured: dict[str, Any], mock_redis: Any
    ) -> None:
        """Omitted means unchanged: renaming a site must not blank its position, and a
        site *without* one must not be refused for not having sent it."""
        await _patch({"name": "Blue Hole (Dahab)"}, mock_redis)

        assert captured["update_data"] == {"name": "Blue Hole (Dahab)"}

    @pytest.mark.asyncio
    async def test_an_edit_touching_no_coordinate_survives_a_half_pair_already_in_the_row(
        self, captured: dict[str, Any], mock_redis: Any
    ) -> None:
        """The rule has no `CHECK` behind it, so a half pair can reach the table another
        way - the admin panel writes through `DiveSiteUpdate`, which carries no validator.
        Enforcing it on a PATCH that named neither coordinate would leave the owner unable
        to so much as rename the site until they guessed which field to send."""
        captured["db_dive_site"].latitude = BLUE_HOLE[0]

        await _patch({"notes": "Deep, dark, and busier than it looks"}, mock_redis)

        assert captured["update_data"] == {"notes": "Deep, dark, and busier than it looks"}


class TestDiveCacheInvalidation:
    """`DiveSiteInfo` - the site summary embedded in every dive read - is `uuid`, `name`
    and `location`. Only a change to one of those can make a cached dive stale, and
    dropping a diver's whole cached logbook because they dragged a marker would be a real
    cost for no staleness avoided.
    """

    @pytest.mark.asyncio
    async def test_a_coordinate_edit_leaves_the_cached_dives_alone(
        self, captured: dict[str, Any], mock_redis: Any
    ) -> None:
        await _patch({"latitude": BLUE_HOLE[0], "longitude": BLUE_HOLE[1]}, mock_redis)

        captured["invalidate_dive_caches"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_rename_still_drops_them(self, captured: dict[str, Any], mock_redis: Any) -> None:
        await _patch({"name": "Blue Hole (Dahab)"}, mock_redis)

        captured["invalidate_dive_caches"].assert_awaited_once_with(1)
