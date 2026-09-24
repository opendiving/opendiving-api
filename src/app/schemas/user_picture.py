from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class PictureKind(StrEnum):
    """An account's two pictures. The avatar is whatever the diver shows the app; the portrait
    is an identification photo, shown where a dive shop checks a diver in and nowhere else."""

    AVATAR = "avatar"
    PORTRAIT = "portrait"


class PictureCrop(BaseModel):
    """A rectangle in the **upright** original's pixels - after its EXIF orientation is applied,
    which is the space a browser decodes a picked file into - at the picture's ratio, 1:1 for
    the avatar and 7:9 for the portrait, to within a pixel. Whether it fits the image is only
    known once the image is read, so that half is the route's 422."""

    model_config = ConfigDict(extra="forbid")

    x: Annotated[int, Field(ge=0, examples=[0])]
    y: Annotated[int, Field(ge=0, examples=[72])]
    width: Annotated[int, Field(ge=1, examples=[3024])]
    height: Annotated[int, Field(ge=1, examples=[3888])]


class PictureCropRequest(BaseModel):
    """The body of `PATCH /user/{avatar,portrait}` and `POST /user/portrait/from-avatar`."""

    model_config = ConfigDict(extra="forbid")

    crop: PictureCrop


class PictureRead(BaseModel):
    """What a write to either picture answers: its rendition's hex digest, and nothing else.

    The same value `UserRead.avatar_sha256` or `portrait_sha256` carries, returned so a client
    can render the new picture without a `GET /user` - it is the rendition route's `ETag` and
    the `?v=` that gives each version its own cache entry. The original's digest and crop are
    on `UserRead`.
    """

    sha256: Annotated[str, Field(examples=["e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"])]
