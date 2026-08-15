"""Unit tests for `core/schemas.py::RejectsExplicitNulls` and every update schema that
declares its `NOT NULL` columns through it.

A PATCH body types every field `T | None` because that is how "omit it to leave it
alone" is spelled - but for a `NOT NULL` column an explicit `null` is a different thing
entirely, and one that survives `model_dump(exclude_unset=True)` all the way into the
UPDATE. `DiveUpdate` had a validator for this; the other resources didn't, so
`PATCH /dive-site/{uuid}` with `{"name": null}` reached Postgres, raised an
`IntegrityError` and came back as a 500 with the resource's caches left un-invalidated.

`test_the_declared_fields_match_the_table` is the one that matters over time: it reads
each list back off the SQLAlchemy model, so a column added (or made nullable) without a
matching change here fails rather than quietly reopening the hole. It needs no database
- `Table.columns` is metadata, populated at import.

The dive-specific half of this behaviour (including the `start_time`/`utc_offset_minutes`
pairing it protects) stays in `test_dive_update.py`.
"""

import uuid as uuid_pkg
from collections.abc import Generator, Sequence
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.api.v1 import dive_sites as dive_sites_module
from src.app.core.config import settings
from src.app.core.schemas import RejectsExplicitNulls
from src.app.core.setup import create_application
from src.app.core.utils import cache as cache_module
from src.app.models.certification import Certification
from src.app.models.dive import Dive
from src.app.models.dive_site import DiveSite
from src.app.models.gear_item import GearItem
from src.app.models.gear_service_record import GearServiceRecord
from src.app.models.gear_service_schedule import GearServiceSchedule
from src.app.models.gear_set import GearSet
from src.app.models.trip import Trip
from src.app.models.user import User
from src.app.schemas.certification import CertificationUpdate
from src.app.schemas.dive import DiveUpdate
from src.app.schemas.dive_site import DiveSiteUpdate
from src.app.schemas.gear_item import GearItemUpdate
from src.app.schemas.gear_service import GearServiceRecordUpdate, GearServiceScheduleUpdate
from src.app.schemas.gear_set import GearSetUpdate
from src.app.schemas.trip import TripUpdate
from src.app.schemas.user import UserAdminUpdate, UserUpdate

# Every update schema the public API accepts a PATCH body into, paired with the table it
# writes to. The admin-panel-only ones (`DiveMixtureUpdate`, `UserDiveStatsUpdate`, the
# join-table schemas) are deliberately absent - they are reachable only through
# CRUDAdmin's own forms, which submit a rendered field set rather than arbitrary JSON.
SCHEMAS_AND_TABLES: list[tuple[type[RejectsExplicitNulls], Any]] = [
    (DiveUpdate, Dive),
    (DiveSiteUpdate, DiveSite),
    (TripUpdate, Trip),
    (GearItemUpdate, GearItem),
    (GearSetUpdate, GearSet),
    (CertificationUpdate, Certification),
    (GearServiceScheduleUpdate, GearServiceSchedule),
    (GearServiceRecordUpdate, GearServiceRecord),
    (UserUpdate, User),
    (UserAdminUpdate, User),
]

NULLABLE_CASES = [
    (schema, name)
    for schema, model in SCHEMAS_AND_TABLES
    for name in schema.model_fields
    if name in model.__table__.columns and model.__table__.columns[name].nullable
]

NON_NULLABLE_CASES = [(schema, name) for schema, _ in SCHEMAS_AND_TABLES for name in schema.NON_NULLABLE_FIELDS]


def _ids(cases: Sequence[tuple[type[RejectsExplicitNulls], str]]) -> list[str]:
    return [f"{schema.__name__}.{name}" for schema, name in cases]


@pytest.mark.parametrize(("schema", "field"), NON_NULLABLE_CASES, ids=_ids(NON_NULLABLE_CASES))
def test_rejects_an_explicit_null(schema: type[RejectsExplicitNulls], field: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        schema.model_validate({field: None})

    message = str(exc_info.value)
    assert field in message
    assert "cannot be null" in message
    # The old failure mode on the dive routes: a not-null violation described as a
    # missing relation. Everywhere else it was a bare 500.
    assert "related record" not in message


@pytest.mark.parametrize(("schema", "field"), NULLABLE_CASES, ids=_ids(NULLABLE_CASES))
def test_still_accepts_a_null_for_a_genuinely_nullable_column(schema: type[RejectsExplicitNulls], field: str) -> None:
    # Clearing these is a real operation - a trip that turns out to be open-ended, a
    # dive site whose location was mistyped - so the guard must not overreach.
    values = schema.model_validate({field: None})

    assert field in values.model_fields_set
    assert getattr(values, field) is None


@pytest.mark.parametrize(("schema", "model"), SCHEMAS_AND_TABLES, ids=lambda p: getattr(p, "__name__", str(p)))
def test_the_declared_fields_match_the_table(schema: type[RejectsExplicitNulls], model: Any) -> None:
    """`NON_NULLABLE_FIELDS` is hand-written, so it can drift from the columns it
    describes - a new `NOT NULL` column reopens the hole silently.
    """
    columns = model.__table__.columns
    from_table = {name for name in schema.model_fields if name in columns and not columns[name].nullable}

    assert set(schema.NON_NULLABLE_FIELDS) == from_table


@pytest.mark.parametrize(("schema", "_model"), SCHEMAS_AND_TABLES, ids=lambda p: getattr(p, "__name__", str(p)))
def test_every_declared_field_exists_on_the_schema(schema: type[RejectsExplicitNulls], _model: Any) -> None:
    # A renamed field would otherwise leave a stale entry that silently guards nothing.
    assert set(schema.NON_NULLABLE_FIELDS) <= set(schema.model_fields)


@pytest.mark.parametrize(("schema", "_model"), SCHEMAS_AND_TABLES, ids=lambda p: getattr(p, "__name__", str(p)))
def test_an_omitted_field_is_still_fine(schema: type[RejectsExplicitNulls], _model: Any) -> None:
    values = schema.model_validate({})

    assert values.model_fields_set == set()


def test_names_every_offending_field_at_once() -> None:
    with pytest.raises(ValidationError) as exc_info:
        DiveSiteUpdate.model_validate({"name": None, "notes": None})

    message = str(exc_info.value)
    assert "name" in message
    assert "notes" in message


def test_a_schema_that_declares_nothing_accepts_any_null() -> None:
    class Anything(RejectsExplicitNulls):
        value: str | None = None

    assert Anything.model_validate({"value": None}).value is None


OWNER = {"id": 7, "uuid": uuid_pkg.uuid4(), "username": "ada", "is_superuser": False}


def _fake_redis() -> Any:
    """Just the calls `@cache` makes on the write path - delete the item key, then scan
    and delete the list keys.
    """
    fake = Mock()
    fake.get = AsyncMock(return_value=None)
    fake.set = AsyncMock()
    fake.expire = AsyncMock()
    fake.delete = AsyncMock()
    fake.scan = AsyncMock(return_value=(0, []))
    return fake


@pytest.fixture(scope="module")
def owned_app() -> Any:
    """Its own app with `create_tables_on_start=False`, as in `test_ownership.py` - the
    shared `client` fixture opens one whose startup hook connects to Postgres, which
    would put this in the database-backed subset for no reason.
    """
    return create_application(router=router, settings=settings, create_tables_on_start=False)


@pytest.fixture
def signed_in_client(owned_app: Any) -> Generator[TestClient]:
    owned_app.dependency_overrides[get_current_user] = lambda: OWNER
    try:
        with TestClient(owned_app) as test_client:
            yield test_client
    finally:
        owned_app.dependency_overrides = {}


class TestTheNullNeverReachesTheDatabase:
    """The reported bug, asserted at the wire: `PATCH /dive-site/{uuid}` with
    `{"name": null}` was a 500 from an `IntegrityError`, and because the write raised,
    the dive caches that embed this site's name were never invalidated either.

    Over HTTP rather than against the schema alone, because that is where the difference
    between 422 and 500 lives, and because "no UPDATE was attempted" is the half a
    schema test can't see.
    """

    def test_an_explicit_null_for_a_not_null_column_is_a_422(
        self, signed_in_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        update = AsyncMock()
        monkeypatch.setattr(dive_sites_module, "_get_owned_dive_site", AsyncMock())
        monkeypatch.setattr(dive_sites_module.crud_dive_sites, "update", update)

        response = signed_in_client.patch(f"/api/v1/dive-site/{uuid_pkg.uuid4()}", json={"name": None})

        assert response.status_code == 422
        assert "name cannot be null" in str(response.json()["detail"])
        update.assert_not_awaited()

    def test_a_null_for_a_nullable_column_still_writes(
        self, signed_in_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The other half: clearing `location` is how a mistyped one is corrected, so the
        # guard must not turn it into a 422.
        update = AsyncMock()
        # Unlike the 422 above, this one runs the handler to completion - and
        # `patch_dive_site` is `@cache`-decorated, so it reaches Redis on the way out.
        monkeypatch.setattr(cache_module, "client", _fake_redis())
        monkeypatch.setattr(dive_sites_module, "_get_owned_dive_site", AsyncMock())
        monkeypatch.setattr(dive_sites_module.crud_dive_sites, "update", update)
        monkeypatch.setattr(dive_sites_module, "dive_site_name_exists", AsyncMock(return_value=False))
        monkeypatch.setattr(dive_sites_module._dive_site_cache, "invalidate_list", AsyncMock())
        monkeypatch.setattr(dive_sites_module, "invalidate_dive_caches", AsyncMock())

        response = signed_in_client.patch(f"/api/v1/dive-site/{uuid_pkg.uuid4()}", json={"location": None})

        assert response.status_code == 200
        update.assert_awaited_once()
        assert update.await_args_list[0].kwargs["object"] == {"location": None}
