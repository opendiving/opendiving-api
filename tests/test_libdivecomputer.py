"""The phone's dive-computer originals, read by `divejson-raw` in a process of its own.

Three groups. The tag's validation and the reader-version spelling are pure. The isolation
promises - no environment, the argument vector, the ceiling, the timeout, a death by signal - are
held against stand-ins for the binary, so they hold whatever the binary does; they need Linux,
whose address-space ceiling the wrapper sets, and refuse to run elsewhere rather than read a file
with no ceiling. The read itself runs the real binary over a Perdix 3 original, and skips where
the host has none: CI installs the one the `Dockerfile` pins and fails on any skip.
"""

import json
import shutil
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import arq.worker
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app.api.dependencies import get_current_user
from src.app.api.v1 import dives as dives_module
from src.app.api.v1.dives import router as dives_router
from src.app.core.db.database import async_get_db
from src.app.core.worker.settings import WorkerSettings
from src.app.services import dive_files, dive_reader, libdivecomputer
from src.app.services.dive_profiles import READER_VERSION, current_reader_versions, reader_version_of
from src.app.services.libdivecomputer import LIBDIVECOMPUTER, Descriptor

PERDIX = Path(__file__).parent / "fixtures" / "dive_files" / "shearwater-perdix-3_D9772626_2026-10-26T1503.bin"
PERDIX_DESCRIPTOR = Descriptor(vendor="Shearwater", product="Perdix 3", model=14)

needs_the_ceiling = pytest.mark.skipif(
    sys.platform != "linux", reason="the address-space ceiling is set the Linux way, and refused elsewhere"
)
needs_the_binary = pytest.mark.skipif(shutil.which(libdivecomputer.CLI) is None, reason="no divejson-raw on PATH")


@pytest.fixture(autouse=True)
def _fresh_install() -> Iterator[None]:
    """Each test finds the binary afresh, so a stand-in one test installs is gone for the next."""
    libdivecomputer._installed.cache_clear()
    yield
    libdivecomputer._installed.cache_clear()


def _stand_in(tmp_path: Path, body: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An executable the api runs in place of `divejson-raw`, with a record file beside it."""
    script = tmp_path / "divejson-raw"
    script.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body))
    script.chmod(0o755)
    monkeypatch.setattr(libdivecomputer, "_installed", lambda: (str(script), "0.0.0+stand-in"))
    return script


def _convert(content: bytes = b"bytes", **overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "descriptor": PERDIX_DESCRIPTOR,
        "fallback_start": None,
        "exported_at": datetime(2026, 11, 3, 12, 0, tzinfo=UTC),
    }
    arguments.update(overrides)
    return libdivecomputer.convert(content, **arguments)  # type: ignore[arg-type]


class TestTheTag:
    def test_a_whole_descriptor_is_taken(self) -> None:
        assert libdivecomputer.descriptor_from({"vendor": "Suunto", "product": "Ocean", "model": 1}) == Descriptor(
            vendor="Suunto", product="Ocean", model=1
        )

    @pytest.mark.parametrize(
        "entry",
        [
            None,
            "Suunto Ocean",
            {"vendor": "Suunto", "product": "Ocean"},
            {"vendor": "", "product": "Ocean", "model": 1},
            {"vendor": "Suunto", "product": "Ocean", "model": "1"},
            {"vendor": "Suunto", "product": "Ocean", "model": True},
            {"vendor": "Suunto", "product": "Ocean", "model": -1},
            {"vendor": "Suunto", "product": "Ocean", "model": 2**31},
            {"vendor": "S" * 65, "product": "Ocean", "model": 1},
            {"vendor": "Suun\0to", "product": "Ocean", "model": 1},
        ],
    )
    def test_anything_else_is_no_descriptor(self, entry: object) -> None:
        """Nothing that could not be stored in the row's columns, or passed on an argument
        vector, gets as far as either."""
        assert libdivecomputer.descriptor_from(entry) is None

    def test_a_fallback_start_is_an_instant(self) -> None:
        assert libdivecomputer.fallback_start_from("2026-10-02T11:36:00+03:00") == datetime(
            2026, 10, 2, 11, 36, tzinfo=timezone(timedelta(hours=3))
        )
        assert libdivecomputer.fallback_start_from("2026-10-02T11:36:00") is None
        assert libdivecomputer.fallback_start_from("yesterday") is None
        assert libdivecomputer.fallback_start_from(1759397760) is None


class TestTheReaderVersion:
    def test_a_profile_names_every_reader_that_fed_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(libdivecomputer, "_installed", lambda: ("/bin/false", "0.1.0+1b201bb"))

        assert reader_version_of(["fit"]) == READER_VERSION
        assert reader_version_of([LIBDIVECOMPUTER]) == "0.1.0+1b201bb"
        assert reader_version_of([LIBDIVECOMPUTER, "fit", "suunto_json"]) == f"{READER_VERSION} 0.1.0+1b201bb"
        assert reader_version_of(["divejson_import"]) is None
        assert current_reader_versions() == {READER_VERSION, "0.1.0+1b201bb", f"{READER_VERSION} 0.1.0+1b201bb"}

    def test_without_the_binary_an_original_names_no_reader(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(libdivecomputer, "_installed", lambda: None)

        assert reader_version_of([LIBDIVECOMPUTER, "fit"]) == READER_VERSION
        assert current_reader_versions() == {READER_VERSION}
        assert not dive_reader.reads(LIBDIVECOMPUTER)


class TestTheDiveFormNeitherOffersNorAcceptsOne:
    """A bare buffer names no computer, and nothing on the form could supply one."""

    def test_the_formats_the_form_names_leave_it_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(libdivecomputer, "_installed", lambda: ("/bin/false", "0.1.0+1b201bb"))

        assert dive_reader.reads(LIBDIVECOMPUTER)
        assert dive_reader.FORMAT_LABELS[LIBDIVECOMPUTER] not in dive_reader.formats_this_build_reads()

    def test_a_lone_original_is_a_415(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(libdivecomputer, "_installed", lambda: ("/bin/false", "0.1.0+1b201bb"))
        monkeypatch.setattr(dives_module, "ensure_room_to_attach", AsyncMock())
        app = FastAPI()
        app.include_router(dives_router)
        app.dependency_overrides[get_current_user] = lambda: {
            "id": 1,
            "uuid": "00000000-0000-0000-0000-000000000001",
            "is_superuser": False,
        }
        nothing = MagicMock()
        nothing.__iter__.return_value = iter(())
        session = AsyncMock()
        session.execute = AsyncMock(return_value=nothing)
        app.dependency_overrides[async_get_db] = lambda: session

        response = TestClient(app).post(
            "/dive/parse", files={"file": (PERDIX.name, PERDIX.read_bytes(), "application/octet-stream")}
        )

        assert response.status_code == 415


@needs_the_ceiling
class TestTheChildIsIsolated:
    def test_it_gets_no_environment_and_its_tag_on_the_argument_vector(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No `SECRET_KEY`, no database or storage credential: the parsers trust their input,
        and an archive may name any of them over bytes its owner chose."""
        record = tmp_path / "record.txt"
        _stand_in(
            tmp_path,
            f"""
            import json, os, resource, sys
            open({str(record)!r}, "w").write(json.dumps({{
                "argv": sys.argv[1:],
                "environ": dict(os.environ),
                "stdin": sys.stdin.buffer.read().decode(),
                "ceiling": resource.getrlimit(resource.RLIMIT_AS)[0],
            }}))
            print("{{}}")
            """,
            monkeypatch,
        )
        monkeypatch.setenv("SECRET_KEY", "not-for-the-child")

        _convert(
            b"the buffer",
            descriptor=Descriptor(vendor="-Suunto", product="Ocean", model=1),
            fallback_start=datetime(2026, 10, 2, 11, 36, tzinfo=timezone(timedelta(hours=3))),
        )

        seen = json.loads(record.read_text())
        assert seen["argv"] == [
            "--vendor=-Suunto",
            "--product=Ocean",
            "--model=1",
            "--exported-at=2026-11-03T12:00:00+00:00",
            "--fallback-start=2026-10-02T08:36:00+00:00",
            "-",
        ]
        # The stand-in is an interpreter, which may coerce its own locale into its environment.
        assert set(seen["environ"]) <= {"LC_CTYPE"}
        assert seen["stdin"] == "the buffer"
        assert seen["ceiling"] == libdivecomputer.MEMORY_CEILING_BYTES

    def test_one_that_never_returns_is_stopped_at_the_timeout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stand_in(tmp_path, "import time\ntime.sleep(600)\n", monkeypatch)
        monkeypatch.setattr(libdivecomputer, "TIMEOUT_SECONDS", 0.5)
        started = time.monotonic()

        with pytest.raises(libdivecomputer.OriginalReadError, match="did not finish"):
            _convert()

        assert time.monotonic() - started < 5

    def test_a_death_by_signal_is_a_refusal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A vendored parser can still fault, and a consumer reads that as an unreadable file."""
        _stand_in(tmp_path, "import os, signal\nos.kill(os.getpid(), signal.SIGSEGV)\n", monkeypatch)

        with pytest.raises(libdivecomputer.OriginalReadError, match="signal 11"):
            _convert()

    def test_a_refusal_carries_its_line_of_stderr(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _stand_in(tmp_path, "import sys\nsys.stderr.write('no descriptor matches\\n')\nsys.exit(1)\n", monkeypatch)

        with pytest.raises(libdivecomputer.OriginalReadError, match="no descriptor matches"):
            _convert()

    def test_extract_file_never_raises_on_any_of_them(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _stand_in(tmp_path, "import sys\nsys.stdout.write('not json')\n", monkeypatch)

        extraction = dive_files.extract_file(
            b"bytes", LIBDIVECOMPUTER, descriptor=PERDIX_DESCRIPTOR, fallback_start=None
        )

        assert extraction is dive_files._UNREAD

    def test_an_original_with_no_descriptor_reads_as_unread(self) -> None:
        assert (
            dive_files.extract_file(b"bytes", LIBDIVECOMPUTER, descriptor=None, fallback_start=None)
            is dive_files._UNREAD
        )


class TestAHangCostsOneTimeout:
    def test_a_batch_of_reads_that_all_time_out_finishes_inside_the_job_timeout(self) -> None:
        """The fixes cron's batch commits only at its end, so a batch that outlived the worker's
        job timeout would be rolled back and selected again on every run."""
        assert not hasattr(WorkerSettings, "job_timeout")
        job_timeout = arq.worker.Worker.__init__.__kwdefaults__["job_timeout"]  # type: ignore[index]
        job_seconds = job_timeout.total_seconds() if isinstance(job_timeout, timedelta) else job_timeout

        assert dive_files.FIX_READ_BATCH_SIZE * libdivecomputer.TIMEOUT_SECONDS < job_seconds


@needs_the_binary
@needs_the_ceiling
class TestTheRealBinary:
    def test_it_reads_a_perdix_3_original(self) -> None:
        extraction = dive_files.extract_file(
            PERDIX.read_bytes(), LIBDIVECOMPUTER, descriptor=PERDIX_DESCRIPTOR, fallback_start=None
        )

        assert extraction.parsed is not None
        assert extraction.parsed.max_depth == 47.7
        assert extraction.start == (datetime(2026, 10, 26, 15, 3, 23, tzinfo=UTC), None)
        assert extraction.profile is not None

    def test_a_start_the_bytes_state_wins_over_the_fallback(self) -> None:
        """A re-read never moves a dive: the fallback is used only where the computer states none."""
        extraction = dive_files.extract_file(
            PERDIX.read_bytes(),
            LIBDIVECOMPUTER,
            descriptor=PERDIX_DESCRIPTOR,
            fallback_start=datetime(2020, 1, 1, tzinfo=UTC),
        )

        assert extraction.start == (datetime(2026, 10, 26, 15, 3, 23, tzinfo=UTC), None)

    def test_the_wrong_computer_reads_nothing(self) -> None:
        extraction = dive_files.extract_file(
            PERDIX.read_bytes(),
            LIBDIVECOMPUTER,
            descriptor=Descriptor(vendor="Nobody", product="Nothing", model=0),
            fallback_start=None,
        )

        assert extraction is dive_files._UNREAD

    def test_its_version_is_what_a_profile_it_fed_records(self) -> None:
        version = subprocess.run(
            [libdivecomputer.CLI, "--version"], capture_output=True, text=True, check=True
        ).stdout.strip()

        assert libdivecomputer.cli_version() == version
        assert reader_version_of([LIBDIVECOMPUTER]) == version
        assert len(f"{READER_VERSION} {version}") <= 64

    def test_its_document_is_the_one_every_other_reader_s_goes_through(self) -> None:
        read = dive_reader.read_original(PERDIX.read_bytes(), descriptor=PERDIX_DESCRIPTOR, fallback_start=None)

        assert read.format == LIBDIVECOMPUTER
        assert read.recording is not None
        assert read.started_at == "2026-10-26T15:03:23"
