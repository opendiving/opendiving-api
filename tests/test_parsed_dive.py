"""`POST /dive/parse`'s schema: every bounded column a file can reach has a guard on the way in,
but the dive's own members the form validates in front of the diver.

These are the schema's own validators, which the dive form's projection (`dive_reader.prefill`)
builds its values through - so a value the database would refuse never prefills a form whose
save it would then fail, over a field the diver never chose and cannot see.
"""

import json
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError
from sqlalchemy import CheckConstraint

from src.app.core.schemas import NOTES_MAX_LENGTH
from src.app.models.dive import Dive
from src.app.models.dive_mixture import DiveMixture
from src.app.models.dive_recording import DiveRecording
from src.app.schemas.dive import DiveCreate
from src.app.schemas.dive_mixture import GasRole
from src.app.schemas.parsed_dive import (
    LATITUDE_LIMIT,
    LONGITUDE_LIMIT,
    DiveMixtureSchema,
    ParsedDecoModel,
    ParsedDevice,
    ParsedDiveResponse,
    ParsedDiveSchema,
)


def _validated_fields(model: type[BaseModel]) -> set[str]:
    """Field names some `field_validator` on this schema covers - read off Pydantic's own
    registry rather than listed by hand, so the guard below cannot pass by being updated
    alongside the thing it checks."""
    return {
        field
        for decorator in model.__pydantic_decorators__.field_validators.values()
        for field in decorator.info.fields
    }


def _dive(**overrides: Any) -> ParsedDiveSchema:
    """A `ParsedDiveSchema` with the undefaulted block supplied."""
    defaults: dict[str, Any] = {
        "avg_depth": None,
        "bottom_temperature": None,
        "dive_number": None,
        "duration": None,
        "max_depth": None,
        "start_time": None,
        "mixtures": [],
    }
    return ParsedDiveSchema(**(defaults | overrides))


def _mixture(**overrides: Any) -> DiveMixtureSchema:
    defaults: dict[str, Any] = {
        "end_pressure": None,
        "gas_number": None,
        "helium": None,
        "oxygen": None,
        "po2_limit": None,
        "role": None,
        "start_pressure": None,
        "volume": None,
    }
    return DiveMixtureSchema(**(defaults | overrides))


def _constraints(model: type) -> dict[str, str]:
    return {
        constraint.name: str(constraint.sqltext)
        for constraint in model.__table__.constraints  # type: ignore[attr-defined]
        if isinstance(constraint, CheckConstraint) and isinstance(constraint.name, str)
    }


class TestEveryBoundHasAGuard:
    def test_every_single_column_bound_a_file_can_reach_has_a_parse_side_guard(self) -> None:
        """The drift guard for the rule itself, counted against the constraints rather than
        restated in prose, so the next column with a `CHECK` either gets a validator or fails
        this.

        **Single-column bounds only, and the rest is deliberate rather than forgotten.**
        `ck_dive_mixture_oxygen_helium_sum`, `ck_dive_mixture_pressure_order` and
        `ck_dive_avg_depth_within_max` constrain a *pair*, so there is no "the bad value" to
        null. The two `ck_dive_*_position_pair` constraints are pairs in the same sense and
        are honoured by `_drop_half_positions`, a *model* validator this counter cannot see;
        the four coordinate ranges under them are single-column and are counted.
        `visibility`, `weight`, `altitude` and `rating` are bounded and a file reaches them,
        and they pass unguarded by ruling - `TestTheDivesOwnMembers` below.
        """
        bounded = {
            (Dive, "avg_depth"),
            (Dive, "max_depth"),
            (DiveRecording, "cns_start"),
            (DiveRecording, "cns_end"),
            (DiveRecording, "otu_start"),
            (DiveRecording, "otu_end"),
            (DiveRecording, "surface_pressure_bar"),
            (Dive, "entry_latitude"),
            (Dive, "entry_longitude"),
            (Dive, "exit_latitude"),
            (Dive, "exit_longitude"),
            (DiveMixture, "po2_limit"),
            (DiveMixture, "gas_number"),
        }
        for model, column in bounded:
            constraints = " ".join(_constraints(model).values())
            assert column in constraints, f"{model.__name__}.{column} has no CHECK"

        guarded = {
            *((Dive, name) for name in _validated_fields(ParsedDiveSchema)),
            *((DiveRecording, name) for name in _validated_fields(ParsedDiveSchema)),
            *((DiveMixture, name) for name in _validated_fields(DiveMixtureSchema)),
        }
        assert bounded <= guarded, f"unguarded: {bounded - guarded}"

    def test_the_surface_pressure_bounds_are_the_ones_the_database_enforces(self) -> None:
        sqltext = _constraints(DiveRecording)["ck_dive_recording_surface_pressure_range"]

        assert "0.4" in sqltext and "1.2" in sqltext
        assert _dive(surface_pressure_bar=0.4).surface_pressure_bar == 0.4
        assert _dive(surface_pressure_bar=1.2).surface_pressure_bar == 1.2
        assert _dive(surface_pressure_bar=0.39).surface_pressure_bar is None
        assert _dive(surface_pressure_bar=1.21).surface_pressure_bar is None

    def test_the_po2_bounds_are_the_ones_the_database_enforces(self) -> None:
        sqltext = _constraints(DiveMixture)["ck_dive_mixture_po2_limit_range"]

        assert "0.4" in sqltext and "2.0" in sqltext
        assert [_mixture(po2_limit=value).po2_limit for value in (0.0, 0.39, 1.4, 2.0, 2.01)] == [
            None,
            None,
            1.4,
            2.0,
            None,
        ]

    def test_the_coordinate_bounds_are_the_ones_the_database_enforces(self) -> None:
        constraints = _constraints(Dive)

        assert f"{LATITUDE_LIMIT:g}" in constraints["ck_dive_entry_latitude_range"]
        assert f"{LATITUDE_LIMIT:g}" in constraints["ck_dive_exit_latitude_range"]
        assert f"{LONGITUDE_LIMIT:g}" in constraints["ck_dive_entry_longitude_range"]
        assert f"{LONGITUDE_LIMIT:g}" in constraints["ck_dive_exit_longitude_range"]

    def test_neither_position_can_be_half_stored(self) -> None:
        constraints = _constraints(Dive)

        assert constraints["ck_dive_entry_position_pair"] == "(entry_latitude IS NULL) = (entry_longitude IS NULL)"
        assert constraints["ck_dive_exit_position_pair"] == "(exit_latitude IS NULL) = (exit_longitude IS NULL)"


class TestReadingsThatAreNotReadings:
    def test_no_non_finite_float_leaves_the_parse_on_any_field(self) -> None:
        """The rule is on `_ParserOutput`, not on the bounds, because the bounds are
        comparisons and `NaN` compares `False` against all of them - and `avg_depth`,
        `max_depth` and `bottom_temperature` carry no bound at all."""
        dive = _dive(
            avg_depth=float("nan"),
            bottom_temperature=float("nan"),
            max_depth=float("inf"),
            cns_start=float("nan"),
            cns_end=float("inf"),
            otu_start=float("-inf"),
            otu_end=float("nan"),
            surface_pressure_bar=float("nan"),
            visibility=float("nan"),
            weight=float("inf"),
            air_temperature=float("-inf"),
        )
        mixture = _mixture(
            end_pressure=float("nan"),
            helium=float("nan"),
            oxygen=float("inf"),
            po2_limit=float("nan"),
            start_pressure=float("inf"),
            volume=float("nan"),
        )

        assert all(value is None for value in dive.model_dump().values() if not isinstance(value, list))
        assert all(value is None for value in mixture.model_dump().values())
        json.dumps(dive.model_dump(), allow_nan=False)
        json.dumps(mixture.model_dump(), allow_nan=False)

    def test_the_finite_guard_does_not_touch_anything_else(self) -> None:
        """It runs for every field, including the ones it must leave alone."""
        mixture = _mixture(
            end_pressure=120.0, gas_number=0, helium=0.0, oxygen=21.0, po2_limit=1.4, role=GasRole.BOTTOM, volume=11.1
        )
        dive = _dive(
            avg_depth=12.5,
            bottom_temperature=8.0,
            dive_number=41,
            duration=2400,
            max_depth=27.3,
            start_time="2026-06-03T12:15:00",
            mixtures=[mixture],
            cns_start=0.0,
            otu_end=53.0,
        )

        assert (mixture.gas_number, mixture.role) == (0, GasRole.BOTTOM)
        assert (dive.dive_number, dive.duration, dive.start_time) == (41, 2400, "2026-06-03T12:15:00")
        assert (dive.avg_depth, dive.max_depth, dive.bottom_temperature) == (12.5, 27.3, 8.0)
        assert dive.cns_start == 0.0
        assert dive.mixtures == [mixture]

    def test_a_zero_or_negative_depth_reads_as_no_depth_while_a_zero_exposure_is_a_reading(self) -> None:
        """`<= 0` against `< 0` one validator over - the two constraints genuinely differ,
        because a dive that began with no oxygen loading recorded a real 0 and a dive to 0 m
        did not happen."""
        dive = _dive(avg_depth=0.0, max_depth=-3.2, cns_start=0.0, otu_start=0.0, cns_end=-4.0)

        assert (dive.avg_depth, dive.max_depth) == (None, None)
        assert (dive.cns_start, dive.otu_start, dive.cns_end) == (0.0, 0.0, None)

    def test_a_negative_gas_number_reads_as_no_label_and_zero_is_a_real_one(self) -> None:
        assert _mixture(gas_number=-7).gas_number is None
        assert _mixture(gas_number=0).gas_number == 0

    def test_a_lone_ordinate_left_by_a_field_validator_takes_its_partner_with_it(self) -> None:
        """`_drop_non_finite` nulling a `NaN` latitude would leave a good longitude behind,
        and the surviving half would pin the dive to the equator."""
        dive = _dive(entry_latitude=float("nan"), entry_longitude=34.2)

        assert (dive.entry_latitude, dive.entry_longitude) == (None, None)

    def test_null_island_is_not_a_position(self) -> None:
        dive = _dive(exit_latitude=0.0, exit_longitude=0.0)

        assert (dive.exit_latitude, dive.exit_longitude) == (None, None)


class TestTheDivesOwnMembers:
    """The rest of the dive a file states reaches the form as the file has it, where the
    form's validation refuses what this app cannot store and the diver decides what was
    meant. Logbook import drops the same values with a note, having nobody to ask."""

    def test_a_value_the_app_cannot_store_passes_as_written(self) -> None:
        notes = "x" * (NOTES_MAX_LENGTH + 1)
        dive = _dive(visibility=2.5, weight=-1.0, altitude=7000, rating=7, notes=notes, tags=[" ", "a" * 200])

        assert (dive.visibility, dive.weight, dive.altitude, dive.rating) == (2.5, -1.0, 7000, 7)
        assert dive.notes == notes
        assert dive.tags == [" ", "a" * 200]

    def test_a_member_the_file_does_not_state_is_still_in_the_response(self) -> None:
        """Every member is always present, so a client reads `null` or `[]` as "the file
        states nothing" rather than as a missing key."""
        body = ParsedDiveResponse(**_dive().model_dump(), file_token="token").model_dump(mode="json")

        assert body["notes"] is None and body["water_type"] is None and body["boat_name"] is None
        assert body["tags"] == []


class TestTheDevice:
    def test_a_padded_or_empty_identity_is_absent_rather_than_empty(self) -> None:
        """`""` compares unequal to `None`, so a device that named itself nothing would fail
        to match the same computer read out of another export."""
        device = ParsedDevice(serial="   ", firmware="", model="  Suunto D5  ")

        assert (device.model, device.serial, device.firmware) == ("Suunto D5", None, None)

    def test_a_negative_counter_is_not_a_count(self) -> None:
        assert ParsedDevice(dive_number=0).dive_number == 0
        assert ParsedDevice(dive_number=-1).dive_number is None

    def test_a_device_of_nothing_is_not_a_device(self) -> None:
        assert _dive(device=ParsedDevice(serial=" ")).device is None

    def test_the_parse_response_carries_the_device_through(self) -> None:
        response = ParsedDiveResponse(**_dive(device=ParsedDevice(dive_number=5)).model_dump(), file_token="token")

        assert response.device is not None and response.device.dive_number == 5

    def test_creating_a_dive_still_forbids_the_member(self) -> None:
        """The device is a fact about a file, not a field of the logbook entry. A client
        echoing the parse response straight back gets a 422 rather than a silently dropped
        member."""
        assert "device" not in DiveCreate.model_fields
        valid = {"dive_number": 1, "duration": 1800, "start_time": "2026-04-17T11:49:23+02:00"}
        assert DiveCreate(**valid).dive_number == 1

        with pytest.raises(ValidationError) as raised:
            DiveCreate(**valid, device={"brand": "Suunto"})

        assert [(error["type"], error["loc"]) for error in raised.value.errors()] == [("extra_forbidden", ("device",))]


class TestTheDecoModelShape:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [(0, 0), (-1, -1), (2.0, 2), (1.5, None), (True, None), ("P2", None), (None, None)],
    )
    def test_a_setting_is_a_whole_number_or_nothing(self, value: Any, expected: int | None) -> None:
        """Nothing is floored: `-1` is Suunto's P-1 rather than an absence."""
        assert ParsedDecoModel(conservatism=value).conservatism == expected

    def test_a_model_name_longer_than_the_column_is_truncated_rather_than_refused(self) -> None:
        model = ParsedDecoModel(name="  " + "Suunto Fused RGBM " * 10 + "  ")

        assert model.name is not None and len(model.name) == 64
        assert model.name.startswith("Suunto Fused RGBM")

    def test_a_name_that_is_only_whitespace_is_absent(self) -> None:
        assert ParsedDecoModel(name="   ").name is None

    def test_one_gradient_factor_or_an_inverted_pair_names_no_setting(self) -> None:
        assert (ParsedDecoModel(gf_low=50).gf_low, ParsedDecoModel(gf_high=85).gf_high) == (None, None)
        inverted = ParsedDecoModel(gf_low=85, gf_high=50)
        assert (inverted.gf_low, inverted.gf_high) == (None, None)
        assert (ParsedDecoModel(gf_low=150, gf_high=85).gf_low, ParsedDecoModel(gf_low=30, gf_high=70).gf_high) == (
            None,
            70,
        )

    def test_a_model_of_nothing_is_not_a_model_and_one_member_is(self) -> None:
        assert _dive(deco_model=ParsedDecoModel(gf_low=50)).deco_model is None
        kept = _dive(deco_model=ParsedDecoModel(conservatism=0)).deco_model
        assert kept is not None and kept.conservatism == 0
