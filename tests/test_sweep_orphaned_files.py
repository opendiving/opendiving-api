"""Tests for `src/scripts/sweep_orphaned_files.py`.

The interesting behaviour is all refusal. A sweeper that deletes what it should is a loop
over `unlink`; a sweeper that deletes what it shouldn't is unrecoverable, and the way that
happens in practice is not a bug in the diff - it is being pointed at the wrong database,
where *everything* scans as orphaned. That is the Gitea `doctor --fix` failure this is
shaped against, and the grace window does nothing about it, because the files are old and
the database is simply wrong.
"""

import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.app.services import blob_store
from src.scripts import sweep_orphaned_files as sweeper

REFERENCED = "dive-files/aa/referenced"
ORPHAN = "dive-files/bb/orphan"


@pytest.fixture
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


def _write(volume: Path, key: str, *, age_hours: float = 0.0) -> Path:
    path = volume / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"payload")
    if age_hours:
        stamp = time.time() - age_hours * 3600
        import os

        os.utime(path, (stamp, stamp))
    return path


def _with_referenced(keys: set[str]):
    """Patch out the database read, so these tests need no Postgres to say what is
    referenced - which is the whole input the diff runs on."""
    return patch.object(sweeper, "_referenced_keys", AsyncMock(return_value=keys))


class TestReporting:
    @pytest.mark.asyncio
    async def test_a_dry_run_finds_the_orphan_and_deletes_nothing(self, volume: Path) -> None:
        _write(volume, REFERENCED, age_hours=48)
        _write(volume, ORPHAN, age_hours=48)

        with _with_referenced({REFERENCED}):
            report = await sweeper.sweep()

        assert (report.on_disk, report.orphaned, report.deleted) == (2, 1, 0)
        assert (volume / ORPHAN).is_file()

    @pytest.mark.asyncio
    async def test_delete_removes_only_the_unreferenced_one(self, volume: Path) -> None:
        _write(volume, REFERENCED, age_hours=48)
        _write(volume, ORPHAN, age_hours=48)

        with _with_referenced({REFERENCED}):
            report = await sweeper.sweep(delete=True)

        assert report.deleted == 1
        assert (volume / REFERENCED).is_file()
        assert not (volume / ORPHAN).exists()

    @pytest.mark.asyncio
    async def test_a_file_younger_than_the_grace_window_is_left_alone(self, volume: Path) -> None:
        """This is what makes an *online* sweep safe: a file is written before the row that
        references it commits, so a fresh unreferenced file may simply be an upload in
        flight."""
        _write(volume, REFERENCED, age_hours=48)
        _write(volume, ORPHAN)

        with _with_referenced({REFERENCED}):
            report = await sweeper.sweep(delete=True)

        assert (report.orphaned, report.within_grace, report.deleted) == (0, 1, 0)
        assert (volume / ORPHAN).is_file()


class TestTheWrongDatabaseGuard:
    @pytest.mark.asyncio
    async def test_it_refuses_when_the_database_references_nothing_at_all(self, volume: Path) -> None:
        """Mid-restore, or `POSTGRES_*` pointed at the wrong host. Every file scans as
        orphaned and every one of them is real."""
        _write(volume, REFERENCED, age_hours=48)
        _write(volume, ORPHAN, age_hours=48)

        with _with_referenced(set()):
            report = await sweeper.sweep(delete=True)

        assert report.deleted == 0
        assert report.refused is not None
        assert (volume / REFERENCED).is_file()

    @pytest.mark.asyncio
    async def test_it_refuses_when_too_much_of_the_volume_is_unreferenced(self, volume: Path) -> None:
        for index in range(30):
            _write(volume, f"dive-files/cc/orphan-{index}", age_hours=48)
        referenced = {f"dive-files/dd/kept-{index}" for index in range(5)}
        for key in referenced:
            _write(volume, key, age_hours=48)

        with _with_referenced(referenced):
            report = await sweeper.sweep(delete=True)

        assert report.deleted == 0
        assert report.refused is not None

    @pytest.mark.asyncio
    async def test_a_small_tree_is_not_judged_by_its_fraction(self, volume: Path) -> None:
        """One orphan out of four is 25% and says nothing. A guard that fired here would
        teach whoever runs this to reach for `--force`, which is how a guard stops working
        on the day it matters."""
        for index in range(3):
            _write(volume, f"dive-files/dd/kept-{index}", age_hours=48)
        _write(volume, ORPHAN, age_hours=48)
        referenced = {f"dive-files/dd/kept-{index}" for index in range(3)}

        with _with_referenced(referenced):
            report = await sweeper.sweep(delete=True)

        assert report.refused is None
        assert report.deleted == 1

    @pytest.mark.asyncio
    async def test_force_overrides_the_refusal(self, volume: Path) -> None:
        _write(volume, ORPHAN, age_hours=48)

        with _with_referenced(set()):
            report = await sweeper.sweep(delete=True, force=True)

        assert report.deleted == 1
        assert report.refused is None

    @pytest.mark.asyncio
    async def test_an_empty_database_and_an_empty_volume_is_not_a_refusal(self, volume: Path) -> None:
        """A fresh install has no rows and no files, and reporting that as suspicious would
        train whoever runs this to reach for `--force`."""
        with _with_referenced(set()):
            report = await sweeper.sweep(delete=True)

        assert report.refused is None
        assert (report.on_disk, report.orphaned) == (0, 0)

    @pytest.mark.asyncio
    async def test_a_dry_run_never_reports_a_refusal(self, volume: Path) -> None:
        """There is nothing to refuse - the run was only ever going to print."""
        _write(volume, ORPHAN, age_hours=48)

        with _with_referenced(set()):
            report = await sweeper.sweep()

        assert report.refused is None


class TestTempDirectory:
    @pytest.mark.asyncio
    async def test_a_stale_part_file_is_counted_and_removed(self, volume: Path) -> None:
        """A `.part` file is live only between the write and the rename, so one a day old
        is the remains of a process that died in between."""
        _write(volume, REFERENCED, age_hours=48)
        stale = _write(volume, f"{blob_store.TMP_DIRNAME}/.dead.part", age_hours=48)

        with _with_referenced({REFERENCED}):
            report = await sweeper.sweep(delete=True)

        assert report.stale_temp_files == 1
        assert not stale.exists()

    @pytest.mark.asyncio
    async def test_a_fresh_part_file_is_left_alone(self, volume: Path) -> None:
        _write(volume, REFERENCED, age_hours=48)
        fresh = _write(volume, f"{blob_store.TMP_DIRNAME}/.inflight.part")

        with _with_referenced({REFERENCED}):
            report = await sweeper.sweep(delete=True)

        assert report.stale_temp_files == 0
        assert fresh.is_file()

    @pytest.mark.asyncio
    async def test_temp_files_are_never_counted_as_orphans(self, volume: Path) -> None:
        _write(volume, REFERENCED, age_hours=48)
        _write(volume, f"{blob_store.TMP_DIRNAME}/.dead.part", age_hours=48)

        with _with_referenced({REFERENCED}):
            report = await sweeper.sweep()

        assert (report.on_disk, report.orphaned) == (1, 0)
