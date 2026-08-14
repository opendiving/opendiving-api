"""Where each stored binary lands inside the archive.

Its own module because two writers have to agree on the answer and neither may import
the other: `archive.py` writes the members, and `envelope.py` records the same paths in
`export.json` so the metadata beside a `sha256` says which file it describes.

The paths are planned **up front, for the whole archive at once**, because uniqueness is
not a property of any single file. Dive numbers can legitimately repeat (that is what
`DiveNumberingSummary.duplicate_count` counts), two certifications can share a name, and
`original_filename` is whatever the diver's dive computer wrote - so two members can
collide even though nothing in the database does. A zip with two entries of the same name
is a valid archive that most extractors silently resolve to one file, which is the worst
of the available failure modes.
"""

import posixpath
import re
import unicodedata
from dataclasses import dataclass, field

from .loader import ExportBundle

DIVE_FILE_DIRECTORY = "files"
CERTIFICATION_DIRECTORY = "certifications"


def archive_member_name(name: str, *, default: str) -> str:
    """Reduce a stored filename to something safe as one path segment inside the zip.

    This *is* path-traversal defence, unlike `core/utils/uploads.py::safe_filename`
    which only has a header to protect: `original_filename` is diver-supplied, and a
    member called `../../.bashrc` is a real archive that real extractors have honoured.
    Directory separators and leading dots are removed rather than escaped, and the
    result is always a single non-empty segment.

    Non-ASCII is folded away too. Zip's UTF-8 filename flag is widely but not
    universally honoured, and a c-card scan named in Thai should still extract to
    *something* on a tool that reads the name as cp437.
    """
    stem = name.replace("\\", "/").rsplit("/", 1)[-1]
    folded = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", folded).strip("-.")
    return cleaned or default


def _slug(value: str, *, default: str) -> str:
    cleaned = re.sub(
        r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().lower()
    )
    return cleaned.strip("-") or default


@dataclass(slots=True)
class ArchivePaths:
    """Every stored binary's path inside the archive, keyed the way it is addressed.

    A dive has at most one export and a certification at most one image per side, so
    these two maps between them name every member the archive carries beyond the four
    generated documents.
    """

    dive_files: dict[int, str] = field(default_factory=dict)
    certification_files: dict[tuple[int, str], str] = field(default_factory=dict)


def _claim(taken: set[str], directory: str, stem: str, suffix: str) -> str:
    """`<directory>/<stem><suffix>`, with a counter appended until it is unique.

    Compared case-insensitively: the archive is extracted on macOS and Windows as often
    as on Linux, and `Dive.XML` overwriting `dive.xml` there would be the same lost file
    as an exact duplicate here.
    """
    candidate = posixpath.join(directory, f"{stem}{suffix}")
    counter = 2
    while candidate.lower() in taken:
        candidate = posixpath.join(directory, f"{stem}-{counter}{suffix}")
        counter += 1
    taken.add(candidate.lower())
    return candidate


def plan_archive_paths(bundle: ExportBundle) -> ArchivePaths:
    """Assign every stored file a unique path, in the bundle's own (stable) order."""
    paths = ArchivePaths()
    taken: set[str] = set()

    for dive in bundle.dives:
        info = bundle.file_by_dive[dive.id]
        if info is None:
            continue
        name = archive_member_name(info.original_filename, default="dive-file")
        stem, extension = posixpath.splitext(name)
        paths.dive_files[dive.id] = _claim(taken, DIVE_FILE_DIRECTORY, f"{dive.dive_number:04d}-{stem}", extension)

    for certification in bundle.certifications:
        for file_info in bundle.cert_files_by_cert.get(certification.id, []):
            name = archive_member_name(file_info.original_filename, default="card")
            _, extension = posixpath.splitext(name)
            stem = f"{_slug(certification.name, default='certification')}-{file_info.side.value}"
            paths.certification_files[(certification.id, file_info.side.value)] = _claim(
                taken, CERTIFICATION_DIRECTORY, stem, extension
            )

    return paths
