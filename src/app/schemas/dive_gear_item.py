from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class DiveGearItemBase(BaseModel):
    dive_id: Annotated[int, Field(examples=[1], description="ID of the dive")]
    gear_item_id: Annotated[int, Field(examples=[1], description="ID of the gear item")]
    position: Annotated[int, Field(default=0, examples=[0], description="Order the item was listed in")]


class DiveGearItemRead(DiveGearItemBase):
    id: int


class DiveGearItemCreate(DiveGearItemBase):
    model_config = ConfigDict(extra="forbid")


class DiveGearItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dive_id: Annotated[int | None, Field(default=None, description="ID of the dive")]
    gear_item_id: Annotated[int | None, Field(default=None, description="ID of the gear item")]
    position: Annotated[int | None, Field(default=None, description="Order the item was listed in")]


class DiveGearItemDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
