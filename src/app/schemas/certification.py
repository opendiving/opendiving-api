import uuid as uuid_pkg
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.schemas import NOTES_MAX_LENGTH, PublicUUIDSchema, RejectsExplicitNulls, StoredVocabulary


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
    DECISIONS.md), which is why widening it needs no migration.

    **Value for value, and in order, the DiveJSON vocabulary** (spec §6.16, shared with
    §6.17's courses). That is a stronger statement than it is for `GearType`, because
    `agency` is a REQUIRED member of a closed set and the format freezes those at 1.0 - so
    this list cannot grow again without a major version, and the five that arrived with
    the importer (`andi`, `snsi`, `acuc`, `pss`, `ida`) are the last additions there will
    be. The alternative was laundering five real agencies through `other`/`agency_other`
    on the way in, which would have made a round trip lossy on a member the format
    guarantees.
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
    ANDI = "andi"
    SNSI = "snsi"
    ACUC = "acuc"
    PSS = "pss"
    IDA = "ida"
    OTHER = "other"


AGENCY_OTHER_REQUIRED_MESSAGE = "agency_other is required when agency is 'other'"
AGENCY_OTHER_NOT_ALLOWED_MESSAGE = "agency_other may only be set when agency is 'other'"


def validate_agency_pairing(agency: CertificationAgency, agency_other: str | None) -> None:
    """The one place the `agency`/`agency_other` pairing is decided.

    Public because four callers need the same rule and must not spell it four ways:
    `CertificationBase` and `CourseBase` check it on a whole-object write, and
    `patch_certification`/`patch_course` re-check it on a PATCH's merged stored+incoming
    values, which is the case neither schema can see. Only the reporting differs - a
    `ValueError` here is a per-field 422 from the schema, and the flat `{"detail": ...}`
    from a route.

    Rejecting `agency_other` alongside a *named* agency (rather than quietly ignoring it)
    keeps the stored row unambiguous: a row with `agency="padi"` can never also carry a
    stray agency name some future read path might decide to display.
    """
    if agency == CertificationAgency.OTHER:
        if not (agency_other or "").strip():
            raise ValueError(AGENCY_OTHER_REQUIRED_MESSAGE)
    elif agency_other is not None:
        raise ValueError(AGENCY_OTHER_NOT_ALLOWED_MESSAGE)


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

    # The enum stays here, unlike every other stored vocabulary on a read shape (see
    # *"A stored vocabulary is read back as a string"* in DECISIONS.md). `side` is
    # structural rather than descriptive: it selects which of two slots a file occupies,
    # and the export uses it as a dict key, a filename stem and the blob lookup's
    # argument. It is also the only one no client ever supplies - the server writes it
    # from a path parameter FastAPI has already validated against this enum.
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
        validate_agency_pairing(self.agency, self.agency_other)
        return self


class CertificationRead(CertificationBase, PublicUUIDSchema):
    """Public representation of a certification, keyed by its opaque `uuid` rather than
    the sequential internal `id` (which is never exposed over the API).
    """

    # Overrides `CertificationBase.agency`, which stays `CertificationAgency` for the
    # writes that base validates. See *"A stored vocabulary is read back as a string"* in DECISIONS.md.
    agency: StoredVocabulary  # type: ignore[assignment]  # widening a write base's field; see `StoredVocabulary`

    user_uuid: uuid_pkg.UUID
    # The training course this card came out of, if the diver recorded one. Filled in by
    # all three producers - both cached readers resolve it in a batched lookup, and
    # `write_certification` passes the value straight from the request.
    course_uuid: Annotated[
        uuid_pkg.UUID | None,
        Field(default=None, description="Public id of the training course this certification came from"),
    ]
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
    public shape, which additionally resolves `user_id`/`course_id` to the owning user's
    and the course's `uuid`).
    """

    agency: StoredVocabulary  # type: ignore[assignment]  # widening a write base's field; see `StoredVocabulary`

    id: int
    user_id: int
    course_id: int | None = None
    created_at: datetime


class CertificationCreate(CertificationBase):
    """Request body for creating a certification.

    `course_uuid` sits here rather than on `CertificationBase`, and that placement is
    load-bearing: `CertificationCreateInternal` inherits the base and is CRUDAdmin's
    create form for `Certification`, so a non-column `course_uuid` on the base would land
    in the admin form as a field the panel could not resolve - the trap
    `TripUpdateRequest`'s docstring records.
    """

    model_config = ConfigDict(extra="forbid")

    user_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of the user this certification belongs to")]
    course_uuid: Annotated[
        uuid_pkg.UUID | None,
        Field(default=None, description="Public id of the training course this certification came from"),
    ]


class CertificationCreateInternal(CertificationBase):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    # The column, not the uuid - so the admin create form gets a field it can actually
    # fill, matching how `DiveCreateInternal` exposes `trip_id`.
    course_id: int | None = None


class CertificationUpdate(RejectsExplicitNulls):
    """Partial update.

    Unlike `CertificationBase` this does *not* validate `agency`/`agency_other` against
    each other - a PATCH may carry either field alone, so the pairing can only be
    checked against the merged result. `patch_certification` does that once it has the
    stored row in hand.
    """

    model_config = ConfigDict(extra="forbid")

    # `agency_other` is nullable and stays off this list - clearing it is half of moving
    # a certification off `agency="other"`, and `patch_certification` validates the pair
    # against the merged result.
    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("agency", "name", "notes")

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


class CertificationUpdateRequest(CertificationUpdate):
    """Request body for updating a certification, including re-pointing it at a course.

    Separate from `CertificationUpdate` rather than a field on it because
    `CertificationUpdate` is CRUDAdmin's Certification form schema (and the shape
    `test_update_explicit_nulls.py` sweeps against the `certification` table's columns),
    and `course_uuid` is neither a column nor something the admin form could resolve - the
    same split, and the same reason, as `TripUpdateRequest`.

    Omit `course_uuid` and the existing link is left alone; send `null` and it is cleared.
    """

    course_uuid: Annotated[
        uuid_pkg.UUID | None,
        Field(default=None, description="Public id of the training course this certification came from"),
    ]


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
    agency: StoredVocabulary
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
