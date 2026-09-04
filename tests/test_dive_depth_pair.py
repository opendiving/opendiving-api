"""Unit tests for `avg_depth <= max_depth` (`schemas/dive.py::validate_depth_pair`).

A mean cannot be deeper than a maximum, so a dive claiming otherwise records at least one
wrong number - and until DiveJSON landed the app had no comparison anywhere, only the two
independent positive-depth `CHECK`s. `POST /dive` accepted `avg_depth` 30 with `max_depth`
20, and the export then produced a document the format's own validator rejects (spec §6.2,
and §3's cross-member arithmetic list).

Three routes into the rule and one statement of it, which is what these tests pin:

* a create body carrying both depths, refused by `DiveBase`;
* a PATCH carrying both, refused by `DiveUpdate`;
* a PATCH carrying **one**, which neither schema can judge - the other half is stored, and
  `patch_dive` is where the merged pair is checked.

`test_dive_check_constraints.py` covers the `CheckConstraint` underneath all three against
a live Postgres, and `test_dive_constraint_messages.py` the message it produces.
"""

import uuid as uuid_pkg
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from uuid6 import uuid7

from src.app.api.v1 import dives as dives_module
from src.app.core.exceptions.http_exceptions import UnprocessableEntityException
from src.app.schemas.dive import DiveCreateRequest, DiveUpdateRequest, validate_depth_pair

START_TIME = datetime(2026, 4, 4, 10, 4, 47, tzinfo=timezone(timedelta(hours=2)))


def _create(**overrides: Any) -> DiveCreateRequest:
    body: dict[str, Any] = {
        "dive_number": 1,
        "start_time": START_TIME,
        "duration": 1800,
        "user_uuid": uuid7(),
    }
    body.update(overrides)
    return DiveCreateRequest.model_validate(body)


class TestTheRuleItself:
    def test_an_average_deeper_than_the_maximum_is_refused(self) -> None:
        with pytest.raises(ValueError, match="avg_depth cannot be greater than max_depth"):
            validate_depth_pair(30.0, 20.0)

    def test_an_equal_pair_is_fine(self) -> None:
        """A perfectly square profile is unusual, not impossible."""
        validate_depth_pair(20.0, 20.0)

    @pytest.mark.parametrize(("avg_depth", "max_depth"), [(30.0, None), (None, 20.0), (None, None)])
    def test_a_half_pair_is_no_pair(self, avg_depth: float | None, max_depth: float | None) -> None:
        """The rule compares two recorded numbers; a dive that recorded one of them has
        nothing to compare, and "not recorded" is not a violation."""
        validate_depth_pair(avg_depth, max_depth)


class TestTheWriteSchemas:
    def test_a_create_body_carrying_both_is_judged(self) -> None:
        with pytest.raises(ValidationError, match="avg_depth cannot be greater than max_depth"):
            _create(avg_depth=30.0, max_depth=20.0)

    def test_a_create_body_that_agrees_is_accepted(self) -> None:
        assert _create(avg_depth=16.0, max_depth=28.4).avg_depth == 16.0

    def test_a_patch_carrying_both_is_judged(self) -> None:
        with pytest.raises(ValidationError, match="avg_depth cannot be greater than max_depth"):
            DiveUpdateRequest.model_validate({"avg_depth": 30.0, "max_depth": 20.0})

    def test_a_patch_carrying_one_is_not_judged_here(self) -> None:
        """Deliberately: the other half is stored, and refusing on the strength of the
        absent one would reject every legitimate single-depth edit. `patch_dive` is where
        the merged pair is checked - see below."""
        assert DiveUpdateRequest.model_validate({"avg_depth": 30.0}).avg_depth == 30.0


class TestTheMergedPatch:
    """The case neither schema can see, and the reason `patch_dive` re-runs the rule -
    the same shape `patch_course` uses for its date range."""

    @pytest.fixture
    def stored(self, monkeypatch: pytest.MonkeyPatch) -> MagicMock:
        db_dive = MagicMock()
        db_dive.id = 11
        db_dive.user_id = 1
        db_dive.avg_depth = 16.0
        db_dive.max_depth = 28.4
        monkeypatch.setattr(dives_module, "_get_owned_dive", AsyncMock(return_value=db_dive))
        monkeypatch.setattr(dives_module.crud_dives, "update", AsyncMock())
        monkeypatch.setattr(dives_module, "recalculate_dive_stats", AsyncMock())
        monkeypatch.setattr(dives_module, "recalculate_gear_dive_counts", AsyncMock())
        monkeypatch.setattr(dives_module, "invalidate_dive_caches", AsyncMock())
        monkeypatch.setattr(dives_module, "invalidate_gear_caches", AsyncMock())
        return db_dive

    async def _patch(self, values: DiveUpdateRequest) -> None:
        await dives_module.patch_dive(
            request=MagicMock(),
            uuid=uuid_pkg.UUID(str(uuid7())),
            values=values,
            current_user={"id": 1, "uuid": uuid7()},
            db=MagicMock(),
        )

    @pytest.mark.asyncio
    async def test_a_new_average_is_checked_against_the_stored_maximum(self, stored: MagicMock) -> None:
        with pytest.raises(UnprocessableEntityException, match="avg_depth cannot be greater than max_depth"):
            await self._patch(DiveUpdateRequest.model_validate({"avg_depth": 40.0}))

    @pytest.mark.asyncio
    async def test_a_new_maximum_is_checked_against_the_stored_average(self, stored: MagicMock) -> None:
        with pytest.raises(UnprocessableEntityException, match="avg_depth cannot be greater than max_depth"):
            await self._patch(DiveUpdateRequest.model_validate({"max_depth": 10.0}))

    @pytest.mark.asyncio
    async def test_an_edit_that_still_agrees_goes_through(self, stored: MagicMock) -> None:
        await self._patch(DiveUpdateRequest.model_validate({"max_depth": 30.0}))

    @pytest.mark.asyncio
    async def test_clearing_the_stored_maximum_leaves_nothing_to_violate(self, stored: MagicMock) -> None:
        """`max_depth` is nullable, so an explicit null is a real edit: the dive stops
        recording a maximum, and a pair with one half missing is not a pair."""
        await self._patch(DiveUpdateRequest.model_validate({"max_depth": None}))

    @pytest.mark.asyncio
    async def test_an_edit_touching_neither_depth_is_left_alone(self, stored: MagicMock) -> None:
        """The stored pair is not re-judged on every save - it cannot violate the rule,
        since nothing could have stored it."""
        await self._patch(DiveUpdateRequest.model_validate({"notes": "Thresher on the second pass"}))
