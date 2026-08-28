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

`TestMixtureFieldsAreClosed` is here for a related reason - it is about a body the schema
must refuse rather than one it must interpret, and this is the only module that validates
the dive request schemas directly.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import BaseModel, ValidationError
from uuid6 import uuid7

from src.app.api.v1 import dives as dives_module
from src.app.core.exceptions.http_exceptions import UnprocessableEntityException
from src.app.schemas.dive import DiveCreateRequest, DiveUpdate, DiveUpdateRequest, WaterType
from src.app.schemas.dive_mixture import GasRole, TankUsage

TRIP_UUID = uuid7()
# The one species uuid the stubbed resolver refuses, so the "not found" branch is reachable
# without a database.
UNKNOWN_SPECIES_UUID = uuid7()
START_TIME = datetime(2026, 4, 4, 10, 4, 47, tzinfo=timezone(timedelta(hours=2)))


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stubs out everything `patch_dive` touches and records the `update_data` it builds.

    `resolve_trip_id_for_user` is stubbed to a fixed id so the attach case can assert the
    route translated the public uuid rather than storing it.
    """
    seen: dict[str, Any] = {}

    async def _fake_resolve_species(*, db: Any, species_uuids: list[uuid_pkg.UUID]) -> dict | None:
        """Every uuid resolves, unless a test asked for the unknown-species branch by
        passing `UNKNOWN_SPECIES_UUID`."""
        if UNKNOWN_SPECIES_UUID in species_uuids:
            return None
        return {value: index + 100 for index, value in enumerate(species_uuids)}

    async def _record_species(*, db: Any, dive_id: int, species_ids: list[int]) -> None:
        seen["species_ids"] = species_ids

    db_dive = MagicMock()
    db_dive.id = 11
    db_dive.user_id = 1

    async def fake_update(*, db: Any, object: dict, uuid: uuid_pkg.UUID) -> None:
        seen["update_data"] = object

    monkeypatch.setattr(dives_module, "_get_owned_dive", AsyncMock(return_value=db_dive))
    monkeypatch.setattr(dives_module.crud_dives, "update", fake_update)
    monkeypatch.setattr(dives_module, "resolve_trip_id_for_user", AsyncMock(return_value=77))
    monkeypatch.setattr(dives_module, "resolve_species_ids", AsyncMock(side_effect=_fake_resolve_species))
    monkeypatch.setattr(dives_module, "replace_species_for_dive", AsyncMock(side_effect=_record_species))
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


class TestSpeciesReplacement:
    """`species_uuids` follows the rule the other list-valued fields already follow -
    omitted leaves them alone, `[]` clears them, a list replaces them wholesale.

    Worth its own class rather than trusting the symmetry, because species arrived last and
    the branch that gates cache invalidation had to grow a fourth clause. A `species_uuids`
    edit that skipped it would leave a dive's cached read showing the old sightings for the
    full hour that key lives.
    """

    @pytest.mark.asyncio
    async def test_a_list_is_written_in_the_order_given(self, captured: dict[str, Any]) -> None:
        first, second = uuid7(), uuid7()

        await _patch(DiveUpdateRequest.model_validate({"species_uuids": [str(first), str(second)]}))

        # Resolved to internal ids, in order: the public uuid must never reach the column.
        assert captured["species_ids"] == [100, 101]

    @pytest.mark.asyncio
    async def test_an_empty_list_clears_them(self, captured: dict[str, Any]) -> None:
        """Distinct from omitting the key, and the only way a diver removes their last
        sighting."""
        await _patch(DiveUpdateRequest.model_validate({"species_uuids": []}))

        assert captured["species_ids"] == []

    @pytest.mark.asyncio
    async def test_an_omitted_key_leaves_them_untouched(self, captured: dict[str, Any]) -> None:
        await _patch(DiveUpdateRequest.model_validate({"notes": "Turtle on the safety stop"}))

        assert "species_ids" not in captured

    @pytest.mark.asyncio
    async def test_an_unknown_species_is_rejected_before_the_write(self, captured: dict[str, Any]) -> None:
        """A 422 naming which, like an unknown trip or gear item - not a 403. There is no
        ownership to fail here (the catalog is global), only existence."""
        with pytest.raises(UnprocessableEntityException, match="Species not found."):
            await _patch(DiveUpdateRequest.model_validate({"species_uuids": [str(UNKNOWN_SPECIES_UUID)]}))

        assert "species_ids" not in captured

    @pytest.mark.asyncio
    async def test_a_species_only_edit_still_invalidates_the_caches(
        self, captured: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fourth clause on the invalidation gate. A PATCH carrying nothing but
        `species_uuids` leaves `update_data` empty, so without it the whole block is skipped
        and `user_{id}_dive:{uuid}` keeps serving the old sightings for an hour."""
        invalidate = AsyncMock()
        monkeypatch.setattr(dives_module, "invalidate_dive_caches", invalidate)

        await _patch(DiveUpdateRequest.model_validate({"species_uuids": [str(uuid7())]}))

        invalidate.assert_awaited_once()


class TestNonNullableFields:
    """`dive_number`, `start_time`, `duration` and `notes` map to `NOT NULL` columns, so
    an explicit null is refused by the schema with a message naming the field - rather
    than surviving into the UPDATE and coming back as a foreign-key error.

    Only the dive-specific half is here. The rule itself now lives on
    `RejectsExplicitNulls` and is exercised against every update schema (`DiveUpdate`
    included) in `test_update_explicit_nulls.py`; what that generic sweep *can't* reach
    is below - `trip_uuid`, which is a field with no column of its own, and the
    `start_time`/`utc_offset_minutes` pairing the guard exists to protect.
    """

    def test_still_accepts_a_null_for_trip_uuid(self) -> None:
        # The one nullable field on this schema that isn't a column, so the generic
        # nullable-column sweep can't see it - and the one whose explicit null is load
        # bearing: it is how the web client detaches a dive from its trip.
        values = DiveUpdate.model_validate({"trip_uuid": None})

        assert "trip_uuid" in values.model_fields_set
        assert values.trip_uuid is None

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


class TestMixtureFieldsAreClosed:
    """`DiveMixtureCreate` is `extra="forbid"`, so a field the API doesn't have is a 422
    naming it rather than a value quietly dropped on the floor.

    Pinned because the removal of `DiveMixture.name` (see DECISIONS.md) *relies* on this:
    a client that keeps sending the old label has to be told, since a silent drop would
    let a form go on collecting something that no longer lands anywhere. Loosening the
    config would turn that into data loss with no failure to notice.
    """

    @pytest.mark.parametrize("request_schema", [DiveCreateRequest, DiveUpdateRequest])
    def test_a_removed_mixture_field_is_refused_by_name(self, request_schema: type[BaseModel]) -> None:
        body: dict[str, Any] = {
            "mixtures": [{"volume": 12.0, "oxygen": 21.0, "helium": 0.0, "name": "Back Gas"}],
        }
        if request_schema is DiveCreateRequest:
            body |= {
                "user_uuid": str(uuid7()),
                "dive_number": 1,
                "start_time": START_TIME.isoformat(),
                "duration": 2048,
            }

        with pytest.raises(ValidationError) as exc_info:
            request_schema.model_validate(body)

        message = str(exc_info.value)
        assert "name" in message
        assert "extra_forbidden" in message

    def test_the_surviving_mixture_fields_still_validate(self) -> None:
        values = DiveUpdateRequest.model_validate(
            {
                "mixtures": [
                    {
                        "volume": 12.0,
                        "oxygen": 32.0,
                        "helium": 0.0,
                        "po2_limit": 1.4,
                        "role": "deco",
                        "usage": "staged",
                    }
                ]
            }
        )

        assert values.mixtures is not None
        assert values.mixtures[0].role is GasRole.DECO
        assert values.mixtures[0].usage is TankUsage.STAGED

    def test_a_mixture_with_no_usage_reads_back_as_not_recorded(self) -> None:
        """Null is the answer for every cylinder until a diver gives another one - no
        dive-computer format carries the distinction, so no import can set it.
        """
        values = DiveUpdateRequest.model_validate({"mixtures": [{"volume": 12.0, "oxygen": 21.0, "helium": 0.0}]})

        assert values.mixtures is not None
        assert values.mixtures[0].usage is None

    @pytest.mark.parametrize("bad", ["manifolded", "Parallel", "sidemount", ""])
    def test_a_usage_outside_the_vocabulary_is_refused(self, bad: str) -> None:
        """The closed vocabulary is enforced in Pydantic and nowhere else - there is no DB
        `CHECK`, the same call as `GasRole` and `GearType`. So this is the whole guard, and
        `"manifolded"` is here because it is the value that was considered and rejected.
        """
        with pytest.raises(ValidationError) as exc_info:
            DiveUpdateRequest.model_validate(
                {"mixtures": [{"volume": 12.0, "oxygen": 21.0, "helium": 0.0, "usage": bad}]}
            )

        assert "usage" in str(exc_info.value)


class TestWaterTypeAndAltitude:
    """The two environment fields are ordinary `DiveBase` scalars on every write path,
    which is the whole point of them not being `DiveTechScalars` - a diver types them, an
    import only ever seeds them.

    The clearing half matters most: both columns are nullable, so `DiveUpdate`'s
    "absent = untouched, null = cleared" contract applies with no entry in
    `NON_NULLABLE_FIELDS` and no special case anywhere - and the route has to carry the
    null through to `update_data` rather than dropping it with the unset keys.
    """

    def test_create_accepts_both(self) -> None:
        values = DiveCreateRequest.model_validate(
            {
                "user_uuid": str(uuid7()),
                "dive_number": 1,
                "start_time": START_TIME.isoformat(),
                "duration": 2048,
                "water_type": "brackish",
                "altitude": 372,
            }
        )

        assert values.water_type is WaterType.BRACKISH
        assert values.altitude == 372

    def test_create_leaves_both_unset_by_default(self) -> None:
        values = DiveCreateRequest.model_validate(
            {
                "user_uuid": str(uuid7()),
                "dive_number": 1,
                "start_time": START_TIME.isoformat(),
                "duration": 2048,
            }
        )

        assert values.water_type is None
        assert values.altitude is None

    @pytest.mark.parametrize("request_schema", [DiveCreateRequest, DiveUpdateRequest])
    def test_a_water_type_outside_the_vocabulary_is_a_422_naming_the_field(
        self, request_schema: type[BaseModel]
    ) -> None:
        """The enum is the only guard this column has - there is no DB `CHECK` behind it
        (see DECISIONS.md), so a free-text value has to die in Pydantic or not at all."""
        body: dict[str, Any] = {"water_type": "soda"}
        if request_schema is DiveCreateRequest:
            body |= {
                "user_uuid": str(uuid7()),
                "dive_number": 1,
                "start_time": START_TIME.isoformat(),
                "duration": 2048,
            }

        with pytest.raises(ValidationError) as exc_info:
            request_schema.model_validate(body)

        assert "water_type" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_a_patch_sets_both(self, captured: dict[str, Any]) -> None:
        await _patch(DiveUpdateRequest.model_validate({"water_type": "fresh", "altitude": 1500}))

        assert captured["update_data"]["water_type"] is WaterType.FRESH
        assert captured["update_data"]["altitude"] == 1500

    @pytest.mark.asyncio
    async def test_an_explicit_null_clears_both(self, captured: dict[str, Any]) -> None:
        await _patch(DiveUpdateRequest.model_validate({"water_type": None, "altitude": None}))

        assert captured["update_data"]["water_type"] is None
        assert captured["update_data"]["altitude"] is None

    @pytest.mark.asyncio
    async def test_omitting_them_leaves_them_alone(self, captured: dict[str, Any]) -> None:
        await _patch(DiveUpdateRequest.model_validate({"visibility": 15}))

        assert "water_type" not in captured["update_data"]
        assert "altitude" not in captured["update_data"]
