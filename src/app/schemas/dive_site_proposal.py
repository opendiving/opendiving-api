import uuid as uuid_pkg
from datetime import datetime
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from ..core.schemas import PublicUUIDSchema, StoredName, StoredVocabulary, Trimmed
from .dive_site import (
    DiveSiteCommonsMatchRead,
    DiveSiteMembers,
    DiveSiteMembersChange,
    DiveSiteMembersRead,
    DiveSiteProposalKind,
)

PROPOSAL_TEXT_MAX_LENGTH = 2000

# What the proposer affirms, the repository's own attestation less its last clause: there the
# record and the attestation were public, and here the attestation is private and goes with the
# account. A client shows these words and sends them back, so what is stored is what was ticked.
ATTESTATIONS = {
    DiveSiteProposalKind.NEW_SITE: (
        "I am adding this dive site from my own diving. It is not copied from another database, app or "
        "publication. I dedicate it to the public domain under CC0 1.0."
    ),
    DiveSiteProposalKind.CHANGE: (
        "I am proposing this change from my own diving. It is not copied from another database, app or "
        "publication. I dedicate it to the public domain under CC0 1.0."
    ),
}

_ProposalText = Annotated[str, StringConstraints(strip_whitespace=True, max_length=PROPOSAL_TEXT_MAX_LENGTH)]


class DiveSiteProposalCreate(BaseModel):
    """Request body for `POST /dive-site/{uuid}/proposals`."""

    model_config = ConfigDict(extra="forbid")

    attestation: Annotated[
        str,
        Field(
            description="The words the diver ticked, exactly. For the diver's own site: "
            f"{ATTESTATIONS[DiveSiteProposalKind.NEW_SITE]!r} For a Commons record: "
            f"{ATTESTATIONS[DiveSiteProposalKind.CHANGE]!r}"
        ),
    ]
    message: Annotated[_ProposalText, Field(default="", description="An optional word for the reviewer")]
    members: Annotated[
        DiveSiteMembersChange | None,
        Field(
            default=None,
            description="For a Commons record, the shared members the diver wants different; "
            "not sent for their own site, which is proposed as it stands",
        ),
    ]


class DiveSiteProposalSite(PublicUUIDSchema):
    """The site a proposal is on: once accepted, the Commons record it became or joined."""

    name: StoredName


class DiveSiteProposalRead(PublicUUIDSchema):
    """A diver's own proposal: what they read of it, the reviewer's reason included."""

    kind: Annotated[StoredVocabulary, Field(description="`new_site` or `change`")]
    status: Annotated[StoredVocabulary, Field(description="`waiting`, `accepted`, `declined` or `withdrawn`")]
    reason: Annotated[str | None, Field(default=None, description="Why it was declined")]
    site: DiveSiteProposalSite
    created_at: datetime
    decided_at: datetime | None = None


class DiveSiteProposer(BaseModel):
    """Who proposed, as far as a reviewer needs to weigh it: a name and a track record."""

    name: str
    accepted_count: int
    declined_count: int


class AdminDiveSiteProposalRead(PublicUUIDSchema):
    """One row of the queue. Of the site it carries only what a Commons record would."""

    kind: Annotated[StoredVocabulary, Field(description="`new_site` or `change`")]
    message: str
    created_at: datetime
    proposer: DiveSiteProposer
    site_uuid: uuid_pkg.UUID
    current: Annotated[DiveSiteMembersRead, Field(description="The site's shared members as they stand now")]
    proposed: Annotated[
        DiveSiteMembersRead,
        Field(
            description="The shared members as proposed: for a new site as it stood at its last edit "
            "while the proposal waits, for a change the record with the change applied"
        ),
    ]
    changed: Annotated[list[str], Field(description="The members the proposer named")]
    matches: Annotated[
        list[DiveSiteCommonsMatchRead],
        Field(description="The Commons records the proposed site may be, strongest first"),
    ]


class DiveSiteProposalAccept(BaseModel):
    """Request body for `POST /admin/dive-site-proposal/{uuid}/accept`."""

    model_config = ConfigDict(extra="forbid")

    members: Annotated[
        DiveSiteMembers | None,
        Field(
            default=None,
            description="The record's final shared members, written exactly as sent with the pin rounded to six "
            "decimals. Required unless `into` is sent, and with `into` the changes to that record, if any",
        ),
    ]
    into: Annotated[
        uuid_pkg.UUID | None,
        Field(default=None, description="For a new site that duplicates one, the Commons record to fold it into"),
    ]

    @model_validator(mode="after")
    def _says_what_to_write(self) -> Self:
        if self.members is None and self.into is None:
            raise ValueError("accepting a proposal sends the record's members, or `into` for a record that has them")
        return self


class DiveSiteProposalDecline(BaseModel):
    """Request body for `POST /admin/dive-site-proposal/{uuid}/decline`."""

    model_config = ConfigDict(extra="forbid")

    reason: Annotated[
        str,
        Trimmed,
        Field(max_length=PROPOSAL_TEXT_MAX_LENGTH, description="Why, in words the proposer will read"),
    ]
