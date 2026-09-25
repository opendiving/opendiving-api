"""Unit tests for gear service tracking (`models/gear_service_*.py`,
`schemas/gear_service.py`, `services/gear_service.py`).

Same convention as `test_gear.py`: these cover the pieces that are pure logic or pure
SQL construction and so need no database - the month arithmetic, the due-date
derivation, the status truth table, the fire-once notification rule, and the shape of
the recalculation/cascade statements. Endpoint behaviour on top of a live
Postgres/Redis is exercised by hand (see DECISIONS.md), not here.
"""

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from uuid6 import uuid7

from src.app.api.v1 import gear_service as gear_service_module
from src.app.core.exceptions.http_exceptions import NotFoundException
from src.app.crud.crud_gear_service_schedules import get_due_overview_for_user
from src.app.schemas.gear_item import GearItemCreate, GearItemInfo, GearItemRead
from src.app.schemas.gear_service import (
    GearServiceDueItem,
    GearServiceDueResponse,
    GearServiceRecordCreate,
    GearServiceScheduleBase,
    GearServiceScheduleCreate,
    GearServiceScheduleInfo,
    GearServiceScheduleUpdate,
    ServiceKind,
    ServiceStatus,
)
from src.app.services.gear_service import (
    SERVICE_DUE_SOON_DAYS,
    SERVICE_DUE_SOON_DIVES,
    SERVICE_OVERDUE_RENAG_DAYS,
    add_months,
    dives_since,
    next_due_from,
    recalculate_service_schedule,
    service_status,
    should_notify,
)


class TestAddMonths:
    """Month arithmetic is the classic source of bugs in interval tracking - a service
    logged at the end of a long month must not silently skip into the next one.
    """

    def test_adds_whole_months_within_a_year(self) -> None:
        assert add_months(date(2026, 3, 14), 6) == date(2026, 9, 14)

    def test_crosses_the_year_boundary(self) -> None:
        assert add_months(date(2026, 8, 1), 12) == date(2027, 8, 1)
        assert add_months(date(2026, 11, 30), 3) == date(2027, 2, 28)

    def test_clamps_to_the_end_of_a_shorter_target_month(self) -> None:
        # 31 August + 6 months has no 31 February to land on.
        assert add_months(date(2025, 8, 31), 6) == date(2026, 2, 28)
        assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
        assert add_months(date(2026, 3, 31), 1) == date(2026, 4, 30)

    def test_clamps_to_29_february_in_a_leap_year(self) -> None:
        assert add_months(date(2027, 8, 31), 6) == date(2028, 2, 29)
        assert add_months(date(2028, 2, 29), 12) == date(2029, 2, 28)

    def test_handles_the_intervals_the_presets_actually_use(self) -> None:
        start = date(2026, 6, 15)
        assert add_months(start, 12) == date(2027, 6, 15)  # annual service
        assert add_months(start, 24) == date(2028, 6, 15)  # computer battery
        assert add_months(start, 60) == date(2031, 6, 15)  # cylinder hydro


class TestNextDueFrom:
    """Each interval arm is independent, and a dive threshold is absolute rather than
    relative - that's what decouples it from the item's live dive count.
    """

    def test_time_only_rule_has_no_dive_threshold(self) -> None:
        due_on, due_at = next_due_from(
            baseline_on=date(2026, 1, 1), baseline_dive_count=40, interval_months=12, interval_dives=None
        )
        assert due_on == date(2027, 1, 1)
        assert due_at is None

    def test_dive_only_rule_has_no_due_date(self) -> None:
        due_on, due_at = next_due_from(
            baseline_on=date(2026, 1, 1), baseline_dive_count=40, interval_months=None, interval_dives=100
        )
        assert due_on is None
        assert due_at == 140

    def test_both_arms_are_computed_independently(self) -> None:
        due_on, due_at = next_due_from(
            baseline_on=date(2026, 1, 1), baseline_dive_count=40, interval_months=12, interval_dives=100
        )
        assert due_on == date(2027, 1, 1)
        assert due_at == 140

    def test_the_dive_threshold_is_absolute_not_remaining(self) -> None:
        # 40 dives done, service every 100 -> due at lifetime dive 140, a number that
        # never has to be rewritten when the diver logs another dive.
        _, due_at = next_due_from(
            baseline_on=date(2026, 1, 1), baseline_dive_count=40, interval_months=None, interval_dives=100
        )
        assert due_at == 140


class TestServiceStatus:
    """The truth table for "annually or every N dives, whichever comes first"."""

    TODAY = date(2026, 8, 10)

    def _status(self, **overrides) -> ServiceStatus:
        defaults = {
            "next_due_on": None,
            "next_due_at_dive_count": None,
            "dive_count": 0,
            "today": self.TODAY,
        }
        return service_status(**{**defaults, **overrides})

    def test_a_schedule_with_no_threshold_at_all_is_ok(self) -> None:
        assert self._status() is ServiceStatus.OK

    def test_time_arm_far_in_the_future_is_ok(self) -> None:
        assert self._status(next_due_on=self.TODAY + timedelta(days=200)) is ServiceStatus.OK

    def test_time_arm_within_the_window_is_due_soon(self) -> None:
        assert self._status(next_due_on=self.TODAY + timedelta(days=10)) is ServiceStatus.DUE_SOON

    def test_time_arm_on_or_past_the_due_date_is_overdue(self) -> None:
        assert self._status(next_due_on=self.TODAY) is ServiceStatus.OVERDUE
        assert self._status(next_due_on=self.TODAY - timedelta(days=1)) is ServiceStatus.OVERDUE

    def test_time_arm_boundaries(self) -> None:
        # Exactly at the window edge counts as due soon; one day beyond it doesn't.
        assert self._status(next_due_on=self.TODAY + timedelta(days=SERVICE_DUE_SOON_DAYS)) is ServiceStatus.DUE_SOON
        assert self._status(next_due_on=self.TODAY + timedelta(days=SERVICE_DUE_SOON_DAYS + 1)) is ServiceStatus.OK
        assert (
            self._status(next_due_on=self.TODAY + timedelta(days=SERVICE_DUE_SOON_DAYS - 1)) is ServiceStatus.DUE_SOON
        )

    def test_dive_arm_far_from_the_threshold_is_ok(self) -> None:
        assert self._status(next_due_at_dive_count=140, dive_count=40) is ServiceStatus.OK

    def test_dive_arm_within_the_window_is_due_soon(self) -> None:
        assert self._status(next_due_at_dive_count=140, dive_count=135) is ServiceStatus.DUE_SOON

    def test_dive_arm_at_or_past_the_threshold_is_overdue(self) -> None:
        assert self._status(next_due_at_dive_count=140, dive_count=140) is ServiceStatus.OVERDUE
        assert self._status(next_due_at_dive_count=140, dive_count=207) is ServiceStatus.OVERDUE

    def test_dive_arm_boundaries(self) -> None:
        assert (
            self._status(next_due_at_dive_count=140, dive_count=140 - SERVICE_DUE_SOON_DIVES) is ServiceStatus.DUE_SOON
        )
        assert self._status(next_due_at_dive_count=140, dive_count=140 - SERVICE_DUE_SOON_DIVES - 1) is ServiceStatus.OK

    def test_the_more_urgent_arm_wins(self) -> None:
        # Time is fine, dives have blown past the threshold.
        assert (
            self._status(next_due_on=self.TODAY + timedelta(days=300), next_due_at_dive_count=140, dive_count=150)
            is ServiceStatus.OVERDUE
        )
        # Dives are fine, the calendar has run out.
        assert (
            self._status(next_due_on=self.TODAY - timedelta(days=1), next_due_at_dive_count=140, dive_count=10)
            is ServiceStatus.OVERDUE
        )
        # One arm due soon, the other still fine.
        assert (
            self._status(next_due_on=self.TODAY + timedelta(days=5), next_due_at_dive_count=140, dive_count=10)
            is ServiceStatus.DUE_SOON
        )

    def test_both_arms_ok_is_ok(self) -> None:
        assert (
            self._status(next_due_on=self.TODAY + timedelta(days=300), next_due_at_dive_count=140, dive_count=10)
            is ServiceStatus.OK
        )


class TestDivesSince:
    """`gear_item.dive_count` is a lifetime counter, so it can move backwards."""

    def test_counts_dives_logged_since_the_baseline(self) -> None:
        assert dives_since(dive_count=150, baseline_dive_count=40) == 110

    def test_is_zero_at_the_baseline(self) -> None:
        assert dives_since(dive_count=40, baseline_dive_count=40) == 0

    def test_clamps_at_zero_when_dives_have_been_deleted(self) -> None:
        # Deleting dives decrements the item's lifetime count on the next
        # recalculation, which would otherwise produce "-5 dives since service".
        assert dives_since(dive_count=35, baseline_dive_count=40) == 0


class TestShouldNotify:
    """The digest must fire once per threshold crossing, not once per day."""

    NOW = datetime(2026, 8, 10, 7, 0, tzinfo=UTC)
    DUE_ON = date(2026, 8, 20)

    def _should(self, **overrides) -> bool:
        defaults = {
            "status": ServiceStatus.DUE_SOON,
            "next_due_on": self.DUE_ON,
            "next_due_at_dive_count": None,
            "notified_stage": None,
            "notified_for_due_on": None,
            "notified_for_due_at_dive_count": None,
            "notified_at": None,
            "now": self.NOW,
        }
        return should_notify(**{**defaults, **overrides})

    def test_never_notifies_about_a_healthy_schedule(self) -> None:
        assert self._should(status=ServiceStatus.OK) is False

    def test_notifies_the_first_time_a_schedule_becomes_due_soon(self) -> None:
        assert self._should() is True

    def test_stays_silent_on_the_next_run_with_nothing_changed(self) -> None:
        assert self._should(notified_stage="due_soon", notified_for_due_on=self.DUE_ON, notified_at=self.NOW) is False

    def test_notifies_again_when_due_soon_becomes_overdue(self) -> None:
        assert (
            self._should(
                status=ServiceStatus.OVERDUE,
                notified_stage="due_soon",
                notified_for_due_on=self.DUE_ON,
                notified_at=self.NOW,
            )
            is True
        )

    def test_re_arms_when_the_due_date_moves(self) -> None:
        # Logging a service (or editing the interval) moves `next_due_on`, so the
        # stored tuple no longer matches and the next cycle gets its own reminder.
        assert (
            self._should(
                next_due_on=date(2027, 8, 20),
                notified_stage="due_soon",
                notified_for_due_on=self.DUE_ON,
                notified_at=self.NOW,
            )
            is True
        )

    def test_re_arms_when_recalculation_clears_the_notify_state(self) -> None:
        # `recalculate_service_schedule` nulls all four fields whenever it moves the
        # due date, which on its own is enough to re-arm.
        assert self._should(notified_stage=None, notified_for_due_on=None, notified_at=None) is True

    def test_a_dive_arm_tripping_notifies_even_though_nothing_wrote_to_the_row(self) -> None:
        # Status is recomputed against the item's live dive count every run, so a dive
        # can flip due_soon -> overdue with no write to the schedule at all.
        assert (
            self._should(
                status=ServiceStatus.OVERDUE,
                next_due_on=None,
                next_due_at_dive_count=140,
                notified_stage="due_soon",
                notified_for_due_on=None,
                notified_for_due_at_dive_count=140,
                notified_at=self.NOW,
            )
            is True
        )

    def test_a_persistently_overdue_schedule_re_nags_after_the_quiet_period(self) -> None:
        stale = self.NOW - timedelta(days=SERVICE_OVERDUE_RENAG_DAYS)
        assert (
            self._should(
                status=ServiceStatus.OVERDUE,
                notified_stage="overdue",
                notified_for_due_on=self.DUE_ON,
                notified_at=stale,
            )
            is True
        )

    def test_a_persistently_overdue_schedule_stays_quiet_before_that(self) -> None:
        recent = self.NOW - timedelta(days=SERVICE_OVERDUE_RENAG_DAYS - 1)
        assert (
            self._should(
                status=ServiceStatus.OVERDUE,
                notified_stage="overdue",
                notified_for_due_on=self.DUE_ON,
                notified_at=recent,
            )
            is False
        )

    def test_a_persistently_due_soon_schedule_never_re_nags(self) -> None:
        # The quarterly re-nag is deliberately overdue-only; nagging about something
        # that isn't due yet is what trains people to ignore the emails.
        stale = self.NOW - timedelta(days=SERVICE_OVERDUE_RENAG_DAYS * 2)
        assert self._should(notified_stage="due_soon", notified_for_due_on=self.DUE_ON, notified_at=stale) is False


class TestGearServiceSchemas:
    def test_service_kind_is_a_closed_vocabulary(self) -> None:
        assert [k.value for k in ServiceKind] == [
            "service",
            "visual_inspection",
            "hydrostatic_test",
            "battery",
            "oxygen_clean",
            "other",
        ]

    def test_a_schedule_needs_at_least_one_interval(self) -> None:
        with pytest.raises(ValidationError, match="needs an interval"):
            GearServiceScheduleBase(kind=ServiceKind.SERVICE, starts_on=date(2026, 1, 1))

    def test_either_interval_alone_is_enough(self) -> None:
        assert (
            GearServiceScheduleBase(
                kind=ServiceKind.SERVICE, starts_on=date(2026, 1, 1), interval_months=12
            ).interval_dives
            is None
        )
        assert (
            GearServiceScheduleBase(
                kind=ServiceKind.SERVICE, starts_on=date(2026, 1, 1), interval_dives=100
            ).interval_months
            is None
        )

    def test_intervals_must_be_positive(self) -> None:
        with pytest.raises(ValidationError):
            GearServiceScheduleBase(kind=ServiceKind.SERVICE, starts_on=date(2026, 1, 1), interval_months=0)

    def test_create_rejects_server_owned_fields(self) -> None:
        from uuid6 import uuid7

        # `dive_count_at_start` is snapshotted by the route and the due dates are owned
        # by `recalculate_service_schedule`; sending either must be a loud 422 rather
        # than a silently-ignored key.
        with pytest.raises(ValidationError):
            GearServiceScheduleCreate(
                gear_item_uuid=uuid7(),
                kind=ServiceKind.SERVICE,
                starts_on=date(2026, 1, 1),
                interval_months=12,
                dive_count_at_start=40,
            )
        with pytest.raises(ValidationError):
            GearServiceScheduleCreate(
                gear_item_uuid=uuid7(),
                kind=ServiceKind.SERVICE,
                starts_on=date(2026, 1, 1),
                interval_months=12,
                next_due_on=date(2027, 1, 1),
            )

    def test_record_create_rejects_the_dive_count_snapshot(self) -> None:
        from uuid6 import uuid7

        with pytest.raises(ValidationError):
            GearServiceRecordCreate(
                gear_item_uuid=uuid7(),
                kind=ServiceKind.SERVICE,
                serviced_on=date(2026, 3, 14),
                dive_count_at_service=40,
            )

    def test_a_record_needs_no_schedule(self) -> None:
        from uuid6 import uuid7

        # Logging "hydro done" on a cylinder with no reminder set up is a real thing to
        # want to do, so the link is optional and inferred when it can be.
        record = GearServiceRecordCreate(
            gear_item_uuid=uuid7(), kind=ServiceKind.HYDROSTATIC_TEST, serviced_on=date(2026, 3, 14)
        )
        assert record.gear_service_schedule_uuid is None


class TestAnUnrecognizedKindDoesNotFiveHundred:
    """A `kind` outside `ServiceKind` is readable, and takes nothing else down with it.

    The column is deliberately unconstrained (see `models/gear_item.py` and DECISIONS.md),
    so a direct SQLAlchemy write - a fixture, a script, a hand-run UPDATE - puts whatever
    it likes there. While the read schemas typed the enum, one such row failed validation
    of the *whole* response: `GET /gear-items` and `GET /gear-service-due` 500'd for every
    account owning one, so the dashboard card and the entire gear list rendered nothing.

    These are the two halves that have to hold together: reads carry the value through,
    writes still refuse it.
    """

    ODD = "inspection"

    def test_the_embedded_summary_carries_the_stored_value(self) -> None:
        summary = GearServiceScheduleInfo(uuid=uuid7(), kind=self.ODD)
        assert summary.kind == self.ODD
        assert summary.model_dump(mode="json")["kind"] == self.ODD

    def test_a_known_kind_still_serializes_to_its_own_string(self) -> None:
        """The wire format must not have moved for the values that were always valid."""
        summary = GearServiceScheduleInfo(uuid=uuid7(), kind=ServiceKind.VISUAL_INSPECTION)
        assert summary.model_dump(mode="json")["kind"] == "visual_inspection"

    def test_a_sibling_schedule_survives_the_odd_one(self) -> None:
        """The blast radius, which is the actual defect: one row used to fail the response
        that carried it *and* every other row in the same list."""
        item = GearItemRead(
            uuid=uuid7(),
            user_uuid=uuid7(),
            name="MK25 EVO",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            service=[
                GearServiceScheduleInfo(uuid=uuid7(), kind=self.ODD),
                GearServiceScheduleInfo(uuid=uuid7(), kind=ServiceKind.SERVICE),
            ],
        )
        assert [schedule.kind for schedule in item.service] == [self.ODD, "service"]

    def test_the_due_overview_row_carries_it_too(self) -> None:
        """`GET /gear-service-due` builds these, and it is the dashboard's safety-adjacent
        card - under-reporting there is the wrong direction to fail in (see
        `GearServiceDueResponse.truncated`), and 500ing is worse still."""
        row = GearServiceDueItem(schedule_uuid=uuid7(), kind=self.ODD, gear_item_uuid=uuid7(), gear_item_name="AL80")
        assert GearServiceDueResponse(data=[row]).data[0].kind == self.ODD

    def test_creating_one_is_still_a_422(self) -> None:
        """The enum stays the write boundary - nothing about this makes the vocabulary
        open. `ServiceKind` is what the route body is typed with, so the API cannot be the
        thing that produces a row like the one above."""
        with pytest.raises(ValidationError):
            GearServiceScheduleCreate(
                gear_item_uuid=uuid7(), kind=self.ODD, starts_on=date(2026, 1, 1), interval_months=12
            )
        with pytest.raises(ValidationError):
            GearServiceRecordCreate(gear_item_uuid=uuid7(), kind=self.ODD, serviced_on=date(2026, 3, 14))

    def test_patching_a_schedule_to_one_is_still_a_422(self) -> None:
        with pytest.raises(ValidationError):
            GearServiceScheduleUpdate(kind=self.ODD)

    def test_a_gear_type_outside_the_vocabulary_reads_back_the_same_way(self) -> None:
        """`GearItemInfo` is embedded in every dive and gear set, so `gear_item.type` has
        the same blast radius as `kind` - one row, every dive in the list."""
        assert GearItemInfo(uuid=uuid7(), name="Fins", type="frobnicator").type == "frobnicator"
        with pytest.raises(ValidationError):
            GearItemCreate(name="Fins", type="frobnicator")


class TestRecalculateServiceSchedule:
    """The derived fields and the notify state must move together, in one statement."""

    def _db(self, schedule, latest_record) -> MagicMock:
        db = MagicMock()
        schedule_result = MagicMock()
        schedule_result.scalar_one_or_none.return_value = schedule
        record_result = MagicMock()
        record_result.first.return_value = latest_record
        db.execute = AsyncMock(side_effect=[schedule_result, record_result, MagicMock()])
        db.commit = AsyncMock()
        return db

    def _schedule(self, **overrides) -> MagicMock:
        schedule = MagicMock()
        schedule.starts_on = date(2026, 1, 1)
        schedule.dive_count_at_start = 40
        schedule.interval_months = 12
        schedule.interval_dives = 100
        for key, value in overrides.items():
            setattr(schedule, key, value)
        return schedule

    @pytest.mark.asyncio
    async def test_uses_the_schedule_baseline_when_never_serviced(self) -> None:
        db = self._db(self._schedule(), latest_record=None)

        await recalculate_service_schedule(db, schedule_id=3)

        values = db.execute.await_args_list[2].args[0].compile().params
        assert values["last_service_on"] is None
        assert values["next_due_on"] == date(2027, 1, 1)
        assert values["next_due_at_dive_count"] == 140
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_latest_record_overrides_the_schedule_baseline(self) -> None:
        latest = MagicMock()
        latest.serviced_on = date(2026, 3, 14)
        latest.dive_count_at_service = 62
        db = self._db(self._schedule(), latest_record=latest)

        await recalculate_service_schedule(db, schedule_id=3)

        values = db.execute.await_args_list[2].args[0].compile().params
        assert values["last_service_on"] == date(2026, 3, 14)
        assert values["next_due_on"] == date(2027, 3, 14)
        assert values["next_due_at_dive_count"] == 162

    @pytest.mark.asyncio
    async def test_clears_the_notify_state_in_the_same_statement(self) -> None:
        # This is what guarantees a reminder is always armed for the due date currently
        # stored, never for a superseded one.
        db = self._db(self._schedule(), latest_record=None)

        await recalculate_service_schedule(db, schedule_id=3)

        values = db.execute.await_args_list[2].args[0].compile().params
        assert values["notified_stage"] is None
        assert values["notified_for_due_on"] is None
        assert values["notified_for_due_at_dive_count"] is None
        assert values["notified_at"] is None

    @pytest.mark.asyncio
    async def test_orders_records_by_date_then_id(self) -> None:
        # Two services entered for the same day would otherwise resolve arbitrarily.
        db = self._db(self._schedule(), latest_record=None)

        await recalculate_service_schedule(db, schedule_id=3)

        statement = str(db.execute.await_args_list[1].args[0].compile(compile_kwargs={"literal_binds": True}))
        assert "ORDER BY gear_service_record.serviced_on DESC, gear_service_record.id DESC" in statement
        assert "gear_service_record.is_deleted IS false" in statement

    @pytest.mark.asyncio
    async def test_is_a_no_op_for_a_missing_schedule(self) -> None:
        db = self._db(schedule=None, latest_record=None)

        await recalculate_service_schedule(db, schedule_id=999)

        # Only the schedule lookup ran - no record query, no update, no commit.
        assert db.execute.await_count == 1
        db.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_commit_when_commit_is_false(self) -> None:
        db = self._db(self._schedule(), latest_record=None)

        await recalculate_service_schedule(db, schedule_id=3, commit=False)

        db.commit.assert_not_awaited()


class TestDueOverview:
    """The dashboard's `GET /gear-service-due` list, and specifically its row cap.

    Without a `truncated` flag a diver past `DUE_OVERVIEW_LIMIT` saw a list that looked
    complete while some overdue kit simply wasn't in it - the wrong direction to fail in
    for a safety-adjacent card.
    """

    def _db(self, rows: list) -> MagicMock:
        db = MagicMock()
        db.execute = AsyncMock(return_value=rows)
        return db

    def _row(self, name: str = "MK25 EVO") -> SimpleNamespace:
        return SimpleNamespace(
            schedule_uuid=uuid7(),
            kind=ServiceKind.SERVICE,
            label=None,
            last_service_on=date(2025, 8, 1),
            next_due_on=date(2026, 8, 1),
            next_due_at_dive_count=None,
            gear_item_uuid=uuid7(),
            gear_item_name=name,
            gear_item_brand="Scubapro",
            gear_item_dive_count=42,
        )

    @pytest.mark.asyncio
    async def test_returns_rows_unflagged_when_under_the_cap(self) -> None:
        db = self._db([self._row("MK25 EVO"), self._row("Wing 17L")])

        data, truncated = await get_due_overview_for_user(db, user_id=1, limit=200)

        assert [item.gear_item_name for item in data] == ["MK25 EVO", "Wing 17L"]
        assert truncated is False

    @pytest.mark.asyncio
    async def test_flags_truncation_and_trims_to_the_limit(self) -> None:
        db = self._db([self._row() for _ in range(4)])

        data, truncated = await get_due_overview_for_user(db, user_id=1, limit=3)

        assert len(data) == 3
        assert truncated is True

    @pytest.mark.asyncio
    async def test_does_not_flag_truncation_at_exactly_the_limit(self) -> None:
        db = self._db([self._row() for _ in range(3)])

        data, truncated = await get_due_overview_for_user(db, user_id=1, limit=3)

        assert len(data) == 3
        assert truncated is False

    @pytest.mark.asyncio
    async def test_selects_one_row_past_the_limit(self) -> None:
        db = self._db([])

        await get_due_overview_for_user(db, user_id=1, limit=200)

        statement = str(db.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
        assert "LIMIT 201" in statement

    @pytest.mark.asyncio
    async def test_excludes_paused_schedules_and_archived_gear(self) -> None:
        db = self._db([])

        await get_due_overview_for_user(db, user_id=7, limit=200)

        statement = str(db.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
        # Pausing is how a diver silences a rule; archiving retires a whole item without
        # having to also pause every rule on it. Both must keep it off the dashboard.
        assert "gear_service_schedule.is_active IS true" in statement
        assert "gear_item.is_archived IS false" in statement
        assert "gear_service_schedule.user_id = 7" in statement

    def test_the_response_defaults_truncated_to_false(self) -> None:
        # An older client (or a cached response predating the field) must not read as
        # "the list is partial" just because the flag is absent.
        assert GearServiceDueResponse(data=[]).truncated is False


class TestAVanishedGearItemDoesNotFiveHundred:
    """The window hard delete opened, and the one behaviour change in this file that is not
    a docstring.

    Every route here reads its schedule/record rows and then resolves those rows'
    `gear_item_id`s to uuids in a *second* statement, at READ COMMITTED. A
    `DELETE /gear-item/{uuid}` committing in between takes the schedule or record with it
    (`ON DELETE CASCADE`) and leaves an id that resolves to nothing. Through the soft-delete
    era the `gear_item` row survived, so the lookup could not miss and indexing it directly
    was safe; it is not any more, and a `KeyError` here is a 500 on a plain `GET`.

    Stubbed, because arranging a real commit between two statements of one request is a
    great deal of machinery to reproduce something a mismatched pair of return values
    states exactly.
    """

    @staticmethod
    def _schedule_row(gear_item_id: int) -> dict[str, Any]:
        return {
            "id": 1,
            "uuid": uuid7(),
            "user_id": 7,
            "gear_item_id": gear_item_id,
            "kind": "service",
            "label": None,
            "starts_on": date(2026, 1, 1),
            "interval_months": 12,
            "interval_dives": None,
            "dive_count_at_start": 0,
            "is_active": True,
            "last_service_on": None,
            "next_due_on": None,
            "next_due_at_dive_count": None,
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        }

    @pytest.mark.asyncio
    async def test_the_schedule_list_drops_the_row_rather_than_raising(self, monkeypatch) -> None:
        """A page of one whose item vanished comes back empty, which is what a fresh read a
        moment later returns anyway."""
        page = {"data": [self._schedule_row(gear_item_id=3)], "total_count": 1}
        monkeypatch.setattr(gear_service_module.crud_gear_service_schedules, "get_multi", AsyncMock(return_value=page))
        # The delete landed between the two statements, so the id resolves to nothing.
        monkeypatch.setattr(gear_service_module, "get_gear_item_uuids_by_id", AsyncMock(return_value={}))

        result = await cast(Any, gear_service_module._cached_read_schedules).__wrapped__(
            request=None,
            user_id=7,
            user_uuid=uuid7(),
            db=MagicMock(),
            page=1,
            items_per_page=10,
            gear_item_id=None,
        )

        assert result["data"] == []

    @pytest.mark.asyncio
    async def test_the_schedule_list_still_renders_the_rows_that_resolve(self, monkeypatch) -> None:
        """The other half, so the test above cannot pass by the route dropping everything."""
        item_uuid = uuid7()
        page = {"data": [self._schedule_row(gear_item_id=3)], "total_count": 1}
        monkeypatch.setattr(gear_service_module.crud_gear_service_schedules, "get_multi", AsyncMock(return_value=page))
        monkeypatch.setattr(gear_service_module, "get_gear_item_uuids_by_id", AsyncMock(return_value={3: item_uuid}))

        result = await cast(Any, gear_service_module._cached_read_schedules).__wrapped__(
            request=None,
            user_id=7,
            user_uuid=uuid7(),
            db=MagicMock(),
            page=1,
            items_per_page=10,
            gear_item_id=None,
        )

        assert [row["gear_item_uuid"] for row in result["data"]] == [item_uuid]

    @staticmethod
    def _record_row(gear_item_id: int) -> dict[str, Any]:
        return {
            "id": 1,
            "uuid": uuid7(),
            "user_id": 7,
            "gear_item_id": gear_item_id,
            "gear_service_schedule_id": None,
            "kind": "service",
            "label": None,
            "serviced_on": date(2026, 1, 1),
            "dive_count_at_service": 0,
            "performed_by": None,
            "contact_id": None,
            "notes": "",
            "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        }

    @pytest.mark.asyncio
    async def test_the_record_list_drops_the_row_rather_than_raising(self, monkeypatch) -> None:
        """The record half got the identical fix, so it needs the identical test - the two
        call sites are the reason this class is not named after schedules."""
        page = {"data": [self._record_row(gear_item_id=3)], "total_count": 1}
        monkeypatch.setattr(gear_service_module.crud_gear_service_records, "get_multi", AsyncMock(return_value=page))
        monkeypatch.setattr(gear_service_module, "get_gear_item_uuids_by_id", AsyncMock(return_value={}))
        monkeypatch.setattr(gear_service_module, "_schedule_uuids_by_id", AsyncMock(return_value={}))

        result = await cast(Any, gear_service_module._cached_read_records).__wrapped__(
            request=None,
            user_id=7,
            user_uuid=uuid7(),
            db=MagicMock(),
            page=1,
            items_per_page=10,
            gear_item_id=None,
            gear_service_schedule_id=None,
        )

        assert result["data"] == []

    @pytest.mark.asyncio
    async def test_the_record_list_still_renders_the_rows_that_resolve(self, monkeypatch) -> None:
        item_uuid = uuid7()
        page = {"data": [self._record_row(gear_item_id=3)], "total_count": 1}
        monkeypatch.setattr(gear_service_module.crud_gear_service_records, "get_multi", AsyncMock(return_value=page))
        monkeypatch.setattr(gear_service_module, "get_gear_item_uuids_by_id", AsyncMock(return_value={3: item_uuid}))
        monkeypatch.setattr(gear_service_module, "_schedule_uuids_by_id", AsyncMock(return_value={}))

        result = await cast(Any, gear_service_module._cached_read_records).__wrapped__(
            request=None,
            user_id=7,
            user_uuid=uuid7(),
            db=MagicMock(),
            page=1,
            items_per_page=10,
            gear_item_id=None,
            gear_service_schedule_id=None,
        )

        assert [row["gear_item_uuid"] for row in result["data"]] == [item_uuid]

    @pytest.mark.asyncio
    async def test_the_single_record_read_404s(self, monkeypatch) -> None:
        """Deleting the item cascades to the record, so the addressed resource is gone."""
        record = SimpleNamespace(id=1, uuid=uuid7(), user_id=7, gear_item_id=3, gear_service_schedule_id=None)
        monkeypatch.setattr(gear_service_module, "resolve_record_for_user", AsyncMock(return_value=record))
        monkeypatch.setattr(gear_service_module, "get_gear_item_uuids_by_id", AsyncMock(return_value={}))
        monkeypatch.setattr(gear_service_module, "_schedule_uuids_by_id", AsyncMock(return_value={}))

        with pytest.raises(NotFoundException):
            await gear_service_module.read_gear_service_record(
                request=MagicMock(),
                uuid=record.uuid,
                current_user={"id": 7, "uuid": uuid7()},
                db=MagicMock(),
            )

    @pytest.mark.asyncio
    async def test_the_single_schedule_read_404s(self, monkeypatch) -> None:
        """A 404 rather than a skip: the addressed resource really is gone, since deleting
        the item cascades to the schedule."""
        schedule = SimpleNamespace(id=1, uuid=uuid7(), user_id=7, gear_item_id=3)
        monkeypatch.setattr(gear_service_module, "resolve_schedule_for_user", AsyncMock(return_value=schedule))
        monkeypatch.setattr(gear_service_module, "get_gear_item_uuids_by_id", AsyncMock(return_value={}))

        with pytest.raises(NotFoundException):
            await gear_service_module.read_gear_service_schedule(
                request=MagicMock(),
                uuid=schedule.uuid,
                current_user={"id": 7, "uuid": uuid7()},
                db=MagicMock(),
            )
