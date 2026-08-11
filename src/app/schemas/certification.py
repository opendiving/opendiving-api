import uuid as uuid_pkg
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.schemas import NOTES_MAX_LENGTH, PublicUUIDSchema


class CertificationAgency(StrEnum):
    """The training agency that issued a certification.

    A closed vocabulary rather than free text so the same agency is named the same way
    across a diver's whole list (no "PADI"/"Padi"/"P.A.D.I." drift), which is what lets
    the UI badge and group by it. `OTHER` (paired with `agency_other`) is the escape
    hatch for the long tail of national and regional bodies.

    Declared roughly by how many divers hold cards from each rather than
    alphabetically, so the picker's default order is useful - the same reasoning as
    `GearType`'s deliberate ordering. This is the single source of truth for the
    vocabulary; it is deliberately *not* mirrored by a DB `CHECK` constraint (see
    DECISIONS.md).
    """

    PADI = "padi"
    SSI = "ssi"
    NAUI = "naui"
    SDI = "sdi"
    TDI = "tdi"
    CMAS = "cmas"
    RAID = "raid"
    BSAC = "bsac"
    GUE = "gue"
    IANTD = "iantd"
    PSAI = "psai"
    DAN = "dan"
    EFR = "efr"
    OTHER = "other"


class CertificationSide(StrEnum):
    """Which face of the physical card a stored file shows.

    Both matter: the front carries the diver's name and level, the back the
    certification number and issue date - and a shop checking a card usually wants one
    specific side of it.
    """

    FRONT = "front"
    BACK = "back"


class CertificationFileInfo(PublicUUIDSchema):
    """Metadata about one stored card image or PDF - **never** its bytes.

    Embedded in every `CertificationRead` so the list view knows which cards have
    images (and can size their placeholders) without a request per row. The bytes
    themselves come from `GET /certification/{uuid}/file/{side}`.
    """

    side: CertificationSide
    content_type: str
    byte_size: int
    original_filename: str
    updated_at: datetime | None = None


class CertificationBase(BaseModel):
    agency: Annotated[
        CertificationAgency,
        Field(examples=[CertificationAgency.PADI], description="Training agency that issued this certification"),
    ]
    agency_other: Annotated[
        str | None,
        Field(default=None, max_length=64, description="Agency name, required when `agency` is `other`"),
    ]
    name: Annotated[
        str,
        Field(min_length=1, max_length=255, examples=["Advanced Open Water Diver"], description="Level as printed"),
    ]
    certification_number: Annotated[str | None, Field(default=None, max_length=64, examples=["1234567"])]
    certified_on: Annotated[date | None, Field(default=None, description="Date the certification was issued")]
    expires_on: Annotated[
        date | None,
        Field(default=None, description="Expiry date, for the certifications that have one (rescue, EFR, most tech)"),
    ]
    instructor_name: Annotated[str | None, Field(default=None, max_length=255)]
    instructor_number: Annotated[str | None, Field(default=None, max_length=64)]
    training_center: Annotated[
        str | None,
        Field(default=None, max_length=255, description="Dive shop, resort or club that ran the course"),
    ]
    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH)]

    @model_validator(mode="after")
    def _check_agency_other(self) -> Self:
        """`agency_other` is meaningful only alongside `agency == OTHER`.

        Rejecting it in the other direction too (rather than quietly ignoring it) keeps
        the stored row unambiguous: a certification with `agency="padi"` can never also
        carry a stray agency name that some future read path might decide to display.
        """
        if self.agency == CertificationAgency.OTHER:
            if not (self.agency_other or "").strip():
                raise ValueError("agency_other is required when agency is 'other'")
        elif self.agency_other is not None:
            raise ValueError("agency_other may only be set when agency is 'other'")
        return self


class CertificationRead(CertificationBase, PublicUUIDSchema):
    """Public representation of a certification, keyed by its opaque `uuid` rather than
    the sequential internal `id` (which is never exposed over the API).
    """

    user_uuid: uuid_pkg.UUID
    # The card images this certification has, as metadata only - see
    # `CertificationFileInfo`. Empty for a certification entered but not yet photographed.
    files: Annotated[
        list[CertificationFileInfo],
        Field(default_factory=list, description="Stored card images/PDFs, metadata only"),
    ]
    created_at: datetime


class CertificationReadInternal(CertificationBase, PublicUUIDSchema):
    """Mirrors the actual `certification` table columns (integer PK/FK), for server-side
    lookups only - never returned directly over the API (use `CertificationRead` for the
    public shape, which additionally resolves `user_id` to the owning user's `uuid`).
    """

    id: int
    user_id: int
    created_at: datetime


class CertificationCreate(CertificationBase):
    model_config = ConfigDict(extra="forbid")

    user_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of the user this certification belongs to")]


class CertificationCreateInternal(CertificationBase):
    model_config = ConfigDict(extra="forbid")

    user_id: int


class CertificationUpdate(BaseModel):
    """Partial update.

    Unlike `CertificationBase` this does *not* validate `agency`/`agency_other` against
    each other - a PATCH may carry either field alone, so the pairing can only be
    checked against the merged result. `patch_certification` does that once it has the
    stored row in hand.
    """

    model_config = ConfigDict(extra="forbid")

    agency: Annotated[CertificationAgency | None, Field(default=None)]
    agency_other: Annotated[str | None, Field(default=None, max_length=64)]
    name: Annotated[str | None, Field(default=None, min_length=1, max_length=255)]
    certification_number: Annotated[str | None, Field(default=None, max_length=64)]
    certified_on: Annotated[date | None, Field(default=None)]
    expires_on: Annotated[date | None, Field(default=None)]
    instructor_name: Annotated[str | None, Field(default=None, max_length=255)]
    instructor_number: Annotated[str | None, Field(default=None, max_length=64)]
    training_center: Annotated[str | None, Field(default=None, max_length=255)]
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]


class CertificationUpdateInternal(CertificationUpdate):
    updated_at: datetime


class CertificationDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_deleted: bool
    deleted_at: datetime


# -------------------- dashboard --------------------
class CertificationExpiringItem(BaseModel):
    """One row of `GET /certifications-expiring`: just enough of a certification to
    render a dashboard line and link to the list page.

    The gear twin of this is `GearServiceDueItem`. It carries no card-file metadata:
    the renewal card shows a name, an agency and a date, and embedding `files` here
    would mean a second query per row for something nothing on that card renders.
    """

    uuid: uuid_pkg.UUID
    agency: CertificationAgency
    agency_other: str | None = None
    name: str
    expires_on: date


class CertificationExpiringResponse(BaseModel):
    """Every certification the user owns that has an expiry date at all.

    Deliberately takes no `within_days` parameter, for the same reason as
    `GearServiceDueResponse`: a server-side horizon would bake "today" into a cached
    response and quietly go wrong at midnight. With no date input this is a pure
    function of stored rows, so it can be cached safely and the client buckets it into
    expiring-soon/expired itself.

    Note the *boundary* differs from gear on the client side, and that is deliberate:
    a c-card is valid through its printed date, whereas a service interval that has
    arrived has arrived. Neither belongs here - both are clock-dependent.

    `truncated` says the row cap was hit, so the client can say the list is partial
    instead of implying these are all of them. `GearServiceDueResponse` carries the
    same flag; for a safety-adjacent card, silently under-reporting is the wrong
    direction to fail in.
    """

    data: list[CertificationExpiringItem]
    truncated: bool = False
