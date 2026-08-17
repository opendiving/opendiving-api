"""Unit tests for dive site coordinates (`schemas/dive_site.py`,
`api/v1/dive_sites.py::patch_dive_site`, `schemas/dive.py::DiveSiteInfo`).

One rule is worth this much test: **latitude and longitude are one value**. Half a pair is
not a partial position, it is a meaningless one - a site pinned on the equator or the prime
meridian by accident rather than by a diver.

The rule is enforced in exactly one place, `WholeCoordinatePair`, and it is a rule about
the **body**: name both coordinates or neither. That is why there are two conditions rather
than one - a PATCH can produce a half pair by naming one key (`{"latitude": 27.7}` writes
one column over a row that has neither) or by naming both with one value
(`{"latitude": 27.7, "longitude": null}`). Together they keep a whole row whole without the
route ever reading the stored one.

The rejected alternative was checking the *effective* pair in the route - body merged over
the stored row - which would have allowed nudging one coordinate of a pair that is already
whole. It cost more than it bought: an unrelated edit to a row that already held a half
pair had to be special-cased, and two concurrent PATCHes could still interleave into one.
See DECISIONS.md.

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
from src.app.core.utils import cache as cache_module
from src.app.schemas.dive import DiveSiteInfo
from src.app.schemas.dive_site import DiveSiteCreate, DiveSiteUpdate, WholeCoordinatePair

USER_UUID = uuid7()
# The Blue Hole, Dahab - a real pair, so a transposed lat/lon is visible in a diff.
BLUE_HOLE = (28.5721, 34.5372)

# Both write schemas carry the same rule, and both are worth running every case through:
# `DiveSiteCreate` is the one with no stored row behind it, `DiveSiteUpdate` the one where
# an omitted key means "unchanged" and could quietly write half a position.
WRITE_SCHEMAS: list[type[WholeCoordinatePair]] = [DiveSiteCreate, DiveSiteUpdate]


def _body(schema: type[WholeCoordinatePair], **overrides: Any) -> dict[str, Any]:
    """A minimal valid body for either schema - `DiveSiteCreate` needs a name and an
    owner, `DiveSiteUpdate` needs nothing at all."""
    if schema is DiveSiteCreate:
        return {"name": "Blue Hole", "user_uuid": str(USER_UUID), **overrides}
    return dict(overrides)


@pytest.mark.parametrize("schema", WRITE_SCHEMAS)
class TestTheCoordinatePairRule:
    def test_a_whole_pair_is_accepted(self, schema: type[WholeCoordinatePair]) -> None:
        site = schema.model_validate(_body(schema, latitude=BLUE_HOLE[0], longitude=BLUE_HOLE[1]))

        assert (site.latitude, site.longitude) == BLUE_HOLE

    def test_naming_neither_is_accepted(self, schema: type[WholeCoordinatePair]) -> None:
        """The overwhelmingly common case both ways: a site is a name and maybe a
        location, and an edit to one usually says nothing about the other."""
        site = schema.model_validate(_body(schema))

        assert (site.latitude, site.longitude) == (None, None)

    @pytest.mark.parametrize("half", ["latitude", "longitude"])
    def test_naming_one_coordinate_alone_is_refused(self, schema: type[WholeCoordinatePair], half: str) -> None:
        """On a create this is half a position. On a PATCH it is worse: it would write one
        column and leave whatever the other already held."""
        with pytest.raises(ValidationError, match="must be set together"):
            schema.model_validate(_body(schema, **{half: 12.5}))

    @pytest.mark.parametrize("half", ["latitude", "longitude"])
    def test_naming_both_with_one_value_is_refused(self, schema: type[WholeCoordinatePair], half: str) -> None:
        """The second way to spell the same mistake, and the one the key check alone
        misses."""
        pair = {"latitude": BLUE_HOLE[0], "longitude": BLUE_HOLE[1], half: None}

        with pytest.raises(ValidationError, match="must be set together"):
            schema.model_validate(_body(schema, **pair))

    def test_two_explicit_nulls_clear_the_position(self, schema: type[WholeCoordinatePair]) -> None:
        """How a position is removed - and on `DiveSiteUpdate` the pair has to survive
        `exclude_unset`, which is what distinguishes it from an omitted key."""
        site = schema.model_validate(_body(schema, latitude=None, longitude=None))

        assert (site.latitude, site.longitude) == (None, None)
        assert {"latitude", "longitude"} <= site.model_fields_set

    @pytest.mark.parametrize(("latitude", "longitude"), [(90.0, 180.0), (-90.0, -180.0)])
    def test_the_extremes_of_the_globe_are_inside_the_range(
        self, schema: type[WholeCoordinatePair], latitude: float, longitude: float
    ) -> None:
        site = schema.model_validate(_body(schema, latitude=latitude, longitude=longitude))

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
    def test_a_coordinate_off_the_globe_is_refused(
        self, schema: type[WholeCoordinatePair], latitude: float, longitude: float
    ) -> None:
        with pytest.raises(ValidationError):
            schema.model_validate(_body(schema, latitude=latitude, longitude=longitude))

    def test_not_a_number_is_refused(self, schema: type[WholeCoordinatePair]) -> None:
        """`NaN` is a float and would sail through a bare `float | None`; it fails the
        range check instead, since no comparison against it is ever true."""
        with pytest.raises(ValidationError):
            schema.model_validate(_body(schema, latitude=float("nan"), longitude=float("nan")))


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stubs out everything `patch_dive_site` touches and records the `update_data` it
    builds."""
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


class TestWhatReachesTheColumns:
    """The schema has already refused every half pair by the time the route runs, so what
    is left to pin is that a whole one survives `exclude_unset` and an absent one stays
    absent."""

    @pytest.mark.asyncio
    async def test_a_whole_pair_is_stored(self, captured: dict[str, Any], mock_redis: Any) -> None:
        await _patch({"latitude": BLUE_HOLE[0], "longitude": BLUE_HOLE[1]}, mock_redis)

        assert captured["update_data"] == {"latitude": BLUE_HOLE[0], "longitude": BLUE_HOLE[1]}

    @pytest.mark.asyncio
    async def test_clearing_both_halves_removes_the_position(self, captured: dict[str, Any], mock_redis: Any) -> None:
        """An explicit null is the only way to unset a coordinate, and it survives
        `exclude_unset` because it was explicitly set."""
        captured["db_dive_site"].latitude, captured["db_dive_site"].longitude = BLUE_HOLE

        await _patch({"latitude": None, "longitude": None}, mock_redis)

        assert captured["update_data"] == {"latitude": None, "longitude": None}

    @pytest.mark.asyncio
    async def test_an_unrelated_edit_leaves_the_coordinates_alone(
        self, captured: dict[str, Any], mock_redis: Any
    ) -> None:
        """Omitted means unchanged: renaming a site must not blank its position, and a
        site *without* one must not be refused for not having sent it."""
        await _patch({"name": "Blue Hole (Dahab)"}, mock_redis)

        assert captured["update_data"] == {"name": "Blue Hole (Dahab)"}

    @pytest.mark.asyncio
    async def test_an_unrelated_edit_survives_a_half_pair_already_in_the_row(
        self, captured: dict[str, Any], mock_redis: Any
    ) -> None:
        """Nothing at the database level enforces the rule, so a half pair can be in the
        table (raw SQL, a restored dump). Because the rule reads the body and never the
        row, such a site can still be edited - the version of this check that compared
        against the stored row had to special-case exactly this."""
        captured["db_dive_site"].latitude = BLUE_HOLE[0]

        await _patch({"notes": "Deep, dark, and busier than it looks"}, mock_redis)

        assert captured["update_data"] == {"notes": "Deep, dark, and busier than it looks"}


class TestDiveCacheInvalidation:
    """`DiveSiteInfo` - the site summary embedded in every dive read - is `uuid`, `name`,
    `location` and, since the dive page grew a map, the position. Only a change to one of
    those can make a cached dive stale; notes and the rest still leave the logbook alone.

    The two gates are deliberately not the same gate: staleness covers the position,
    uniqueness does not, so dragging a marker must not pay for a name query it cannot
    fail - and must not 422 on a legacy duplicate name it did not touch.
    """

    @pytest.mark.asyncio
    async def test_a_coordinate_edit_now_drops_them(self, captured: dict[str, Any], mock_redis: Any) -> None:
        await _patch({"latitude": BLUE_HOLE[0], "longitude": BLUE_HOLE[1]}, mock_redis)

        captured["invalidate_dive_caches"].assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_a_rename_still_drops_them(self, captured: dict[str, Any], mock_redis: Any) -> None:
        await _patch({"name": "Blue Hole (Dahab)"}, mock_redis)

        captured["invalidate_dive_caches"].assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_an_edit_to_neither_leaves_them_alone(self, captured: dict[str, Any], mock_redis: Any) -> None:
        await _patch({"notes": "Deep, dark, and busier than it looks"}, mock_redis)

        captured["invalidate_dive_caches"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_coordinate_edit_skips_the_uniqueness_recheck(
        self, captured: dict[str, Any], mock_redis: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A marker drag says nothing about the name, so the query is pointless - and on a
        row whose name is already duplicated somewhere (rows predating the constraint), a
        widened single gate would refuse the move with a 422 about a field it never sent.
        """
        name_exists = AsyncMock(return_value=True)
        monkeypatch.setattr(dive_sites_module, "dive_site_name_exists", name_exists)

        await _patch({"latitude": BLUE_HOLE[0], "longitude": BLUE_HOLE[1]}, mock_redis)

        name_exists.assert_not_awaited()
        assert captured["update_data"] == {"latitude": BLUE_HOLE[0], "longitude": BLUE_HOLE[1]}


class TestTheEmbeddedSiteSummary:
    """`DiveSiteInfo` is what a dive read carries about each site it was logged against,
    and the web app reads the position straight off it rather than fetching every site.
    The unset case has to serialize as an explicit `null` rather than vanish, so a client
    can tell "no position recorded" from a field the API forgot to send.
    """

    def test_a_positioned_site_carries_its_coordinates(self) -> None:
        site = DiveSiteInfo(
            uuid=uuid7(), name="Blue Hole", location="Dahab, Egypt", latitude=BLUE_HOLE[0], longitude=BLUE_HOLE[1]
        )

        assert (site.model_dump()["latitude"], site.model_dump()["longitude"]) == BLUE_HOLE

    def test_a_site_without_a_position_serializes_explicit_nulls(self) -> None:
        dumped = DiveSiteInfo(uuid=uuid7(), name="Blue Hole").model_dump()

        assert dumped["latitude"] is None
        assert dumped["longitude"] is None

    def test_a_coordinate_off_the_globe_is_refused(self) -> None:
        """The read schema shares `Latitude`/`Longitude` with the write ones, so the range
        check comes along - a row that could only exist via raw SQL fails loudly here."""
        with pytest.raises(ValidationError):
            DiveSiteInfo(uuid=uuid7(), name="Null Island Adjacent", latitude=91.0, longitude=0.0)
