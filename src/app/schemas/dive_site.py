from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ..core.schemas import PersistentDeletion, TimestampSchema


class DiveSiteBase(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255, examples=["Blue Hole"])]


class DiveSite(TimestampSchema, DiveSiteBase, PersistentDeletion):
    user_id: int


class DiveSiteRead(DiveSiteBase):
    id: int
    user_id: int
    created_at: datetime


class DiveSiteCreate(DiveSiteBase):
    model_config = ConfigDict(extra="forbid")


class DiveSiteCreateInternal(DiveSiteCreate):
    user_id: int


class DiveSiteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str | None, Field(min_length=1, max_length=255, default=None)]


class DiveSiteUpdateInternal(DiveSiteUpdate):
    updated_at: datetime


class DiveSiteDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_deleted: bool
    deleted_at: datetime
