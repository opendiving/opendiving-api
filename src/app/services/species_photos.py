"""One Wikimedia Commons photograph per species: which one, what it is credited as, how it
is stored, and how it is read back.

The **only** module that knows a `Species` row has a picture at all. It sits beside
`services/user_pictures.py` at the same layer - that one knows what is stored against a
`user` row, this one against a `species` row, and `services/blob_store.py` knows where and
how for both - and it follows the same ordering rule: write the file, then commit the row.

`services/species_service.py` owns everything that leaves this server. It asks Wikidata and
Commons, hands the answers here, and stores what comes back. The split is the one the two
modules' names describe: outbound there, decisions and bytes here.

**What is stored is a scaled copy of a Commons file and nothing else.** Never cropped,
never overlaid, never composited - which is why this does not reuse `user_pictures._normalize`
however similar the decode fencing looks. That is a licence property rather than an aesthetic
one: 24 of 40 sampled files are ShareAlike, and while displaying and scaling an image is not
adaptation, cropping and compositing move toward it. Any square-card presentation is the
browser's business at render time.

Pillow parses untrusted bytes here exactly as it does next door, so the decode is fenced the
same four ways: a `formats` allowlist, a cap on the bytes read, a cap on the pixels the header
*claims*, and a cap on what will actually be rasterized. See `user_pictures` for why the last
two are different questions.
"""

import hashlib
import io
import logging
import re
import uuid as uuid_pkg
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import anyio.to_thread
from PIL import Image, ImageOps
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.db.database import release_read_transaction
from ..models.species import Species
from . import blob_store

logger = logging.getLogger(__name__)

# The key prefix every species photo is stored under, one kind on the volume beside
# `dive-files`, `certification-files`, `user-avatars` and `user-portraits`. See
# `blob_store.new_key`, and `src/scripts/sweep_orphaned_files.py`, which has to know about every
# one of them.
KEY_KIND = "species-photos"

# **Commons thumbnail widths are bucketed, not arbitrary**, which is the one thing about this
# API that is easy to get wrong quietly. The served buckets are 120/250/330/500/960; a request
# for an off-bucket width is refused outright (a hand-built `321px-...` URL answers HTTP 400),
# and `imageinfo`'s own `thumbwidth` reports the width that was *asked for* rather than the one
# served, so arithmetic based on it is wrong by up to ten pixels. 500 is asked for here and 500
# is what comes back, because it is a bucket.
#
# One stored width, not two. 500 is the smallest bucket that does not look soft as a species
# page's lead image, and a list renders the same file downscaled. Storing 330 for lists and 960
# for the page halves list bandwidth and doubles both storage and fetch count, and there is no
# evidence yet that list bandwidth is a problem.
COMMONS_THUMBNAIL_WIDTH = 500

# The hosts this server will fetch image bytes from. Hard-coded on purpose while the API
# endpoint beside it is a setting: this is an SSRF fence, and a fence with an environment
# variable in front of it is not a fence.
#
# **Two names, because one `imageinfo` reply carries two hosts.** Commons answers an
# `iiurlwidth` request with a `thumburl` on `thumb.wikimedia.org` and the full-size `url` on
# `upload.wikimedia.org`, and `_commons_imageinfo` prefers the thumbnail - so a fence holding
# only the second refuses every thumbnail there is, which is a feature that fetches nothing at
# all rather than one that fetches badly. `thumb.wikimedia.org` presents a certificate whose
# SANs include `*.wikimedia.org`, the same wildcard family covering `upload.wikimedia.org`; it
# is Wikimedia's own infrastructure and not a redirect target.
#
# **It stays a set of exact names and does not become a `*.wikimedia.org` suffix match**, which
# would survive Wikimedia moving the host a third time and was refused anyway. A suffix test is
# a pattern, and a subdomain-matching bug in a pattern is an SSRF hole; a name that stops
# resolving is a feature that visibly stops working. Adding a third entry here is a deliberate
# one-line change, which is the property being bought.
PHOTO_BYTE_HOSTS = frozenset({"upload.wikimedia.org", "thumb.wikimedia.org"})

# What the fetch will read before giving up. A 500 px-wide JPEG is tens of kilobytes; this is
# an order of magnitude of headroom over anything Commons serves at that width, and it bounds
# what one resolve buffers.
MAX_PHOTO_DOWNLOAD_BYTES = 4 * 1024 * 1024

PHOTO_CONTENT_TYPE = "image/webp"

# Only these parsers are ever invoked, for the reason `user_pictures.ALLOWED_FORMATS` gives:
# Pillow ships dozens, several with a CVE history, and `formats=` is what keeps them
# unreachable from an anonymous byte string. Commons serves its thumbnails as JPEG or PNG.
ALLOWED_FORMATS = ["JPEG", "PNG", "WEBP", "GIF"]

# The header's claim, checked before any pixel is decoded - the bomb check, same 50 MP figure
# and same reasoning as `user_pictures.MAX_PICTURE_PIXELS`.
MAX_PHOTO_PIXELS = 50_000_000

# What will actually be rasterized, which is the question that governs memory. Far below the
# avatar's cap because the input is far more constrained: this is a 500 px-wide thumbnail
# Commons rendered, so 2 MP covers an 8:1 panorama at that width with room to spare, and
# anything above it is not a thumbnail of the file we asked for.
MAX_PHOTO_DECODE_PIXELS = 2_000_000

# One photo decoded at a time per worker, for the reason `user_pictures._DECODE_LIMITER` gives:
# without it the ceiling is the app's own 100-token threadpool, and a hundred concurrent
# decodes is not a number a 1 GB install survives whatever the per-decode figure is.
_DECODE_LIMITER = anyio.CapacityLimiter(1)

# 85 and method 6 are the avatar encoder's settings and are right here for the same reasons:
# quality 85 is visually lossless at this size, and libwebp's slowest search costs tens of
# milliseconds once per species and is repaid on every render forever after.
_WEBP_QUALITY = 85
_WEBP_METHOD = 6

# Commons file titles that are not raster photographs. An **allowlist**, because the failure
# direction matters: an unrecognized extension refused costs one species its photo, while an
# unrecognized extension accepted puts a diagram, a video still or a scanned monograph plate
# in a slot whose entire claim is "this is what the animal looks like". Wikidata's P18 really
# does carry these - the green turtle's item offers `202304 Green turtle.svg`, a vector
# distribution diagram, beside real photographs.
_PHOTOGRAPH_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".webp")

# Column widths from `models/species.py`, applied by truncating rather than by refusing: a
# photograph with a hundred-author credit line is still the right photograph.
_FILE_MAX_LENGTH = 255
_AUTHOR_MAX_LENGTH = 255
_LICENSE_MAX_LENGTH = 128
_URL_MAX_LENGTH = 512


class UnsupportedPhotoImageError(Exception):
    """The fetched bytes are not an image this can turn into a species photo."""


@dataclass(frozen=True, slots=True)
class ImageCandidate:
    """One P18 value on a Wikidata item: the Commons file title, and the statement's rank.

    The rank is carried rather than resolved away because the selection rule needs it twice:
    `deprecated` is Wikidata's own marker for a value known to be wrong - precisely the
    wrong-animal case the rule exists to refuse - and `preferred` is its marker for the one to
    use when several are true.
    """

    file: str
    rank: str


@dataclass(frozen=True, slots=True)
class PhotoCredit:
    """What a compliant credit line is built from, as parts rather than as one string.

    Every field is independently nullable: measured across a 40-file sample `descriptionurl`
    was present on all forty and `Artist` was absent from one, so "we know the licence and the
    source but not the author" is a real state.
    """

    author: str | None
    license_name: str | None
    license_url: str | None
    source_url: str | None


@dataclass(frozen=True, slots=True)
class FetchedPhoto:
    """A photo that has been chosen, fetched and normalized, but not yet stored.

    Carries the bytes rather than a key: nothing is written to the volume until
    `save_photo_attempt` writes it, so a caller that gives up part way leaves nothing behind.
    """

    data: bytes
    sha256: str
    file: str
    credit: PhotoCredit


@dataclass(frozen=True, slots=True)
class StoredSpeciesPhoto:
    """Where a species' photo is and which version it is. No bytes - see `read_photo_bytes`,
    which the 304 path deliberately never reaches."""

    storage_key: str
    sha256: str


# -------------- choosing the photo --------------


def photograph_candidates(images: tuple[ImageCandidate, ...]) -> list[ImageCandidate]:
    """Step 1 of the selection rule: the P18 values that could be a photograph of the taxon.

    Drops `deprecated` statements and anything that is not a raster image by file extension.
    Kept separate from `choose_photo_file` because the caller has to tell two refusals apart:
    an item with **no** candidates at all is what the synonym retry exists for, while an item
    whose candidates could not be told apart is a deliberate no-photo that no retry may
    overturn. Collapsing them would hand *Triaenodon obesus* a photo from a synonym's item
    after this rule had just refused its own for naming a different shark.
    """
    return [
        candidate
        for candidate in images
        if candidate.rank != "deprecated" and candidate.file.casefold().endswith(_PHOTOGRAPH_SUFFIXES)
    ]


def choose_photo_file(candidates: list[ImageCandidate], *, taxon_name: str | None) -> str | None:
    """Steps 2-6: the one Commons file to use, or `None` to show no photo at all.

    **The rule is a sequence and every step earns its place**, which had to be measured rather
    than assumed. Across a 42-species diver-realistic sample, 10 items carry more than one P18
    and the extra value is sometimes a different species - a great hammerhead on the zebra
    shark's item, a silvertip on the whitetip reef shark's - so "take the first" is a coin flip
    that shows divers the wrong animal. Wikidata's own mechanism for marking the bad value bad
    is barely used on these taxa: only 2 of those 10 rank a statement `preferred`, and
    `deprecated` appears nowhere in the sample. A rule that leans on rank alone therefore throws
    away good photos in bulk, which is why steps 4 and 5 read the file titles instead.

    In order, after `photograph_candidates` has run:

    2. One candidate left - use it.
    3. Exactly one ranked `preferred` - use it.
    4. Keep only candidates whose **file title contains the taxon name or its specific
       epithet**, case-insensitively.
    5. If **any** survive, use one: those whose title *begins* with the taxon name first, then
       the first in statement order.
    6. None survive - no photo.

    **`taxon_name` is the P225 of the item being examined**, not the accepted name this
    instance stores, and the difference decides the flagship case. `Q169468`'s P225 is
    *Stegostoma fasciatum* while the stored name is *Stegostoma tigrinum*, so matching against
    the stored name would keep neither candidate and the zebra shark would silently lose the
    photo this whole retry path exists to reach.

    **Step 5 is not decoration.** Stopping at "if exactly one survives step 4, use it" yields
    33 of 42 rather than 39, because in six of the eight remaining ties *both* candidates name
    the taxon and so neither is uniquely selected - red lionfish, Clark's anemonefish, blacktip
    reef shark, blue dragon, *Aplysina archeri* and *Zenopontonia rex* all lose their photo. It
    is safe precisely because step 4 has already run: every candidate reaching it names the
    taxon, so choosing between them cannot pick a different animal, which is the only thing
    forbidden here. It also picks better - the single-animal *Zenopontonia rex* photo over the
    frame containing two, and the plain binomial title over an incidental one.

    Step 4 is a substring test, and the residual is named rather than defended away: a short
    epithet can appear inside an unrelated word. It is tolerable because step 4 is a filter and
    not the whole safety argument - every candidate reaching it is already a curated lead image
    on *this taxon's own* Wikidata item, so the worst a coincidental match costs is the weaker
    of two photographs of the right animal.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0].file

    preferred = [candidate for candidate in candidates if candidate.rank == "preferred"]
    if len(preferred) == 1:
        return preferred[0].file

    if taxon_name is None:
        # No P225 to match against, so step 4 can keep nothing and step 6 is the answer. An
        # item with an AphiaID and no taxon name is one this app already refuses to build a
        # search row from; it is no better a basis for choosing between two photographs.
        return None

    names = _taxon_match_terms(taxon_name)
    named = [candidate for candidate in candidates if _title_contains_any(candidate.file, names)]
    if not named:
        return None

    binomial = taxon_name.casefold()
    for candidate in named:
        if _normalized_title(candidate.file).startswith(binomial):
            return candidate.file
    return named[0].file


def _normalized_title(file: str) -> str:
    """A Commons file title as it compares: underscores are the URL spelling of a space, and
    the two forms are interchangeable in a title, so a rule that matched only one would depend
    on which spelling a P18 statement happened to carry."""
    return file.replace("_", " ").casefold()


def _taxon_match_terms(taxon_name: str) -> tuple[str, ...]:
    """The taxon's own name and its specific epithet, casefolded.

    The epithet is the last word of the binomial, which is also the whole name for a
    genus-rank or higher taxon - "a moray eel" is a legal sighting here, so this must not
    assume two words. Deduplicated, so a one-word name is one term rather than two identical
    ones.
    """
    folded = taxon_name.casefold()
    epithet = folded.split()[-1] if folded.split() else folded
    return (folded,) if epithet == folded else (folded, epithet)


def _title_contains_any(file: str, terms: tuple[str, ...]) -> bool:
    title = _normalized_title(file)
    return any(term in title for term in terms)


# -------------- the credit --------------


class _TextExtractor(HTMLParser):
    """Collects the visible text of a Commons `extmetadata` HTML fragment.

    **A parser rather than a regex, and the obvious reason is not the reason.** A tag-shaped
    strip does not leak a `title` tooltip into the name - checked against the clownfish file,
    `re.sub(r'<[^>]+>', '', artist)` yields exactly `Raimond Spekking`. What it leaves is
    `&amp;` and `&#39;` sitting in the name, and adjacent elements glued together, so a
    two-author value comes back as one run-on string.

    Both are fixed here by construction: `convert_charrefs` resolves the entities, and every
    tag is a soft boundary, so `<a>A</a><a>B</a>` becomes "A B" rather than "AB" while
    `<bdi>Raimond <b>Spekking</b></bdi>` still collapses back to "Raimond Spekking". This is
    the common path and not a fallback - 36 of 40 sampled files carry HTML in `Artist`.
    """

    _SKIP_CONTENT = frozenset({"script", "style"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._runs: list[str] = []
        self._suppressed = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_CONTENT:
            self._suppressed += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_CONTENT and self._suppressed:
            self._suppressed -= 1

    def handle_data(self, data: str) -> None:
        if not self._suppressed:
            self._runs.append(data)

    @property
    def text(self) -> str:
        return " ".join(self._runs)


def plain_text(value: Any, limit: int = _AUTHOR_MAX_LENGTH) -> str | None:
    """The visible text of an `extmetadata` value, bounded - or `None` for nothing usable.

    Everything Commons returns in these fields is a maybe-string and most of them are HTML, so
    this is the one door they come through. **Never interpolate any of this into markup as
    HTML**: it is plain text by the time it leaves here, and putting it back into a template
    unescaped would reinstate exactly the injection this exists to close.
    """
    if not isinstance(value, str):
        return None
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    # Collapse the whitespace the soft tag boundaries introduce, then pull spaces back off the
    # punctuation they landed in front of - `<a>Foo</a>, <a>Bar</a>` would otherwise read
    # "Foo , Bar".
    text = re.sub(r"\s+([,;.])", r"\1", " ".join(parser.text.split()))
    return text.strip()[:limit] or None


def _metadata_value(metadata: Any, key: str) -> Any:
    """One `extmetadata` entry's `value`. The keys are CamelCase and each maps to an object
    carrying `value` and `source`, so a reader that takes the entry itself gets a dict."""
    if not isinstance(metadata, dict):
        return None
    entry = metadata.get(key)
    return entry.get("value") if isinstance(entry, dict) else None


def credit_from_imageinfo(info: Any) -> PhotoCredit:
    """The credit parts for one Commons file, from one `imageinfo` entry.

    Reads both levels of that entry, which is why it takes the whole thing: `extmetadata`
    carries the author and the licence under CamelCase keys, while the source - the file's own
    description page - is `descriptionurl` beside it. That was present for 40 of 40 sampled
    files, so the "source" element every one of these licences asks for is always available.

    **`Attribution` wins where it exists**, because it is the credit the uploader asked for
    verbatim; it is present on only 7 of 40 sampled files, so composing from `Artist` is the
    common path rather than the fallback. Both land in the same slot: there is one text field
    here, and the licence and source travel as their own linkable fields either way.

    Where neither is present - one file in 40 - the credit is licence and source only, which
    was checked to be acceptable for that file rather than assumed: its `AttributionRequired`
    is false. This does not refuse such a file, because a Commons file is free-licensed by
    policy and a missing author field is a gap in the metadata rather than in the permission.
    """
    metadata = info.get("extmetadata") if isinstance(info, dict) else None
    author = plain_text(_metadata_value(metadata, "Attribution")) or plain_text(_metadata_value(metadata, "Artist"))
    return PhotoCredit(
        author=author,
        license_name=plain_text(_metadata_value(metadata, "LicenseShortName"), _LICENSE_MAX_LENGTH),
        license_url=_https_url(_metadata_value(metadata, "LicenseUrl")),
        source_url=_https_url(info.get("descriptionurl") if isinstance(info, dict) else None),
    )


def _https_url(value: Any) -> str | None:
    """An `https` URL, bounded - or `None` for anything else.

    These are rendered as links by every client, so a `javascript:` or `data:` value arriving
    in a third party's metadata must not reach one. Structural rather than a rule each client
    has to remember.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()[:_URL_MAX_LENGTH]
    return candidate if candidate.lower().startswith("https://") else None


def is_photo_byte_source(url: str) -> bool:
    """Whether this is a URL the server is willing to fetch image bytes from.

    The URL comes out of a Commons API response rather than from a caller, so it is not
    attacker-supplied in the ordinary sense. This is the second fence anyway, because the
    first one's failure mode is server-side request forgery: an `imageinfo` reply that could
    name `http://169.254.169.254/...` and be fetched from inside the network is the entire
    class of bug. Same shape and same reasoning as `user_pictures._is_google_avatar_url`.

    **Membership in `PHOTO_BYTE_HOSTS`, comparing the whole hostname.** That it holds two names
    rather than one makes it no less an exact-match allowlist: `upload.wikimedia.org.evil.example`
    and `evil.example/thumb.wikimedia.org` are refused by the same comparison that admits the
    real ones, and so is every other `*.wikimedia.org` host, none of which serves file bytes.
    """
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "").lower() in PHOTO_BYTE_HOSTS


# -------------- storing --------------


def _normalize(data: bytes) -> bytes:
    """Decode, orient and re-encode as WebP, at the size Commons served. Runs in a thread.

    **No crop and no resize**, which is the whole difference from the avatar pipeline next
    door: what is stored has to remain a scaled copy of the Commons file, so the only two
    things done to it are the ones that are not adaptations - reading the EXIF orientation the
    file itself declares, and re-encoding. The re-encode also strips the metadata, which is
    ordinary hygiene rather than a licence matter here.

    The three rejections are `user_pictures._normalize`'s three, for its reasons: Pillow raises
    `DecompressionBombError` from inside `Image.open` for the very largest inputs, so the open
    sits inside the `try`; `MAX_PHOTO_PIXELS` judges what the header claims; and
    `MAX_PHOTO_DECODE_PIXELS` judges what will actually be rasterized. There is no `draft`
    call between them, because there is no target size to draft toward - this keeps what
    Commons sent.
    """
    try:
        with Image.open(io.BytesIO(data), formats=ALLOWED_FORMATS) as image:
            width, height = image.size
            if width * height > MAX_PHOTO_PIXELS:
                raise UnsupportedPhotoImageError(f"That image describes too many pixels ({width}x{height}).")
            if width * height > MAX_PHOTO_DECODE_PIXELS:
                raise UnsupportedPhotoImageError(f"That image is too large to process ({width}x{height}).")

            ImageOps.exif_transpose(image, in_place=True)

            # Alpha survives rather than being composited onto an invented background, for the
            # reason it does for avatars: the clients draw these on surfaces of several
            # colours, and inventing one would be the compositing this must not do.
            has_alpha = image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info)
            target_mode = "RGBA" if has_alpha else "RGB"
            converted = image if image.mode == target_mode else image.convert(target_mode)

            out = io.BytesIO()
            converted.save(out, format="WEBP", quality=_WEBP_QUALITY, method=_WEBP_METHOD)
            return out.getvalue()
    except Image.DecompressionBombError as exc:
        raise UnsupportedPhotoImageError("That image describes far too many pixels to be a species photo.") from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise UnsupportedPhotoImageError("The fetched bytes are not a decodable image.") from exc


async def process_photo(data: bytes) -> bytes:
    """`_normalize`, off the event loop and behind `_DECODE_LIMITER`."""
    if not data:
        raise UnsupportedPhotoImageError("The fetched file is empty.")
    return await anyio.to_thread.run_sync(_normalize, data, limiter=_DECODE_LIMITER)


def fetched_photo(*, data: bytes, file: str, credit: PhotoCredit) -> FetchedPhoto:
    """Bundle normalized bytes with the credit and the file title they came from."""
    return FetchedPhoto(
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
        file=file[:_FILE_MAX_LENGTH],
        credit=credit,
    )


async def save_photo_attempt(db: AsyncSession, *, species_id: int, photo: FetchedPhoto | None) -> None:
    """Record what one photo attempt produced, whether or not it produced a photo.

    **`photo_fetched_at` is stamped either way, and that is the point rather than tidiness.**
    "No photo" is the permanent outcome for most of the catalog - two of 42 refused by design
    in the sample, and 88.3% of Wikidata items carrying a WoRMS id have no P18 at all - so a
    backfill predicate keyed on the absence of stored *bytes* would never shrink and every
    re-run would re-query Wikidata and Commons for the entire photo-less tail forever. Stamping
    a failed attempt is what makes the second run report zero.

    File first, row second, replaced file unlinked after the commit: the ordering rule
    `user_pictures` states, for its reason. A crash between the first two strands an
    unreferenced file, which the sweeper reclaims; the reverse order would leave a committed
    row naming bytes that do not exist.

    Re-attempting a species that already has a photo replaces it. Only the backfill's `--force`
    reaches that today, and it must not leave the old blob behind.
    """
    now = datetime.now(UTC)
    if photo is None:
        await db.execute(update(Species).where(Species.id == species_id).values(photo_fetched_at=now))
        await db.commit()
        return

    existing_key = (
        await db.execute(select(Species.photo_storage_key).where(Species.id == species_id))
    ).scalar_one_or_none()

    key = blob_store.new_key(KEY_KIND, sha256=photo.sha256)
    # The select above autobegan a transaction that would otherwise sit idle across a
    # threadpool write with an `fsync` in it. It returned a bare `str | None`, so there is no
    # live ORM entity for the rollback to expire.
    await release_read_transaction(db)
    await blob_store.put(key, photo.data)

    await db.execute(
        update(Species)
        .where(Species.id == species_id)
        .values(
            photo_storage_key=key,
            photo_sha256=photo.sha256,
            photo_file=photo.file,
            photo_author=photo.credit.author,
            photo_license=photo.credit.license_name,
            photo_license_url=photo.credit.license_url,
            photo_source_url=photo.credit.source_url,
            photo_fetched_at=now,
        )
    )
    if existing_key is not None and existing_key != key:
        blob_store.delete_after_commit(db, existing_key)
    await db.commit()


async def get_stored_photo(db: AsyncSession, *, species_uuid: uuid_pkg.UUID) -> StoredSpeciesPhoto | None:
    """Where a species' photo is and which version it is, or `None` if it has none.

    One narrow indexed read answering both questions the download route asks: the digest
    settles `If-None-Match` - a 304 stops there, having touched no bytes - and the key is what
    the bytes are then read from. A species that does not exist and one with no photo are the
    same answer here on purpose; the route's 404 says "no photo at this uuid" and nothing more,
    which is all an unauthenticated caller is owed about a global catalog.
    """
    row = (
        await db.execute(select(Species.photo_storage_key, Species.photo_sha256).where(Species.uuid == species_uuid))
    ).one_or_none()
    if row is None or row.photo_storage_key is None or row.photo_sha256 is None:
        return None
    return StoredSpeciesPhoto(storage_key=row.photo_storage_key, sha256=row.photo_sha256)


async def read_photo_bytes(stored: StoredSpeciesPhoto) -> bytes:
    """The bytes `stored` names. Raises `blob_store.BlobMissingError` if they are gone.

    Deliberately not folded into `get_stored_photo`: the conditional-request path must answer
    without reading a file, and a row naming absent bytes is data loss rather than a 404.
    """
    return await blob_store.get(stored.storage_key)
