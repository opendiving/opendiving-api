"""Propose better photos for the species on your life list, judged by a model, through the API.

Run by hand, from a checkout, with a write API key for the instance and a key for the model:

    export OPENDIVING_API_KEY=odk_...    # Settings > API keys, with write access
    export ANTHROPIC_API_KEY=sk-ant-...  # or any credential the Anthropic SDK resolves
    uv run python scripts/propose_species_photos.py --instance https://dives.example.com \\
        --contact you@example.com --dry-run --limit 5

For each species the account has logged and not yet proposed a photo for, it gathers the
species' photographs from Wikidata and Wikimedia Commons, shows them to the model with the one
the instance stores, and proposes the one the model judges clearly better. A proposal waits
for whoever runs the instance to accept or decline it; nothing changes before that.

Wikimedia is asked from this machine, one request at a time, under a User-Agent carrying
`--contact`, as Wikimedia's User-Agent policy requires of every client. The model is sent the
species' names and the pool's 250 px thumbnails as bytes, never a URL for it to fetch.

A run can stop at any point and be run again: a species the account has a waiting, accepted
or declined proposal for is never rated again by that account.
"""

import argparse
import base64
import json
import os
import sys
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

import anthropic
import httpx
from anthropic.types import ImageBlockParam, TextBlockParam

DEFAULT_MODEL = "claude-opus-5-5"

_WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
_COMMONS_API_URL = "https://commons.wikimedia.org/w/api.php"

# The instance's own cap on a pool, and its preview bucket: Commons serves thumbnails only at
# its standard widths, and 250 px is about 54 visual tokens.
_POOL_LIMIT = 24
_THUMBNAIL_WIDTH = 250
_CATEGORY_MEMBER_LIMIT = 50
_THUMBNAIL_MAX_BYTES = 2 * 1024 * 1024

# What a pin accepts as a raster file, and what the model takes as an image.
_RASTER_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".webp")
type MediaType = Literal["image/jpeg", "image/png", "image/gif", "image/webp"]
_IMAGE_MEDIA_TYPES: dict[str, MediaType] = {
    "image/jpeg": "image/jpeg",
    "image/png": "image/png",
    "image/gif": "image/gif",
    "image/webp": "image/webp",
}

_NOTE_MAX_LENGTH = 500
_PAGE_SIZE = 100
_TIMEOUT_SECONDS = 30.0
_NONE = "none"

_SKIP_ALREADY_PROPOSED = "already proposed"
_SKIP_CHOSEN_BY_OPERATOR = "photo chosen by the operator"
_SKIP_NO_WIKIDATA = "no Wikidata item"
_SKIP_POOL_UNREADABLE = "pool unreadable"
_SKIP_NO_ALTERNATIVE = "nothing to compare"
_SKIP_NO_USABLE_ANSWER = "no usable answer"

_PROMPT = """You are choosing the photograph a dive log shows for one species: {names}.

Each image below is a file from Wikimedia Commons, introduced by its title and the size of the
original. {stored}

A better photograph shows a live animal in the water, the whole animal in frame, placed so that
it survives a centred crop to a wide, short band across the middle of the image. It shows the
species the names say: not a drawing, a map, a specimen on a slab, a dead or held animal, or a
different species filed under this one's name. When the images show visibly different animals
and the photographs do not settle which one is this species, choose none.

{bar}

Answer with the title of the image you choose, exactly as it was given, or "none", and a reason
of one or two sentences that a person reviewing the choice will read."""

_STORED_BAR = (
    "Choose another image only when it is clearly better than the stored photo, not marginally; "
    'otherwise answer "none". Never choose the stored photo.'
)
_UNSTORED_BAR = (
    'The species has no photo yet. Choose the image that best meets the bar above, or "none" when no image meets it.'
)


class InstanceError(Exception):
    """The instance answered in a way the run cannot continue past."""


class RateLimited(Exception):
    """The instance's daily limit on proposals answered."""


@dataclass(frozen=True, slots=True)
class PoolFile:
    title: str
    width: int | None
    height: int | None
    media_type: MediaType
    data: bytes
    is_stored: bool


@dataclass(frozen=True, slots=True)
class Verdict:
    """The model's answer: a title it was shown, or `None` for none, and why. `problem` is set
    instead when the answer cannot be used."""

    title: str | None
    reason: str
    problem: str | None = None


def normalize_title(raw: str) -> str | None:
    """A Commons file title as the instance stores it, or `None` for one that is not a raster
    file: no `File:` prefix, spaces for underscores, the first letter capitalized."""
    title = raw.strip()
    if title[:5].casefold() == "file:":
        title = title[5:]
    title = " ".join(title.replace("_", " ").split())
    if not title or not title.casefold().endswith(_RASTER_SUFFIXES):
        return None
    return title[0].upper() + title[1:]


def wikimedia_user_agent(contact: str) -> str:
    return f"OpenDiving-species-photo-rater/1.0 (https://github.com/opendiving/opendiving-api; {contact})"


class Instance:
    """The instance's API, as the caller's key reaches it."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._client.get(path, params=params)
        if response.status_code != 200:
            raise InstanceError(f"GET {path} answered {response.status_code}: {_detail(response)}")
        return response.json()

    def _pages(self, path: str) -> Iterator[dict[str, Any]]:
        page = 1
        while True:
            body = self._get(path, {"page": page, "items_per_page": _PAGE_SIZE})
            yield from body["data"]
            if not body.get("has_more"):
                return
            page += 1

    def life_list(self) -> Iterator[dict[str, Any]]:
        return self._pages("/user/species")

    def proposed_species(self) -> set[str]:
        """The species this account has a proposal for that settles it: one waiting, accepted
        or declined. A withdrawn or closed one leaves the species open to the account."""
        return {
            proposal["species"]["uuid"]
            for proposal in self._pages("/species-photo-proposals")
            if proposal["status"] in {"waiting", "accepted", "declined"}
        }

    def species(self, uuid: str) -> dict[str, Any]:
        body: dict[str, Any] = self._get(f"/species/{uuid}")
        return body

    def propose(self, uuid: str, *, file: str, note: str) -> str | None:
        """`None` once the proposal waits, or why the instance refused it."""
        response = self._client.post(f"/species/{uuid}/photo-proposals", json={"file": file, "note": note})
        if response.status_code == 201:
            return None
        if response.status_code in {409, 422}:
            return f"{response.status_code}: {_detail(response)}"
        if response.status_code == 429:
            raise RateLimited
        raise InstanceError(
            f"POST /species/{uuid}/photo-proposals answered {response.status_code}: {_detail(response)}"
        )


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except ValueError, AttributeError:
        return response.text[:200]
    return detail if isinstance(detail, str) else json.dumps(detail)[:200]


class Wikimedia:
    """Wikidata and Commons, asked in series, as Wikimedia asks of API clients."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def _api(self, url: str, params: dict[str, Any]) -> dict[str, Any] | None:
        try:
            response = self._client.get(url, params={**params, "format": "json"})
            body = response.json() if response.status_code == 200 else None
        except httpx.HTTPError, ValueError:
            return None
        return body if isinstance(body, dict) else None

    def item(self, qid: str) -> tuple[list[str], str | None] | None:
        """The item's non-deprecated image (P18) values and its Commons category (P373)."""
        body = self._api(_WIKIDATA_API_URL, {"action": "wbgetentities", "ids": qid, "props": "claims"})
        entity = (body or {}).get("entities", {}).get(qid)
        if not isinstance(entity, dict):
            return None
        claims = entity.get("claims") or {}
        images = _claim_strings(claims, "P18")
        categories = _claim_strings(claims, "P373")
        return images, categories[0] if categories else None

    def category_files(self, category: str) -> list[str] | None:
        body = self._api(
            _COMMONS_API_URL,
            {
                "action": "query",
                "list": "categorymembers",
                "cmtitle": f"Category:{category}",
                "cmtype": "file",
                "cmlimit": _CATEGORY_MEMBER_LIMIT,
            },
        )
        if body is None:
            return None
        members = body.get("query", {}).get("categorymembers") or []
        return [
            member["title"] for member in members if isinstance(member, dict) and isinstance(member.get("title"), str)
        ]

    def thumbnails(self, titles: list[str]) -> dict[str, dict[str, Any]] | None:
        """Each file's original size and its thumbnail URL at the preview width, keyed by the
        normalized title; a file Commons does not have is absent."""
        body = self._api(
            _COMMONS_API_URL,
            {
                "action": "query",
                "prop": "imageinfo",
                "titles": "|".join(f"File:{title}" for title in titles),
                "iiprop": "size|url",
                "iiurlwidth": _THUMBNAIL_WIDTH,
                "formatversion": 2,
            },
        )
        if body is None:
            return None
        infos: dict[str, dict[str, Any]] = {}
        for page in body.get("query", {}).get("pages") or []:
            if not isinstance(page, dict) or not isinstance(page.get("title"), str):
                continue
            entries = page.get("imageinfo")
            title = normalize_title(page["title"])
            if title is not None and isinstance(entries, list) and entries and isinstance(entries[0], dict):
                infos[title] = entries[0]
        return infos

    def fetch(self, url: str) -> tuple[bytes, str] | None:
        try:
            response = self._client.get(url)
        except httpx.HTTPError:
            return None
        media_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if response.status_code != 200 or len(response.content) > _THUMBNAIL_MAX_BYTES:
            return None
        return response.content, media_type


def _claim_strings(claims: dict[str, Any], prop: str) -> list[str]:
    values = []
    for statement in claims.get(prop) or []:
        if not isinstance(statement, dict) or statement.get("rank") == "deprecated":
            continue
        value = (statement.get("mainsnak") or {}).get("datavalue", {}).get("value")
        if isinstance(value, str):
            values.append(value)
    return values


def gather_pool(wikimedia: Wikimedia, *, qid: str, stored: str | None) -> list[PoolFile] | None:
    """The species' photographs to judge, the stored one among them: the item's images, then
    the stored file, then its Commons category's files, raster only and at most `_POOL_LIMIT`.
    `None` when Wikimedia could not be read, or the stored photo could not be shown."""
    item = wikimedia.item(qid)
    if item is None:
        return None
    images, category = item
    titles = [*images, *([stored] if stored else [])]
    if category:
        members = wikimedia.category_files(category)
        if members is None:
            return None
        titles += members
    chosen = list(dict.fromkeys(title for raw in titles if (title := normalize_title(raw)) is not None))[:_POOL_LIMIT]
    if not chosen:
        return []
    infos = wikimedia.thumbnails(chosen)
    if infos is None:
        return None

    pool = []
    for title in chosen:
        info = infos.get(title)
        url = (info or {}).get("thumburl") or (info or {}).get("url")
        if not isinstance(url, str) or (fetched := wikimedia.fetch(url)) is None:
            continue
        data, content_type = fetched
        if (media_type := _IMAGE_MEDIA_TYPES.get(content_type)) is None:
            continue
        width, height = (info or {}).get("width"), (info or {}).get("height")
        pool.append(
            PoolFile(
                title=title,
                width=width if isinstance(width, int) else None,
                height=height if isinstance(height, int) else None,
                media_type=media_type,
                data=data,
                is_stored=title == stored,
            )
        )
    if stored and not any(file.is_stored for file in pool):
        return None
    return pool


def _names(species: dict[str, Any]) -> str:
    common = species.get("common_name")
    return f"{species['scientific_name']} ({common})" if common else species["scientific_name"]


def build_request(
    species: dict[str, Any], pool: list[PoolFile]
) -> tuple[list[TextBlockParam | ImageBlockParam], dict[str, object]]:
    """The content blocks and the answer's schema for one species."""
    has_stored = any(file.is_stored for file in pool)
    prompt = _PROMPT.format(
        names=_names(species),
        stored="The one marked as the stored photo is the one shown now." if has_stored else "",
        bar=_STORED_BAR if has_stored else _UNSTORED_BAR,
    )
    content: list[TextBlockParam | ImageBlockParam] = [{"type": "text", "text": prompt}]
    for file in pool:
        size = f"{file.width} x {file.height} px" if file.width and file.height else "size unknown"
        label = f"{file.title} ({size}){' - the stored photo' if file.is_stored else ''}"
        content.append({"type": "text", "text": label})
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": file.media_type,
                    "data": base64.standard_b64encode(file.data).decode("ascii"),
                },
            }
        )
    schema: dict[str, object] = {
        "type": "object",
        "properties": {
            "choice": {"type": "string", "enum": [*(file.title for file in pool), _NONE]},
            "reason": {"type": "string"},
        },
        "required": ["choice", "reason"],
        "additionalProperties": False,
    }
    return content, schema


def read_verdict(message: Any, pool: list[PoolFile]) -> Verdict:
    """The model's answer, checked rather than trusted: a refusal, a cut-off answer, one that
    is not the schema's shape, or a title it was not shown is a problem, never a proposal."""
    if message.stop_reason == "refusal":
        return Verdict(None, "", problem="the model declined to answer")
    if message.stop_reason == "max_tokens":
        return Verdict(None, "", problem="the answer was cut off")
    text = "".join(block.text for block in message.content if getattr(block, "type", None) == "text")
    try:
        answer = json.loads(text)
    except ValueError:
        return Verdict(None, "", problem="the answer was not JSON")
    if not isinstance(answer, dict) or not isinstance(choice := answer.get("choice"), str):
        return Verdict(None, "", problem="the answer had no choice")
    reason = raw if isinstance(raw := answer.get("reason"), str) else ""
    if choice == _NONE:
        return Verdict(None, reason)
    shown = {file.title: file for file in pool}
    if choice not in shown:
        return Verdict(None, reason, problem=f"the answer named a file it was not shown: {choice!r}")
    if shown[choice].is_stored:
        return Verdict(None, reason)
    return Verdict(choice, reason)


def judge(model_client: anthropic.Anthropic, model: str, species: dict[str, Any], pool: list[PoolFile]) -> Verdict:
    content, schema = build_request(species, pool)
    message = model_client.messages.create(
        model=model,
        max_tokens=16000,
        messages=[{"role": "user", "content": content}],
        output_config={"format": {"type": "json_schema", "schema": schema}},
    )
    return read_verdict(message, pool)


@dataclass(slots=True)
class Tally:
    read: int = 0
    proposed: int = 0
    refused: int = 0
    answered_none: int = 0
    skipped: Counter[str] = field(default_factory=Counter)

    def line(self, *, dry_run: bool) -> str:
        skipped = ", ".join(f"{count} {reason}" for reason, count in sorted(self.skipped.items())) or "none"
        proposed = "would propose" if dry_run else "proposed"
        return (
            f"Species read: {self.read}; skipped: {skipped}; {proposed}: {self.proposed}; "
            f"refused: {self.refused}; answered none: {self.answered_none}."
        )


def run(
    instance: Instance,
    wikimedia: Wikimedia,
    rate: Callable[[dict[str, Any], list[PoolFile]], Verdict],
    *,
    dry_run: bool,
    limit: int | None,
    out: Callable[[str], None] = print,
) -> Tally:
    """Rate the account's open species one at a time, in life-list order, proposing what the
    model chose. Stops early at `limit` species rated, and on the instance's daily limit."""
    tally = Tally()
    settled = instance.proposed_species()
    rated = 0
    for entry in instance.life_list():
        if limit is not None and rated >= limit:
            break
        tally.read += 1
        name = entry["scientific_name"]
        if entry["uuid"] in settled:
            tally.skipped[_SKIP_ALREADY_PROPOSED] += 1
            continue
        species = instance.species(entry["uuid"])
        if not species.get("accepts_photo_proposals"):
            tally.skipped[_SKIP_CHOSEN_BY_OPERATOR] += 1
            continue
        if not species.get("wikidata_qid"):
            tally.skipped[_SKIP_NO_WIKIDATA] += 1
            continue
        stored = normalize_title(species["photo_file"]) if species.get("photo_file") else None
        pool = gather_pool(wikimedia, qid=species["wikidata_qid"], stored=stored)
        if pool is None:
            tally.skipped[_SKIP_POOL_UNREADABLE] += 1
            out(f"{name}: skipped, Wikimedia could not be read for its photos")
            continue
        if not any(not file.is_stored for file in pool):
            tally.skipped[_SKIP_NO_ALTERNATIVE] += 1
            continue

        rated += 1
        verdict = rate(species, pool)
        if verdict.problem is not None:
            tally.skipped[_SKIP_NO_USABLE_ANSWER] += 1
            out(f"{name}: nothing proposed, {verdict.problem}")
            continue
        if verdict.title is None:
            tally.answered_none += 1
            out(f"{name}: none - {verdict.reason}")
            continue
        note = verdict.reason[:_NOTE_MAX_LENGTH]
        if dry_run:
            tally.proposed += 1
            out(f"{name}: would propose {verdict.title} - {note}")
            continue
        try:
            refusal = instance.propose(species["uuid"], file=verdict.title, note=note)
        except RateLimited:
            out(
                "The instance's daily limit on photo proposals answered (SPECIES_PHOTO_PROPOSAL_RATE_LIMIT_PER_USER). "
                "Run again later to continue: species already proposed are skipped."
            )
            break
        if refusal is not None:
            tally.refused += 1
            out(f"{name}: {verdict.title} refused, {refusal}")
            continue
        tally.proposed += 1
        out(f"{name}: proposed {verdict.title} - {note}")
    return tally


def _arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--instance", required=True, help="the instance's address, e.g. https://opendiving.app")
    parser.add_argument(
        "--contact", required=True, help="an email address or page Wikimedia can reach you at; sent in the User-Agent"
    )
    parser.add_argument(
        "--model", default=DEFAULT_MODEL, help=f"the model that judges the photos (default {DEFAULT_MODEL})"
    )
    parser.add_argument("--dry-run", action="store_true", help="rate and print, proposing nothing")
    parser.add_argument("--limit", type=int, help="rate at most this many species")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    key = os.environ.get("OPENDIVING_API_KEY")
    if not key:
        print("OPENDIVING_API_KEY is not set: create a write API key in the instance's Settings.", file=sys.stderr)
        return 2

    api_url = f"{args.instance.rstrip('/')}/api/v1"
    headers = {"Authorization": f"Bearer {key}", "User-Agent": "OpenDiving-species-photo-rater/1.0"}
    model_client = anthropic.Anthropic()
    tally = Tally()
    status = 0
    with (
        httpx.Client(base_url=api_url, headers=headers, timeout=_TIMEOUT_SECONDS) as api,
        httpx.Client(
            headers={"User-Agent": wikimedia_user_agent(args.contact)}, timeout=_TIMEOUT_SECONDS, follow_redirects=True
        ) as wiki,
    ):
        try:
            tally = run(
                Instance(api),
                Wikimedia(wiki),
                lambda species, pool: judge(model_client, args.model, species, pool),
                dry_run=args.dry_run,
                limit=args.limit,
            )
        except InstanceError as error:
            print(f"Stopped: {error}", file=sys.stderr)
            status = 1
        except httpx.HTTPError as error:
            print(f"Stopped: the instance could not be reached ({type(error).__name__})", file=sys.stderr)
            status = 1
        except anthropic.APIError as error:
            print(f"Stopped: the model request failed ({type(error).__name__})", file=sys.stderr)
            status = 1
    print(tally.line(dry_run=args.dry_run))
    return status


if __name__ == "__main__":
    sys.exit(main())
