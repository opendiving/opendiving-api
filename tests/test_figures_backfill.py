"""The figures backfill: which stored values read as untouched, and what replaces them.

The predicate and the files' order are pure and pinned first; the rows below need Postgres,
under the shared skip-if-unreachable guard, and each test confines its run to its own diver.
"""

import hashlib
import io
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import UploadFile
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.core.security import create_dive_file_token
from src.app.models.dive import Dive
from src.app.models.dive_profile import DiveProfile
from src.app.models.dive_recording import DiveRecording
from src.app.models.user import User
from src.app.models.user_dive_stats import UserDiveStats
from src.app.schemas.parsed_dive import ParsedDiveSchema
from src.app.services import blob_store
from src.app.services.dive_files import store_recording_file
from src.app.services.dive_profiles import (
    IMPORT_PARSER_KEY,
    MERGE_PARSER_KEY,
    NormalizedProfile,
    ProfileSeries,
    profile_payload_digest,
    store_profile,
)
from src.app.services.figures_backfill import (
    Device,
    backfill_dive_figures,
    stated_over_derived,
    untouched_avg_depth,
    untouched_duration,
    whole_recording_mean,
)
from tests.conftest import db_available
from tests.helpers.generators import create_dive, create_dive_recording, create_user

FIXTURES = Path(__file__).parent / "fixtures" / "dive_files"


def _parsed(duration: int | None, avg_depth: float | None, *inferred: str) -> ParsedDiveSchema:
    return ParsedDiveSchema(
        avg_depth=avg_depth,
        bottom_temperature=None,
        dive_number=None,
        duration=duration,
        max_depth=None,
        start_time=None,
        mixtures=[],
        inferred=list(inferred),  # type: ignore[arg-type]
    )


class TestWhatReadsAsUntouched:
    @pytest.mark.parametrize(("stored", "span_ms"), [(3814, 3_814_000), (3815, 3_814_000), (3813, 3_814_000)])
    def test_a_duration_within_a_second_of_the_span(self, stored: int, span_ms: int) -> None:
        """Inclusive: the old FIT reader's session time rounds up past the last sample, so
        most sit exactly a second over."""
        assert untouched_duration(stored, span_ms)

    @pytest.mark.parametrize("stored", [3812, 3816, 3424])
    def test_any_other_duration_is_the_divers(self, stored: int) -> None:
        assert not untouched_duration(stored, 3_814_000)

    def test_a_mean_within_a_tenth_of_the_whole_recordings(self) -> None:
        assert untouched_avg_depth(9.6, Decimal("9.605"))
        assert untouched_avg_depth(9.7, Decimal("9.6"))
        assert not untouched_avg_depth(9.71, Decimal("9.6"))
        assert not untouched_avg_depth(None, Decimal("9.6"))

    def test_the_whole_recordings_mean_is_time_weighted_over_every_interval(self) -> None:
        profile = NormalizedProfile(depth=ProfileSeries(t=[0, 10_000, 40_000], v=[0, 1000, 0]))

        assert whole_recording_mean(profile) == Decimal(10_000 * 1000 + 30_000 * 1000) / 2 / 40_000 / 100
        assert whole_recording_mean(NormalizedProfile(depth=ProfileSeries(t=[0], v=[500]))) is None


class TestTheFilesOrder:
    """The dive form's rule, over one recording's files in attach order."""

    def test_a_later_files_stated_figure_replaces_a_derived_one(self) -> None:
        """The motivating pair: the FIT derived both, the JSON states both."""
        assert stated_over_derived([_parsed(3440, 10.6, "duration", "avg_depth"), _parsed(3424, 10.62)]) == (
            3424,
            10.62,
        )

    def test_a_stated_figure_is_never_replaced(self) -> None:
        assert stated_over_derived([_parsed(3424, 10.62), _parsed(3440, 10.6, "duration", "avg_depth")]) == (
            3424,
            10.62,
        )

    def test_a_derived_figure_stays_when_nothing_later_states_one(self) -> None:
        assert stated_over_derived([_parsed(3440, 10.6, "duration"), _parsed(3500, None, "duration")]) == (3440, 10.6)

    def test_a_blank_takes_the_next_files_figure(self) -> None:
        assert stated_over_derived([_parsed(3424, None), _parsed(3440, 10.6, "avg_depth")]) == (3424, 10.6)


# ---------------------------------------------------------------- against Postgres

pytestmark_db = pytest.mark.skipif(not db_available(), reason="No database connection available")


@pytest.fixture
def volume(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def invalidated(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    """No Redis here; a run's invalidation is recorded rather than sent."""
    dropped = AsyncMock()
    monkeypatch.setattr("src.app.services.cache_invalidation.invalidate_dive_caches", dropped)
    return dropped


@pytest.fixture
def diver(db: Session) -> User:
    return create_user(db)


async def _attach(db: AsyncSession, diver: User, dive: Dive, name: str, fmt: str) -> None:
    content = (FIXTURES / name).read_bytes()
    await store_recording_file(
        db,
        user_id=diver.id,
        user_uuid=diver.uuid,
        dive_id=dive.id,
        upload=UploadFile(filename=name, file=io.BytesIO(content)),
        file_token=create_dive_file_token(
            user_uuid=diver.uuid, sha256=hashlib.sha256(content).hexdigest(), parser_key=fmt
        ),
    )


async def _span_ms(db: AsyncSession, dive: Dive) -> int:
    return (
        await db.execute(
            select(DiveProfile.duration)
            .join(DiveRecording, DiveRecording.id == DiveProfile.recording_id)
            .where(DiveRecording.dive_id == dive.id, DiveRecording.ordinal == 0)
        )
    ).scalar_one()


async def _set(db: AsyncSession, dive: Dive, **values: Any) -> None:
    await db.execute(update(Dive).where(Dive.id == dive.id).values(**values))
    await db.commit()


async def _figures(db: AsyncSession, dive: Dive) -> tuple[int, float | None]:
    row = (await db.execute(select(Dive.duration, Dive.avg_depth).where(Dive.id == dive.id))).one()
    return row.duration, row.avg_depth


async def _ocean_fit_dive(db: AsyncSession, sync_db: Session, diver: User, *, past_span_ms: int = 1000) -> Dive:
    """The 2026 Ocean FIT with its session's figures stored: the session's time, which sits a second
    past the samples' span, and the session's whole-recording mean of 9.49 m."""
    dive = create_dive(sync_db, diver, max_depth=19.04)
    await _attach(db, diver, dive, "suunto-ocean-2026.fit", "fit")
    await _set(db, dive, duration=round((await _span_ms(db, dive) + past_span_ms) / 1000), avg_depth=9.49)
    return dive


async def _sampled_dive(
    db: AsyncSession,
    sync_db: Session,
    diver: User,
    *,
    parser_key: str,
    brand: str | None = None,
    model: str | None = None,
) -> Dive:
    """A dive whose profile no file can re-yield: down a minute in, 18 m for 28 minutes, five
    coming up and five at the surface - 1 980 s in the water of a 2 340 s span. Its stored
    figures are the span and the whole recording's mean."""
    dive = create_dive(sync_db, diver, max_depth=18.0)
    recording = create_dive_recording(sync_db, diver, dive)
    recording.device_brand, recording.device_model = brand, model
    sync_db.commit()
    profile = NormalizedProfile(
        depth=ProfileSeries(t=[0, 60_000, 1_740_000, 2_040_000, 2_340_000], v=[0, 1800, 1800, 0, 0])
    )
    await store_profile(
        db,
        recording_id=recording.id,
        dive_id=dive.id,
        profile=profile,
        source_sha256=profile_payload_digest(profile),
        parser_key=parser_key,
        reader_version=None,
        commit=True,
    )
    whole_mean = whole_recording_mean(profile)
    assert whole_mean is not None
    await _set(db, dive, duration=2340, avg_depth=round(float(whole_mean), 2))
    return dive


IN_WATER = (1980, round((1680 * 18.0 + 300 * 9.0) / 1980, 2))


@pytestmark_db
class TestTheBackfillAgainstTheRows:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("past_span_ms", [0, 1000])
    async def test_a_fits_whole_recording_figures_become_its_time_in_the_water(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User, past_span_ms: int
    ) -> None:
        dive = await _ocean_fit_dive(async_db, db, diver, past_span_ms=past_span_ms)

        report = await backfill_dive_figures(async_db, user_id=diver.id)

        assert await _figures(async_db, dive) == (3063, 10.73)
        [rewrite] = report.rewrites
        assert (rewrite.dive_uuid, rewrite.source, rewrite.avg_depth) == (dive.uuid, "files", (9.49, 10.73))

    @pytest.mark.asyncio
    async def test_a_dry_run_reports_and_writes_nothing(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        dive = await _ocean_fit_dive(async_db, db, diver)
        before = await _figures(async_db, dive)

        report = await backfill_dive_figures(async_db, user_id=diver.id, dry_run=True)

        assert [rewrite.duration for rewrite in report.rewrites] == [(before[0], 3063)]
        assert await _figures(async_db, dive) == before

    @pytest.mark.asyncio
    async def test_a_typed_duration_stays_while_the_untouched_mean_moves(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        dive = await _ocean_fit_dive(async_db, db, diver)
        await _set(async_db, dive, duration=3600)

        await backfill_dive_figures(async_db, user_id=diver.id)

        assert await _figures(async_db, dive) == (3600, 10.73)

    @pytest.mark.asyncio
    async def test_a_typed_mean_stays(self, volume: Any, async_db: AsyncSession, db: Session, diver: User) -> None:
        dive = await _ocean_fit_dive(async_db, db, diver)
        await _set(async_db, dive, avg_depth=15.0)

        await backfill_dive_figures(async_db, user_id=diver.id)

        assert await _figures(async_db, dive) == (3063, 15.0)

    @pytest.mark.asyncio
    async def test_the_jsons_stated_figures_win_over_the_fits_derived_ones(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """The motivating dive: its FIT was attached first and its JSON second, both on one
        recording.

        The pair's depth channel is the JSON's, and this JSON is reduced to five depth samples
        whose mean is 9.67 m, where the whole export's agrees with the FIT's. So the stored mean
        is set to the reduced file's, keeping it the one the old readers wrote."""
        dive = await _ocean_fit_dive(async_db, db, diver)
        await _attach(async_db, diver, dive, "suunto-ocean-2026.json", "suunto_json")
        assert (
            await async_db.scalar(
                select(DiveRecording.id).where(DiveRecording.dive_id == dive.id, DiveRecording.ordinal == 1)
            )
        ) is None
        await _set(async_db, dive, avg_depth=9.67)

        await backfill_dive_figures(async_db, user_id=diver.id)

        assert await _figures(async_db, dive) == (3051, 10.74)

    @pytest.mark.asyncio
    async def test_a_stated_figure_equal_to_the_old_one_is_left_alone(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """A D5's JSON states its whole logged period, which the reader keeps."""
        dive = create_dive(db, diver, max_depth=46.29)
        await _attach(async_db, diver, dive, "suunto-d5.json", "suunto_json")
        await _set(async_db, dive, duration=4683, avg_depth=17.79)

        report = await backfill_dive_figures(async_db, user_id=diver.id)

        assert report.rewrites == []
        assert await _figures(async_db, dive) == (4683, 17.79)

    @pytest.mark.asyncio
    async def test_a_folded_dive_is_derived_from_its_stored_samples(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        dive = await _sampled_dive(async_db, db, diver, parser_key=MERGE_PARSER_KEY)

        report = await backfill_dive_figures(async_db, user_id=diver.id)

        assert await _figures(async_db, dive) == IN_WATER
        assert [rewrite.source for rewrite in report.rewrites] == ["merge"]

    @pytest.mark.asyncio
    async def test_an_imported_dive_moves_only_for_a_named_device(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """Nothing stored says whether its document stated a figure equal to the span, so the
        operator names the devices the old reader wrote for - brand and model as stored."""
        named = await _sampled_dive(
            async_db, db, diver, parser_key=IMPORT_PARSER_KEY, brand="suunto", model="Suunto Ocean"
        )
        other_model = await _sampled_dive(async_db, db, diver, parser_key=IMPORT_PARSER_KEY, brand="suunto")
        unnamed = await _sampled_dive(async_db, db, diver, parser_key=IMPORT_PARSER_KEY, brand="Shearwater")
        stored = await _figures(async_db, unnamed)

        report = await backfill_dive_figures(async_db, user_id=diver.id, devices=[Device("suunto", "Suunto Ocean")])

        assert report.devices == (Device("suunto", "Suunto Ocean"),)
        assert await _figures(async_db, named) == IN_WATER
        assert await _figures(async_db, other_model) == stored
        assert await _figures(async_db, unnamed) == stored
        assert [rewrite.dive_uuid for rewrite in report.rewrites] == [named.uuid]

    @pytest.mark.asyncio
    async def test_no_device_named_touches_no_imported_dive(
        self, volume: Any, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        dive = await _sampled_dive(
            async_db, db, diver, parser_key=IMPORT_PARSER_KEY, brand="suunto", model="Suunto Ocean"
        )
        stored = await _figures(async_db, dive)

        report = await backfill_dive_figures(async_db, user_id=diver.id)

        assert report.devices == ()
        assert report.rewrites == []
        assert await _figures(async_db, dive) == stored

    @pytest.mark.asyncio
    async def test_the_divers_stats_and_caches_follow(
        self, volume: Any, invalidated: AsyncMock, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        dive = await _sampled_dive(async_db, db, diver, parser_key=MERGE_PARSER_KEY)

        await backfill_dive_figures(async_db, user_id=diver.id)

        total = await async_db.scalar(select(UserDiveStats.total_time).where(UserDiveStats.user_id == diver.id))
        assert total == (await _figures(async_db, dive))[0] == IN_WATER[0]
        invalidated.assert_awaited_once_with(diver.id)
