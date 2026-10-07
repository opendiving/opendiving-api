import uuid as uuid_pkg
from datetime import date, datetime
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.schemas import (
    NOTES_MAX_LENGTH,
    PublicUUIDSchema,
    RejectsExplicitNulls,
    validate_date_range,
)
from ..core.utils.pagination import DEFAULT_MAX_ITEMS_PER_PAGE
from .location import LocationInput, LocationRead
from .person import PeopleRead, PeopleUpdate, PeopleWrite

MAX_TRIP_PARTS = 20


class TripPartInput(BaseModel):
    """One stretch of a trip on the way in: an optional date range, an optional place and
    an optional accommodation.

    Every member is optional and each absence means something. Dates and no location is a
    transit day or a week nobody geocoded; a location and no dates is a stop whose timing
    the diver has not filled in. A part carries no name of its own - `location.name` is
    the place's name, and a part without one is identified by its dates or its ordinal.
    """

    model_config = ConfigDict(extra="forbid")

    start_date: Annotated[date | None, Field(default=None, examples=["2024-06-01"])]
    end_date: Annotated[date | None, Field(default=None, examples=["2024-06-08"])]
    location: LocationInput | None = None
    accommodation_uuid: Annotated[
        uuid_pkg.UUID | None,
        Field(default=None, description="Public id of the contact the diver stayed at during this part"),
    ]

    @model_validator(mode="after")
    def check_date_range(self) -> TripPartInput:
        validate_date_range(self.start_date, self.end_date)
        return self


class TripPartRead(BaseModel):
    """Public shape of a trip part. No id: parts are replaced wholesale with the trip, so
    there is nothing to address one by."""

    start_date: date | None = None
    end_date: date | None = None
    location: LocationRead | None = None
    accommodation_uuid: Annotated[
        uuid_pkg.UUID | None,
        Field(default=None, description="Public id of the contact the diver stayed at during this part"),
    ]


class TripPartWithCandidatesRead(TripPartRead):
    """A part as a trip read carries it: with the candidates its card on the trip page shows.

    A subclass rather than a field on `TripPartRead`, which the export reads too: the count is
    required here, so no trip read can leave it out, and nothing but a trip read carries one."""

    candidate_count: Annotated[
        int,
        Field(
            examples=[4],
            description="The owner's live dives on no trip whose own local day this part covers - where parts "
            "overlap, only those the trip page places in this one",
        ),
    ]


class TripBase(BaseModel):
    name: Annotated[str, Field(min_length=1, max_length=255, examples=["Red Sea Liveaboard 2024"])]
    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH)]


class TripRead(TripBase, PublicUUIDSchema):
    """Public representation of a trip, keyed by its opaque `uuid` rather than the
    sequential internal `id` (which is never exposed over the API).

    No span of its own: a trip's dates are its parts'.
    """

    parts: Annotated[list[TripPartWithCandidatesRead], Field(default_factory=list)]
    # Who came on the trip, which is not a walk of its dives: a companion who never dived is
    # here and on none of them.
    people: PeopleRead
    dive_count: Annotated[int, Field(examples=[12], description="The owner's live dives assigned to this trip")]
    dive_site_count: Annotated[
        int, Field(examples=[5], description="Distinct dive sites named by those dives; a site on several counts once")
    ]
    species_count: Annotated[
        int,
        Field(
            examples=[31],
            description="Distinct species recorded on those dives, counted as `species_seen` counts them",
        ),
    ]
    max_depth: Annotated[
        float | None,
        Field(
            examples=[32.4],
            description="The greatest `max_depth` among those dives, in metres; null when none recorded one",
        ),
    ]
    candidate_count: Annotated[
        int,
        Field(
            examples=[9],
            description="The owner's live dives on no trip whose own local day one of its parts covers, each "
            "counted once",
        ),
    ]
    contact_uuids: Annotated[
        list[uuid_pkg.UUID],
        Field(description="The contacts the trip's own live dives name, each once, the newest dive's first"),
    ]
    user_uuid: uuid_pkg.UUID
    created_at: datetime


class TripReadInternal(TripBase, PublicUUIDSchema):
    """Mirrors the actual `trip` table columns (integer PK/FK), for server-side lookups
    only - never returned directly over the API (use `TripRead` for the public shape,
    which additionally resolves `user_id` to the owning user's `uuid` and embeds the parts).
    """

    id: int
    user_id: int
    created_at: datetime


class TripCreate(TripBase):
    model_config = ConfigDict(extra="forbid")

    parts: Annotated[
        list[TripPartInput],
        Field(
            default_factory=list,
            max_length=MAX_TRIP_PARTS,
            description="The stretches this trip ran, in the order the diver arranged them",
        ),
    ]
    people: PeopleWrite


class TripCreateInternal(TripBase):
    model_config = ConfigDict(extra="forbid")
    user_id: int


class TripUpdate(RejectsExplicitNulls):
    model_config = ConfigDict(extra="forbid")

    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("name", "notes")

    name: Annotated[str | None, Field(min_length=1, max_length=255, default=None)]
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]


class TripUpdateRequest(TripUpdate):
    """Request body for updating a trip, including replacing its parts and its people.

    Omit `parts` and the existing ones are left untouched; provide it - even as an empty
    list - and they are replaced wholesale with what was sent. `people` works the same way.

    Separate from `TripUpdate` rather than a field on it because `TripUpdate` is CRUDAdmin's
    Trip form schema (and the shape `test_update_explicit_nulls.py` sweeps against the
    `trip` table's columns), and `parts` is rows in another table rather than a trip column.
    """

    parts: Annotated[
        list[TripPartInput] | None,
        Field(
            default=None,
            max_length=MAX_TRIP_PARTS,
            description="The stretches this trip ran, in order. Omit to leave them unchanged.",
        ),
    ]
    people: PeopleUpdate


class TripUpdateInternal(TripUpdate):
    updated_at: datetime


class TripLookupItem(PublicUUIDSchema):
    """One row of `GET /trips/lookup`: the name, which usually carries the trip's year, and the
    people a dive form fills from the trip picked, as `TripRead` carries them."""

    name: str
    people: PeopleRead


class TripPartDates(BaseModel):
    """A part named by its dates, exactly as a trip read carries them. Parts have no id, and
    are renumbered by every write that sends them, so the dates the diver saw on the part's
    card are what names it."""

    model_config = ConfigDict(extra="forbid")

    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def check_a_date_is_named(self) -> TripPartDates:
        if self.start_date is None and self.end_date is None:
            raise ValueError("A part is named by its dates, and a part with none has no candidates.")
        return self


class TripDiveAddRequest(BaseModel):
    """Which of a trip's candidates to add to it: the dives named, the ones a part takes, or -
    naming neither - every one."""

    model_config = ConfigDict(extra="forbid")

    dive_uuids: Annotated[
        list[uuid_pkg.UUID] | None,
        Field(default=None, min_length=1, max_length=DEFAULT_MAX_ITEMS_PER_PAGE, description="These dives"),
    ]
    part: Annotated[TripPartDates | None, Field(default=None, description="The candidates this part takes")]

    @model_validator(mode="after")
    def check_one_scope(self) -> TripDiveAddRequest:
        # An explicit null would otherwise read as naming nothing, which adds every candidate.
        for name in ("dive_uuids", "part"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} may be omitted but not null.")
        if self.dive_uuids is not None and self.part is not None:
            raise ValueError("Name the dives or the part, not both.")
        return self


class TripDiveAddResult(BaseModel):
    added: Annotated[
        int,
        Field(
            examples=[4],
            description="How many dives were put on the trip; fewer than named when some stopped being candidates",
        ),
    ]
