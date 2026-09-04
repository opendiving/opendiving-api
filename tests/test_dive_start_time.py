"""Unit tests for the `start_time`/UTC-offset handling (`core/utils/datetime_offset.py`,
`schemas/dive.py`, `api/v1/dives.py::patch_dive`).

`Dive.start_time` is stored as a UTC instant plus a separate `utc_offset_minutes` column
(the offset it was originally logged in), and the API presents/accepts a single ISO 8601
`start_time` string built from the two - see `core/utils/datetime_offset.py` for why, and
`api/v1/dives.py` for where they are combined/split.

That string is offset-aware ("2021-04-04T10:04:47.910+02:00") for every dive but one kind.
A dive imported from a source that recorded no offset stores a NULL there and reads back
offset-less, and the second half of this module is about the one write that may carry that
state onward: an update may **preserve** an unknown offset and may not **remove** a stored
one. The rule needs the dive to answer, so it lives in `split_updated_start_time` rather
than in an annotation, and both halves are pinned here - at the rule and at the route.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from uuid6 import uuid7

from src.app.api.v1 import dives as dives_module
from src.app.core.exceptions.http_exceptions import UnprocessableEntityException
from src.app.core.utils.datetime_offset import (
    combine_start_time,
    require_utc_offset,
    split_start_time,
    split_updated_start_time,
)
from src.app.schemas.dive import DiveCreate, DiveUpdate, DiveUpdateRequest


class TestSplitAndCombineStartTime:
    def test_split_returns_utc_instant_and_offset_minutes(self) -> None:
        start_time = datetime(2021, 4, 4, 10, 4, 47, 910000, tzinfo=timezone(timedelta(hours=2)))

        utc_instant, offset_minutes = split_start_time(start_time)

        assert offset_minutes == 120
        assert utc_instant == datetime(2021, 4, 4, 8, 4, 47, 910000, tzinfo=UTC)

    def test_split_handles_negative_offsets(self) -> None:
        start_time = datetime(2024, 1, 1, 6, 0, 0, tzinfo=timezone(timedelta(hours=-5)))

        _, offset_minutes = split_start_time(start_time)

        assert offset_minutes == -300

    def test_split_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            split_start_time(datetime(2024, 1, 1, 6, 0, 0))

    def test_combine_reattaches_the_original_offset(self) -> None:
        utc_instant = datetime(2021, 4, 4, 8, 4, 47, 910000, tzinfo=UTC)

        combined = combine_start_time(utc_instant, 120)

        assert combined == datetime(2021, 4, 4, 10, 4, 47, 910000, tzinfo=timezone(timedelta(hours=2)))
        assert combined.utcoffset() == timedelta(hours=2)

    def test_split_then_combine_round_trips(self) -> None:
        start_time = datetime(2025, 6, 3, 12, 15, 33, 800000, tzinfo=timezone(timedelta(hours=5, minutes=45)))

        utc_instant, offset_minutes = split_start_time(start_time)
        combined = combine_start_time(utc_instant, offset_minutes)

        assert combined == start_time
        assert combined.utcoffset() == start_time.utcoffset()


class TestRequireUtcOffset:
    def test_accepts_offset_aware_datetime(self) -> None:
        value = datetime(2024, 1, 1, tzinfo=UTC)
        assert require_utc_offset(value) is value

    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="UTC offset"):
            require_utc_offset(datetime(2024, 1, 1))


class TestDiveStartTimeValidation:
    """`DiveCreate` (and everything else built on `DiveStartTime`) requires an explicit UTC
    offset on `start_time` - mirroring `require_utc_offset` above through the actual
    Pydantic schemas used by the API.

    `DiveUpdate` is the exception, and deliberately: whether an offsetless update is legal
    depends on the dive being updated, so the schema accepts both spellings and
    `split_updated_start_time` decides. See `TestAnUpdateMayPreserveButNotRemove` below.
    """

    def _dive_kwargs(self, start_time: str) -> dict:
        return {"dive_number": 1, "start_time": start_time, "duration": 1800}

    def test_create_accepts_offset_aware_iso_string(self) -> None:
        dive = DiveCreate(**self._dive_kwargs("2021-04-04T10:04:47.910+02:00"))
        assert dive.start_time.utcoffset() == timedelta(hours=2)

    def test_create_accepts_z_suffix(self) -> None:
        dive = DiveCreate(**self._dive_kwargs("2021-04-04T10:04:47Z"))
        assert dive.start_time.utcoffset() == timedelta(0)

    def test_create_rejects_naive_iso_string(self) -> None:
        with pytest.raises(ValidationError, match="UTC offset"):
            DiveCreate(**self._dive_kwargs("2025-06-03T12:15:33.8"))

    def test_update_accepts_a_naive_iso_string_and_leaves_the_ruling_to_the_route(self) -> None:
        """This used to be `test_update_rejects_naive_iso_string`, and the refusal did not
        disappear - it moved somewhere that can see the dive. Refusing here would refuse the
        legitimate case too, since the schema cannot tell an imported offset-unknown dive
        from an ordinary one."""
        parsed = DiveUpdate(start_time="2025-06-03T12:15:33.8").start_time
        assert parsed is not None
        assert parsed.utcoffset() is None

    def test_update_still_accepts_an_offset_aware_iso_string(self) -> None:
        parsed = DiveUpdate(start_time="2021-04-04T10:04:47.910+02:00").start_time
        assert parsed is not None
        assert parsed.utcoffset() == timedelta(hours=2)

    def test_update_allows_omitting_start_time(self) -> None:
        assert DiveUpdate().start_time is None


class TestSplitUpdatedStartTime:
    """The update half of the offset-unknown state, as a rule rather than as a route.

    Preserving is allowed; removing is not - so the answer depends on the dive's own
    `utc_offset_minutes`, which is the whole reason this is not an `AfterValidator`.
    """

    def test_an_offsetless_update_preserves_an_already_unknown_offset(self) -> None:
        utc_instant, offset_minutes = split_updated_start_time(datetime(2026, 4, 17, 11, 49, 23), None)

        assert offset_minutes is None
        # Stored as the wall clock labelled UTC, exactly as the importer stores it.
        assert utc_instant == datetime(2026, 4, 17, 11, 49, 23, tzinfo=UTC)

    def test_an_offsetless_update_may_not_remove_a_stored_offset(self) -> None:
        with pytest.raises(ValueError, match="already unknown"):
            split_updated_start_time(datetime(2026, 4, 17, 11, 49, 23), 120)

    def test_a_zero_offset_is_a_stored_offset_like_any_other(self) -> None:
        """`0` is `+00:00`, not "no offset" - a dive logged in London has one, and the
        column's own default is `0`. Getting this wrong would let an update strip the
        offset off every UTC dive in the log."""
        with pytest.raises(ValueError, match="already unknown"):
            split_updated_start_time(datetime(2026, 4, 17, 11, 49, 23), 0)

    @pytest.mark.parametrize("stored_offset_minutes", [None, 0, 120])
    def test_an_offset_aware_update_is_accepted_whatever_is_stored(self, stored_offset_minutes: int | None) -> None:
        """Adopting a real offset is the diver deciding they know one - a fact gained, so
        it is allowed even on a dive that had none."""
        start_time = datetime(2026, 4, 17, 11, 49, 23, tzinfo=timezone(timedelta(hours=2)))

        utc_instant, offset_minutes = split_updated_start_time(start_time, stored_offset_minutes)

        assert offset_minutes == 120
        assert utc_instant == datetime(2026, 4, 17, 9, 49, 23, tzinfo=UTC)


class TestAnUpdateMayPreserveButNotRemove:
    """`PATCH /dive/{uuid}` end of the same rule, since the route is what a client meets.

    Stubbed the way `test_dive_depth_pair.py` stubs the other merged-pair rule: the
    assertions are on the `update_data` handed to `crud_dives.update`, which is where the
    offset column is actually decided.
    """

    @pytest.fixture
    def captured(self, monkeypatch: pytest.MonkeyPatch) -> dict:
        seen: dict = {}
        db_dive = MagicMock()
        db_dive.id = 11
        db_dive.user_id = 1
        db_dive.avg_depth = None
        db_dive.max_depth = None
        # A real value rather than a `MagicMock` attribute: the route compares it against
        # `None`, and a mock is never `None` - which would silently test only one half.
        db_dive.utc_offset_minutes = 120

        async def fake_update(*, db: object, object: dict, uuid: uuid_pkg.UUID) -> None:
            seen["update_data"] = object

        monkeypatch.setattr(dives_module, "_get_owned_dive", AsyncMock(return_value=db_dive))
        monkeypatch.setattr(dives_module.crud_dives, "update", fake_update)
        monkeypatch.setattr(dives_module, "recalculate_dive_stats", AsyncMock())
        monkeypatch.setattr(dives_module, "recalculate_gear_dive_counts", AsyncMock())
        monkeypatch.setattr(dives_module, "invalidate_dive_caches", AsyncMock())
        monkeypatch.setattr(dives_module, "invalidate_gear_caches", AsyncMock())
        seen["dive"] = db_dive
        return seen

    async def _patch(self, start_time: str) -> None:
        await dives_module.patch_dive(
            request=MagicMock(),
            uuid=uuid_pkg.UUID(str(uuid7())),
            values=DiveUpdateRequest.model_validate({"start_time": start_time}),
            current_user={"id": 1, "uuid": uuid7()},
            db=MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_an_offsetless_edit_of_an_offset_unknown_dive_keeps_it_unknown(self, captured: dict) -> None:
        captured["dive"].utc_offset_minutes = None

        await self._patch("2026-04-17T12:49:23")

        assert captured["update_data"]["utc_offset_minutes"] is None
        assert captured["update_data"]["start_time"] == datetime(2026, 4, 17, 12, 49, 23, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_an_offsetless_edit_of_an_offset_aware_dive_is_refused(self, captured: dict) -> None:
        with pytest.raises(UnprocessableEntityException, match="already unknown"):
            await self._patch("2026-04-17T12:49:23")

        assert "update_data" not in captured

    @pytest.mark.asyncio
    async def test_an_offset_unknown_dive_may_still_adopt_an_offset(self, captured: dict) -> None:
        captured["dive"].utc_offset_minutes = None

        await self._patch("2026-04-17T12:49:23+02:00")

        assert captured["update_data"]["utc_offset_minutes"] == 120
        assert captured["update_data"]["start_time"] == datetime(2026, 4, 17, 10, 49, 23, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_an_edit_that_leaves_start_time_alone_never_touches_the_offset(self, captured: dict) -> None:
        """A diver fixing a typo in the notes of an imported dive is the commonest edit
        there is, and it must not rewrite the offset column on the way past."""
        captured["dive"].utc_offset_minutes = None

        await dives_module.patch_dive(
            request=MagicMock(),
            uuid=uuid_pkg.UUID(str(uuid7())),
            values=DiveUpdateRequest.model_validate({"notes": "Thresher on the second pass"}),
            current_user={"id": 1, "uuid": uuid7()},
            db=MagicMock(),
        )

        assert "utc_offset_minutes" not in captured["update_data"]
