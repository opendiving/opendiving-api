from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class DiveDiveSiteBase(BaseModel):
    dive_id: Annotated[int, Field(examples=[1], description="ID of the dive")]
    dive_site_id: Annotated[int, Field(examples=[1], description="ID of the dive site")]
    position: Annotated[
        int, Field(default=0, examples=[0], description="Order the site was visited in (0 = primary site)")
    ]


class DiveDiveSiteRead(DiveDiveSiteBase):
    id: int


class DiveDiveSiteCreate(DiveDiveSiteBase):
    model_config = ConfigDict(extra="forbid")


class DiveDiveSiteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dive_id: Annotated[int | None, Field(default=None, description="ID of the dive")]
    dive_site_id: Annotated[int | None, Field(default=None, description="ID of the dive site")]
    position: Annotated[
        int | None, Field(default=None, description="Order the site was visited in (0 = primary site)")
    ]


class DiveDiveSiteDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
