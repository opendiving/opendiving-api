"""A proposed photo for a species: the proposer's view of it, and the operator's."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from ..core.schemas import PublicUUIDSchema, StoredVocabulary

PHOTO_PROPOSAL_TEXT_MAX_LENGTH = 500

_ProposalText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=PHOTO_PROPOSAL_TEXT_MAX_LENGTH)]


class SpeciesPhotoProposalStatus(StrEnum):
    """`closed` is the operator choosing the photo another way, without ruling on the file."""

    WAITING = "waiting"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    WITHDRAWN = "withdrawn"
    CLOSED = "closed"


class SpeciesPhotoProposalCreate(BaseModel):
    """Request body for `POST /species/{uuid}/photo-proposals`."""

    model_config = ConfigDict(extra="forbid")

    file: Annotated[
        str,
        Field(
            min_length=1,
            max_length=1024,
            examples=["Pterois miles 1.jpg", "https://commons.wikimedia.org/wiki/File:Pterois_miles_1.jpg"],
            description="The Wikimedia Commons file proposed as the species' photo: its title, with or without "
            "File:, or its file-page URL",
        ),
    ]
    note: Annotated[_ProposalText, Field(default="", description="Why this photo is better, for whoever reviews it")]


class SpeciesPhotoProposalSpecies(PublicUUIDSchema):
    """The species a proposal is for."""

    scientific_name: str
    common_name: str | None = None


class SpeciesPhotoProposalRead(PublicUUIDSchema):
    """The proposer's own proposal and what became of it."""

    species: SpeciesPhotoProposalSpecies
    file: Annotated[str, Field(description="The Commons file title, without File:")]
    note: str
    status: Annotated[
        StoredVocabulary,
        Field(description="`waiting`, `accepted`, `declined`, `withdrawn` or `closed`"),
    ]
    reason: Annotated[str | None, Field(default=None, description="Why it was declined, when a reason was given")]
    created_at: datetime
    decided_at: Annotated[
        datetime | None, Field(default=None, description="When it was accepted, declined, withdrawn or closed")
    ]


class AdminSpeciesPhotoProposalRead(PublicUUIDSchema):
    """One waiting proposal as the operator reviews it. The file's members are a picker
    candidate's, each null where Wikimedia Commons gave no answer."""

    file: Annotated[str, Field(max_length=255)]
    note: str
    proposer_name: str
    created_at: datetime
    width: int | None = None
    height: int | None = None
    license: Annotated[str | None, Field(default=None, max_length=128)]
    author: Annotated[str | None, Field(default=None, max_length=255)]
    source_url: Annotated[str | None, Field(default=None, max_length=512)]
    preview: Annotated[str | None, Field(default=None, description="A data: URI of the file's 250 px rendition")]


class SpeciesPhotoProposalDecline(BaseModel):
    """Request body for `POST /admin/species-photo-proposal/{uuid}/decline`."""

    model_config = ConfigDict(extra="forbid")

    reason: Annotated[_ProposalText, Field(default="", description="Why, in words the proposer will read; optional")]
