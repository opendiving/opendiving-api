import uuid as uuid_pkg
from datetime import date, datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from ..core.schemas import StoredVocabulary

# `checkin_link.total_dives` is a Postgres `integer`, and so is the `user_dive_stats` column the
# logged count comes from.
POSTGRES_INTEGER_MAX = 2**31 - 1


class CheckinLinkCreate(BaseModel):
    """`POST /user/checkin-link`'s body: the diving figures as the check-in page shows them,
    the diver's correction or the log's, which the link then shows for its whole life.

    Each is required and nullable. A null prints nothing: a diver with no dive logged has no
    last dive, and one who cleared a figure asked for it off the sheet. The count and depth
    take their columns' bounds and no more; the date is bounded by its format alone, because
    the log holds future-dated dives and any other bound would refuse an uncorrected page.
    """

    model_config = ConfigDict(extra="forbid")

    total_dives: Annotated[int | None, Field(ge=0, le=POSTGRES_INTEGER_MAX, description="Dives logged")]
    max_depth: Annotated[float | None, Field(ge=0, allow_inf_nan=False, description="Max depth, in metres")]
    last_dive_on: Annotated[date | None, Field(description="The last dive's own calendar day")]


class CheckinLinkMinted(BaseModel):
    """The one response that carries the token. Only its hash is kept, so it cannot be shown
    again."""

    token: str
    expires_at: datetime


class CheckinLinkRead(BaseModel):
    """The diver's live link, without its token."""

    expires_at: datetime


class CheckinLinkRevokedResponse(BaseModel):
    message: str = "Check-in link revoked"


class CheckinFigures(BaseModel):
    """The figures a link was minted with, as stored. Unbounded, being a read."""

    total_dives: int | None
    max_depth: float | None
    last_dive_on: date | None


class CheckinDiver(BaseModel):
    """What the check-in page prints about the diver, named as `UserRead` names it, and the
    `units` it prints the depth in. `portrait_sha256` is null when there is no portrait, and
    the avatar is never here."""

    name: str
    portrait_sha256: str | None = None
    units: StoredVocabulary
    date_of_birth: date | None = None
    phone: str | None = None
    insurance_provider: str | None = None
    insurance_policy_number: str | None = None
    insurance_expires_on: date | None = None
    emergency_contact_name: str | None = None
    emergency_contact_phone: str | None = None
    emergency_contact_relationship: str | None = None


class CheckinCertification(BaseModel):
    """One card as the check-in page prints it, named as `CertificationRead` names it.

    `contact_name` is the dive centre's name, which the page prints where `CertificationRead`
    carries only `contact_uuid`. `front_content_type` is null when the card has no front; the
    page draws an image front from `GET /checkin/{token}/certification/{uuid}/front` and a PDF
    one as a label. Nothing about the back.
    """

    uuid: uuid_pkg.UUID
    agency: StoredVocabulary
    agency_other: str | None = None
    name: str
    certification_number: str | None = None
    certified_on: date | None = None
    expires_on: date | None = None
    instructor_name: str | None = None
    contact_name: str | None = None
    front_content_type: str | None = None


class CheckinSummary(BaseModel):
    """`GET /checkin/{token}`: everything the check-in page prints, and when the link stops
    working. The certifications come in `GET /certifications`' order."""

    expires_at: datetime
    diver: CheckinDiver
    diving: CheckinFigures
    certifications: list[CheckinCertification]
