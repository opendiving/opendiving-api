"""Tests for the archive writer and its member layout (`services/export/{archive,paths}.py`).

The load-bearing one is the inventory: every stored blob appears exactly once, under a
path `export.json` names, with bytes that hash to the digest the database recorded. That
is the whole promise of "download everything" - a member that silently went missing, or
one whose bytes were mangled on the way through the zip, is the failure that makes the
feature worthless and the one a diver would only find years later.

The rest is about names. `original_filename` is diver-supplied and dive numbers can
legitimately repeat, so path planning is where an archive turns into two members sharing
one name (most extractors resolve that to one file) or, worse, a member that writes
outside the directory it was extracted into.
"""

import hashlib
import io
import json
import zipfile
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.app.models.certification import Certification
from src.app.schemas.certification import CertificationFileInfo, CertificationSide
from src.app.schemas.dive import DiveFileInfo
from src.app.services.certification_files import LoadedCardFile
from src.app.services.dive_files import LoadedDiveFile
from src.app.services.export.archive import write_archive
from src.app.services.export.paths import archive_member_name, plan_archive_paths
from src.app.services.export.uddf import write_uddf
from tests.helpers.export import EXPORTED_AT, UUIDS, build_bundle, full_bundle, make_dive

DIVE_FILE_BYTES = b'{"DeviceLog": {"Header": {}}}'
CARD_FRONT_BYTES = b"\xff\xd8\xff\xe0front"
CARD_BACK_BYTES = b"\x89PNG\r\n\x1a\nback"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _install_blob_loaders(monkeypatch: Any) -> None:
    """Stand in for the two `deferred`-column readers the archive writer calls per row,
    plus the profile read both document writers make."""

    async def fake_load_dive_file(db: Any, *, dive_id: int) -> LoadedDiveFile:
        return LoadedDiveFile(
            data=DIVE_FILE_BYTES,
            content_type="application/json",
            original_filename="export.json",
            sha256=_digest(DIVE_FILE_BYTES),
        )

    async def fake_load_certification_file(db: Any, *, certification_id: int, side: Any) -> LoadedCardFile:
        data = CARD_FRONT_BYTES if side == CertificationSide.FRONT else CARD_BACK_BYTES
        return LoadedCardFile(data=data, content_type="image/jpeg", original_filename="card.jpg", sha256=_digest(data))

    async def fake_load_profile(db: Any, *, dive_id: int) -> None:
        return None

    monkeypatch.setattr("src.app.services.export.archive.load_dive_file", fake_load_dive_file)
    monkeypatch.setattr("src.app.services.export.archive.load_certification_file", fake_load_certification_file)
    monkeypatch.setattr("src.app.services.export.envelope.load_profile", fake_load_profile)
    monkeypatch.setattr("src.app.services.export.uddf.load_profile", fake_load_profile)


def _bundle_matching_the_stub_blobs() -> Any:
    """`full_bundle` pins placeholder digests; the inventory test needs the real ones so
    that "hashes to what the database recorded" is a claim about the round trip."""
    bundle = full_bundle()
    bundle.dive_file_sha256[2] = _digest(DIVE_FILE_BYTES)
    bundle.cert_file_sha256[(1, "front")] = _digest(CARD_FRONT_BYTES)
    bundle.cert_file_sha256[(1, "back")] = _digest(CARD_BACK_BYTES)
    return bundle


async def _build(bundle: Any, monkeypatch: Any) -> zipfile.ZipFile:
    _install_blob_loaders(monkeypatch)
    buffer = await write_archive(AsyncMock(), bundle, exported_at=EXPORTED_AT)
    try:
        return zipfile.ZipFile(io.BytesIO(buffer.read()))
    finally:
        buffer.close()


class TestInventory:
    @pytest.mark.asyncio
    async def test_it_holds_the_documents_and_the_full_csv_set(self, monkeypatch):
        archive = await _build(full_bundle(), monkeypatch)
        assert set(archive.namelist()) == {
            "export.json",
            "dives.uddf",
            "csv/dives.csv",
            "csv/mixtures.csv",
            "csv/trips.csv",
            "csv/dive-sites.csv",
            "csv/gear-items.csv",
            "csv/gear-service.csv",
            "csv/certifications.csv",
            "files/0002-Suunto-Ocean-2026-06-01.json",
            "certifications/open-water-diver-front.jpg",
            "certifications/open-water-diver-back.png",
        }

    @pytest.mark.asyncio
    async def test_every_stored_blob_appears_once_and_hashes_to_its_recorded_digest(self, monkeypatch):
        """Checked against the `sha256` the database stored - carried through
        `export.json` - rather than one computed here, so the assertion covers the whole
        round trip: column, metadata, member."""
        archive = await _build(_bundle_matching_the_stub_blobs(), monkeypatch)
        envelope = json.loads(archive.read("export.json"))

        stored = [dive["source_file"] for dive in envelope["dives"] if dive["source_file"]]
        stored += [file for cert in envelope["certifications"] for file in cert["files"]]
        assert len(stored) == 3

        members = archive.namelist()
        for entry in stored:
            assert members.count(entry["archive_path"]) == 1, entry["archive_path"]
            assert _digest(archive.read(entry["archive_path"])) == entry["sha256"], entry["archive_path"]

    @pytest.mark.asyncio
    async def test_no_blob_is_in_the_archive_that_export_json_does_not_name(self, monkeypatch):
        archive = await _build(_bundle_matching_the_stub_blobs(), monkeypatch)
        envelope = json.loads(archive.read("export.json"))
        named = {dive["source_file"]["archive_path"] for dive in envelope["dives"] if dive["source_file"]}
        named |= {file["archive_path"] for cert in envelope["certifications"] for file in cert["files"]}
        blobs = {name for name in archive.namelist() if name.startswith(("files/", "certifications/"))}
        assert blobs == named

    @pytest.mark.asyncio
    async def test_an_empty_logbook_still_produces_a_readable_archive(self, monkeypatch):
        archive = await _build(build_bundle(), monkeypatch)
        assert archive.testzip() is None
        assert "export.json" in archive.namelist()
        assert not [name for name in archive.namelist() if name.startswith(("files/", "certifications/"))]

    @pytest.mark.asyncio
    async def test_the_uddf_member_is_the_same_bytes_the_endpoint_serves(self, monkeypatch):
        _install_blob_loaders(monkeypatch)
        bundle = full_bundle()
        standalone = b"".join([chunk async for chunk in write_uddf(AsyncMock(), bundle, exported_at=EXPORTED_AT)])
        archive = await _build(bundle, monkeypatch)
        assert archive.read("dives.uddf") == standalone

    @pytest.mark.asyncio
    async def test_the_blob_directories_are_stored_not_deflated(self, monkeypatch):
        """FIT exports and JPEG cards are already compressed; deflating them burns CPU
        proportional to the whole archive to save nothing. The documents do deflate."""
        archive = await _build(full_bundle(), monkeypatch)
        blobs = [i for i in archive.infolist() if i.filename.startswith(("files/", "certifications/"))]
        assert blobs and all(info.compress_type == zipfile.ZIP_STORED for info in blobs)
        assert archive.getinfo("dives.uddf").compress_type == zipfile.ZIP_DEFLATED

    @pytest.mark.asyncio
    async def test_two_archives_of_an_unchanged_logbook_are_byte_identical(self, monkeypatch):
        """Member timestamps come from `exported_at`, not from `datetime.now()`."""
        _install_blob_loaders(monkeypatch)
        first = await write_archive(AsyncMock(), full_bundle(), exported_at=EXPORTED_AT)
        second = await write_archive(AsyncMock(), full_bundle(), exported_at=EXPORTED_AT)
        try:
            assert first.read() == second.read()
        finally:
            first.close()
            second.close()


def _file_info(original_filename: str) -> DiveFileInfo:
    return DiveFileInfo(
        uuid=UUIDS["dive-file"],
        original_filename=original_filename,
        content_type="application/xml",
        byte_size=10,
        parser_key="suunto_xml",
    )


class TestMemberNames:
    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ("../../.bashrc", "bashrc"),
            ("/etc/passwd", "passwd"),
            (r"C:\Users\ada\dive.xml", "dive.xml"),
            ("....//....//x.json", "x.json"),
            # A wholly non-ASCII stem folds to nothing, but the extension is split off
            # first so the member is still a JPEG to every tool downstream.
            ("潜水.jpg", "fallback.jpg"),
            ("café.xml", "cafe.xml"),
            ("", "fallback"),
            ("   ", "fallback"),
        ],
    )
    def test_a_diver_supplied_filename_becomes_one_safe_segment(self, stored, expected):
        """`original_filename` is whatever a dive computer or an upload said it was, and
        a member called `../../.bashrc` is a real archive that real extractors honour."""
        name = archive_member_name(stored, default="fallback")
        assert name == expected
        assert "/" not in name and "\\" not in name and not name.startswith(".")

    def test_two_dives_sharing_a_number_and_a_filename_get_distinct_paths(self):
        """Dive numbers legitimately repeat - `DiveNumberingSummary.duplicate_count`
        exists to count them - so nothing upstream guarantees this."""
        bundle = build_bundle(
            dives=[
                make_dive(1, UUIDS["dive-air"], dive_number=7),
                make_dive(2, UUIDS["dive-trimix"], dive_number=7),
            ],
            file_by_dive={1: _file_info("export.xml"), 2: _file_info("export.xml")},
        )
        assert plan_archive_paths(bundle).dive_files == {
            1: "files/0007-export.xml",
            2: "files/0007-export-2.xml",
        }

    def test_collisions_are_resolved_case_insensitively(self):
        """macOS and Windows extract onto case-insensitive filesystems, where `DIVE.XML`
        overwriting `dive.xml` loses a file just as surely as an exact duplicate."""
        bundle = build_bundle(
            dives=[
                make_dive(1, UUIDS["dive-air"], dive_number=7),
                make_dive(2, UUIDS["dive-trimix"], dive_number=7),
            ],
            file_by_dive={1: _file_info("dive.xml"), 2: _file_info("DIVE.XML")},
        )
        paths = plan_archive_paths(bundle)
        assert paths.dive_files[1].lower() != paths.dive_files[2].lower()

    def test_two_certifications_with_the_same_name_get_distinct_paths(self):
        bundle = full_bundle()
        duplicate = Certification(user_id=1, agency="ssi", name="Open Water Diver", notes="")
        duplicate.id = 2
        bundle.certifications.append(duplicate)
        bundle.cert_files_by_cert[2] = [
            CertificationFileInfo(
                uuid=UUIDS["card-front"],
                side=CertificationSide.FRONT,
                content_type="image/jpeg",
                byte_size=10,
                original_filename="card front.jpg",
            )
        ]
        paths = plan_archive_paths(bundle)
        assert paths.certification_files[(2, "front")] == "certifications/open-water-diver-front-2.jpg"

    def test_a_very_long_filename_is_trimmed_to_fit_a_path_component(self):
        """`original_filename` is `String(255)` before a dive number is prepended, and
        ext4/APFS/NTFS all cap one component at 255 bytes - over it an extractor errors or
        drops the member, which is the failure this module exists to prevent."""
        bundle = build_bundle(
            dives=[make_dive(1, UUIDS["dive-air"], dive_number=7)],
            file_by_dive={1: _file_info("x" * 250 + ".xml")},
        )
        member = plan_archive_paths(bundle).dive_files[1]
        assert len(member.removeprefix("files/")) <= 255
        assert member.endswith(".xml")

    def test_dive_numbers_are_zero_padded_so_the_directory_sorts(self):
        bundle = build_bundle(
            dives=[
                make_dive(1, UUIDS["dive-air"], dive_number=9),
                make_dive(2, UUIDS["dive-trimix"], dive_number=104),
            ],
            file_by_dive={1: _file_info("export.xml"), 2: _file_info("export.xml")},
        )
        assert list(plan_archive_paths(bundle).dive_files.values()) == [
            "files/0009-export.xml",
            "files/0104-export.xml",
        ]
