"""Unit tests for the cylinder-pressure bounds, layer by layer.

`start_pressure` is bounded to `(0, 350]` and `end_pressure` to `[0, 350]`, and the
asymmetry between them is the point rather than an oversight: **you cannot start a dive
on an empty cylinder, but you can finish one on an empty cylinder.** See DECISIONS.md
*"A cylinder pressure is a bounded field, and every layer that writes one says so"*.

Four layers write or read these numbers and they do not all answer the same question, so
each gets its own class here:

- the parse layer (`DiveMixtureSchema`) nulls anything outside the band, because a file's
  0 is an absent-marker the diver never typed and cannot see;
- the request layer (`DiveMixtureCreate`/`DiveMixtureUpdate`) **422s** it, because a
  client asserting a dive has `null` available and a 0 there is a bug or a typo;
- the read layer (`DiveMixtureRead`, and the export envelope) stays deliberately
  unbounded - a bound there would turn one bad stored row into a 500 on `GET /dives`;
- the DB `CHECK`s back all three, and live in `test_dive_check_constraints.py`.
"""

import uuid as uuid_pkg
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from src.app.api.dependencies import get_current_user
from src.app.api.v1.dives import router as dives_router
from src.app.core.db.database import async_get_db
from src.app.schemas.dive import DiveCreateRequest, DiveUpdateRequest
from src.app.schemas.dive_mixture import (
    DiveMixtureCreate,
    DiveMixtureRead,
    DiveMixtureUpdate,
)
from src.app.schemas.parsed_dive import DiveMixtureSchema
from src.app.services.dive_parsers.suunto_xml import SuuntoXmlParser
from src.app.services.export.envelope import _mixture

SUUNTO_NS = "http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal"

USER_UUID = uuid_pkg.UUID("00000000-0000-0000-0000-0000000000aa")
DIVE_UUID = uuid_pkg.UUID("00000000-0000-0000-0000-0000000000bb")

START_TIME = "2026-06-03T12:15:00Z"

# The two write schemas carry the same bounds and are exercised as a pair throughout.
WriteSchema = type[DiveMixtureCreate] | type[DiveMixtureUpdate]


def _parsed_mixture(**overrides: Any) -> DiveMixtureSchema:
    """A `DiveMixtureSchema` with every field supplied - it declares no defaults."""
    defaults: dict[str, Any] = {
        "end_pressure": 50.0,
        "gas_number": 0,
        "helium": 0.0,
        "oxygen": 21.0,
        "po2_limit": 1.4,
        "role": None,
        "start_pressure": 200.0,
        "volume": 11.1,
    }
    defaults.update(overrides)
    return DiveMixtureSchema(**defaults)


class TestCreateAndUpdateRejectAnImpossibleStartPressure:
    """The request layer 422s rather than coercing, and that is not a contradiction of
    the parse layer nulling the same value - the two schemas answer different questions
    about the same number. `DiveMixtureSchema` describes a *file*, where a 0 is DM5's
    dialect for "no transmitter"; these describe a dive a client is asserting, where the
    wire format has `null` and every client can send it.
    """

    @pytest.mark.parametrize("schema", [DiveMixtureCreate, DiveMixtureUpdate])
    @pytest.mark.parametrize("value", [0, -1, 350.1, 351, 205203])
    def test_a_start_pressure_outside_the_band_is_refused(self, schema: WriteSchema, value: float) -> None:
        with pytest.raises(ValidationError):
            schema.model_validate({"volume": 11.1, "oxygen": 21.0, "helium": 0.0, "start_pressure": value})

    @pytest.mark.parametrize("schema", [DiveMixtureCreate, DiveMixtureUpdate])
    @pytest.mark.parametrize("value", [0.1, 200, 300, 350])
    def test_a_real_fill_passes(self, schema: WriteSchema, value: float) -> None:
        """300 bar is the highest real DIN fill and must not be collateral of the 350
        bar band - the band is there to catch a unit error, not to have an opinion about
        how hard someone fills a cylinder."""
        mixture = schema.model_validate({"volume": 11.1, "oxygen": 21.0, "helium": 0.0, "start_pressure": value})

        assert mixture.start_pressure == value

    @pytest.mark.parametrize("schema", [DiveMixtureCreate, DiveMixtureUpdate])
    def test_an_explicit_null_start_pressure_is_still_the_way_to_say_unknown(self, schema: WriteSchema) -> None:
        """The bound must not close the escape hatch: an empty box normalizes to `null`,
        and that is what a diver who does not know the fill sends."""
        mixture = schema.model_validate({"volume": 11.1, "oxygen": 21.0, "helium": 0.0, "start_pressure": None})

        assert mixture.start_pressure is None


class TestCreateAndUpdateAllowAnEmptyCylinderAtTheEnd:
    """The asymmetry, pinned deliberately - it is the thing most likely to be "tidied
    up" later by someone who reads the two adjacent fields and sees an inconsistency.

    An out-of-gas ascent is rare but it happens, as does a fully drained stage or bailout
    and an SPG pegged at zero, and those are exactly the dives worth logging honestly.
    """

    @pytest.mark.parametrize("schema", [DiveMixtureCreate, DiveMixtureUpdate])
    def test_a_zero_end_pressure_is_accepted(self, schema: WriteSchema) -> None:
        mixture = schema.model_validate({"volume": 11.1, "oxygen": 21.0, "helium": 0.0, "end_pressure": 0})

        assert mixture.end_pressure == 0

    @pytest.mark.parametrize("schema", [DiveMixtureCreate, DiveMixtureUpdate])
    @pytest.mark.parametrize("value", [-1, 350.1, 351])
    def test_an_end_pressure_outside_the_band_is_refused(self, schema: WriteSchema, value: float) -> None:
        with pytest.raises(ValidationError):
            schema.model_validate({"volume": 11.1, "oxygen": 21.0, "helium": 0.0, "end_pressure": value})

    @pytest.mark.parametrize("schema", [DiveMixtureCreate, DiveMixtureUpdate])
    def test_the_upper_bound_is_the_same_on_both_fields(self, schema: WriteSchema) -> None:
        mixture = schema.model_validate({"volume": 11.1, "oxygen": 21.0, "helium": 0.0, "end_pressure": 350})

        assert mixture.end_pressure == 350


class TestTheReadSchemaStaysUnbounded:
    """This is the test that pins where the bound lives, and it fails the moment someone
    "completes" the work by moving it up to `DiveMixtureBase`.

    `crud_dive_mixtures` runs `DiveMixtureRead.model_validate(row)` over every mixture on
    every read, so a bound on the read path would turn a single violating stored row into
    a 500 on `GET /dives` and `GET /dive/{uuid}` - unviewable rather than merely
    unsavable, and unfixable without hand-written SQL. The write schemas and the `CHECK`s
    are what keep the table clean; the read schema's job is to get the row out.
    """

    @pytest.mark.parametrize("start_pressure", [0, -1, 205203])
    def test_a_row_outside_the_band_still_reads_back(self, start_pressure: float) -> None:
        mixture = DiveMixtureRead.model_validate(
            {"id": 1, "volume": 11.1, "oxygen": 21.0, "helium": 0.0, "start_pressure": start_pressure}
        )

        assert mixture.start_pressure == start_pressure

    @pytest.mark.parametrize("start_pressure", [0, -1, 205203])
    def test_the_export_envelope_survives_it_too(self, start_pressure: float) -> None:
        """`envelope._mixture` rebuilds a `DiveMixtureBase` per mixture, so a bound on
        `Base` would break the *whole* export on one row rather than one dive."""
        row = DiveMixtureRead.model_validate(
            {"id": 1, "volume": 11.1, "oxygen": 21.0, "helium": 0.0, "start_pressure": start_pressure}
        )

        assert _mixture(row).start_pressure == start_pressure

    def test_a_row_that_recorded_no_size_or_mix_reads_back(self) -> None:
        """The same liability arriving from the other direction, and the one the columns
        becoming nullable created: `volume`, `oxygen` and `helium` are NULL-able now, so a
        `float` annotation on `DiveMixtureBase` would turn every mix-only cylinder into a
        500 on `GET /dive/{uuid}` - and, through `envelope._mixture`, into a failure of the
        whole logbook export rather than of one dive."""
        row = DiveMixtureRead.model_validate({"id": 1, "volume": None, "oxygen": None, "helium": None})

        assert (row.volume, row.oxygen, row.helium) == (None, None, None)
        exported = _mixture(row)
        assert (exported.volume, exported.oxygen, exported.helium) == (None, None, None)


class TestTheParseLayerNullsRatherThanRejects:
    """A file with one unusable number is still a file worth storing - the
    `_drop_unpressurized` principle. Rejecting here would fail the attach of an otherwise
    perfectly importable export over a value the diver never chose.
    """

    @pytest.mark.parametrize("field", ["start_pressure", "end_pressure"])
    @pytest.mark.parametrize("value", [0, -1, 350.1, 351, 205203])
    def test_a_pressure_outside_the_band_becomes_none(self, field: str, value: float) -> None:
        assert getattr(_parsed_mixture(**{field: value}), field) is None

    @pytest.mark.parametrize("field", ["start_pressure", "end_pressure"])
    @pytest.mark.parametrize("value", [0.1, 200.0, 300.0, 350.0])
    def test_a_pressure_inside_the_band_survives(self, field: str, value: float) -> None:
        assert getattr(_parsed_mixture(**{field: value}), field) == value

    def test_the_band_is_the_same_on_both_fields_here(self) -> None:
        """Unlike the request layer, which floors `end_pressure` at 0 and `start_pressure`
        above it. A parser has no diver asserting anything: a 0 from a file is an
        absent-marker in either column, so both are nulled."""
        assert _parsed_mixture(end_pressure=0).end_pressure is None


class TestEveryParsedPressureSatisfiesTheRequestSchema:
    """The pattern-closing test: **no parsed value reaches a bounded column without
    having passed the bound the column applies.**

    Scoped naively - a sweep of the corpus fixtures - this passes green while proving
    nothing, because no fixture produces an out-of-range value. So it drives a real
    parser with a file that is out of range on *each* side of the bound, which is the
    recurrence it exists to catch: the DM5 millibar bug stored `start_pressure = 205203`,
    and without the parse-side clause a repeat would prefill the form with it and 422 on
    Save - a field the diver never chose and cannot see.
    """

    @staticmethod
    def _xml(start_millibar: str, end_millibar: str) -> bytes:
        return f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <DiveMixtures>
    <DiveMixture>
      <StartPressure>{start_millibar}</StartPressure>
      <EndPressure>{end_millibar}</EndPressure>
      <Oxygen>21</Oxygen>
      <Size>11.1</Size>
    </DiveMixture>
  </DiveMixtures>
</Dive>
""".encode()

    @pytest.mark.parametrize(
        ("start_millibar", "end_millibar"),
        [
            ("0", "0"),  # the attested low side: DM5's "no transmitter"
            ("500000", "400000"),  # 500/400 bar - a sidemount pair summed, or a unit slip
            ("205203000", "86781000"),  # the millibar-as-bar bug, one factor of 1000 worse
        ],
    )
    def test_an_out_of_range_export_still_yields_a_savable_mixture(
        self, start_millibar: str, end_millibar: str
    ) -> None:
        parsed = SuuntoXmlParser.parse(self._xml(start_millibar, end_millibar)).mixtures[0]

        # What the form does with a parsed cylinder: keep the file's numbers, fill what
        # the file omitted from its own defaults. The pressures have to pass on their own.
        mixture = DiveMixtureCreate.model_validate(
            {
                "volume": parsed.volume or 11.1,
                "oxygen": parsed.oxygen if parsed.oxygen is not None else 21.0,
                "helium": parsed.helium if parsed.helium is not None else 0.0,
                "start_pressure": parsed.start_pressure,
                "end_pressure": parsed.end_pressure,
            }
        )

        assert (mixture.start_pressure, mixture.end_pressure) == (None, None)

    def test_a_real_export_keeps_its_pressures(self) -> None:
        """The other half of the guard: nulling everything would pass the test above and
        lose the data. 205203 millibar is 205.2 bar and is a real fill."""
        parsed = SuuntoXmlParser.parse(self._xml("205203", "86781")).mixtures[0]

        pressures = parsed.model_dump(include={"start_pressure", "end_pressure"})
        mixture = DiveMixtureCreate.model_validate({"volume": 11.1, "oxygen": 21.0, "helium": 0.0} | pressures)

        assert (mixture.start_pressure, mixture.end_pressure) == (205.2, 86.78)


def _make_client() -> tuple[TestClient, AsyncMock]:
    """A minimal app exposing only the dives router, with auth and the session stubbed.

    Returns the stub session alongside the client, because that is what makes the
    assertions below mean something. FastAPI *resolves* `async_get_db` before it
    validates the body - dependencies are solved first - so an override that raised on
    resolution would prove nothing. What has to hold is that the session is never
    **used**: an untouched mock means the handler never ran and no statement was ever
    issued, so no `CHECK` was in play.
    """
    app = FastAPI()
    app.include_router(dives_router)
    session = AsyncMock(spec=AsyncSession)
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "uuid": USER_UUID, "is_superuser": False}
    app.dependency_overrides[async_get_db] = lambda: session
    return TestClient(app), session


class TestTheRouteRefusesBeforeTheDatabase:
    """The 422 has to come from Pydantic, not from the `CHECK`.

    Both are 422s to the client, but only Pydantic's names the field *and the index* -
    `mixtures.0.start_pressure` - which is what lets the form scroll to and focus the
    offending box. A constraint violation can only say "Start pressure must be above 0
    and at most 350 bar." with no clue which cylinder, and it costs a transaction to say
    it.
    """

    def test_post_dive_refuses_a_zero_start_pressure(self) -> None:
        client, session = _make_client()

        response = client.post(
            "/dive",
            json={
                "user_uuid": str(USER_UUID),
                "dive_number": 1,
                "start_time": START_TIME,
                "duration": 2048,
                "mixtures": [{"volume": 11.1, "oxygen": 21.0, "helium": 0.0, "start_pressure": 0}],
            },
        )

        assert response.status_code == 422
        locations = [error["loc"] for error in response.json()["detail"]]
        assert ["body", "mixtures", 0, "start_pressure"] in locations
        assert session.mock_calls == []

    def test_patch_dive_refuses_a_zero_start_pressure(self) -> None:
        client, session = _make_client()

        response = client.patch(
            f"/dive/{DIVE_UUID}",
            json={"mixtures": [{"volume": 11.1, "oxygen": 21.0, "helium": 0.0, "start_pressure": 0}]},
        )

        assert response.status_code == 422
        locations = [error["loc"] for error in response.json()["detail"]]
        assert ["body", "mixtures", 0, "start_pressure"] in locations
        assert session.mock_calls == []

    def test_the_request_schemas_carry_the_bound_through_to_the_list(self) -> None:
        """`DiveCreateRequest.mixtures` is a `list[DiveMixtureCreate]`, so the bound
        arrives by inheritance rather than being restated - pinned so that a future
        refactor of either request schema cannot quietly drop it."""
        body: dict[str, Any] = {"mixtures": [{"volume": 11.1, "oxygen": 21.0, "helium": 0.0, "start_pressure": 0}]}

        with pytest.raises(ValidationError):
            DiveUpdateRequest.model_validate(body)
        with pytest.raises(ValidationError):
            DiveCreateRequest.model_validate(
                body | {"user_uuid": str(USER_UUID), "dive_number": 1, "start_time": START_TIME, "duration": 2048}
            )
