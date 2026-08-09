from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class GearSetItemBase(BaseModel):
    gear_set_id: Annotated[int, Field(examples=[1], description="ID of the gear set")]
    gear_item_id: Annotated[int, Field(examples=[1], description="ID of the gear item")]
    position: Annotated[int, Field(default=0, examples=[0], description="Order the item was added in")]


class GearSetItemRead(GearSetItemBase):
    id: int


class GearSetItemCreate(GearSetItemBase):
    model_config = ConfigDict(extra="forbid")


class GearSetItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gear_set_id: Annotated[int | None, Field(default=None, description="ID of the gear set")]
    gear_item_id: Annotated[int | None, Field(default=None, description="ID of the gear item")]
    position: Annotated[int | None, Field(default=None, description="Order the item was added in")]


class GearSetItemDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
