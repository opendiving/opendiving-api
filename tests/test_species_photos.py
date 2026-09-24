"""Species photos: choosing one, crediting it, storing it, serving it - and above all not
letting any of that reach `POST /species/resolve`.

**The invariant this module exists for is the last one.** `resolve_species` is the only route
by which a species enters the catalog at all, so a diver who cannot resolve cannot add a
species to a dive. The photo work runs after that endpoint's enrichment task group, in a
timeout scope of its own, because `anyio.move_on_after` cancels the whole group it wraps and a
slow Commons sharing that scope would cancel the synonym walk beside it - turning a photo
provider's bad day into a 503. `TestCommonsCannotFailAResolve` is that written down, and it is
the one class here whose absence would be invisible until an outage.

**The selection rule is the other half, and it fails silently rather than loudly.** Wikidata's
P18 is multi-valued and the extra value is sometimes a photograph of a different species - a
great hammerhead on the zebra shark's item, a silvertip on the whitetip reef shark's - so a
wrong rule does not error, it shows divers the wrong animal or quietly drops a photo. Every
case in `TestChoosingThePhoto` is drawn from the live 42-species sample the rule's coverage
figures rest on, so a rule that regresses fails against a shape that really exists rather than
one invented to suit the test.

The credit tests are here for a narrower reason: `Artist` is HTML on 36 of 40 sampled files, so
parsing it is the common path rather than a fallback, and what a regex leaves behind is not the
obvious thing - it is entity escapes sitting in the name and adjacent authors glued together.
"""

import io
from collections.abc import Callable, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api import router as api_router
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.setup import create_application
from src.app.models.species import Species
from src.app.services import blob_store, species_photos, species_service
from src.app.services.species_photos import (
    ImageCandidate,
    PhotoCredit,
    choose_photo_file,
    credit_from_imageinfo,
    is_photo_byte_source,
    photograph_candidates,
    plain_text,
)
from tests.conftest import db_available
from tests.helpers.generators import create_species
from tests.helpers.images import large_jpeg, plain_png, png_with_alpha

_REAL_ASYNC_CLIENT = httpx.AsyncClient

CURRENT_USER = {"id": 7, "uuid": uuid7(), "username": "ada", "is_superuser": False}

# Copied verbatim from the avatar and card responses. `frame-ancestors` is the load-bearing
# clause: a response that sets its own policy opts out of `SecurityHeadersMiddleware`'s
# default, so a shorter CSP here would silently make the photo framable.
EXPECTED_CSP = "default-src 'none'; sandbox; frame-ancestors 'none'"

# A fixed instant for the rows a test stamps by hand, so nothing here depends on when it ran.
_NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)


def _candidates(*files: str | tuple[str, str]) -> list[ImageCandidate]:
    """P18 values as they reach the rule: a bare title is `normal`-ranked."""
    return [
        ImageCandidate(file=entry, rank="normal")
        if isinstance(entry, str)
        else ImageCandidate(file=entry[0], rank=entry[1])
        for entry in files
    ]


# -------------- the selection rule --------------


class TestWhatCountsAsAPhotograph:
    """Step 1: deprecated statements and non-raster files, dropped before anything is chosen.

    Kept separate from the choosing below because the caller reads the two answers
    differently - an item with no candidates is what the synonym retry is for, an item whose
    candidates were refused must never be retried.
    """

    def test_a_deprecated_statement_is_dropped(self) -> None:
        """Wikidata's own marker for a value the community has ruled wrong, which is exactly
        the wrong-animal case this rule exists to refuse. The sample contains none at all,
        which is precisely why nothing else would ever exercise this."""
        candidates = photograph_candidates(
            (ImageCandidate("Wrong animal.jpg", "deprecated"), ImageCandidate("Right animal.jpg", "normal"))
        )

        assert [candidate.file for candidate in candidates] == ["Right animal.jpg"]

    def test_a_vector_diagram_is_dropped(self) -> None:
        """The green turtle's item (Q199458) really carries `202304 Green turtle.svg`, a
        distribution diagram, alongside photographs."""
        candidates = photograph_candidates(
            (ImageCandidate("202304 Green turtle.svg", "normal"), ImageCandidate("Chelonia mydas.jpg", "normal"))
        )

        assert [candidate.file for candidate in candidates] == ["Chelonia mydas.jpg"]

    @pytest.mark.parametrize("suffix", [".pdf", ".djvu", ".ogv", ".webm", ".stl", ".xcf"])
    def test_other_non_raster_media_are_dropped_too(self, suffix: str) -> None:
        """An allowlist rather than a blocklist, so an unfamiliar Commons format is refused
        rather than rendered. The cost of being wrong in this direction is one missing photo;
        in the other it is a video still or a monograph plate presented as the animal."""
        assert photograph_candidates((ImageCandidate(f"Something{suffix}", "normal"),)) == []

    def test_the_extension_test_ignores_case(self) -> None:
        assert len(photograph_candidates((ImageCandidate("Shark.JPG", "normal"),))) == 1


class TestChoosingThePhoto:
    """Steps 2-6, over the shapes the live 42-species sample actually contains."""

    def test_a_single_candidate_is_used(self) -> None:
        assert choose_photo_file(_candidates("Some fish.jpg"), taxon_name="Amphiprion ocellaris") == "Some fish.jpg"

    def test_nothing_at_all_is_no_photo(self) -> None:
        assert choose_photo_file([], taxon_name="Amphiprion ocellaris") is None

    def test_a_lone_preferred_statement_wins(self) -> None:
        """Step 3, and it fires before the title filter: where Wikidata has said which value
        to use, this app does not second-guess it with a string test. Only 2 of the 10
        multi-valued items in the sample use `preferred` at all."""
        chosen = choose_photo_file(
            _candidates(("Unrelated frame.jpg", "normal"), ("Chosen by wikidata.jpg", "preferred")),
            taxon_name="Amphiprion ocellaris",
        )

        assert chosen == "Chosen by wikidata.jpg"

    def test_two_preferred_statements_fall_through_to_the_title_filter(self) -> None:
        """ "Exactly one preferred" rather than "any preferred": two of them is Wikidata
        failing to disambiguate, which is the situation step 4 exists for."""
        chosen = choose_photo_file(
            _candidates(("Unrelated frame.jpg", "preferred"), ("Amphiprion ocellaris close.jpg", "preferred")),
            taxon_name="Amphiprion ocellaris",
        )

        assert chosen == "Amphiprion ocellaris close.jpg"

    def test_the_wrong_species_is_filtered_out_by_its_title(self) -> None:
        """Q169468, the zebra shark, carries a photograph of a **great hammerhead** at equal
        rank beside the correct one. This is the case the whole rule exists for."""
        chosen = choose_photo_file(
            _candidates("Great hammerhead shark carcharhinus.jpg", "Stegostoma fasciatum maldives.jpg"),
            taxon_name="Stegostoma fasciatum",
        )

        assert chosen == "Stegostoma fasciatum maldives.jpg"

    def test_the_specific_epithet_alone_is_enough(self) -> None:
        """Commons titles rarely carry the full binomial, so matching only on it would drop
        most of what step 4 is supposed to keep."""
        chosen = choose_photo_file(
            _candidates("Underwater scene.jpg", "Ocellaris clownfish in anemone.jpg"),
            taxon_name="Amphiprion ocellaris",
        )

        assert chosen == "Ocellaris clownfish in anemone.jpg"

    def test_underscores_in_a_title_match_a_spaced_name(self) -> None:
        """Underscores are the URL spelling of a space and the two forms are interchangeable
        in a Commons title, so a rule matching only one would depend on which spelling a P18
        statement happened to carry."""
        chosen = choose_photo_file(
            _candidates("Sea floor.jpg", "Amphiprion_ocellaris_2.jpg"), taxon_name="Amphiprion ocellaris"
        )

        assert chosen == "Amphiprion_ocellaris_2.jpg"

    def test_when_several_survive_the_one_beginning_with_the_binomial_wins(self) -> None:
        """**Step 5, which an earlier draft of this rule left out** - and leaving it out is not
        a rounding error. In six of the eight remaining ties in the sample *both* candidates
        name the taxon, so a rule that stopped at "exactly one survives" would silently drop
        the red lionfish, Clark's anemonefish, the blacktip reef shark, the blue dragon,
        *Aplysina archeri* and *Zenopontonia rex* - taking coverage from 39 of 42 to 33.

        It is safe because step 4 has already run: every candidate here names the taxon, so
        choosing between them cannot pick a different animal. It also picks better - the plain
        binomial over the incidental mention.
        """
        chosen = choose_photo_file(
            _candidates("Diver with Pterois volitans at night.jpg", "Pterois volitans Manado.jpg"),
            taxon_name="Pterois volitans",
        )

        assert chosen == "Pterois volitans Manado.jpg"

    def test_when_several_survive_and_none_leads_the_first_is_taken(self) -> None:
        """The other half of step 5. Statement order is the tie-break, and it is the *raw*
        serialization order - the ranked reordering `_claim_values` applies is for a different
        question."""
        chosen = choose_photo_file(
            _candidates("A Pterois volitans hunting.jpg", "Another Pterois volitans.jpg"),
            taxon_name="Pterois volitans",
        )

        assert chosen == "A Pterois volitans hunting.jpg"

    def test_two_equal_candidates_naming_neither_the_taxon_get_no_photo(self) -> None:
        """*Triaenodon obesus*, the whitetip reef shark, and the answer that has to stay
        "nothing". Its item carries `Carcharhinus albimarginatus-shark.jpg` - a **silvertip**,
        a different species - beside a correct photograph, at equal rank, with neither title
        naming the taxon. A photo appearing here means divers are being shown the wrong
        animal, which is the one thing this feature refuses to do."""
        chosen = choose_photo_file(
            _candidates("Carcharhinus albimarginatus-shark.jpg", "Whitetip reef shark Maldives.jpg"),
            taxon_name="Triaenodon obesus",
        )

        assert chosen is None

    def test_a_lone_survivor_of_the_title_filter_is_used(self) -> None:
        """The giant moray's shape: `Moray eel komodo.jpg` beside a candidate naming the
        taxon. Explicit because "if several survive" would leave the exactly-one case
        unspecified, which is how it was lost from an earlier draft."""
        chosen = choose_photo_file(
            _candidates("Moray eel komodo.jpg", "Gymnothorax javanicus Red Sea.jpg"),
            taxon_name="Gymnothorax javanicus",
        )

        assert chosen == "Gymnothorax javanicus Red Sea.jpg"

    def test_an_item_with_no_taxon_name_cannot_disambiguate(self) -> None:
        """Step 4 has nothing to match against, so step 6 is the answer. An item with an
        AphiaID and no P225 is one this app already refuses to build a search row from."""
        assert choose_photo_file(_candidates("One.jpg", "Two.jpg"), taxon_name=None) is None

    def test_a_single_candidate_is_used_even_without_a_taxon_name(self) -> None:
        """Step 2 runs before step 4 needs a name at all - there is nothing to disambiguate."""
        assert choose_photo_file(_candidates("One.jpg"), taxon_name=None) == "One.jpg"

    def test_a_genus_rank_taxon_matches_on_its_one_word(self) -> None:
        """ "a moray eel" is an honest log entry, so a genus or family row is legal here and the
        epithet split must not assume a binomial."""
        chosen = choose_photo_file(_candidates("Reef scene.jpg", "Acropora colony.jpg"), taxon_name="Acropora")

        assert chosen == "Acropora colony.jpg"


# -------------- the credit --------------


class TestReadingTheArtistField:
    def test_html_is_parsed_rather_than_stripped(self) -> None:
        """The live clownfish file's shape. A regex would get this particular one right, which
        is why the two cases below exist."""
        assert plain_text('<a href="//commons.wikimedia.org/wiki/User:Raimond">Raimond Spekking</a>') == (
            "Raimond Spekking"
        )

    def test_entity_escapes_are_resolved(self) -> None:
        """What a tag-shaped regex actually leaves behind - not a leaked tooltip, which is the
        usual guess, but `&amp;` and `&#39;` sitting inside a person's name."""
        assert plain_text("Smith &amp; Jones&#39; estate") == "Smith & Jones' estate"

    def test_two_adjacent_authors_do_not_run_together(self) -> None:
        """The other thing a regex loses. Every tag is a soft boundary here, so adjacent
        elements are separated rather than concatenated."""
        assert plain_text("<a href='#'>Ada Lovelace</a><a href='#'>Grace Hopper</a>") == ("Ada Lovelace Grace Hopper")

    def test_nested_markup_inside_one_name_still_collapses(self) -> None:
        """The control for the test above: the soft boundary must not put a space *inside* a
        single name that happens to carry emphasis."""
        assert plain_text("<bdi>Raimond <b>Spekking</b></bdi>") == "Raimond Spekking"

    def test_punctuation_does_not_drift_away_from_the_word_before_it(self) -> None:
        assert plain_text("<a href='#'>Ada</a>, <a href='#'>Grace</a>") == "Ada, Grace"

    def test_a_hidden_stylesheet_does_not_leak_into_the_name(self) -> None:
        assert plain_text("<style>.a{color:red}</style><bdi>Ada Lovelace</bdi>") == "Ada Lovelace"

    def test_a_non_string_is_nothing(self) -> None:
        assert plain_text(None) is None
        assert plain_text({"value": "Ada"}) is None

    def test_markup_with_no_text_at_all_is_nothing(self) -> None:
        assert plain_text("<span></span>") is None


class TestComposingTheCredit:
    def _imageinfo(self, **metadata: str) -> dict[str, Any]:
        return {
            "descriptionurl": "https://commons.wikimedia.org/wiki/File:Clownfish.jpg",
            "extmetadata": {key: {"value": value, "source": "commons-desc-page"} for key, value in metadata.items()},
        }

    def test_the_parts_come_off_both_levels_of_the_entry(self) -> None:
        """`extmetadata` carries the author and the licence; `descriptionurl` sits beside it
        rather than inside it, and is the "source" every one of these licences asks for. It
        was present on 40 of 40 sampled files."""
        credit = credit_from_imageinfo(
            self._imageinfo(
                Artist="<bdi>Raimond Spekking</bdi>",
                LicenseShortName="CC BY-SA 4.0",
                LicenseUrl="https://creativecommons.org/licenses/by-sa/4.0",
            )
        )

        assert credit == PhotoCredit(
            author="Raimond Spekking",
            license_name="CC BY-SA 4.0",
            license_url="https://creativecommons.org/licenses/by-sa/4.0",
            source_url="https://commons.wikimedia.org/wiki/File:Clownfish.jpg",
        )

    def test_a_ready_made_attribution_wins_over_the_artist(self) -> None:
        """It is the credit the uploader asked for verbatim. Present on only 7 of 40 sampled
        files, which is why composing from `Artist` is the common path rather than this."""
        credit = credit_from_imageinfo(
            self._imageinfo(Attribution="Photo by A. Diver", Artist="<bdi>Someone Else</bdi>")
        )

        assert credit.author == "Photo by A. Diver"

    def test_a_file_with_no_author_at_all_still_yields_licence_and_source(self) -> None:
        """One file in 40. Not a refusal: a Commons file is free-licensed by policy, so a
        missing author field is a gap in the metadata rather than in the permission - and the
        credit line still has two things to say."""
        credit = credit_from_imageinfo(self._imageinfo(LicenseShortName="CC0"))

        assert credit.author is None
        assert credit.license_name == "CC0"
        assert credit.source_url is not None

    def test_a_non_https_licence_url_is_refused(self) -> None:
        """These are rendered as links by every client, so a `javascript:` value arriving in a
        third party's metadata must not reach one. Structural rather than a rule each client
        has to remember."""
        credit = credit_from_imageinfo(self._imageinfo(LicenseUrl="javascript:alert(1)"))

        assert credit.license_url is None

    def test_an_entry_that_is_not_a_dict_yields_nothing_rather_than_raising(self) -> None:
        assert credit_from_imageinfo(None) == PhotoCredit(None, None, None, None)


# -------------- the SSRF fence --------------


class TestWhereBytesMayComeFrom:
    @pytest.mark.parametrize(
        "url",
        [
            "https://thumb.wikimedia.org/wikipedia/commons/thumb/e/ef/x.jpg/500px-x.jpg",
            "https://upload.wikimedia.org/wikipedia/commons/e/ef/x.jpg",
        ],
    )
    def test_both_hosts_one_imageinfo_reply_names_are_allowed(self, url: str) -> None:
        """`thumburl` and `url` come back from the same Commons response on **different**
        hosts, and the code prefers the first. A fence admitting only the second refuses every
        thumbnail there is, which is a pipeline that fetches nothing rather than one that
        fetches badly - so the thumbnail host is the case that matters most here."""
        assert is_photo_byte_source(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://thumb.wikimedia.org/x.jpg",
            "http://upload.wikimedia.org/x.jpg",
            "https://169.254.169.254/latest/meta-data/",
            "https://localhost/x.jpg",
            "https://thumb.wikimedia.org.evil.example/x.jpg",
            "https://upload.wikimedia.org.evil.example/x.jpg",
            "https://evil.example/thumb.wikimedia.org/x.jpg",
            "https://evil.example/upload.wikimedia.org/x.jpg",
            "https://commons.wikimedia.org/x.jpg",
            "https://en.wikipedia.org/x.jpg",
        ],
    )
    def test_everything_else_is_refused(self, url: str) -> None:
        """**Two exact names, not a `*.wikimedia.org` suffix match**, which is the whole reason
        the last four cases are here rather than only the obvious ones. A suffix test would
        admit `thumb.wikimedia.org.evil.example`; a "contains" test would admit
        `evil.example/upload.wikimedia.org`; and either would admit `commons.wikimedia.org`,
        which is Wikimedia's own but serves no file bytes and has no business here. Widening
        this fence a third time means adding a name to `PHOTO_BYTE_HOSTS` and a case above,
        which is the deliberate act the allowlist shape is bought for."""
        assert not is_photo_byte_source(url)


# -------------- normalization --------------


class TestNormalizingTheBytes:
    def test_the_output_is_a_webp(self) -> None:
        result = Image.open(io.BytesIO(species_photos._normalize(plain_png(size=(500, 333)))))

        assert result.format == "WEBP"

    def test_nothing_is_cropped_or_resized(self) -> None:
        """**A licence property, not an aesthetic one.** What is stored has to remain a scaled
        copy of the Commons file: 24 of 40 sampled files are ShareAlike, and while displaying
        and scaling is not adaptation, cropping and compositing move toward it. This is the one
        assertion that separates this pipeline from the avatar one it otherwise mirrors, whose
        whole job is to crop square.
        """
        result = Image.open(io.BytesIO(species_photos._normalize(plain_png(size=(500, 333)))))

        assert result.size == (500, 333)

    def test_alpha_survives_rather_than_being_composited_onto_a_background(self) -> None:
        """Inventing a background colour would be exactly the compositing the rule above
        forbids, and the clients draw these on surfaces of several colours."""
        result = Image.open(io.BytesIO(species_photos._normalize(png_with_alpha())))

        assert result.mode in ("RGBA", "LA", "P")

    def test_an_image_too_large_to_rasterize_is_refused(self) -> None:
        """The cap that governs memory, and it is far below the avatar's because the input is
        far more constrained: this is a thumbnail Commons rendered at 500 px wide, so anything
        over two megapixels is not the file that was asked for."""
        with pytest.raises(species_photos.UnsupportedPhotoImageError):
            species_photos._normalize(large_jpeg(size=(2000, 2000)))

    def test_bytes_that_are_not_an_image_are_refused(self) -> None:
        with pytest.raises(species_photos.UnsupportedPhotoImageError):
            species_photos._normalize(b"<html>404</html>")

    @pytest.mark.asyncio
    async def test_empty_bytes_are_refused_before_pillow_sees_them(self) -> None:
        with pytest.raises(species_photos.UnsupportedPhotoImageError):
            await species_photos.process_photo(b"")


# -------------- fetching, end to end against a mocked Wikimedia --------------


class _Wikimedia:
    """Stands in for Wikidata, WoRMS and Commons at the transport layer.

    Patches the client *construction* the way `test_species.py`'s `_Providers` does, so the
    headers and query parameters the service actually builds are under test while nothing
    leaves the machine. Both `httpx.AsyncClient` uses in the module are patched - the JSON one
    and the byte fetch, which builds its own with `follow_redirects=False`.
    """

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._handler = handler
        self._patcher: Any = None

    def _record(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._handler(request)

    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]

    def __enter__(self) -> _Wikimedia:
        def build(**kwargs: Any) -> httpx.AsyncClient:
            return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(self._record), **kwargs)

        self._patcher = patch("src.app.services.species_service.httpx.AsyncClient", side_effect=build)
        self._patcher.start()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._patcher.stop()


def _entity_payload(qid: str, *, aphia_id: int, taxon_name: str, images: list[tuple[str, str]]) -> dict[str, Any]:
    """A `wbgetentities` answer carrying P850, P225 and however many P18 statements."""
    return {
        "entities": {
            qid: {
                "claims": {
                    "P850": [{"rank": "normal", "mainsnak": {"datavalue": {"value": str(aphia_id)}}}],
                    "P225": [{"rank": "normal", "mainsnak": {"datavalue": {"value": taxon_name}}}],
                    "P18": [{"rank": rank, "mainsnak": {"datavalue": {"value": title}}} for title, rank in images],
                }
            }
        }
    }


# **The host Commons actually serves thumbnails from, which is the invariant this whole file
# rests on.** This named `upload.wikimedia.org` until 2026-08-31, and the cost was not one bad
# test: `iiurlwidth` is answered with a `thumburl` on `thumb.wikimedia.org`, the code prefers
# it, and the fence admitted only the other host - so twenty tests passed green against a fake
# Commons handing back a host the fence happened to like, over a feature that had never fetched
# a single photo in production. A fixture that names something upstream does not send cannot
# fail, whatever it asserts.
_THUMB_URL = "https://thumb.wikimedia.org/wikipedia/commons/thumb/e/ef/Some_fish.jpg/500px-Some_fish.jpg"

# The full-size original, on the other host. Commons returns both in one reply.
_FULL_URL = "https://upload.wikimedia.org/wikipedia/commons/e/ef/Some_fish.jpg"

# The hosts the fake serves bytes from, **spelled out rather than read from
# `species_photos.PHOTO_BYTE_HOSTS`**. Sourcing them from the constant under test would make
# the fake agree with the fence by construction and reinstate exactly the blindness above: the
# fake stands in for Commons, so it has to be able to disagree.
_COMMONS_BYTE_HOSTS = frozenset({"thumb.wikimedia.org", "upload.wikimedia.org"})


def _imageinfo_payload(*, thumb_url: str | None = _THUMB_URL, url: str = _FULL_URL) -> dict[str, Any]:
    """One Commons `imageinfo` entry, carrying both of the URLs a real reply carries.

    `thumb_url=None` omits `thumburl` altogether, which is what Commons does when the source
    file is narrower than the width asked for - it does not upscale - and is the one case where
    the full-size `url` on the other host is what actually gets fetched.
    """
    info: dict[str, Any] = {
        "url": url,
        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Some_fish.jpg",
        "extmetadata": {
            "Artist": {"value": "<bdi>Raimond Spekking</bdi>"},
            "LicenseShortName": {"value": "CC BY-SA 4.0"},
            "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
        },
    }
    if thumb_url is not None:
        info["thumburl"] = thumb_url
        info["thumbwidth"] = 500
    return {
        "batchcomplete": True,
        "query": {"pages": [{"pageid": 1, "title": "File:Some fish.jpg", "imageinfo": [info]}]},
    }


def _wikimedia(
    *,
    entity: dict[str, Any] | None = None,
    synonym_search: dict[str, Any] | None = None,
    imageinfo: dict[str, Any] | None = None,
    image_bytes: bytes | None = None,
    commons_raises: bool = False,
    image_status: int = 200,
) -> _Wikimedia:
    """Every upstream this feature touches, answering from canned payloads and routed by host.

    **By host, not by substring.** A `"upload.wikimedia.org" in url` test would answer bytes for
    `https://evil.example/upload.wikimedia.org/x.jpg` too, so a fence that let one through would
    be met by an obliging fake rather than by a failure - the routing has to be at least as
    strict as the thing it is checking.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        host = (request.url.host or "").lower()
        if host in _COMMONS_BYTE_HOSTS:
            return httpx.Response(image_status, content=image_bytes or b"")
        if host == "commons.wikimedia.org":
            if commons_raises:
                raise httpx.ConnectError("commons is unreachable")
            return httpx.Response(200, json=imageinfo or {"batchcomplete": True, "query": {"pages": []}})
        if "wbgetentities" in url:
            return httpx.Response(200, json=entity or {"entities": {}})
        if "wikidata" in url:
            return httpx.Response(200, json=synonym_search or {"query": {"search": []}})
        raise AssertionError(f"unexpected request to {url}")

    return _Wikimedia(handle)


@pytest.fixture(autouse=True)
def unthrottled() -> Generator[None]:
    with patch("src.app.services.species_service.enforce_rate_limit", new_callable=AsyncMock):
        yield


class TestFetchingAPhoto:
    @pytest.mark.asyncio
    async def test_the_whole_pipeline_produces_stored_bytes_and_a_credit(self) -> None:
        entity = species_service._image_candidates(
            {"P18": [{"rank": "normal", "mainsnak": {"datavalue": {"value": "Amphiprion ocellaris.jpg"}}}]}
        )
        candidate_entity = species_service._WikidataEntity(
            qid="Q1126155",
            aphia_id=278400,
            scientific_name="Amphiprion ocellaris",
            label=None,
            aliases=(),
            rank="Species",
            images=entity,
        )

        with _wikimedia(imageinfo=_imageinfo_payload(thumb_url=_THUMB_URL), image_bytes=plain_png(size=(500, 333))):
            photo = await species_service.fetch_species_photo(
                scientific_name="Amphiprion ocellaris", aphia_id=278400, entity=candidate_entity, synonym_aphia_ids=[]
            )

        assert photo is not None
        assert photo.file == "Amphiprion ocellaris.jpg"
        assert photo.credit.author == "Raimond Spekking"
        assert photo.credit.license_name == "CC BY-SA 4.0"
        assert Image.open(io.BytesIO(photo.data)).format == "WEBP"
        assert len(photo.sha256) == 64

    @pytest.mark.asyncio
    async def test_the_bytes_are_fetched_from_the_thumbnail_host(self) -> None:
        """**The pin on the defect that made this feature inert.** `thumburl` comes back on
        `thumb.wikimedia.org` while the full-size `url` is on `upload.wikimedia.org`, the code
        prefers the thumbnail, and a fence carrying only the second silently refused every one
        of them. Asserting the host that was actually contacted is what makes that visible;
        asserting only that a photo came back would pass on either host."""
        with _wikimedia(imageinfo=_imageinfo_payload(), image_bytes=plain_png(size=(500, 333))) as wikimedia:
            photo = await species_service.fetch_species_photo(
                scientific_name="Amphiprion ocellaris",
                aphia_id=278400,
                entity=_local_entity(images=[("Amphiprion ocellaris.jpg", "normal")]),
                synonym_aphia_ids=[],
            )

        assert photo is not None
        byte_hosts = [r.url.host for r in wikimedia.requests if (r.url.host or "") in _COMMONS_BYTE_HOSTS]
        assert byte_hosts == ["thumb.wikimedia.org"]

    @pytest.mark.asyncio
    async def test_a_file_too_narrow_to_thumbnail_is_fetched_whole_from_the_other_host(self) -> None:
        """Commons omits `thumburl` when the source file is narrower than the width asked for,
        because it does not upscale - and the full-size `url` is the right answer there, since
        such a file is already thumbnail-sized. `thumburl or url` is therefore unchanged by the
        fence fix; what changed is that **both** of the hosts those two fields name are now
        admitted, so this path and the one above both reach bytes."""
        with _wikimedia(imageinfo=_imageinfo_payload(thumb_url=None), image_bytes=plain_png(size=(320, 240))) as w:
            photo = await species_service.fetch_species_photo(
                scientific_name="Amphiprion ocellaris",
                aphia_id=278400,
                entity=_local_entity(images=[("Amphiprion ocellaris.jpg", "normal")]),
                synonym_aphia_ids=[],
            )

        assert photo is not None
        assert [r.url.host for r in w.requests if (r.url.host or "") in _COMMONS_BYTE_HOSTS] == ["upload.wikimedia.org"]

    @pytest.mark.asyncio
    async def test_the_thumbnail_is_asked_for_at_a_real_bucket_width(self) -> None:
        """Commons serves thumbnails only at 120/250/330/500/960 and refuses anything else
        outright, so this asks the API to name the URL rather than building one. 500 is a
        bucket, which is why the width asked for is the width served."""
        candidate_entity = _local_entity(images=[("Amphiprion ocellaris.jpg", "normal")])

        with _wikimedia(
            imageinfo=_imageinfo_payload(thumb_url=_THUMB_URL), image_bytes=plain_png(size=(500, 333))
        ) as wikimedia:
            await species_service.fetch_species_photo(
                scientific_name="Amphiprion ocellaris", aphia_id=278400, entity=candidate_entity, synonym_aphia_ids=[]
            )

        commons = next(url for url in wikimedia.urls() if "commons.wikimedia.org" in url)
        assert f"iiurlwidth={species_photos.COMMONS_THUMBNAIL_WIDTH}" in commons
        assert species_photos.COMMONS_THUMBNAIL_WIDTH == 500

    @pytest.mark.asyncio
    async def test_the_byte_fetch_identifies_itself(self) -> None:
        """Wikimedia's policy blocks generic and empty User-Agents, and an empty header returns
        403 on `upload.wikimedia.org` as well as on `api.php` - which bites servers rather than
        `<img>` tags, because a browser always sends one."""
        with _wikimedia(
            imageinfo=_imageinfo_payload(thumb_url=_THUMB_URL), image_bytes=plain_png(size=(500, 333))
        ) as wikimedia:
            await species_service.fetch_species_photo(
                scientific_name="Amphiprion ocellaris",
                aphia_id=278400,
                entity=_local_entity(images=[("Amphiprion ocellaris.jpg", "normal")]),
                synonym_aphia_ids=[],
            )

        byte_request = next(r for r in wikimedia.requests if (r.url.host or "") in _COMMONS_BYTE_HOSTS)
        assert byte_request.headers["User-Agent"] == settings.SPECIES_USER_AGENT

    @pytest.mark.asyncio
    async def test_a_refused_choice_never_reaches_commons(self) -> None:
        """The whitetip reef shark. Declining is not "fetch it and discard it" - nothing is
        asked of Commons at all, which is also what keeps the refusal cheap."""
        entity = _local_entity(
            taxon_name="Triaenodon obesus",
            images=[("Carcharhinus albimarginatus-shark.jpg", "normal"), ("Whitetip reef shark.jpg", "normal")],
        )

        with _wikimedia() as wikimedia:
            photo = await species_service.fetch_species_photo(
                scientific_name="Triaenodon obesus", aphia_id=214557, entity=entity, synonym_aphia_ids=[]
            )

        assert photo is None
        assert not [url for url in wikimedia.urls() if "commons" in url or "upload" in url]

    @pytest.mark.asyncio
    async def test_a_refused_choice_is_never_retried_through_the_synonyms(self) -> None:
        """The sharpest edge of the retry, and getting it wrong undoes the one refusal this
        design exists to make: the retry fires only when the accepted item offered **no**
        candidate, never when the rule looked at candidates and declined them."""
        entity = _local_entity(
            taxon_name="Triaenodon obesus",
            images=[("Carcharhinus albimarginatus-shark.jpg", "normal"), ("Whitetip reef shark.jpg", "normal")],
        )

        with _wikimedia() as wikimedia:
            photo = await species_service.fetch_species_photo(
                scientific_name="Triaenodon obesus", aphia_id=214557, entity=entity, synonym_aphia_ids=[220032]
            )

        assert photo is None
        assert not [url for url in wikimedia.urls() if "list=search" in url]

    @pytest.mark.asyncio
    async def test_an_item_with_no_image_at_all_falls_back_to_the_synonyms(self) -> None:
        """The zebra shark, and the single most valuable case in this feature. WoRMS's accepted
        AphiaID is 313100, whose Wikidata item carries no image; the *unaccepted* 220032 reaches
        Q169468, which has one. Storing the accepted id is the obviously correct thing to do and
        is exactly what misses the photo.

        The title filter here runs against `Q169468`'s **own** P225, *Stegostoma fasciatum* -
        not the *Stegostoma tigrinum* this instance stores. Matching the stored name would keep
        neither candidate and the shark would silently lose its photo.
        """
        with _wikimedia(
            synonym_search={"query": {"search": [{"title": "Q169468"}]}},
            entity=_entity_payload(
                "Q169468",
                aphia_id=220032,
                taxon_name="Stegostoma fasciatum",
                images=[("Great hammerhead.jpg", "normal"), ("Stegostoma fasciatum Maldives.jpg", "normal")],
            ),
            imageinfo=_imageinfo_payload(thumb_url=_THUMB_URL),
            image_bytes=plain_png(size=(500, 333)),
        ) as wikimedia:
            photo = await species_service.fetch_species_photo(
                scientific_name="Stegostoma tigrinum",
                aphia_id=313100,
                entity=None,
                synonym_aphia_ids=[220032],
            )

        assert photo is not None
        assert photo.file == "Stegostoma fasciatum Maldives.jpg"
        search = next(url for url in wikimedia.urls() if "list=search" in url)
        assert "P850%3D220032" in search and "P850%3D313100" in search

    @pytest.mark.asyncio
    async def test_every_synonym_id_goes_into_one_search(self) -> None:
        """`haswbstatement` ORs its values inside a single query, so "try a synonym" needs no
        per-synonym request however long the list is - the flagship case has 22 ids and some
        taxa have 55."""
        with _wikimedia() as wikimedia:
            await species_service.fetch_species_photo(
                scientific_name="Fucus vesiculosus",
                aphia_id=145548,
                entity=None,
                synonym_aphia_ids=list(range(900000, 900055)),
            )

        searches = [url for url in wikimedia.urls() if "list=search" in url]
        assert len(searches) == 1

    @pytest.mark.asyncio
    async def test_a_taxon_with_no_item_and_no_synonyms_asks_nothing_further(self) -> None:
        """The common case by a distance - across the whole register only 11.7% of items
        carrying a WoRMS id have a P18 - so it must not cost a search that can find nothing."""
        with _wikimedia() as wikimedia:
            photo = await species_service.fetch_species_photo(
                scientific_name="zzfixture nothing", aphia_id=1, entity=None, synonym_aphia_ids=[]
            )

        assert photo is None
        assert wikimedia.urls() == []

    @pytest.mark.asyncio
    async def test_an_unroutable_commons_yields_no_photo_rather_than_raising(self) -> None:
        with _wikimedia(commons_raises=True):
            photo = await species_service.fetch_species_photo(
                scientific_name="Amphiprion ocellaris",
                aphia_id=278400,
                entity=_local_entity(images=[("Amphiprion ocellaris.jpg", "normal")]),
                synonym_aphia_ids=[],
            )

        assert photo is None

    @pytest.mark.asyncio
    async def test_a_non_200_from_the_byte_host_yields_no_photo(self) -> None:
        with _wikimedia(imageinfo=_imageinfo_payload(thumb_url=_THUMB_URL), image_status=403, image_bytes=b"denied"):
            photo = await species_service.fetch_species_photo(
                scientific_name="Amphiprion ocellaris",
                aphia_id=278400,
                entity=_local_entity(images=[("Amphiprion ocellaris.jpg", "normal")]),
                synonym_aphia_ids=[],
            )

        assert photo is None

    @pytest.mark.asyncio
    async def test_a_thumbnail_url_pointing_somewhere_else_is_refused(self) -> None:
        """The SSRF fence in the position that matters. The URL comes out of a Commons response
        rather than from a caller, so it is not attacker-supplied in the ordinary sense - this
        is the second fence, because the first one's failure mode is an `imageinfo` reply
        naming a link-local address and being fetched from inside the network."""
        with _wikimedia(
            imageinfo=_imageinfo_payload(thumb_url="https://169.254.169.254/latest/meta-data/"),
            image_bytes=plain_png(size=(500, 333)),
        ) as wikimedia:
            photo = await species_service.fetch_species_photo(
                scientific_name="Amphiprion ocellaris",
                aphia_id=278400,
                entity=_local_entity(images=[("Amphiprion ocellaris.jpg", "normal")]),
                synonym_aphia_ids=[],
            )

        assert photo is None
        assert not [url for url in wikimedia.urls() if "169.254" in url]

    @pytest.mark.asyncio
    async def test_a_wikimedia_host_that_is_not_on_the_allowlist_is_refused_too(self) -> None:
        """**The fence is two exact names, not `*.wikimedia.org`.** This is the case the
        link-local one above cannot cover: a host that looks entirely legitimate, is genuinely
        Wikimedia's, and still is not one of the two that serve file bytes. Relaxing the fence
        to a suffix match would make this test pass bytes through, which is the point of it -
        a subdomain-matching bug in a pattern is an SSRF hole, while a name that stops resolving
        is a feature that visibly stops working.
        """
        with _wikimedia(
            imageinfo=_imageinfo_payload(thumb_url="https://static.wikimedia.org/wikipedia/commons/x.jpg"),
            image_bytes=plain_png(size=(500, 333)),
        ) as wikimedia:
            photo = await species_service.fetch_species_photo(
                scientific_name="Amphiprion ocellaris",
                aphia_id=278400,
                entity=_local_entity(images=[("Amphiprion ocellaris.jpg", "normal")]),
                synonym_aphia_ids=[],
            )

        assert photo is None
        assert not [url for url in wikimedia.urls() if "static.wikimedia.org" in url]

    @pytest.mark.asyncio
    async def test_bytes_that_do_not_decode_yield_no_photo_rather_than_raising(self) -> None:
        """A Commons error page served with a 200, which is the shape that would otherwise
        escape as an exception from Pillow into a caller mid-resolve."""
        with _wikimedia(imageinfo=_imageinfo_payload(thumb_url=_THUMB_URL), image_bytes=b"<html>oops</html>"):
            photo = await species_service.fetch_species_photo(
                scientific_name="Amphiprion ocellaris",
                aphia_id=278400,
                entity=_local_entity(images=[("Amphiprion ocellaris.jpg", "normal")]),
                synonym_aphia_ids=[],
            )

        assert photo is None


def _local_entity(
    *, taxon_name: str = "Amphiprion ocellaris", images: list[tuple[str, str]], aphia_id: int = 278400
) -> Any:
    return species_service._WikidataEntity(
        qid="Q1126155",
        aphia_id=aphia_id,
        scientific_name=taxon_name,
        label=None,
        aliases=(),
        rank="Species",
        images=tuple(ImageCandidate(file=title, rank=rank) for title, rank in images),
    )


# -------------- the invariant --------------


class TestCommonsCannotFailAResolve:
    """**The one thing in this feature that must never be true**: that a photo provider can
    change what `POST /species/resolve` answers.

    It is the only route by which a species enters the catalog, so a 503 here means divers
    cannot add species to their dives at all. The hazard is specific and not obvious:
    `anyio.move_on_after` cancels the whole task group it wraps, so photo work sharing the
    enrichment scope would cancel the synonym walk beside it, `synonyms` would come back `None`,
    and the existing "refuse rather than store an unvetted name" branch would raise.
    """

    @pytest.mark.asyncio
    async def test_a_photo_step_that_hangs_past_its_budget_still_resolves(self) -> None:
        """The exact failure the scope separation exists to prevent, forced rather than
        simulated: the photo work sleeps past every budget in the module."""
        db = _resolve_db()

        async def hangs(**_kwargs: Any) -> None:
            await anyio.sleep(3600)

        with (
            patch.object(species_service, "fetch_species_photo", hangs),
            patch.object(species_service, "_PHOTO_BUDGET_SECONDS", 0.05),
            patch.object(species_service, "_worms", AsyncMock(side_effect=_worms_answers)),
            patch.object(species_service, "_wikidata", AsyncMock(return_value={"query": {"search": []}})),
        ):
            stored = await species_service.resolve_species(db, 278400)

        assert stored.scientific_name == "Amphiprion ocellaris"

    @pytest.mark.asyncio
    async def test_a_budget_expiry_writes_no_verdict_about_the_species(self) -> None:
        """**Nothing found and nothing asked are different answers**, and only one of them is
        worth writing down. `photo_fetched_at` is stamped on a *failed* attempt on purpose - it
        is what stops the backfill re-querying the photo-less majority forever - but a fetch the
        budget cancelled established nothing, and stamping it would make one slow Wikimedia
        afternoon permanently photo-less for every species resolved during it, invisible to the
        backfill's own predicate."""
        db = _resolve_db()
        save = AsyncMock()

        async def hangs(**_kwargs: Any) -> None:
            await anyio.sleep(3600)

        with (
            patch.object(species_service, "fetch_species_photo", hangs),
            patch.object(species_service, "_PHOTO_BUDGET_SECONDS", 0.05),
            patch.object(species_service.species_photos, "save_photo_attempt", save),
            patch.object(species_service, "_worms", AsyncMock(side_effect=_worms_answers)),
            patch.object(species_service, "_wikidata", AsyncMock(return_value={"query": {"search": []}})),
        ):
            await species_service.resolve_species(db, 278400)

        save.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_completed_attempt_that_found_nothing_is_still_written_down(self) -> None:
        """The control for the test above, and the property the backfill's second run rests on:
        a search that finished and found nothing *is* an attempt, and gets stamped."""
        db = _resolve_db()
        save = AsyncMock()

        with (
            patch.object(species_service, "fetch_species_photo", AsyncMock(return_value=None)),
            patch.object(species_service.species_photos, "save_photo_attempt", save),
            patch.object(species_service, "_worms", AsyncMock(side_effect=_worms_answers)),
            patch.object(species_service, "_wikidata", AsyncMock(return_value={"query": {"search": []}})),
        ):
            await species_service.resolve_species(db, 278400)

        save.assert_awaited_once()
        assert save.await_args is not None
        assert save.await_args.kwargs["photo"] is None

    @pytest.mark.asyncio
    async def test_a_photo_step_that_raises_still_resolves(self) -> None:
        """Everything inside `fetch_species_photo` already degrades to `None`, so reaching here
        means the storage half failed - a full volume, a database error. The species row is
        committed by then and shared with every account, so turning that into a 500 the client
        reads as failure would be a lie about what happened."""
        db = _resolve_db()

        with (
            patch.object(
                species_service.species_photos, "save_photo_attempt", AsyncMock(side_effect=OSError("no space"))
            ),
            patch.object(species_service, "fetch_species_photo", AsyncMock(return_value=None)),
            patch.object(species_service, "_worms", AsyncMock(side_effect=_worms_answers)),
            patch.object(species_service, "_wikidata", AsyncMock(return_value={"query": {"search": []}})),
        ):
            stored = await species_service.resolve_species(db, 278400)

        assert stored.scientific_name == "Amphiprion ocellaris"

    @pytest.mark.asyncio
    async def test_the_photo_runs_after_the_row_is_committed(self) -> None:
        """Ordering, asserted rather than assumed. The row committing first is what makes every
        photo failure above harmless - a species the diver asked for is in the catalog and
        attachable to a dive whatever the picture did."""
        db = _resolve_db()
        order: list[str] = []

        async def commit() -> None:
            order.append("commit")

        async def save(*_args: Any, **_kwargs: Any) -> None:
            order.append("photo")

        db.commit = AsyncMock(side_effect=commit)
        with (
            patch.object(species_service.species_photos, "save_photo_attempt", AsyncMock(side_effect=save)),
            patch.object(species_service, "fetch_species_photo", AsyncMock(return_value=None)),
            patch.object(species_service, "_worms", AsyncMock(side_effect=_worms_answers)),
            patch.object(species_service, "_wikidata", AsyncMock(return_value={"query": {"search": []}})),
        ):
            await species_service.resolve_species(db, 278400)

        assert order[0] == "commit"
        assert "photo" in order


async def _worms_answers(endpoint: str, segment: Any, params: Any = None) -> Any:
    """WoRMS answering the three calls a resolve makes, and nothing about photos."""
    if endpoint == "AphiaRecordByAphiaID":
        return {
            "AphiaID": 278400,
            "scientificname": "Amphiprion ocellaris",
            "status": "accepted",
            "rank": "Species",
            "valid_AphiaID": 278400,
            "valid_name": "Amphiprion ocellaris",
        }
    return []


def _resolve_db() -> MagicMock:
    """A session that finds no existing row and accepts the write."""
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    return db


# -------------- storing --------------


@pytest.fixture
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


@pytest.mark.skipif(not db_available(), reason="requires a database")
class TestStoringAnAttempt:
    def _photo(self, data: bytes = b"webp-bytes") -> species_photos.FetchedPhoto:
        return species_photos.fetched_photo(
            data=data,
            file="Amphiprion ocellaris.jpg",
            credit=PhotoCredit(
                author="Raimond Spekking",
                license_name="CC BY-SA 4.0",
                license_url="https://creativecommons.org/licenses/by-sa/4.0",
                source_url="https://commons.wikimedia.org/wiki/File:X.jpg",
            ),
        )

    @pytest.mark.asyncio
    async def test_a_successful_attempt_writes_the_file_and_every_column(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        species = create_species(db)
        photo = self._photo()

        await species_photos.save_photo_attempt(async_db, species_id=species.id, photo=photo)

        db.refresh(species)
        assert species.photo_sha256 == photo.sha256
        assert species.photo_file == "Amphiprion ocellaris.jpg"
        assert species.photo_author == "Raimond Spekking"
        assert species.photo_license == "CC BY-SA 4.0"
        assert species.photo_fetched_at is not None
        assert species.photo_storage_key is not None
        assert (volume / species.photo_storage_key).read_bytes() == b"webp-bytes"

    @pytest.mark.asyncio
    async def test_a_failed_attempt_stamps_the_timestamp_and_nothing_else(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        """**The whole reason the backfill's second run reports zero.** "No photo" is the
        permanent outcome for most of the catalog, so a predicate keyed on the absence of
        stored bytes would never shrink and every re-run would re-query Wikidata and Commons
        for the entire photo-less tail forever."""
        species = create_species(db)

        await species_photos.save_photo_attempt(async_db, species_id=species.id, photo=None)

        db.refresh(species)
        assert species.photo_fetched_at is not None
        assert species.photo_storage_key is None
        assert species.photo_sha256 is None

    @pytest.mark.asyncio
    async def test_the_key_is_stored_under_the_species_photo_kind(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        """Which is what `sweep_orphaned_files` has to know about - see its own tests."""
        species = create_species(db)

        await species_photos.save_photo_attempt(async_db, species_id=species.id, photo=self._photo())

        db.refresh(species)
        assert species.photo_storage_key is not None
        assert species.photo_storage_key.startswith(f"{species_photos.KEY_KIND}/")

    @pytest.mark.asyncio
    async def test_replacing_a_photo_retires_the_old_blob(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        """Only `--force` reaches this today, and it must not leave the previous file behind -
        the sweeper would reclaim it eventually, but "eventually" is a day and a manual run."""
        species = create_species(db)
        await species_photos.save_photo_attempt(async_db, species_id=species.id, photo=self._photo(b"old"))
        db.refresh(species)
        first_key = species.photo_storage_key
        assert first_key is not None

        await species_photos.save_photo_attempt(async_db, species_id=species.id, photo=self._photo(b"new"))

        db.refresh(species)
        assert species.photo_storage_key != first_key
        assert not (volume / first_key).exists()
        assert species.photo_storage_key is not None
        assert (volume / species.photo_storage_key).read_bytes() == b"new"

    @pytest.mark.asyncio
    async def test_a_species_with_no_photo_reads_back_as_none(self, db: Session, async_db: AsyncSession) -> None:
        species = create_species(db)

        assert await species_photos.get_stored_photo(async_db, species_uuid=species.uuid) is None

    @pytest.mark.asyncio
    async def test_an_unknown_uuid_reads_back_as_none_too(self, async_db: AsyncSession) -> None:
        """The same answer as "no photo", deliberately: telling them apart is exactly the
        existence oracle an unauthenticated route should not be."""
        assert await species_photos.get_stored_photo(async_db, species_uuid=uuid7()) is None


# -------------- the route --------------


@pytest.fixture(scope="module")
def photo_app() -> Any:
    return create_application(router=api_router, settings=settings, apply_migrations_on_start=False)


@pytest.fixture
def photo_client(photo_app: Any) -> Generator[TestClient]:
    """**No `get_current_user` override**, which is the point: every request below arrives
    with no credential at all, the way an `<img src>` does."""
    with TestClient(photo_app) as client:
        yield client
    photo_app.dependency_overrides = {}


class TestServingThePhoto:
    def _stub_db(self, photo_app: Any) -> None:
        """The route's own session, stubbed out: every test here patches the two service calls
        it makes, so nothing below reaches Postgres."""
        photo_app.dependency_overrides[async_get_db] = lambda: MagicMock()

    def test_an_anonymous_request_gets_the_bytes(self, photo_app: Any, photo_client: TestClient) -> None:
        """The first route in this app that serves stored bytes without authentication, and
        the reason is structural rather than a convenience: access tokens here are Bearer-only,
        so an `<img src>` cannot authenticate at all. The alternative is fetching every
        thumbnail through the API client and rendering from a blob URL, which re-fetches on
        every mount - tolerable for one certification card, not for a life list."""
        self._stub_db(photo_app)
        stored = species_photos.StoredSpeciesPhoto(storage_key="species-photos/aa/x", sha256="a" * 64)

        with (
            patch("src.app.api.v1.species.get_stored_photo", AsyncMock(return_value=stored)),
            patch("src.app.api.v1.species.read_photo_bytes", AsyncMock(return_value=b"webp")),
        ):
            response = photo_client.get(f"/api/v1/species/{uuid7()}/photo")

        assert response.status_code == 200
        assert response.content == b"webp"
        assert response.headers["content-type"] == species_photos.PHOTO_CONTENT_TYPE
        assert response.headers["etag"] == f'"{"a" * 64}"'

    def test_the_cache_is_public_and_the_rendering_inline(self, photo_app: Any, photo_client: TestClient) -> None:
        """Two departures from the authenticated binary reads next door, both consequences of
        these bytes being public and immutable. `attachment` on those exists because the web
        app fetches them through its API client - which is precisely what this route exists not
        to do, so a `Content-Disposition` here would break the `<img>` it is built for."""
        self._stub_db(photo_app)
        stored = species_photos.StoredSpeciesPhoto(storage_key="species-photos/aa/x", sha256="b" * 64)

        with (
            patch("src.app.api.v1.species.get_stored_photo", AsyncMock(return_value=stored)),
            patch("src.app.api.v1.species.read_photo_bytes", AsyncMock(return_value=b"webp")),
        ):
            response = photo_client.get(f"/api/v1/species/{uuid7()}/photo")

        assert "public" in response.headers["cache-control"]
        assert "private" not in response.headers["cache-control"]
        assert "content-disposition" not in response.headers
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-security-policy"] == EXPECTED_CSP

    def test_a_matching_etag_is_a_304_that_reads_no_bytes(self, photo_app: Any, photo_client: TestClient) -> None:
        """The conditional path must answer without touching the volume, which is why the
        digest comes from a narrow query rather than from the bytes."""
        self._stub_db(photo_app)
        stored = species_photos.StoredSpeciesPhoto(storage_key="species-photos/aa/x", sha256="c" * 64)
        read = AsyncMock(return_value=b"webp")

        with (
            patch("src.app.api.v1.species.get_stored_photo", AsyncMock(return_value=stored)),
            patch("src.app.api.v1.species.read_photo_bytes", read),
        ):
            response = photo_client.get(f"/api/v1/species/{uuid7()}/photo", headers={"If-None-Match": f'"{"c" * 64}"'})

        assert response.status_code == 304
        assert response.content == b""
        read.assert_not_awaited()

    def test_a_species_with_no_photo_is_a_404(self, photo_app: Any, photo_client: TestClient) -> None:
        self._stub_db(photo_app)

        with patch("src.app.api.v1.species.get_stored_photo", AsyncMock(return_value=None)):
            response = photo_client.get(f"/api/v1/species/{uuid7()}/photo")

        assert response.status_code == 404

    def test_a_malformed_uuid_is_a_422_rather_than_reaching_the_query(
        self, photo_app: Any, photo_client: TestClient
    ) -> None:
        self._stub_db(photo_app)

        assert photo_client.get("/api/v1/species/not-a-uuid/photo").status_code == 422


# -------------- the backfill --------------


@pytest.mark.skipif(not db_available(), reason="requires a database")
class TestBackfill:
    @pytest.mark.asyncio
    async def test_it_selects_only_species_never_attempted(self, db: Session, async_db: AsyncSession) -> None:
        """`photo_fetched_at IS NULL`, not "has no stored photo" - the distinction that makes
        re-running cheap. Two of 42 species in the sample are refused by design and 88.3% of
        P850-bearing Wikidata items carry no P18 at all, so a predicate keyed on the absence of
        bytes would re-query the whole photo-less tail on every run, forever."""
        from src.scripts import backfill_species_photos as backfill

        fresh = create_species(db)
        attempted = create_species(db, photo_fetched_at=datetime.now(UTC))

        candidates = await backfill._candidates(async_db, limit=None, force=False)

        ids = {species.id for species in candidates}
        assert fresh.id in ids
        assert attempted.id not in ids

    @pytest.mark.asyncio
    async def test_force_reconsiders_a_species_already_attempted(self, db: Session, async_db: AsyncSession) -> None:
        from src.scripts import backfill_species_photos as backfill

        attempted = create_species(db, photo_fetched_at=datetime.now(UTC))

        candidates = await backfill._candidates(async_db, limit=None, force=True)

        assert attempted.id in {species.id for species in candidates}

    @pytest.mark.asyncio
    async def test_force_reattempts_a_species_stamped_with_no_photo(self, db: Session, async_db: AsyncSession) -> None:
        """**The remedy for rows a broken fetch poisoned**, and the reason no narrower flag
        exists. A species attempted while the photo fence refused Commons' thumbnail host looks
        exactly like one the selection rule declined on its merits - a stamped
        `photo_fetched_at` over a null `photo_storage_key` - so nothing can select the first
        without the second, and `--force` taking both is the honest answer rather than a
        limitation."""
        from src.scripts import backfill_species_photos as backfill

        poisoned = create_species(db, photo_fetched_at=datetime.now(UTC), photo_storage_key=None)

        ordinary = await backfill._candidates(async_db, limit=None, force=False)
        forced = await backfill._candidates(async_db, limit=None, force=True)

        assert poisoned.id not in {species.id for species in ordinary}
        assert poisoned.id in {species.id for species in forced}

    @pytest.mark.asyncio
    async def test_force_with_a_limit_redraws_the_same_slice_rather_than_advancing(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """`--force` drops the predicate entirely, so nothing excludes the rows the previous run
        just handled and the same first `limit` ids come back every time. Pinned because the
        docstring said the opposite until 2026-08-31 and an operator chunking a forced re-walk
        would have re-attempted one slice forever while believing they were making progress."""
        from src.scripts import backfill_species_photos as backfill

        for _ in range(3):
            create_species(db)

        first = await backfill._candidates(async_db, limit=2, force=True)
        second = await backfill._candidates(async_db, limit=2, force=True)

        assert [species.id for species in first] == [species.id for species in second]

    @pytest.mark.asyncio
    async def test_a_dry_run_writes_nothing(self, db: Session, async_db: AsyncSession) -> None:
        from src.scripts import backfill_species_photos as backfill

        species = create_species(db)

        report = await backfill.backfill_species_photos(async_db, limit=1, dry_run=True)

        db.refresh(species)
        assert report.examined >= 1
        assert (report.stored, report.without_photo) == (0, 0)
        assert species.photo_fetched_at is None

    @pytest.mark.asyncio
    async def test_a_species_with_no_photo_is_stamped_so_the_next_run_skips_it(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """End to end: the second run reporting zero is the property, and stamping a *failed*
        attempt is what produces it."""
        from src.scripts import backfill_species_photos as backfill

        # This database accumulates rows across the whole suite and `_candidates` walks it
        # oldest-first, so the run below has to be given exactly one thing to do. Stamping
        # everything already there first is also the state a real second run is in.
        await async_db.execute(update(Species).where(Species.photo_fetched_at.is_(None)).values(photo_fetched_at=_NOW))
        await async_db.commit()
        species = create_species(db)

        completed_with_nothing = species_service.PhotoAttempt(photo=None, completed=True)
        with patch.object(backfill, "fetch_photo_for_species", AsyncMock(return_value=completed_with_nothing)):
            first = await backfill.backfill_species_photos(async_db, limit=None)

        assert (first.examined, first.without_photo, first.stored, first.timed_out) == (1, 1, 0, 0)
        db.refresh(species)
        assert species.photo_fetched_at is not None

        second = await backfill._candidates(async_db, limit=None, force=False)
        assert second == []

    @pytest.mark.asyncio
    async def test_a_timed_out_attempt_is_left_for_the_next_run(self, db: Session, async_db: AsyncSession) -> None:
        """**Nothing found and nothing asked are different answers.** Stamping a fetch the budget
        cancelled would write a permanent no-photo verdict for a species nothing actually asked
        about - so one slow Wikimedia afternoon would strand every taxon resolved during it,
        invisible to this script's own predicate and recoverable only by `--force`."""
        from src.scripts import backfill_species_photos as backfill

        await async_db.execute(update(Species).where(Species.photo_fetched_at.is_(None)).values(photo_fetched_at=_NOW))
        await async_db.commit()
        species = create_species(db)

        cancelled = species_service.PhotoAttempt(photo=None, completed=False)
        with patch.object(backfill, "fetch_photo_for_species", AsyncMock(return_value=cancelled)):
            report = await backfill.backfill_species_photos(async_db, limit=None)

        assert (report.examined, report.timed_out, report.without_photo) == (1, 1, 0)
        db.refresh(species)
        assert species.photo_fetched_at is None
        assert [row.id for row in await backfill._candidates(async_db, limit=None, force=False)] == [species.id]

    @pytest.mark.asyncio
    async def test_candidates_are_detached_rows_rather_than_orm_entities(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """`save_photo_attempt` calls `release_read_transaction` on the stored-photo path, which
        is a `Session.rollback()` - and that expires **every** instance in the identity map,
        `expire_on_commit=False` notwithstanding. A live `Species` held across it would turn the
        next attribute read into a lazy refresh on an `AsyncSession` outside a greenlet, so the
        loop would die on the first species that actually got a photo. Selecting columns is what
        satisfies that helper's stated precondition."""
        from src.scripts import backfill_species_photos as backfill

        create_species(db)

        candidates = await backfill._candidates(async_db, limit=1, force=True)

        assert candidates and not isinstance(candidates[0], Species)
        assert isinstance(candidates[0], backfill._Candidate)

    @pytest.mark.asyncio
    async def test_a_stored_photo_does_not_break_the_next_log_line(
        self, db: Session, async_db: AsyncSession, volume: Path
    ) -> None:
        """The end-to-end shape of the expiry hazard above: the branch that actually writes a
        blob, followed by the loop reading the species' name again. Every other backfill test
        stubs the photo away, which is precisely the branch that skips
        `release_read_transaction`."""
        from src.scripts import backfill_species_photos as backfill

        await async_db.execute(update(Species).where(Species.photo_fetched_at.is_(None)).values(photo_fetched_at=_NOW))
        await async_db.commit()
        species = create_species(db)

        photo = species_photos.fetched_photo(
            data=b"webp", file="Some fish.jpg", credit=PhotoCredit(None, None, None, None)
        )
        attempt = species_service.PhotoAttempt(photo=photo, completed=True)
        with patch.object(backfill, "fetch_photo_for_species", AsyncMock(return_value=attempt)):
            report = await backfill.backfill_species_photos(async_db, limit=None)

        assert (report.examined, report.stored) == (1, 1)
        db.refresh(species)
        assert species.photo_storage_key is not None


# -------------- the digest on the wire --------------


@pytest.mark.skipif(not db_available(), reason="requires a database")
class TestTheDigestIsTheContract:
    @pytest.mark.asyncio
    async def test_a_dive_embeds_the_digest_and_never_the_key(self, db: Session, async_db: AsyncSession) -> None:
        """`SpeciesInfo` gains one nullable field and no URL, following the avatar precedent
        verbatim: one digest answers existence, version and cache-busting at once, and the
        client builds the URL. The storage key is internal and must not appear."""
        from src.app.crud.crud_dive_species import get_species_for_dive
        from src.app.models.dive_species import DiveSpecies
        from tests.helpers.generators import create_dive, create_user

        diver = create_user(db)
        dive = create_dive(db, diver)
        species = create_species(db, photo_sha256="d" * 64, photo_storage_key=f"species-photos/dd/{uuid7()}_{'d' * 64}")
        db.add(DiveSpecies(dive_id=dive.id, species_id=species.id, position=0))
        db.commit()

        embedded = await get_species_for_dive(async_db, dive.id)

        assert [info.photo_sha256 for info in embedded] == ["d" * 64]
        assert "photo_storage_key" not in embedded[0].model_dump()

    @pytest.mark.asyncio
    async def test_the_public_species_read_carries_the_credit_but_not_the_key(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """The species page renders the credit, so `SpeciesRead` carries its parts - and the
        blob key and the operational timestamp stay server-side, exactly as `UserRead` carries
        `avatar_sha256` and never the rendition's storage key."""
        from src.app.schemas.species import SpeciesRead

        # A fresh key per run: `ux_species_photo_storage_key` is unique and these rows persist
        # in the suite's own database, so a literal here fails on the second run.
        species = create_species(
            db,
            photo_sha256="e" * 64,
            photo_storage_key=f"species-photos/ee/{uuid7()}_{'e' * 64}",
            photo_author="Raimond Spekking",
            photo_license="CC BY-SA 4.0",
            photo_license_url="https://creativecommons.org/licenses/by-sa/4.0",
            photo_source_url="https://commons.wikimedia.org/wiki/File:X.jpg",
            photo_fetched_at=_NOW,
        )
        row = (await async_db.execute(select(Species).where(Species.id == species.id))).scalar_one()

        payload = SpeciesRead.model_validate(row, from_attributes=True).model_dump()

        assert payload["photo_sha256"] == "e" * 64
        assert payload["photo_author"] == "Raimond Spekking"
        assert payload["photo_license_url"] == "https://creativecommons.org/licenses/by-sa/4.0"
        assert "photo_storage_key" not in payload
        assert "photo_fetched_at" not in payload


# -------------- the widened synonym list --------------


class TestSynonymsCarryTheirIds:
    @pytest.mark.asyncio
    async def test_each_synonym_brings_its_aphia_id(self) -> None:
        """The id is already in the same response as the name, and it is the key the retry
        searches Wikidata by - so discarding it, as this used to, would mean a second WoRMS
        call to get back what was in hand."""
        rows = [
            {"AphiaID": 220032, "scientificname": "Stegostoma fasciatum"},
            {"AphiaID": 221111, "scientificname": "Squalus fasciatus"},
        ]

        with patch.object(species_service, "_worms", AsyncMock(return_value=rows)):
            synonyms = await species_service._worms_synonyms(313100)

        assert synonyms == [("Stegostoma fasciatum", 220032), ("Squalus fasciatus", 221111)]

    @pytest.mark.asyncio
    async def test_a_row_with_no_usable_id_still_contributes_its_name(self) -> None:
        """The name is this list's first job - it is `_choose_common_name`'s reject list, and a
        missing id costs a photo retry rather than a wrong display name."""
        with patch.object(species_service, "_worms", AsyncMock(return_value=[{"scientificname": "Orca gladiator"}])):
            synonyms = await species_service._worms_synonyms(137102)

        assert synonyms == [("Orca gladiator", None)]

    @pytest.mark.asyncio
    async def test_a_list_that_failed_to_arrive_is_still_none_and_not_empty(self) -> None:
        """The contract that keeps `resolve_species` from storing a name nothing vetted. It
        survives the widening untouched, and it is the reason that endpoint 503s rather than
        degrading."""
        with patch.object(species_service, "_worms", AsyncMock(return_value=None)):
            assert await species_service._worms_synonyms(278400) is None
