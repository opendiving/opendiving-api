"""Unit tests for `PATCH /dive/{uuid}` (`api/v1/dives.py::patch_dive`,
`schemas/dive.py::DiveUpdate`).

Two behaviours live here that nothing else pinned, both about the difference between a
field being *omitted* and being sent as an explicit `null`:

* `trip_uuid` is nullable, so an explicit null is the only way to detach a dive from its
  trip. The web client relies on this - it is how "remove from trip" is implemented - and
  the whole thing rests on one `model_fields_set` branch that had no test.
* `dive_number`/`start_time`/`duration`/`notes` are `NOT NULL`, so an explicit null is
  simply invalid, and used to surface as a 422 reading "Invalid reference: a related
  record does not exist." - a foreign-key message for a not-null problem.

No database: `patch_dive`'s collaborators are stubbed and the assertions are on the
`update_data` it hands to `crud_dives.update`, which is where both behaviours are decided.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from uuid6 import uuid7

from src.app.api.v1 import dives as dives_module
from src.app.core.exceptions.http_exceptions import UnprocessableEntityException
from src.app.schemas.dive import DiveUpdate, DiveUpdateRequest

TRIP_UUID = uuid7()
START_TIME = datetime(2026, 4, 4, 10, 4, 47, tzinfo=timezone(timedelta(hours=2)))


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stubs out everything `patch_dive` touches and records the `update_data` it builds.

    `resolve_trip_id_for_user` is stubbed to a fixed id so the attach case can assert the
    route translated the public uuid rather than storing it.
    """
    seen: dict[str, Any] = {}

    db_dive = MagicMock()
    db_dive.id = 11
    db_dive.user_id = 1

    async def fake_update(*, db: Any, object: dict, uuid: uuid_pkg.UUID) -> None:
        seen["update_data"] = object

    monkeypatch.setattr(dives_module, "_get_owned_dive", AsyncMock(return_value=db_dive))
    monkeypatch.setattr(dives_module.crud_dives, "update", fake_update)
    monkeypatch.setattr(dives_module, "resolve_trip_id_for_user", AsyncMock(return_value=77))
    monkeypatch.setattr(dives_module, "recalculate_dive_stats", AsyncMock())
    monkeypatch.setattr(dives_module, "recalculate_gear_dive_counts", AsyncMock())
    monkeypatch.setattr(dives_module, "invalidate_dive_caches", AsyncMock())
    monkeypatch.setattr(dives_module, "invalidate_gear_caches", AsyncMock())

    return seen


async def _patch(values: DiveUpdateRequest) -> None:
    await dives_module.patch_dive(
        request=MagicMock(),
        uuid=uuid7(),
        values=values,
        current_user={"id": 1, "uuid": uuid7()},
        db=MagicMock(),
    )


class TestTripDetach:
    """An explicit `trip_uuid: null` detaches a dive from its trip; omitting the key
    leaves the trip alone.

    This is the fix the web client's "remove from trip" depends on, and it rests entirely
    on `if "trip_uuid" in values.model_fields_set`. Delete that branch and the first test
    here fails - `trip_id` never reaches `update_data`, so the dive keeps its trip and the
    diver's edit silently does nothing.
    """

    @pytest.mark.asyncio
    async def test_explicit_null_detaches_the_dive(self, captured: dict[str, Any]) -> None:
        await _patch(DiveUpdateRequest.model_validate({"trip_uuid": None}))

        assert captured["update_data"]["trip_id"] is None

    @pytest.mark.asyncio
    async def test_an_omitted_key_leaves_the_trip_alone(self, captured: dict[str, Any]) -> None:
        # `notes` is here only so there is *something* to update - `patch_dive` skips the
        # write entirely when `update_data` is empty, which would pass this test for the
        # wrong reason.
        await _patch(DiveUpdateRequest.model_validate({"notes": "Viz was better than forecast"}))

        assert "trip_id" not in captured["update_data"]

    @pytest.mark.asyncio
    async def test_a_uuid_is_translated_to_the_internal_id(self, captured: dict[str, Any]) -> None:
        await _patch(DiveUpdateRequest.model_validate({"trip_uuid": str(TRIP_UUID)}))

        # The public uuid must never reach the column; `resolve_trip_id_for_user` is what
        # scopes the lookup to trips this user actually owns.
        assert captured["update_data"]["trip_id"] == 77
        assert "trip_uuid" not in captured["update_data"]

    @pytest.mark.asyncio
    async def test_an_unknown_trip_is_rejected_before_the_write(
        self, captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Via `monkeypatch` rather than a bare assignment, which would leak the stub into
        # every later test in the session.
        monkeypatch.setattr(dives_module, "resolve_trip_id_for_user", AsyncMock(return_value=None))

        with pytest.raises(UnprocessableEntityException, match="Trip not found"):
            await _patch(DiveUpdateRequest.model_validate({"trip_uuid": str(TRIP_UUID)}))

        assert "update_data" not in captured


class TestNonNullableFields:
    """`dive_number`, `start_time`, `duration` and `notes` map to `NOT NULL` columns, so
    an explicit null is refused by the schema with a message naming the field - rather
    than surviving into the UPDATE and coming back as a foreign-key error.
    """

    @pytest.mark.parametrize("field", ["dive_number", "start_time", "duration", "notes"])
    def test_rejects_an_explicit_null(self, field: str) -> None:
        with pytest.raises(ValidationError) as exc_info:
            DiveUpdate.model_validate({field: None})

        message = str(exc_info.value)
        assert field in message
        assert "cannot be null" in message
        # The old failure mode: a not-null violation described as a missing relation.
        assert "related record" not in message

    def test_names_every_offending_field_at_once(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            DiveUpdate.model_validate({"duration": None, "notes": None})

        message = str(exc_info.value)
        assert "duration" in message
        assert "notes" in message

    @pytest.mark.parametrize(
        "field",
        ["max_depth", "avg_depth", "bottom_temperature", "visibility", "weight", "trip_uuid"],
    )
    def test_still_accepts_a_null_for_a_genuinely_nullable_field(self, field: str) -> None:
        # Clearing these is a real operation - a diver correcting a mistyped max depth
        # back to "not recorded" - so the guard must not overreach.
        values = DiveUpdate.model_validate({field: None})

        assert field in values.model_fields_set
        assert getattr(values, field) is None

    def test_an_omitted_field_is_still_fine(self) -> None:
        values = DiveUpdate.model_validate({})

        assert values.model_fields_set == set()
        assert values.duration is None

    @pytest.mark.asyncio
    async def test_a_real_start_time_still_splits_into_instant_and_offset(self, captured: dict[str, Any]) -> None:
        await _patch(DiveUpdateRequest.model_validate({"start_time": START_TIME.isoformat()}))

        # The pair must move together: storing one without the other is what left
        # `utc_offset_minutes` describing the previous start time.
        assert captured["update_data"]["start_time"] == datetime(2026, 4, 4, 8, 4, 47, tzinfo=UTC)
        assert captured["update_data"]["utc_offset_minutes"] == 120

    @pytest.mark.asyncio
    async def test_an_untouched_start_time_leaves_the_offset_alone(self, captured: dict[str, Any]) -> None:
        await _patch(DiveUpdateRequest.model_validate({"duration": 2048}))

        assert "start_time" not in captured["update_data"]
        assert "utc_offset_minutes" not in captured["update_data"]
