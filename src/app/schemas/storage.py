from typing import Annotated

from pydantic import BaseModel, Field


class StorageUsageRead(BaseModel):
    """What the caller's uploads occupy on this instance, against its storage limit.

    Every figure is bytes as stored. A dive-computer file stored compressed counts its
    compressed size, less than the `byte_size` its recording lists; one stored before
    compression began counts that full size. A card image or a picture's files count their
    own sizes. Species photographs belong to the shared catalogue and are not here.
    """

    used_bytes: Annotated[int, Field(description="The sum of the three parts below", examples=[137_512_960])]
    limit_bytes: Annotated[
        int | None,
        Field(description="The most this account may store, or null when this instance sets no limit"),
    ]
    dive_files_bytes: Annotated[int, Field(description="Dive-computer files, as stored", examples=[74_842_112])]
    certification_files_bytes: Annotated[int, Field(description="Certification card images", examples=[52_428_800])]
    pictures_bytes: Annotated[
        int, Field(description="The profile picture and the portrait, originals and renditions", examples=[10_242_048])
    ]
