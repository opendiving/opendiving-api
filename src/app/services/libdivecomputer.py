"""The phone's dive-computer originals, read by `divejson-raw` in a process of its own.

The iOS app downloads each dive over Bluetooth through libdivecomputer and keeps the dive's
bytes exactly as the download handed them over. Those bytes mean nothing without the
libdivecomputer *descriptor* of the computer that wrote them - its vendor string, product
string and model number, never a family number, which is numbered per build - so the app tags
the stored file with it, and with the start its walk fell back on where the computer states
none. `divejson-raw`, the command of DiveJSON's C implementation, reads one such buffer with
that descriptor and writes a DiveJSON document of the one dive; `dive_reader.read_original`
hands the document to the step every other reader's output goes through.

**A subprocess, not a binding.** The parsers it runs are libdivecomputer's, which trust their
input: fuzzing finds reads past the buffer's end in several families and an input on which the
Suunto Nautic parser never returns, and an imported archive may name any descriptor over bytes
its owner chose. So it runs isolated: an argument vector and no shell; an environment holding
nothing of this process's, which carries `SECRET_KEY` and the database and storage credentials;
a timeout; and an address-space ceiling set by an exec'd wrapper before it becomes the binary,
since `preexec_fn` is documented as unsafe in a threaded process and this runs in conversion
threads. Every way it ends other than exit 0 with a document - a refusal, a signal, the timeout,
the ceiling - is one unreadable file.

The binary is found on `PATH`, where the image puts it; an api with none reads no such file.
"""

import functools
import json
import logging
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# The format id a tagged original is stored and re-read under (`dive_file.parser_key`).
LIBDIVECOMPUTER = "libdivecomputer"

CLI = "divejson-raw"

# Per read. A dive's buffer reads in tens of milliseconds, so this bounds only a parser that
# never returns, and it is sized against the worker's fixes cron: a full `FIX_READ_BATCH_SIZE`
# batch whose every read times out still commits inside arq's default 300 s job timeout, so a
# batch of hanging files is marked read rather than rolled back and selected again forever.
TIMEOUT_SECONDS = 5

# The child's address space. The larger fixture, a 1.7 MB Suunto Ocean log, peaks near 16 MB;
# the ceiling stops a runaway allocation well short of an instance whose memory the api's
# workers share.
MEMORY_CEILING_BYTES = 128 * 1024 * 1024

# The `dive_file` columns' widths, which a tag's strings are held to before they are stored.
DESCRIPTOR_TEXT_LENGTH = 64
_MODEL_MAX = 2**31 - 1

# Run as `python -I -S -c`, it sets the ceiling on itself and then becomes the binary, so the
# limit holds from the binary's first instruction. Where the platform refuses the ceiling, as
# macOS does, it exits before the binary runs at all. `execve` with an empty environment, since
# the interpreter may have set a locale variable in its own.
_WRAPPER = (
    "import os, resource, sys\n"
    "ceiling = int(sys.argv[1])\n"
    "resource.setrlimit(resource.RLIMIT_AS, (ceiling, ceiling))\n"
    "os.execve(sys.argv[2], sys.argv[2:], {})\n"
)


class OriginalReadError(Exception):
    """`divejson-raw` did not read the buffer, for the reason given."""


@dataclass(frozen=True, slots=True)
class Descriptor:
    """The computer a buffer came from, as libdivecomputer names it."""

    vendor: str
    product: str
    model: int


def _text(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > DESCRIPTOR_TEXT_LENGTH or "\0" in value:
        return None
    return value


def descriptor_from(entry: Any) -> Descriptor | None:
    """A tag's `descriptor` member, or `None` where it is not one this app can pass on."""
    if not isinstance(entry, Mapping):
        return None
    vendor, product, model = _text(entry.get("vendor")), _text(entry.get("product")), entry.get("model")
    if vendor is None or product is None or isinstance(model, bool) or not isinstance(model, int):
        return None
    if not 0 <= model <= _MODEL_MAX:
        return None
    return Descriptor(vendor=vendor, product=product, model=model)


def descriptor_of_columns(vendor: str | None, product: str | None, model: int | None) -> Descriptor | None:
    """A stored row's descriptor - the three columns are all set or all NULL."""
    if vendor is None or product is None or model is None:
        return None
    return Descriptor(vendor=vendor, product=product, model=model)


def fallback_start_from(value: Any) -> datetime | None:
    """A tag's `fallback_start`: an instant, so a date-time with no offset is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def spell_instant(value: datetime) -> str:
    """An instant in UTC, as the tag and the CLI's `--fallback-start` both take it."""
    return value.astimezone(UTC).isoformat()


@functools.cache
def _installed() -> tuple[str, str] | None:
    """The binary's path and its `--version`, or `None` where this host has no working one.

    Asked once per process: the image either carries the binary or it does not.
    """
    path = shutil.which(CLI)
    if path is None:
        return None
    try:
        result = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=TIMEOUT_SECONDS, env={}, check=False
        )
    except OSError, subprocess.TimeoutExpired:
        logger.exception("`%s --version` did not run", path)
        return None
    version = result.stdout.strip()
    if result.returncode != 0 or not version:
        logger.error("`%s --version` exited %s and printed %r", path, result.returncode, version)
        return None
    return path, version


def cli_version() -> str | None:
    """`divejson-raw --version`, which a profile it fed records as its reader's version."""
    installed = _installed()
    return None if installed is None else installed[1]


def available() -> bool:
    """Whether this process can read a tagged original."""
    return _installed() is not None


def convert(
    content: bytes, *, descriptor: Descriptor, fallback_start: datetime | None, exported_at: datetime
) -> dict[str, Any]:
    """One buffer as the DiveJSON document `divejson-raw` writes of it.

    Raises `OriginalReadError` for every way the read can fail; the caller decides what an
    unreadable file means.
    """
    installed = _installed()
    if installed is None:
        raise OriginalReadError(f"this server has no `{CLI}` to read it with")
    path, _ = installed
    # `--option=value` throughout, so a vendor or product spelled with a leading `-` is a value.
    arguments = [
        f"--vendor={descriptor.vendor}",
        f"--product={descriptor.product}",
        f"--model={descriptor.model}",
        f"--exported-at={spell_instant(exported_at)}",
    ]
    if fallback_start is not None:
        arguments.append(f"--fallback-start={spell_instant(fallback_start)}")
    argv = [sys.executable, "-I", "-S", "-c", _WRAPPER, str(MEMORY_CEILING_BYTES), path, *arguments, "-"]
    try:
        result = subprocess.run(
            argv, input=content, capture_output=True, timeout=TIMEOUT_SECONDS, env={}, check=False, close_fds=True
        )
    except subprocess.TimeoutExpired as exc:
        raise OriginalReadError(f"`{CLI}` did not finish within {TIMEOUT_SECONDS} seconds") from exc
    except OSError as exc:
        raise OriginalReadError(f"`{CLI}` could not be started: {exc}") from exc

    if result.returncode < 0:
        raise OriginalReadError(f"`{CLI}` was ended by signal {-result.returncode}")
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise OriginalReadError(message[0] if message else f"`{CLI}` exited {result.returncode}")
    try:
        document = json.loads(result.stdout)
    except ValueError as exc:
        raise OriginalReadError(f"`{CLI}` wrote something that is not JSON") from exc
    if not isinstance(document, dict):
        raise OriginalReadError(f"`{CLI}` wrote JSON that is not a document")
    return document
