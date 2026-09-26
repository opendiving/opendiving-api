"""Unit tests for the renewal reminder's rules (`services/renewals.py`).

Pure, like `TestShouldNotify` in `test_gear_service.py`: which stage a date is in, when the
email fires, and what it calls each subject. The job that feeds these rows is in
`test_worker.py`.
"""

from datetime import date, timedelta

import pytest

from src.app.schemas.certification import CertificationAgency
from src.app.services.renewals import (
    CERTIFICATION_EXPIRING_SOON_DAYS,
    ExpiryStage,
    certification_label,
    expiry_stage,
    expiry_text,
    insurance_label,
    should_remind,
)

TODAY = date(2026, 9, 26)


class TestExpiryStage:
    """The web's `certificationExpiryStatus`, boundary for boundary."""

    def test_the_window_matches_the_web(self) -> None:
        # `CERTIFICATION_EXPIRING_SOON_DAYS` in `src/lib/certification.ts`. Change both.
        assert CERTIFICATION_EXPIRING_SOON_DAYS == 90

    def test_no_date_has_no_stage(self) -> None:
        assert expiry_stage(None, TODAY) is None

    def test_a_date_past_the_window_has_no_stage(self) -> None:
        assert expiry_stage(TODAY + timedelta(days=CERTIFICATION_EXPIRING_SOON_DAYS + 1), TODAY) is None

    def test_the_last_day_of_the_window_is_expiring_soon(self) -> None:
        assert (
            expiry_stage(TODAY + timedelta(days=CERTIFICATION_EXPIRING_SOON_DAYS), TODAY) is ExpiryStage.EXPIRING_SOON
        )

    def test_a_card_expiring_today_is_still_valid_today(self) -> None:
        assert expiry_stage(TODAY, TODAY) is ExpiryStage.EXPIRING_SOON

    def test_yesterday_is_expired(self) -> None:
        assert expiry_stage(TODAY - timedelta(days=1), TODAY) is ExpiryStage.EXPIRED

    def test_the_stage_values_are_the_webs(self) -> None:
        assert {stage.value for stage in ExpiryStage} == {"expiring_soon", "expired"}


class TestShouldRemind:
    """Once per (stage, date), never daily, and no re-nag."""

    EXPIRES_ON = date(2026, 11, 1)

    def test_the_first_time_a_card_enters_the_window(self) -> None:
        assert should_remind(
            stage=ExpiryStage.EXPIRING_SOON, expires_on=self.EXPIRES_ON, notified_stage=None, notified_for=None
        )

    def test_silent_on_the_next_run_with_nothing_changed(self) -> None:
        assert not should_remind(
            stage=ExpiryStage.EXPIRING_SOON,
            expires_on=self.EXPIRES_ON,
            notified_stage="expiring_soon",
            notified_for=self.EXPIRES_ON,
        )

    def test_again_when_the_card_expires(self) -> None:
        assert should_remind(
            stage=ExpiryStage.EXPIRED,
            expires_on=self.EXPIRES_ON,
            notified_stage="expiring_soon",
            notified_for=self.EXPIRES_ON,
        )

    def test_an_expired_card_is_not_nagged_again(self) -> None:
        assert not should_remind(
            stage=ExpiryStage.EXPIRED,
            expires_on=self.EXPIRES_ON,
            notified_stage="expired",
            notified_for=self.EXPIRES_ON,
        )

    def test_a_renewal_moves_the_date_and_re_arms(self) -> None:
        renewed = self.EXPIRES_ON.replace(year=self.EXPIRES_ON.year + 1)
        assert should_remind(
            stage=ExpiryStage.EXPIRING_SOON, expires_on=renewed, notified_stage="expired", notified_for=self.EXPIRES_ON
        )

    def test_nothing_without_a_stage(self) -> None:
        assert not should_remind(stage=None, expires_on=self.EXPIRES_ON, notified_stage=None, notified_for=None)


class TestWording:
    def test_expiry_text_uses_the_renewals_cards_verbs(self) -> None:
        assert expiry_text(ExpiryStage.EXPIRING_SOON, date(2026, 12, 5)) == "expires 5 Dec 2026"
        assert expiry_text(ExpiryStage.EXPIRED, date(2026, 9, 3)) == "expired 3 Sep 2026"

    def test_a_card_is_named_by_agency_and_level(self) -> None:
        assert certification_label(agency="padi", agency_other=None, name="Rescue Diver") == "PADI Rescue Diver"
        assert certification_label(agency="scotsac", agency_other=None, name="Club Diver") == "ScotSAC Club Diver"

    def test_other_is_named_by_the_divers_own_agency(self) -> None:
        assert certification_label(agency="other", agency_other=" VDST ", name="CMAS**") == "VDST CMAS**"
        assert certification_label(agency="other", agency_other=None, name="Rescue") == "Other Rescue"

    def test_a_stored_agency_outside_the_vocabulary_prints_as_it_is(self) -> None:
        assert certification_label(agency="newbody", agency_other=None, name="Rescue") == "newbody Rescue"

    @pytest.mark.parametrize("agency", list(CertificationAgency))
    def test_every_agency_has_a_label(self, agency: CertificationAgency) -> None:
        # A new member without a label would print its raw value in someone's inbox.
        assert certification_label(agency=agency.value, agency_other="X", name="N") != f"{agency.value} N"

    def test_the_insurance_is_named_by_its_insurer(self) -> None:
        assert insurance_label("DAN Europe") == "DAN Europe dive insurance"
        assert insurance_label(None) == "Dive insurance"
        assert insurance_label("  ") == "Dive insurance"
