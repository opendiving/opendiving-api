"""Write schemas refuse what their columns cannot honestly store.

Two holes, one per test class below. A request body can carry a bare `NaN` or `Infinity`
token - FastAPI parses them, and Pydantic floats allow them by default - and Postgres
takes a `NaN` past every one-sided `CHECK` (`'NaN'::float8 > 0` is true). And an integer
past the column's width fails as a `DataError` before any `CHECK` is consulted, so the
caller gets a 500 where a 422 belongs.

The sweep reads the schemas off the package rather than from a list, so a float field
added to any `extra="forbid"` shape is held to the rule without anyone remembering to.
"""

import importlib
import math
import pkgutil
import typing
import uuid as uuid_pkg
from collections.abc import Generator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, ValidationError

from src.app import schemas as schemas_pkg
from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.core.config import settings
from src.app.core.schemas import POSTGRES_INTEGER_MAX
from src.app.core.setup import create_application
from src.app.schemas.dive import DiveCreate, DiveUpdate

NON_FINITE = (math.nan, math.inf, -math.inf)


def _carries_a_float(annotation: Any) -> bool:
    return annotation is float or any(_carries_a_float(arg) for arg in typing.get_args(annotation))


def _write_schema_float_fields() -> Iterator[tuple[type[BaseModel], str]]:
    """Every float field on every `extra="forbid"` schema - the marker every request body
    and admin form in `app.schemas` carries, and no read shape does."""
    seen: set[type[BaseModel]] = set()
    for module_info in pkgutil.iter_modules(schemas_pkg.__path__):
        module = importlib.import_module(f"{schemas_pkg.__name__}.{module_info.name}")
        for obj in vars(module).values():
            if not (isinstance(obj, type) and issubclass(obj, BaseModel)) or obj.__module__ != module.__name__:
                continue
            if obj in seen or obj.model_config.get("extra") != "forbid":
                continue
            seen.add(obj)
            for name, field in obj.model_fields.items():
                if _carries_a_float(field.annotation):
                    yield obj, name


WRITE_FLOAT_FIELDS = sorted(_write_schema_float_fields(), key=lambda case: (case[0].__qualname__, case[1]))


def test_the_sweep_finds_the_fields_it_exists_for() -> None:
    assert (DiveCreate, "max_depth") in WRITE_FLOAT_FIELDS
    assert (DiveUpdate, "air_temperature") in WRITE_FLOAT_FIELDS


@pytest.mark.parametrize(
    ("schema", "field"),
    WRITE_FLOAT_FIELDS,
    ids=[f"{schema.__qualname__}.{field}" for schema, field in WRITE_FLOAT_FIELDS],
)
@pytest.mark.parametrize("value", NON_FINITE, ids=["nan", "inf", "-inf"])
def test_every_write_schema_float_refuses_a_non_finite_value(schema: type[BaseModel], field: str, value: float) -> None:
    with pytest.raises(ValidationError) as excinfo:
        schema.model_validate({field: value})

    alias = schema.model_fields[field].alias or field
    assert any(error["loc"][:1] == (alias,) for error in excinfo.value.errors())


OWNER = {"id": 7, "uuid": uuid_pkg.uuid4(), "username": "ada", "is_superuser": False}


@pytest.fixture(scope="module")
def owned_app() -> Any:
    """Its own app with `apply_migrations_on_start=False`, as in `test_ownership.py`, so
    this stays out of the database-backed subset."""
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@pytest.fixture
def signed_in_client(owned_app: Any) -> Generator[TestClient]:
    owned_app.dependency_overrides[get_current_user] = lambda: OWNER
    try:
        with TestClient(owned_app) as test_client:
            yield test_client
    finally:
        owned_app.dependency_overrides = {}


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_a_bare_non_finite_token_on_the_wire_is_a_422(signed_in_client: TestClient, token: str) -> None:
    """At the wire, because the parse is the half a schema test cannot see: the token is
    not valid JSON, and is accepted anyway."""
    response = signed_in_client.patch(
        f"/api/v1/dive/{uuid_pkg.uuid4()}",
        content=f'{{"max_depth": {token}}}',
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    errors = [error for error in response.json()["detail"] if error["loc"][-1] == "max_depth"]
    assert [error["input"] for error in errors] == [str(float(token))]


DIVE_INTEGERS = ("dive_number", "submerged_time", "visibility", "altitude", "rating")


class TestDiveIntegersStopAtTheColumnsWidth:
    @pytest.mark.parametrize("schema", [DiveCreate, DiveUpdate], ids=lambda schema: schema.__name__)
    @pytest.mark.parametrize("field", DIVE_INTEGERS)
    @pytest.mark.parametrize("value", [POSTGRES_INTEGER_MAX + 1, -(2**31) - 1], ids=["above", "below"])
    def test_a_value_past_the_column_is_refused(self, schema: type[BaseModel], field: str, value: int) -> None:
        with pytest.raises(ValidationError) as excinfo:
            schema.model_validate({field: value})

        assert any(error["loc"] == (field,) for error in excinfo.value.errors())

    @pytest.mark.parametrize("field", DIVE_INTEGERS)
    def test_the_columns_own_extremes_still_reach_the_check(self, field: str) -> None:
        """The column's width is the only bound here: `ck_dive_rating_range` and the others
        answer an in-range value with a message of their own (`_fk_error_detail`)."""
        assert getattr(DiveUpdate.model_validate({field: POSTGRES_INTEGER_MAX}), field) == POSTGRES_INTEGER_MAX
        assert getattr(DiveUpdate.model_validate({field: -(2**31)}), field) == -(2**31)
