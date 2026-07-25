from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class DiveMixtureBase(BaseModel):
    name: Annotated[
        str | None, Field(default=None, max_length=50, examples=["Back Gas"], description="Mixture name/label")
    ]
    volume: Annotated[float, Field(examples=[12.0], description="Cylinder volume in liters")]
    start_pressure: Annotated[
        float | None, Field(default=None, examples=[200.0], description="Starting pressure in bar")
    ]
    end_pressure: Annotated[
        float | None, Field(default=None, examples=[50.0], description="Ending pressure in bar")
    ]
    po2: Annotated[float, Field(default=1.4, description="Partial pressure of oxygen set-point in bar")]
    oxygen: Annotated[float, Field(default=21.0, description="Oxygen percentage")]


class DiveMixtureCreate(DiveMixtureBase):
    model_config = ConfigDict(extra="forbid")


class DiveMixtureRead(DiveMixtureBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
