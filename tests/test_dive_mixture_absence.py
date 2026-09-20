"""Unit tests for the third state a cylinder's size and mix can be in: not recorded.

`dive_mixture.volume`, `.oxygen` and `.helium` are nullable columns, and NULL is a value
rather than a gap - the source never wrote one down. All three are OPTIONAL in DiveJSON,
whose §6.3 blesses a cylinder converted from a mix-only source with its vessel members
absent and says of `oxygen` in as many words that absent means not recorded, **not 21**.

The layers, and why each gets its own class - the same split
`test_dive_mixture_pressures.py` makes for the pressure bounds:

* the **read** schema (`DiveMixtureBase`, and every schema that inherits it) must admit the
  NULL, because it is validated over every stored row on every dive read and is the export
  document's cylinder shape too;
* the **write** schema must be able to *express* it, which is the half that is easy to skip
  and makes the rest achieve nothing: with `oxygen` still defaulting to 21 on the shared
  base, the table could hold "not recorded" while the API could not say it.

`test_dive_check_constraints.py` covers the `CHECK`s underneath both against a live
Postgres, `test_logbook_import.py` the import path that produces such a row, and
`test_dive_gas.py` the consumption figure a missing size costs.
"""

import pytest

from src.app.schemas.dive_mixture import (
    DiveMixtureBase,
    DiveMixtureCreate,
    DiveMixtureRead,
    GasRole,
    TankUsage,
    as_create,
)

_ABSENT = ("volume", "oxygen", "helium")


class TestTheWriteSchemaCanSayNotRecorded:
    """The API contract change, and the reason it is a `feat!`.

    `oxygen` and `helium` carried `default=21.0` / `default=0.0` on the shared base, which
    `DiveMixtureCreate` inherited. A client that omitted them was not saying "not recorded"
    - it was saying air, and there was no way to say anything else. The default is a
    convenience that belongs in a form a diver can see and change, not on the wire.
    """

    @pytest.mark.parametrize("field", _ABSENT)
    def test_an_omitted_member_is_absent_rather_than_a_default(self, field: str) -> None:
        mixture = DiveMixtureCreate.model_validate({})

        assert getattr(mixture, field) is None

    @pytest.mark.parametrize("field", _ABSENT)
    def test_an_explicit_null_is_accepted(self, field: str) -> None:
        """What a converter actually sends. The distinction between omitting a member and
        sending `null` is one this schema deliberately does not draw - both mean the source
        had nothing - and it is `RejectsExplicitNulls` that draws it for the columns where
        it matters, none of which are these any more."""
        assert getattr(DiveMixtureCreate.model_validate({field: None}), field) is None

    @pytest.mark.parametrize("field", _ABSENT)
    def test_the_published_contract_says_so(self, field: str) -> None:
        """`/openapi.json` is what the web and iOS clients are generated from, so a client
        reading `oxygen: number` while the API sends `null` is the failure that survives
        every test in this repo. `anyOf` with a null branch is how Pydantic spells the
        widened type there."""
        published = DiveMixtureCreate.model_json_schema()["properties"][field]

        assert {"type": "null"} in published["anyOf"]
        assert published["default"] is None

    def test_a_recorded_value_still_arrives_unchanged(self) -> None:
        mixture = DiveMixtureCreate.model_validate({"volume": 11.1, "oxygen": 32.0, "helium": 0.0})

        assert (mixture.volume, mixture.oxygen, mixture.helium) == (11.1, 32.0, 0.0)


class TestTheReadSchemaAdmitsTheStoredRow:
    """`DiveMixtureBase`'s own docstring is the argument: a read schema that can reject its
    own table is a liability. `crud_dive_mixtures` runs `DiveMixtureRead.model_validate` over
    every mixture on every read, and `services/export/envelope.py` re-wraps each one as the
    base - so a `float` annotation over a nullable column is a 500 on `GET /dive/{uuid}` and
    a failure of the whole logbook export, not a validation error a diver could act on.
    """

    @pytest.mark.parametrize("schema", [DiveMixtureBase, DiveMixtureRead])
    def test_a_cylinder_with_none_of_the_three_reads_back(self, schema: type[DiveMixtureBase]) -> None:
        payload: dict[str, object] = {"volume": None, "oxygen": None, "helium": None}
        if schema is DiveMixtureRead:
            payload["id"] = 1

        mixture = schema.model_validate(payload)

        assert [getattr(mixture, field) for field in _ABSENT] == [None, None, None]

    def test_the_mix_only_cylinder_keeps_everything_else(self) -> None:
        """The flagship shape: a UDDF `<tankdata>` with a gas link, both pressures and no
        `<tankvolume>`. Only the size is missing, and the rest has to survive intact - it is
        what the dive page shows instead of a consumption figure."""
        mixture = DiveMixtureRead.model_validate(
            {"id": 1, "volume": None, "oxygen": 32.0, "helium": 0.0, "start_pressure": 200.0, "end_pressure": 80.0}
        )

        assert mixture.volume is None
        assert (mixture.oxygen, mixture.start_pressure, mixture.end_pressure) == (32.0, 200.0, 80.0)


class TestCopyingAStoredCylinderOntoAnotherDive:
    """`as_create` is what a merge, a recording attach and the logbook importer use to copy
    a stored cylinder onto another dive.

    It exists because those four call sites each rebuilt `DiveMixtureCreate(**row.model_dump())`
    by hand, which stopped being safe when `DiveMixtureRead.role`/`usage` widened to the stored
    string: the write schema still types the enums, so a value outside one raised out of a
    merge, an attach or an import. See *"A stored vocabulary is read back as a string"* in
    DECISIONS.md.
    """

    def test_the_numbers_survive_a_role_this_build_cannot_name(self) -> None:
        copy = as_create(DiveMixtureRead(id=1, volume=12.0, oxygen=32.0, start_pressure=200.0, role="frobnicator"))

        assert (copy.volume, copy.oxygen, copy.start_pressure) == (12.0, 32.0, 200.0)
        assert copy.role is None

    def test_usage_is_dropped_on_the_same_terms(self) -> None:
        copy = as_create(DiveMixtureRead(id=1, volume=12.0, usage="frobnicator"))

        assert copy.usage is None

    def test_a_recognized_pair_is_carried_across_unchanged(self) -> None:
        """The other half, so the test above cannot pass by dropping everything."""
        copy = as_create(DiveMixtureRead(id=1, volume=12.0, role=GasRole.DECO, usage=TankUsage.STAGED))

        assert (copy.role, copy.usage) == (GasRole.DECO, TankUsage.STAGED)
