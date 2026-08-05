"""Unit tests for the `start_time`/UTC-offset handling (`core/utils/datetime_offset.py`,
`schemas/dive.py`).

`Dive.start_time` is stored as a UTC instant plus a separate `utc_offset_minutes` column
(the offset it was originally logged in), but the API always presents/accepts a single
offset-aware ISO 8601 `start_time` string (e.g. "2021-04-04T10:04:47.910+02:00") - see
`core/utils/datetime_offset.py` for why, and `api/v1/dives.py` for where the two are
combined/split.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.app.core.utils.datetime_offset import combine_start_time, require_utc_offset, split_start_time
from src.app.schemas.dive import DiveCreate, DiveUpdate


class TestSplitAndCombineStartTime:
    def test_split_returns_utc_instant_and_offset_minutes(self) -> None:
        start_time = datetime(2021, 4, 4, 10, 4, 47, 910000, tzinfo=timezone(timedelta(hours=2)))

        utc_instant, offset_minutes = split_start_time(start_time)

        assert offset_minutes == 120
        assert utc_instant == datetime(2021, 4, 4, 8, 4, 47, 910000, tzinfo=UTC)

    def test_split_handles_negative_offsets(self) -> None:
        start_time = datetime(2024, 1, 1, 6, 0, 0, tzinfo=timezone(timedelta(hours=-5)))

        _, offset_minutes = split_start_time(start_time)

        assert offset_minutes == -300

    def test_split_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            split_start_time(datetime(2024, 1, 1, 6, 0, 0))

    def test_combine_reattaches_the_original_offset(self) -> None:
        utc_instant = datetime(2021, 4, 4, 8, 4, 47, 910000, tzinfo=UTC)

        combined = combine_start_time(utc_instant, 120)

        assert combined == datetime(2021, 4, 4, 10, 4, 47, 910000, tzinfo=timezone(timedelta(hours=2)))
        assert combined.utcoffset() == timedelta(hours=2)

    def test_split_then_combine_round_trips(self) -> None:
        start_time = datetime(2025, 6, 3, 12, 15, 33, 800000, tzinfo=timezone(timedelta(hours=5, minutes=45)))

        utc_instant, offset_minutes = split_start_time(start_time)
        combined = combine_start_time(utc_instant, offset_minutes)

        assert combined == start_time
        assert combined.utcoffset() == start_time.utcoffset()


class TestRequireUtcOffset:
    def test_accepts_offset_aware_datetime(self) -> None:
        value = datetime(2024, 1, 1, tzinfo=UTC)
        assert require_utc_offset(value) is value

    def test_rejects_naive_datetime(self) -> None:
        with pytest.raises(ValueError, match="UTC offset"):
            require_utc_offset(datetime(2024, 1, 1))


class TestDiveStartTimeValidation:
    """`DiveCreate`/`DiveUpdate` (and everything else built on `DiveBase`) require an
    explicit UTC offset on `start_time` - mirroring `require_utc_offset` above through
    the actual Pydantic schemas used by the API.
    """

    def _dive_kwargs(self, start_time: str) -> dict:
        return {"dive_number": 1, "start_time": start_time, "duration": 1800}

    def test_create_accepts_offset_aware_iso_string(self) -> None:
        dive = DiveCreate(**self._dive_kwargs("2021-04-04T10:04:47.910+02:00"))
        assert dive.start_time.utcoffset() == timedelta(hours=2)

    def test_create_accepts_z_suffix(self) -> None:
        dive = DiveCreate(**self._dive_kwargs("2021-04-04T10:04:47Z"))
        assert dive.start_time.utcoffset() == timedelta(0)

    def test_create_rejects_naive_iso_string(self) -> None:
        with pytest.raises(ValidationError, match="UTC offset"):
            DiveCreate(**self._dive_kwargs("2025-06-03T12:15:33.8"))

    def test_update_rejects_naive_iso_string(self) -> None:
        with pytest.raises(ValidationError, match="UTC offset"):
            DiveUpdate(start_time="2025-06-03T12:15:33.8")

    def test_update_allows_omitting_start_time(self) -> None:
        assert DiveUpdate().start_time is None
