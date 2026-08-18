import uuid as uuid_pkg
from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..core.schemas import NOTES_MAX_LENGTH, PublicUUIDSchema, RejectsExplicitNulls


class ServiceKind(StrEnum):
    """What kind of servicing a schedule or record is about.

    A closed vocabulary rather than free text, for the same reason `GearType` is one:
    the UI groups and filters by it, and "VIP"/"vis"/"visual inspection" spelled three
    ways in one diver's list defeats that. `label` is the free-text companion that makes
    `OTHER` usable more than once on an item without inventing a new member each time.

    Declared in the order a diver is likely to reach for them rather than
    alphabetically, so the frontend's picker can take its order straight from the enum.
    This is the single source of truth for the vocabulary - deliberately *not* mirrored
    by a DB `CHECK` constraint (see DECISIONS.md).
    """

    SERVICE = "service"
    VISUAL_INSPECTION = "visual_inspection"
    HYDROSTATIC_TEST = "hydrostatic_test"
    BATTERY = "battery"
    OXYGEN_CLEAN = "oxygen_clean"
    OTHER = "other"


class ServiceStatus(StrEnum):
    """How urgent a schedule is right now.

    Deliberately **not** a stored column and **not** a field on any read schema: it's a
    function of today's date (and the item's live dive count), so a cached response
    carrying it would be wrong the next morning. The API returns only clock-stable
    facts - `next_due_on`, `next_due_at_dive_count`, `last_service_on` - and this is
    derived from them by whoever is displaying them: the browser
    (`lib/gear-service.ts`) for the UI, and `services.gear_service.service_status` for
    the digest job, which has no browser. See DECISIONS.md.
    """

    OK = "ok"
    DUE_SOON = "due_soon"
    OVERDUE = "overdue"


LABEL_MAX_LENGTH = 120


# -------------------- schedule --------------------
class GearServiceScheduleBase(BaseModel):
    kind: Annotated[ServiceKind, Field(examples=[ServiceKind.SERVICE], description="What kind of servicing this is")]
    starts_on: Annotated[
        date,
        Field(
            examples=["2025-04-01"],
            description="Baseline the first due date is measured from, until a service is logged",
        ),
    ]
    label: Annotated[
        str | None,
        Field(default=None, max_length=LABEL_MAX_LENGTH, examples=["First stage"], description="Optional free-text"),
    ]
    interval_months: Annotated[
        int | None, Field(default=None, gt=0, examples=[12], description="Service every N months")
    ]
    interval_dives: Annotated[
        int | None, Field(default=None, gt=0, examples=[100], description="Service every N dives")
    ]

    @model_validator(mode="after")
    def require_an_interval(self) -> Self:
        """A schedule with neither interval set could never become due, so it would sit
        in the table producing nothing. Mirrors `ck_gear_service_schedule_has_an_interval`
        and the frontend's Zod refine.
        """
        if self.interval_months is None and self.interval_dives is None:
            raise ValueError("A service schedule needs an interval in months, in dives, or both")
        return self


class GearServiceScheduleInfo(PublicUUIDSchema):
    """Summary of a schedule as embedded in `GearItemRead`, so the gear list and detail
    pages can show service status without a second request per item.

    Carries only clock-stable fields - see `ServiceStatus` for why the status itself
    isn't one of them.
    """

    kind: ServiceKind
    label: str | None = None
    interval_months: int | None = None
    interval_dives: int | None = None
    last_service_on: date | None = None
    next_due_on: date | None = None
    next_due_at_dive_count: int | None = None
    is_active: bool = True


class GearServiceScheduleRead(GearServiceScheduleBase, PublicUUIDSchema):
    """Public representation of a schedule, keyed by its opaque `uuid` rather than the
    sequential internal `id` (which is never exposed over the API).
    """

    user_uuid: uuid_pkg.UUID
    gear_item_uuid: uuid_pkg.UUID
    is_active: bool = True
    dive_count_at_start: int = 0
    last_service_on: date | None = None
    next_due_on: date | None = None
    next_due_at_dive_count: int | None = None
    created_at: datetime


class GearServiceScheduleReadInternal(GearServiceScheduleBase, PublicUUIDSchema):
    """Mirrors the actual `gear_service_schedule` columns (integer PK/FKs), for
    server-side lookups only - never returned directly over the API.
    """

    id: int
    user_id: int
    gear_item_id: int
    is_active: bool = True
    dive_count_at_start: int = 0
    last_service_on: date | None = None
    next_due_on: date | None = None
    next_due_at_dive_count: int | None = None
    notified_stage: str | None = None
    notified_for_due_on: date | None = None
    notified_for_due_at_dive_count: int | None = None
    notified_at: datetime | None = None
    created_at: datetime


class GearServiceScheduleCreate(GearServiceScheduleBase):
    """Carries `gear_item_uuid` rather than `user_uuid`: ownership is derived from the
    item the schedule hangs off, which is strictly stronger than trusting a user id in
    the body.

    `dive_count_at_start` and every derived/notify field are absent on purpose - the
    route snapshots the former from the item and `recalculate_service_schedule` owns the
    rest, so `extra="forbid"` turns an attempt to set them into a 422 rather than a
    silently-ignored key.
    """

    model_config = ConfigDict(extra="forbid")

    gear_item_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of the gear item this schedule is for")]


class GearServiceScheduleCreateInternal(GearServiceScheduleBase):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    gear_item_id: int
    dive_count_at_start: int = 0
    is_active: bool = True


class GearServiceScheduleUpdate(RejectsExplicitNulls):
    """Partial update. Interval and `starts_on` changes move the due date, so the route
    re-runs `recalculate_service_schedule` afterwards (which also re-arms the reminder).

    The at-least-one-interval invariant can't be checked here - a patch that only sets
    `interval_dives` says nothing about `interval_months` - so the route validates it
    against the merged result instead.
    """

    model_config = ConfigDict(extra="forbid")

    # The intervals stay off this list: dropping one is how a rule goes from "every 12
    # months or 100 dives" to just one of the two, and the route checks the merged result
    # still has at least one (which the DB's own `CheckConstraint` also enforces).
    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("kind", "starts_on", "is_active")

    kind: Annotated[ServiceKind | None, Field(default=None)]
    label: Annotated[str | None, Field(default=None, max_length=LABEL_MAX_LENGTH)]
    starts_on: Annotated[date | None, Field(default=None)]
    interval_months: Annotated[int | None, Field(default=None, gt=0)]
    interval_dives: Annotated[int | None, Field(default=None, gt=0)]
    is_active: Annotated[bool | None, Field(default=None, description="Set to pause/resume reminders")]


class GearServiceScheduleUpdateInternal(GearServiceScheduleUpdate):
    """Adds the fields only the server ever writes: the derived due dates and the
    digest's notify state.
    """

    last_service_on: date | None = None
    next_due_on: date | None = None
    next_due_at_dive_count: int | None = None
    notified_stage: str | None = None
    notified_for_due_on: date | None = None
    notified_for_due_at_dive_count: int | None = None
    notified_at: datetime | None = None
    updated_at: datetime


class GearServiceScheduleDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_deleted: bool
    deleted_at: datetime


# -------------------- record --------------------
class GearServiceRecordBase(BaseModel):
    kind: Annotated[ServiceKind, Field(examples=[ServiceKind.SERVICE])]
    serviced_on: Annotated[date, Field(examples=["2026-03-14"], description="When the work was done")]
    label: Annotated[str | None, Field(default=None, max_length=LABEL_MAX_LENGTH)]
    performed_by: Annotated[str | None, Field(default=None, max_length=255, examples=["Blue Ocean Dive Resort"])]
    notes: Annotated[str, Field(default="", max_length=NOTES_MAX_LENGTH)]


class GearServiceRecordRead(GearServiceRecordBase, PublicUUIDSchema):
    user_uuid: uuid_pkg.UUID
    gear_item_uuid: uuid_pkg.UUID
    # NULL in three cases, and the third is much the most common: the record was never
    # attached to a schedule; the schedule was hard-deleted (the FK is `ON DELETE SET
    # NULL`); or the schedule was *soft*-deleted, in which case the FK still points at it
    # and `_schedule_uuids_by_id` declines to resolve it. Only the middle one empties the
    # column - the other two are a live `gear_service_schedule_id` reading back as null,
    # which is why nothing should infer the FK's state from this field.
    gear_service_schedule_uuid: uuid_pkg.UUID | None = None
    dive_count_at_service: int = 0
    created_at: datetime


class GearServiceRecordReadInternal(GearServiceRecordBase, PublicUUIDSchema):
    id: int
    user_id: int
    gear_item_id: int
    gear_service_schedule_id: int | None = None
    dive_count_at_service: int = 0
    created_at: datetime


class GearServiceRecordCreate(GearServiceRecordBase):
    """`gear_service_schedule_uuid` is optional: if it's omitted and the item has
    exactly one non-deleted schedule matching `(kind, label)`, the route links it
    automatically, so "log the annual service" from the item page satisfies the rule
    without the diver picking anything.

    `dive_count_at_service` is absent - the route snapshots it from the gear item.
    """

    model_config = ConfigDict(extra="forbid")

    gear_item_uuid: Annotated[uuid_pkg.UUID, Field(description="Public id of the gear item that was serviced")]
    gear_service_schedule_uuid: Annotated[
        uuid_pkg.UUID | None, Field(default=None, description="Schedule this satisfies; inferred when omitted")
    ]


class GearServiceRecordCreateInternal(GearServiceRecordBase):
    model_config = ConfigDict(extra="forbid")

    user_id: int
    gear_item_id: int
    gear_service_schedule_id: int | None = None
    dive_count_at_service: int = 0


class GearServiceRecordUpdate(RejectsExplicitNulls):
    """Partial update. Changing `serviced_on` moves the schedule's due date, so the
    route re-runs `recalculate_service_schedule` for the affected schedule.

    `dive_count_at_service` isn't editable: it's a snapshot of a moment that has already
    passed, and letting it be rewritten would silently shift a dive-based due threshold.
    """

    model_config = ConfigDict(extra="forbid")

    NON_NULLABLE_FIELDS: ClassVar[tuple[str, ...]] = ("kind", "serviced_on", "notes")

    kind: Annotated[ServiceKind | None, Field(default=None)]
    serviced_on: Annotated[date | None, Field(default=None)]
    label: Annotated[str | None, Field(default=None, max_length=LABEL_MAX_LENGTH)]
    performed_by: Annotated[str | None, Field(default=None, max_length=255)]
    notes: Annotated[str | None, Field(default=None, max_length=NOTES_MAX_LENGTH)]


class GearServiceRecordUpdateInternal(GearServiceRecordUpdate):
    updated_at: datetime


class GearServiceRecordDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    is_deleted: bool
    deleted_at: datetime


# -------------------- dashboard --------------------
class GearServiceDueItem(BaseModel):
    """One row of `GET /gear-service-due`: a schedule plus just enough of its gear item
    to render a dashboard line without a second lookup.

    Carries the item's brand and name but not its `type` - the line reads "Scubapro
    MK25 EVO - service overdue", where the *service kind* is the useful category and the
    gear type would be redundant. Leaving it out also keeps this module free of any
    import from `gear_item`, which is what lets that module import `GearServiceScheduleInfo`
    from here without a cycle.
    """

    schedule_uuid: uuid_pkg.UUID
    kind: ServiceKind
    label: str | None = None
    last_service_on: date | None = None
    next_due_on: date | None = None
    next_due_at_dive_count: int | None = None
    gear_item_uuid: uuid_pkg.UUID
    gear_item_name: str
    gear_item_brand: str | None = None
    gear_item_dive_count: int = 0


class GearServiceDueResponse(BaseModel):
    """Every active schedule the user owns, unfiltered by any date horizon.

    Deliberately takes no `within_days` parameter: a server-side horizon would bake
    "today" into a cached response and quietly go wrong at midnight. With no date input
    this is a pure function of stored rows, so it can be cached safely and the client
    buckets it into due-soon/overdue itself.

    `truncated` says the `DUE_OVERVIEW_LIMIT` row cap was hit. Without it the dashboard
    card silently under-reported: a diver past the cap would see a list that looked
    complete while some overdue kit simply wasn't in it. For a safety-adjacent card that
    is the wrong direction to fail in, so the client says the list is partial instead.
    """

    data: list[GearServiceDueItem]
    truncated: bool = False
