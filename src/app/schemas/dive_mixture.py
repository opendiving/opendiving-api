from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class GasRole(StrEnum):
    """What a cylinder was carried for on the dive.

    A closed vocabulary rather than free text, for the same reason as `GearType`
    (`schemas/gear_item.py`): the value exists so the UI can label and, later, so
    per-tank gas accounting can tell a back gas from a bottle that was only breathed
    on the ascent. Free text would let one diver's log spell the same role three ways.

    This is deliberately **not** the gas-name synthesis that was rejected (see
    DECISIONS.md). That rejection was about the since-removed `DiveMixture.name` - using
    a Suunto `Gases[].State` of "Primary" as a *label a diver would recognize*, where the
    honest label is "Air" or "EAN32". Role is the orthogonal fact, it has its own column,
    and nothing derives a name from it: a dive detail page shows `gasName()`'s "EAN50"
    **and** a "deco" badge, not one standing in for the other.

    Members are ordered the way a diver lists cylinders - the gas breathed at depth
    first, then what is carried for the way up - so a caller that wants that order can
    take it from the enum. Like `GearType`, it has no mirroring DB `CHECK`: it is a
    Pydantic field, so every write through the API and the admin panel is already
    rejected server-side, and a DB copy of the list would need a `DROP`/`ADD CONSTRAINT`
    each time the vocabulary grew.
    """

    BOTTOM = "bottom"
    DECO = "deco"
    DILUENT = "diluent"
    OXYGEN = "oxygen"


class TankUsage(StrEnum):
    """How a cylinder was breathed, which is not what it was carried for.

    Orthogonal to `GasRole`, and the reason the two are separate columns: a diver can
    carry a `bottom` gas in a sidemount pair and a `deco` bottle staged on the same dive,
    and it is *this* fact that decides whether consumption can be summed.

    - `parallel` - a sidemount pair or independent doubles, breathed alternately at the
      same depth. Their litres are additive against the dive's own average depth and
      duration, which is what `compute_parallel_gas_use` does with them.
    - `staged` - a bottle breathed at its own depth. Deliberately documentary today: it
      changes no arithmetic, because summing it against the whole dive's average depth is
      the exact misattribution the multi-cylinder refusal exists to prevent. It is the
      honest answer for the cylinder that is not parallel, and the hook for the
      per-mixture time-on-gas work that would compute it properly.

    Null means "not recorded", and is what every stored row says until a diver answers:
    no format this app parses carries the distinction (see DECISIONS.md).

    `parallel` is declared first because clients take list order from the enum, the same
    rule `GasRole` states - and parallel is the answer that does something, so it leads.
    A third `manifolded` value was considered and rejected as purely documentary: a
    manifolded twinset is already logged as *one* cylinder at the pair's combined water
    capacity with a single shared pressure, and computes correctly today.

    Like `GasRole` and `GearType`, it has no mirroring DB `CHECK`: it is a Pydantic field
    on every write path including the admin panel, and a DB copy of the list would need a
    `DROP`/`ADD CONSTRAINT` each time the vocabulary grew.
    """

    PARALLEL = "parallel"
    STAGED = "staged"


class DiveMixtureBase(BaseModel):
    """The shape a mixture is read back in - and deliberately the *unbounded* one.

    `DiveMixtureRead` inherits this and `crud_dive_mixtures` runs
    `DiveMixtureRead.model_validate(row)` over every mixture on every read, so a bound
    declared here would validate stored rows on the way **out**: one violating row would
    turn `GET /dives` and `GET /dive/{uuid}` into a 500, making the dive unviewable
    rather than merely unsavable, and `services/export/envelope.py` would break the whole
    export on the same row. That is not hypothetical for this model - `_ParserOutput`'s
    docstring records that one stored `NaN` already does exactly that.

    A read schema that can reject its own table is a liability. The pressure bounds
    therefore live on `DiveMixtureCreate`/`DiveMixtureUpdate` and on the DB `CHECK`s,
    which together are what keep the table clean; a row that somehow slips past both
    still reaches the diver as a field with a message rather than as a 500.
    """

    volume: Annotated[float, Field(examples=[12.0], description="Cylinder volume in liters")]
    start_pressure: Annotated[
        float | None, Field(default=None, examples=[200.0], description="Starting pressure in bar")
    ]
    end_pressure: Annotated[float | None, Field(default=None, examples=[50.0], description="Ending pressure in bar")]
    oxygen: Annotated[float, Field(default=21.0, description="Oxygen percentage")]
    helium: Annotated[float, Field(default=0.0, description="Helium percentage")]
    po2_limit: Annotated[
        float | None,
        Field(
            default=None,
            ge=0.4,
            le=2.0,
            examples=[1.4],
            description="The ppO2 this gas was planned to, in bar - the limit its MOD is derived from. Imported "
            "from the dive computer where the export records one, and editable. Null falls back to the client's "
            "own working-ppO2 default.",
        ),
    ]
    gas_number: Annotated[
        int | None,
        Field(
            default=None,
            ge=0,
            examples=[1],
            description="How the source export identifies this cylinder, and the join key to the profile's "
            "per-cylinder pressure channels. A label, not an index - some devices number from 0. Clients are "
            "expected to echo back what a dive was imported with rather than assign one; a hand-added cylinder "
            "has none.",
        ),
    ]
    role: Annotated[
        GasRole | None,
        Field(default=None, examples=[GasRole.BOTTOM], description="What the cylinder was carried for"),
    ]
    usage: Annotated[
        TankUsage | None,
        Field(
            default=None,
            examples=[TankUsage.PARALLEL],
            description="How the cylinder was breathed, which is not what it was carried for. `parallel` is a "
            "sidemount pair or independent doubles breathed alternately at the same depth; `staged` is a bottle "
            "breathed at its own depth. Null is 'not recorded' - no dive-computer format carries the distinction, so "
            "an import never sets it. A dive whose cylinders are *all* `parallel` gets a gas-consumption figure by "
            "summing their litres, which is otherwise unavailable without per-cylinder gas switches.",
        ),
    ]


class DiveMixtureCreate(DiveMixtureBase):
    model_config = ConfigDict(extra="forbid")

    start_pressure: Annotated[
        float | None,
        Field(
            default=None,
            gt=0,
            le=350,
            examples=[200.0],
            description="Starting pressure in bar. Above 0: a cylinder at 0 bar delivers nothing, so no dive "
            "began on one - see `DiveMixtureSchema._drop_unpressurized` for the corpus evidence that a file's 0 "
            "is an absent-marker. At most 350: above any real 300 bar DIN fill, so what it catches is a unit "
            "error or two cylinders summed as one.",
        ),
    ]
    end_pressure: Annotated[
        float | None,
        Field(
            default=None,
            ge=0,
            le=350,
            examples=[50.0],
            description="Ending pressure in bar. 0 is legal here and start is not: you cannot start a dive on an "
            "empty cylinder, but you can finish one on an empty cylinder - an out-of-gas ascent, a drained stage "
            "and an SPG pegged at zero are all dives worth logging honestly.",
        ),
    ]


class DiveMixtureCreateInternal(DiveMixtureCreate):
    dive_id: int


class DiveMixtureRead(DiveMixtureBase):
    model_config = ConfigDict(from_attributes=True)

    id: int


class DiveMixtureUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    volume: Annotated[float | None, Field(default=None, description="Cylinder volume in liters")]
    start_pressure: Annotated[
        float | None,
        Field(default=None, gt=0, le=350, description="Starting pressure in bar - above 0 and at most 350"),
    ]
    end_pressure: Annotated[
        float | None,
        Field(default=None, ge=0, le=350, description="Ending pressure in bar - 0 is legal, unlike a start"),
    ]
    oxygen: Annotated[float | None, Field(default=None, description="Oxygen percentage")]
    helium: Annotated[float | None, Field(default=None, description="Helium percentage")]
    po2_limit: Annotated[
        float | None, Field(default=None, ge=0.4, le=2.0, description="Planned ppO2 for this gas, in bar")
    ]
    gas_number: Annotated[
        int | None, Field(default=None, ge=0, description="How the source export identifies this cylinder")
    ]
    role: Annotated[GasRole | None, Field(default=None, description="What the cylinder was carried for")]
    usage: Annotated[TankUsage | None, Field(default=None, description="How the cylinder was breathed")]
