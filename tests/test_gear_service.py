"""Unit tests for gear service tracking (`models/gear_service_*.py`,
`schemas/gear_service.py`, `services/gear_service.py`).

Same convention as `test_gear.py`: these cover the pieces that are pure logic or pure
SQL construction and so need no database - the month arithmetic, the due-date
derivation, the status truth table, the fire-once notification rule, and the shape of
the recalculation/cascade statements. Endpoint behaviour on top of a live
Postgres/Redis is exercised by hand (see DECISIONS.md), not here.
"""

from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from src.app.schemas.gear_service import (
    GearServiceRecordCreate,
    GearServiceScheduleBase,
    GearServiceScheduleCreate,
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
    soft_delete_schedules_for_gear_item,
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


class TestSoftDeleteSchedulesForGearItem:
    """Soft-deleting a gear item has to silence its reminders, and must not blow up on
    the common case of an item that never had any.
    """

    @pytest.mark.asyncio
    async def test_soft_deletes_the_items_live_schedules(self) -> None:
        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        await soft_delete_schedules_for_gear_item(db, gear_item_id=7)

        statement = str(db.execute.await_args.args[0].compile(compile_kwargs={"literal_binds": True}))
        # A plain UPDATE, not a fastcrud `delete(allow_multiple=True)`, which raises
        # NoResultFound when zero rows match - i.e. for most gear.
        assert statement.startswith("UPDATE gear_service_schedule SET")
        assert "gear_service_schedule.gear_item_id = 7" in statement
        assert "gear_service_schedule.is_deleted IS false" in statement
        db.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_leaves_the_service_history_alone(self) -> None:
        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        await soft_delete_schedules_for_gear_item(db, gear_item_id=7)

        # Records are unreachable once the item is gone, and a soft delete is meant to
        # be recoverable - discarding the receipts would make it much less so.
        assert db.execute.await_count == 1
        assert "gear_service_record" not in str(db.execute.await_args.args[0].compile())

    @pytest.mark.asyncio
    async def test_skips_commit_when_commit_is_false(self) -> None:
        db = MagicMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()

        await soft_delete_schedules_for_gear_item(db, gear_item_id=7, commit=False)

        db.commit.assert_not_awaited()
