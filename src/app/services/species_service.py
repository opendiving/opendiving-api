"""The species catalog: a merged live search over two public registers, and the resolve
step that turns a search hit into a local row.

Modelled closely on `services.geocoding_service`, and the doctrine is the same with one
deliberate exception. **Search never raises for provider trouble.** WoRMS down, Wikidata
down, Redis down, both providers down - `search_species` still answers, from the local
catalog if that is all there is, because a diver filling in a dive form must not be blocked
by somebody else's outage. **Resolve is the exception**: it 503s rather than inventing a
catalog row, because a row here is a claim about what a taxon *is*, keyed to an identifier
this app does not own. A missing suggestion costs a diver one retry; a wrong catalog row is
shared with every account on the instance and never rewritten (see `models/species.py`).

**Two sources, because one cannot do the job.** WoRMS is the taxonomic authority - it
supplies scientific names, synonyms and the accepted-taxon mapping that gives every row its
identity - but its vernacular coverage is thin to the point of uselessness for a picker:
*Amphiprion ocellaris* carries exactly one common name in WoRMS, and it is in Japanese, so a
diver typing "clownfish" would not find Nemo. Wikidata fills that in, is CC0, and is keyed
to WoRMS through property P850, which is what lets the two be merged on `aphia_id` at all.

**Why this asks one taxon at a time rather than importing the register.** WoRMS's full
database download is proprietary - registration, a vetted application, a non-transferable
licence - and the CC BY copy on GBIF is served only to GBIF. The REST webservice used here
is free with citation and asks callers not to harvest the register wholesale, which is
exactly what an on-demand catalog does not do. The lawful bulk route (paging GBIF's own
copy) is recorded in DECISIONS.md as the escalation, not taken here.

Both providers are throttled instance-wide and every answer is cached, for the reason
Nominatim's policy makes load-bearing next door: a courtesy that is only observed when
traffic is low is not one.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import anyio
import httpx
from fastapi import HTTPException
from pydantic import ValidationError
from redis.exceptions import RedisError
from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.exceptions.http_exceptions import RateLimitException
from ..core.utils import cache
from ..core.utils.rate_limit import enforce_rate_limit
from ..core.utils.search import LIKE_ESCAPE_CHAR, escape_like
from ..models.species import Species
from ..models.species_name import SpeciesName
from ..schemas.species import SpeciesSearchResponse, SpeciesSearchResult

logger = logging.getLogger(__name__)

# **WoRMS is slow, and these numbers are measured rather than inherited.** The geocoder next
# door uses a 5 s read timeout against Nominatim, which answers in tens of milliseconds; the
# first draft of this module copied that figure and the result was a feature that did not
# work at all. Measured against the live register: `AphiaRecordsByName?like=true` 6.3 s,
# `AphiaRecordsByVernacular?like=true` 8.6 s, `AphiaRecordByAphiaID` 11.2 s, and one name
# search that had not answered after 40 s. A substring scan over a quarter of a million taxa
# is simply not a fast query, and `like=true` is the only way to back a type-ahead.
#
# So the bounds are set where WoRMS can actually answer, and the *callers* below decide how
# long they are each willing to wait - search on a keystroke budget, resolve on a much longer
# one. There is still no retry, for the reason the geocoder gives.
_TIMEOUT = httpx.Timeout(15.0)

# The per-request hard cap: `_TIMEOUT` bounds each socket read, so a host that dribbles one
# byte at a time never trips it and would hold the request open forever. Above the longest
# budget below, so it only ever fires for a host behaving pathologically rather than slowly.
_DEADLINE_SECONDS = 30.0
_MAX_RESPONSE_BYTES = 512 * 1024

# What the whole search fan-out is willing to spend before answering with whatever arrived.
# A picker is typed into, so this is a human-patience number, not a provider number - and on
# the measurements above WoRMS will often miss it. That is the intended outcome: Wikidata and
# the local catalog answer in well under a second, and a diver gets those now rather than the
# complete answer in eight seconds. What must not happen is the *incomplete* answer being
# cached as though it were complete - see `_store_search`.
_SEARCH_BUDGET_SECONDS = 6.0

# What `resolve_species` is willing to spend on the one call it cannot do without. Far longer,
# because this is a deliberate "add this species" click with a spinner against it rather than
# a keystroke, and because the alternative to waiting is a 503 that leaves the diver unable to
# log what they saw. The enrichment fan-out that follows gets its own, shorter budget: names
# and a Wikidata id are worth a moment, not a stall.
_RESOLVE_BUDGET_SECONDS = 25.0
_ENRICHMENT_BUDGET_SECONDS = 10.0

# What a picker can usefully show before "keep typing" is better advice than another row.
_MAX_RESULTS = 25

# WoRMS pages its list endpoints at 50, and this app never asks for page 2: more than fifty
# raw hits for one typed fragment is what `has_more` exists to say. Wikidata's search is
# asked for ten, which is plenty once it is merged into WoRMS's fifty.
_WORMS_PAGE_SIZE = 50
_WIKIDATA_SEARCH_LIMIT = 10

# How many entities to ask `wbgetentities` for at once. Small on purpose - see
# `_wikidata_entities`: a taxon entity with all its claims runs ~50 KB, so a batch of ten
# regularly exceeds `_MAX_RESPONSE_BYTES` and costs the whole Wikidata contribution. Four
# leaves roughly a two-fold margin against the heaviest entities measured.
_WIKIDATA_ENTITY_BATCH = 4

# A taxon's name does not change - that is rather the point of a nomenclatural register - so
# a hit is held for a month. A *miss* is held for an hour, because an empty answer is far
# more likely to be provider weirdness than a fact about the sea, and pinning it for a month
# would make one bad afternoon look like a broken feature.
_HIT_TTL_SECONDS = 30 * 24 * 60 * 60
_MISS_TTL_SECONDS = 60 * 60

# Bumped whenever the cached shape or the way it is composed changes. What is cached is the
# *normalized, merged* remote list rather than raw provider payloads, so a change to the
# normalizer has to invalidate the old entries - a new prefix does that without a flush.
_CACHE_VERSION = "v1"

# Wikidata's "WoRMS AphiaID" property. The single hinge the whole two-source design turns
# on: without a shared key there would be nothing to merge two registers *on*.
_APHIA_PROPERTY = "P850"
# Wikidata's "taxon name" property - the scientific name, used when an entity's English
# label is something else.
_TAXON_NAME_PROPERTY = "P225"

# Attribution travels per result because it is a licence condition of the data, not a footer.
# WoRMS asks to be cited; Wikidata is CC0 and asks for nothing, and is credited anyway
# because "where did this name come from" is a fair question to be able to answer.
#
# Catalog rows re-emit the WoRMS credit: every row in the catalog was resolved through
# WoRMS, whatever else enriched it. Constants rather than a column - the string is a
# property of the source, not of the row, and storing it would mean a migration to correct
# a typo.
_WORMS_ATTRIBUTION = "World Register of Marine Species (marinespecies.org)"
_WIKIDATA_ATTRIBUTION = "Wikidata (CC0)"

# Mirror the bounds on `schemas.species` and the column widths behind them. Applied by
# truncating here rather than by letting an over-long provider string raise inside a
# normalizer, which would turn one verbose record into a failed search.
_NAME_MAX_LENGTH = 255
_RANK_MAX_LENGTH = 64
_QID_MAX_LENGTH = 32
_LANGUAGE_CODE_MAX_LENGTH = 3

# How long a request will wait on a provider's cap before giving up on that provider. Unlike
# the geocoder, which waits a beat because it has one provider and no other answer, a search
# here has two more sources - so a saturated provider drops out immediately rather than
# holding the whole fan-out open.
_PROVIDER_WORMS = "worms"
_PROVIDER_WIKIDATA = "wikidata"


@dataclass(frozen=True, slots=True)
class _Taxon:
    """One WoRMS record, normalized - everything `models.species` stores about a taxon.

    Only `resolve_species` builds these. Search works in `SpeciesSearchResult`s directly,
    since it never needs the classification or the habitat flags.
    """

    aphia_id: int
    scientific_name: str
    authority: str | None
    rank: str
    status: str
    kingdom: str | None
    phylum: str | None
    class_name: str | None
    order_name: str | None
    family: str | None
    genus: str | None
    is_marine: bool | None
    is_brackish: bool | None
    is_freshwater: bool | None
    # The accepted taxon this record defers to, when it is not itself accepted. Equal to
    # `aphia_id` for an accepted record, and the whole reason `resolve_species` can be handed
    # a synonym's id and still store the right row.
    valid_aphia_id: int | None


@dataclass(frozen=True, slots=True)
class _WikidataEntity:
    """One Wikidata entity, reduced to the four things this app wants from it."""

    qid: str
    aphia_id: int
    scientific_name: str | None
    label: str | None
    aliases: tuple[str, ...]


# -------------- caching --------------


def _cache_key(kind: str, discriminator: str) -> str:
    """Species cache keys are the **second** deliberate exception to "cache keys stay
    user-scoped", after `geocode:`, and for the same reason: "what is *Amphiprion ocellaris*
    called" has one answer for everybody, and one lookup serving every diver who ever types
    it is precisely what makes asking a free public register defensible. Keying per user
    would multiply outbound calls by the number of accounts for no benefit to anyone.

    The `species:` prefix keeps them clear of the `user_{id}_*` namespace
    `services.cache_invalidation` sweeps by pattern, so nothing here is collateral damage of
    a mutation elsewhere - and nothing here needs invalidating, only expiring.

    The query rides in the key as itself, unlike the geocoder's digest. It is bounded at 255
    characters by the route and whitespace-collapsed before it gets here, so key length is
    not the concern it is next door - and a readable key is what makes
    `redis-cli --scan --pattern 'species:*'` a usable way to see whether the cache is doing
    its job.
    """
    return f"species:{_CACHE_VERSION}:{kind}:{discriminator}"


async def _cached_search(key: str) -> SpeciesSearchResponse | None:
    """The cached remote results for `key`, or `None` for "nothing usable cached".

    An empty result list is a real cached answer ("neither register had anything"), and is
    distinct from `None` - that distinction is what stops a negative answer being re-asked on
    every keystroke. Redis absent or unreachable is a miss, not an error.
    """
    if cache.client is None:
        return None

    try:
        raw = await cache.client.get(key)
    except RedisError as exc:
        logger.warning("Species cache read failed (%s); asking the providers instead.", type(exc).__name__)
        return None

    if raw is None:
        return None

    try:
        return SpeciesSearchResponse(**json.loads(raw.decode()))
    except ValueError, TypeError, ValidationError:
        logger.warning("Discarding an unreadable species cache entry at %s.", key)
        return None


async def _store_search(key: str, response: SpeciesSearchResponse, *, complete: bool) -> None:
    """Cache a search answer, for a month or for an hour.

    `complete` is the flag that keeps a slow day from poisoning a month. WoRMS regularly
    misses `_SEARCH_BUDGET_SECONDS` (see the measurements at the top of this module), and the
    answer that comes back without it is real, useful and *partial* - Wikidata common names
    with no taxonomy behind them. Storing that under the month-long hit TTL would mean one
    slow afternoon deciding what "clownfish" returns until the key expired, including on every
    later day when WoRMS was answering in a second.

    So a partial answer keeps the short TTL and is re-asked within the hour. The same
    principle the geocoder applies to "we could not ask at all", extended to "we could not ask
    all of them" - which is a distinction a two-source search has and a one-source one does
    not.
    """
    if cache.client is None:
        return

    ttl = _HIT_TTL_SECONDS if response.results and complete else _MISS_TTL_SECONDS
    try:
        await cache.client.set(key, response.model_dump_json(), ex=ttl)
    except RedisError as exc:
        logger.warning("Species cache write failed (%s).", type(exc).__name__)


# -------------- outbound requests --------------


async def _claim_provider_slot(provider: str) -> bool:
    """Take one slot against the instance-wide cap for `provider`, or report there is none.

    A boolean rather than the exception `enforce_rate_limit` raises: being over the cap here
    is an ordinary branch a caller decides about, not an error to propagate. Neither cap is
    published by the provider it throttles - both are self-imposed politeness (see
    `core/config.py`) - so exceeding one is never a caller's fault and never a 429.
    """
    limits = {
        _PROVIDER_WORMS: (settings.SPECIES_WORMS_RATE_LIMIT_REQUESTS, settings.SPECIES_WORMS_RATE_LIMIT_WINDOW_SECONDS),
        _PROVIDER_WIKIDATA: (
            settings.SPECIES_WIKIDATA_RATE_LIMIT_REQUESTS,
            settings.SPECIES_WIKIDATA_RATE_LIMIT_WINDOW_SECONDS,
        ),
    }
    max_requests, window = limits[provider]
    try:
        await enforce_rate_limit(f"species:provider:{provider}", max_requests, window)
    except RateLimitException:
        return False
    return True


async def _request(provider: str, url: str, params: dict[str, Any]) -> Any | None:
    """Ask a provider, returning its parsed body - or `None` when we could not ask at all.

    That distinction is the one thing this function exists to preserve, exactly as in the
    geocoder: "the register answered, and had nothing" is worth caching, "the register timed
    out" is not, and caching the second would turn a thirty-second outage into a month of
    empty answers.

    **A `204` is an answer.** WoRMS says "no such name" with an empty body and that status,
    not with `[]`, so a reader that only handles 200 turns every genuine miss into a
    provider failure - and then never caches it, and re-asks on the next keystroke.

    Being over the provider's cap counts as "could not ask". Unlike the geocoder this does
    not wait a beat for the cap to free up: a search has two other sources in flight, so
    dropping one immediately degrades the answer rather than the latency.
    """
    if not url:
        logger.warning("No URL configured for the %s species provider.", provider)
        return None

    if not await _claim_provider_slot(provider):
        logger.warning("Skipping a %s call: this instance is over its provider rate limit.", provider)
        return None

    try:
        # `_TIMEOUT` bounds each socket read, which is not the same as bounding the call.
        # `fail_after` is the actual deadline; the byte cap below bounds how much a slow,
        # endless host can make this process buffer.
        with anyio.fail_after(_DEADLINE_SECONDS):
            # A client per call, deliberately, and knowingly against httpx's advice: the
            # provider caps above hold this to a few requests a second, so a pooled
            # connection would sit idle far longer than any keep-alive, and a module-level
            # client would need lifespan wiring to be closed. Same trade as the geocoder.
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                headers = {"User-Agent": settings.SPECIES_USER_AGENT, "Accept": "application/json"}
                async with client.stream("GET", url, params=params, headers=headers) as response:
                    response.raise_for_status()
                    if response.status_code == 204:
                        return []

                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_RESPONSE_BYTES:
                            logger.warning("A %s response exceeded %d bytes.", provider, _MAX_RESPONSE_BYTES)
                            return None

        # An empty 200 body, which WoRMS also produces for some no-result queries. Same
        # meaning as the 204 above; `json.loads` on it would raise and be logged as garbage.
        if not body:
            return []
        return json.loads(body)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        # `InvalidURL` is listed separately because it descends from `Exception` rather than
        # `HTTPError`, so a merely malformed `WORMS_API_URL` would otherwise escape as a 500
        # and break this module's one promise. The provider name is logged, never the built
        # URL: it carries the diver's typed query, and logs get collected and kept.
        logger.warning("A %s request failed (%s).", provider, type(exc).__name__)
        return None
    except TimeoutError:
        logger.warning("A %s request exceeded its %ss deadline.", provider, _DEADLINE_SECONDS)
        return None
    except ValueError:
        logger.warning("A %s response was not JSON.", provider)
        return None


async def _worms(endpoint: str, segment: str | int, params: dict[str, Any] | None = None) -> Any | None:
    """Call one of WoRMS's `/{endpoint}/{segment}` routes.

    **The segment is percent-encoded here, and that is why this signature takes it apart
    rather than accepting a ready-made path.** WoRMS puts the search term in the URL *path*,
    unlike the geocoder next door which passes user text as a query parameter - so
    interpolating it raw is a path injection: httpx resolves dot segments before sending, and
    a diver searching for `../../../etc/passwd` would have this server request
    `marinespecies.org/etc/passwd` instead, with the answer cached for a month under their
    string. A `#` or `?` in the term is the quieter half of the same bug - both truncate the
    search silently rather than being sent as part of the name.

    `safe=""` because *nothing* is safe in a taxon name: `/` has to be encoded or it makes a
    new path segment, and WoRMS's own routes take one segment. Structural rather than a call
    to remember at each site - there is no way to reach `_request` with an unencoded segment
    from here.
    """
    quoted = quote(str(segment), safe="")
    url = f"{settings.WORMS_API_URL.rstrip('/')}/{endpoint}/{quoted}"
    return await _request(_PROVIDER_WORMS, url, params or {})


async def _wikidata(params: dict[str, Any]) -> Any | None:
    return await _request(_PROVIDER_WIKIDATA, settings.WIKIDATA_API_URL, {**params, "format": "json"})


# -------------- normalization --------------


def _text(value: Any, limit: int = _NAME_MAX_LENGTH) -> str | None:
    """A trimmed, bounded string, or `None` for anything blank or not a string.

    Provider JSON is untyped, so every field either register hands over is a maybe-string -
    and a bounded one, because these are written straight into `VARCHAR(n)` columns and
    handed to every client. Truncating beats dropping the record: a taxon with a
    three-hundred-character authority string is still the taxon the diver searched for.
    """
    if not isinstance(value, str):
        return None
    return value.strip()[:limit] or None


def _flag(value: Any) -> bool | None:
    """WoRMS's habitat flags, which arrive as `1`/`0`/`null` rather than as booleans.

    Three-valued on the way out too: `None` means "WoRMS did not say", which is genuinely
    different from "no" and is what the nullable columns exist to preserve.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    return None


def _worms_taxon(row: Any) -> _Taxon | None:
    """One WoRMS `AphiaRecord`, or `None` for a row this app can do nothing with.

    A record with no AphiaID or no scientific name is not a taxon this catalog can key on,
    and surfacing it in a picker would offer the diver something unresolvable.
    """
    if not isinstance(row, dict):
        return None

    aphia_id = row.get("AphiaID")
    scientific_name = _text(row.get("scientificname"))
    if not isinstance(aphia_id, int) or aphia_id <= 0 or scientific_name is None:
        return None

    valid_aphia_id = row.get("valid_AphiaID")
    return _Taxon(
        aphia_id=aphia_id,
        scientific_name=scientific_name,
        authority=_text(row.get("authority")),
        # Both fall back rather than dropping the record: they are `NOT NULL` columns whose
        # values are somebody else's open vocabulary, so a record WoRMS sent without one is
        # better stored as "unknown" than refused.
        rank=_text(row.get("rank"), _RANK_MAX_LENGTH) or "unknown",
        status=_text(row.get("status"), _RANK_MAX_LENGTH) or "unknown",
        kingdom=_text(row.get("kingdom")),
        phylum=_text(row.get("phylum")),
        # WoRMS sends these under the bare rank names; the columns are `class_name`/
        # `order_name` because `class` is a Python keyword and `order` a reserved SQL word.
        class_name=_text(row.get("class")),
        order_name=_text(row.get("order")),
        family=_text(row.get("family")),
        genus=_text(row.get("genus")),
        is_marine=_flag(row.get("isMarine")),
        is_brackish=_flag(row.get("isBrackish")),
        is_freshwater=_flag(row.get("isFreshwater")),
        valid_aphia_id=valid_aphia_id if isinstance(valid_aphia_id, int) and valid_aphia_id > 0 else None,
    )


def _worms_result(row: Any) -> SpeciesSearchResult | None:
    """One WoRMS record as a search hit, folded onto its accepted taxon.

    This is where a diver typing *Manta birostris* gets *Mobula birostris* back. WoRMS sends
    `valid_AphiaID`/`valid_name` inline on an unaccepted record, so the fold costs no second
    request - and the superseded name the diver actually typed is kept as `matched_name`,
    since being told your name is out of date is useful and being silently handed a different
    binomial is not.
    """
    taxon = _worms_taxon(row)
    if taxon is None:
        return None

    matched_name: str | None = None
    scientific_name = taxon.scientific_name
    aphia_id = taxon.aphia_id
    if taxon.valid_aphia_id is not None and taxon.valid_aphia_id != taxon.aphia_id:
        valid_name = _text(row.get("valid_name"))
        if valid_name is not None:
            matched_name = taxon.scientific_name
            scientific_name = valid_name
            aphia_id = taxon.valid_aphia_id

    return SpeciesSearchResult(
        aphia_id=aphia_id,
        uuid=None,
        scientific_name=scientific_name,
        common_name=None,
        rank=taxon.rank,
        # "accepted" rather than the record's own status whenever the fold above happened:
        # the row now describes the accepted taxon, and reporting the synonym's status would
        # label the wrong thing.
        status="accepted" if matched_name is not None else taxon.status,
        matched_name=matched_name,
        source="worms",
        attribution=_WORMS_ATTRIBUTION,
    )


def _wikidata_entity(qid: str, entity: Any) -> _WikidataEntity | None:
    """One `wbgetentities` entity, reduced - or `None` when it carries no AphiaID.

    Entities without P850 should not reach here at all, since the search asks for
    `haswbstatement:P850`. Dropped rather than trusted: an entity with no AphiaID cannot be
    merged with anything WoRMS said, and a search hit the client cannot resolve is worse
    than one fewer row.
    """
    if not isinstance(entity, dict):
        return None

    claims = entity.get("claims")
    if not isinstance(claims, dict):
        return None

    raw_aphia = _claim_value(claims, _APHIA_PROPERTY)
    if raw_aphia is None:
        return None
    try:
        # Stored as a string in Wikidata - external identifiers are strings there, whatever
        # they look like.
        aphia_id = int(raw_aphia)
    except ValueError, TypeError:
        return None
    if aphia_id <= 0:
        return None

    labels = entity.get("labels")
    label = None
    if isinstance(labels, dict) and isinstance(labels.get("en"), dict):
        label = _text(labels["en"].get("value"))

    aliases: list[str] = []
    raw_aliases = entity.get("aliases")
    if isinstance(raw_aliases, dict) and isinstance(raw_aliases.get("en"), list):
        for alias in raw_aliases["en"]:
            if isinstance(alias, dict) and (value := _text(alias.get("value"))) is not None:
                aliases.append(value)

    return _WikidataEntity(
        qid=qid[:_QID_MAX_LENGTH],
        aphia_id=aphia_id,
        scientific_name=_claim_value(claims, _TAXON_NAME_PROPERTY),
        label=label,
        aliases=tuple(aliases),
    )


def _claim_value(claims: dict[str, Any], prop: str) -> str | None:
    """The first plain-string value of a Wikidata property, if it has one.

    Wikidata nests every claim four levels deep and any level can be missing or be a type
    this cares nothing about, so each step is checked rather than assumed - a malformed
    entity should cost its own row, never the search.
    """
    statements = claims.get(prop)
    if not isinstance(statements, list):
        return None
    for statement in statements:
        if not isinstance(statement, dict):
            continue
        snak = statement.get("mainsnak")
        if not isinstance(snak, dict):
            continue
        datavalue = snak.get("datavalue")
        if not isinstance(datavalue, dict):
            continue
        if (value := _text(datavalue.get("value"))) is not None:
            return value
    return None


def _wikidata_result(entity: _WikidataEntity) -> SpeciesSearchResult | None:
    """A Wikidata entity as a search hit.

    Needs a scientific name to show, which is P225 when the entity has it and the English
    label otherwise - for most taxa the label *is* the binomial, which is exactly why
    `_choose_common_name` prefers a label that differs from it.
    """
    scientific_name = entity.scientific_name or entity.label
    if scientific_name is None:
        return None

    common_name = _choose_common_name(scientific_name=scientific_name, label=entity.label, aliases=entity.aliases)
    return SpeciesSearchResult(
        aphia_id=entity.aphia_id,
        uuid=None,
        scientific_name=scientific_name,
        common_name=common_name,
        # Wikidata does not carry WoRMS's rank vocabulary, and guessing from the entity's
        # "instance of" claims would mean a third property and a mapping table for a field
        # the picker only shows as context. A hit that WoRMS also returned is merged onto
        # WoRMS's record and gets the real rank; one only Wikidata found says so.
        rank="unknown",
        status="unknown",
        matched_name=None,
        source="wikidata",
        attribution=_WIKIDATA_ATTRIBUTION,
    )


def _choose_common_name(
    *, scientific_name: str, label: str | None, aliases: tuple[str, ...], vernaculars: tuple[str, ...] = ()
) -> str | None:
    """The one English name to display, or `None` to fall back to the scientific name.

    The order is forced by what the sources actually contain. A taxon's English Wikidata
    *label* is very often the binomial itself, which would make "common name" a duplicate of
    the column next to it - so a label is only taken when it differs, and the first English
    alias ("ocellaris clownfish") is what usually carries the real name. WoRMS vernaculars
    come last because their English coverage is the thin part; they are still tried, because
    a taxon Wikidata has never heard of may well have one.

    Comparison is case-insensitive, and it is a *prefix* test rather than equality. Both
    halves of that were forced by real answers. "Amphiprion Ocellaris" as a label is the
    scientific name wearing a capital; and Wikidata labels obscure taxa with the binomial plus
    its authority - resolving one returned the label
    "Leptasterias (Leptasterias) muelleri muelleri (M. Sars, 1846)", which under an equality
    test is "different from the scientific name" and would have been displayed as that taxon's
    common name. A name that begins with the binomial is the binomial with decoration on it,
    not something a diver would ever call the animal.
    """
    folded = scientific_name.casefold()

    def is_vernacular(candidate: str) -> bool:
        return not candidate.casefold().startswith(folded)

    if label is not None and is_vernacular(label):
        return label
    for candidate in (*aliases, *vernaculars):
        if is_vernacular(candidate):
            return candidate
    return None


# -------------- search --------------


@dataclass(frozen=True, slots=True)
class _SourceAnswer:
    """What one source contributed to a search, and whether it actually managed to answer.

    `ok` is the field that exists to be *false*, and it is the difference between "this
    register has nothing for you" and "this register did not tell us anything". Both look
    identical downstream - an empty `results` list - and conflating them is what let a
    thirty-second outage get written into a thirty-day cache entry: a source that failed
    fast still returned, so it counted as having answered, and the partial result was stored
    as though it were the whole truth. `_store_search` reads this.
    """

    results: list[SpeciesSearchResult]
    # True when the source had more matches than it sent, which is `has_more` regardless of
    # what the merge does afterwards.
    page_was_full: bool
    ok: bool


async def _worms_by_name(query: str) -> _SourceAnswer:
    """Scientific names and synonyms.

    `marine_only=false` because this app logs freshwater dives too, and WoRMS carries
    brackish and freshwater taxa that the default would hide.
    """
    rows = await _worms("AphiaRecordsByName", query, {"like": "true", "marine_only": "false"})
    return _worms_page(rows)


async def _worms_by_vernacular(query: str) -> _SourceAnswer:
    """Common names, as far as WoRMS has them - which is not far, hence Wikidata."""
    rows = await _worms("AphiaRecordsByVernacular", query, {"like": "true"})
    return _worms_page(rows)


def _worms_page(rows: Any) -> _SourceAnswer:
    """A page of WoRMS records as results, plus whether the page was full and whether WoRMS
    answered at all.

    `_request` hands back `None` for every way of not getting an answer - unreachable, an
    error status, a body over the size cap, a body that did not parse - and `[]` only when
    the register genuinely said "no such name". That distinction is preserved here rather
    than collapsed, because it decides how long the merged answer is cached for.
    """
    if rows is None:
        return _SourceAnswer([], False, ok=False)
    if not isinstance(rows, list):
        # Valid JSON that is not an array is not WoRMS answering - a proxy or an error page.
        return _SourceAnswer([], False, ok=False)
    results = [result for row in rows if (result := _worms_result(row)) is not None]
    return _SourceAnswer(results, len(rows) >= _WORMS_PAGE_SIZE, ok=True)


async def _wikidata_search(query: str) -> _SourceAnswer:
    """Common names and aliases, via CirrusSearch filtered to entities that carry an AphiaID.

    Two chained steps, counting as one provider against the throttle: the search returns QIDs
    and nothing else useful, so the entities have to be fetched to get P850 at all. The
    `haswbstatement:P850` filter is what keeps the result set to taxa WoRMS also knows, which
    is what makes the merge possible.
    """
    payload = await _wikidata(
        {
            "action": "query",
            "list": "search",
            "srsearch": f"{query} haswbstatement:{_APHIA_PROPERTY}",
            "srlimit": _WIKIDATA_SEARCH_LIMIT,
        }
    )
    qids = _wikidata_qids(payload)
    if qids is None:
        # Not a search response at all: transport failure, or a 200 carrying an error. Either
        # way this source learned nothing, which is not the same as finding nothing.
        return _SourceAnswer([], False, ok=False)
    if not qids:
        return _SourceAnswer([], False, ok=True)

    entities, entities_ok = await _wikidata_entities(qids)
    results = [result for entity in entities if (result := _wikidata_result(entity)) is not None]
    return _SourceAnswer(results, len(qids) >= _WIKIDATA_SEARCH_LIMIT, ok=entities_ok)


def _wikidata_qids(payload: Any) -> list[str] | None:
    """The QIDs a search returned, `[]` for a search that matched nothing, or `None` for a
    response that was not a search result at all.

    **Three outcomes rather than two, and the third is the one worth having.** The Action API
    reports most failures as **HTTP 200 with an `{"error": ...}` body** - read-only mode, a
    busy CirrusSearch backend, a malformed query - so a reader that only checks the status
    code sees a successful request, finds no `query.search` key, and reports "Wikidata has
    nothing for you". That is indistinguishable downstream from a genuine miss, and it is what
    let a WoRMS-only answer be cached for thirty days while Wikidata was simply refusing.

    `None` is also what a `None` payload maps to, so the transport failure and the
    application-level failure travel the same path from here on.
    """
    if not isinstance(payload, dict):
        return None
    if "error" in payload:
        error = payload["error"]
        code = error.get("code") if isinstance(error, dict) else None
        logger.warning("Wikidata refused a search (%s).", code or "unknown")
        return None

    query = payload.get("query")
    search = query.get("search") if isinstance(query, dict) else None
    if not isinstance(search, list):
        # A 200 that is neither an error nor a search result - a proxy, a CDN page rendered as
        # JSON, or an API change. Not this app's business to interpret, and definitely not an
        # empty register.
        return None
    return [title for hit in search if isinstance(hit, dict) and isinstance(title := hit.get("title"), str) and title]


async def _wikidata_entities(qids: list[str]) -> tuple[list[_WikidataEntity], bool]:
    """The reduced entities for a batch of QIDs, in the order asked for, and whether every
    chunk came back.

    **Asked for in chunks of `_WIKIDATA_ENTITY_BATCH`, not all at once**, even though
    `wbgetentities` accepts fifty ids. `props=claims` returns *every* statement on an entity,
    and a taxon carries a great many - dozens of external-database identifiers alone - so a
    single entity runs around 50 KB. Ten of them in one response is routinely over the
    512 KB `_MAX_RESPONSE_BYTES` cap: measured against the live API for the ids this code's
    own search returns, "shark" came back 667 KB, "turtle" 642 KB and "dolphin" 568 KB.

    That mattered far more than it looks. Tripping the cap makes `_request` return `None`,
    which emptied the entire Wikidata contribution - the common-name layer this whole
    two-source design exists for - for exactly the words divers type most, silently, and only
    for the *popular* queries. Chunking keeps each response comfortably inside the cap
    instead of tuning the cap up to meet an unbounded payload.

    The chunks run concurrently: they are independent, they share the provider throttle, and
    the whole thing is inside the caller's search budget either way.
    """
    chunks = [qids[i : i + _WIKIDATA_ENTITY_BATCH] for i in range(0, len(qids), _WIKIDATA_ENTITY_BATCH)]
    by_qid: dict[str, _WikidataEntity] = {}
    failures = 0

    async def fetch(chunk: list[str]) -> None:
        nonlocal failures
        payload = await _wikidata(
            {
                "action": "wbgetentities",
                "ids": "|".join(chunk),
                "props": "claims|labels|aliases",
                "languages": "en",
            }
        )
        if not isinstance(payload, dict) or not isinstance(entities := payload.get("entities"), dict):
            failures += 1
            return
        for qid in chunk:
            if (entity := _wikidata_entity(qid, entities.get(qid))) is not None:
                by_qid[qid] = entity

    async with anyio.create_task_group() as tasks:
        for chunk in chunks:
            tasks.start_soon(fetch, chunk)

    # Re-ordered against `qids` rather than trusting completion order, so the merge's
    # first-writer-wins tie-breaks do not depend on which chunk came back first.
    return [by_qid[qid] for qid in qids if qid in by_qid], failures == 0


async def _remote_search(query: str) -> SpeciesSearchResponse:
    """Both registers, merged and cached under one key.

    The three fetches run concurrently and **each guards itself**, which is the whole point
    of the shape: an exception escaping one task would cancel the other two through the task
    group, turning one register's bad day into a search that finds nothing. Every failure
    mode below therefore degrades to "this source contributed nothing".

    **The whole fan-out is bounded by `_SEARCH_BUDGET_SECONDS`, and whatever arrived is the
    answer.** This is the difference between a picker and a report. WoRMS routinely takes six
    to twelve seconds on the `like=true` endpoints this needs (measurements at the top of the
    module), and waiting for it would make every keystroke feel broken while Wikidata and the
    local catalog sat finished. `move_on_after` cancels the stragglers; the tasks append to
    `collected` as they finish, so everything already in hand survives the cancellation.

    What that costs is completeness, and the cost is *recorded* rather than swallowed: a
    fan-out that lost a source is cached for an hour instead of a month, so a slow afternoon
    cannot decide what a query returns until the key expires. See `_store_search`.

    A cached entry holds the merged remote list with no `uuid` on any row. That is
    deliberate: whether a species is in the local catalog is a fact that changes the moment
    somebody resolves it, and freezing it into a month-long cache entry would have the
    picker keep offering to resolve a species that already exists. The uuids are attached on
    every read instead, by `_attach_catalog_uuids`.
    """
    key = _cache_key("search", query)
    cached = await _cached_search(key)
    if cached is not None:
        return cached

    collected: list[_SourceAnswer] = []

    async def fetch(source: str, call: Any) -> None:
        try:
            collected.append(await call(query))
        except ValidationError, ValueError, TypeError, KeyError:
            # `_request` already swallows every network and parse failure, so reaching here
            # means a provider sent a shape the normalizers did not expect. Same treatment:
            # this source contributed nothing, and the search still answers.
            #
            # Cancellation does not land here: `move_on_after` raises the backend's cancelled
            # exception, which descends from `BaseException` rather than from any of these.
            logger.warning("Discarding the %s results: the response could not be normalized.", source, exc_info=True)

    sources = (
        (_PROVIDER_WORMS, _worms_by_name),
        (_PROVIDER_WORMS, _worms_by_vernacular),
        (_PROVIDER_WIKIDATA, _wikidata_search),
    )
    with anyio.move_on_after(_SEARCH_BUDGET_SECONDS):
        async with anyio.create_task_group() as tasks:
            for source, call in sources:
                tasks.start_soon(fetch, source, call)

    # **Two different ways of not being complete, and both have to count.** A source can run
    # out of *time* - still in flight when the budget expired, so it is not in `collected` at
    # all - or it can come back having *failed*: unreachable, an error status, a body over the
    # size cap, a body that did not parse. The second kind is the dangerous one, because it
    # returns fast and looks exactly like "this register had nothing", so an answer missing
    # half its sources would otherwise be stored for thirty days as though it were the whole
    # truth. `_SourceAnswer.ok` is what tells them apart.
    answered = len(collected) == len(sources)
    complete = answered and all(answer.ok for answer in collected)
    if not complete:
        logger.info(
            "A species search was incomplete: %d of %d sources answered, %d of those failed.",
            len(collected),
            len(sources),
            sum(1 for answer in collected if not answer.ok),
        )

    # Re-sorted before merging because a task group completes in whatever order the network
    # allowed, and the merge below is first-writer-wins: without this, which register defines
    # a shared row would depend on the weather. WoRMS first, since it owns the taxonomy.
    collected.sort(key=lambda answer: 0 if answer.results and answer.results[0].source == "worms" else 1)

    merged: dict[int, SpeciesSearchResult] = {}
    truncated = False
    for answer in collected:
        truncated = truncated or answer.page_was_full
        for result in answer.results:
            _merge_result(merged, result)

    ordered = _ordered(list(merged.values()), query)
    response = SpeciesSearchResponse(results=ordered[:_MAX_RESULTS], has_more=truncated or len(ordered) > _MAX_RESULTS)
    await _store_search(key, response, complete=complete)
    return response


def _merge_result(merged: dict[int, SpeciesSearchResult], result: SpeciesSearchResult) -> None:
    """Fold one result into the merge, enriching rather than duplicating.

    Two registers describing the same taxon is the normal case, not the exception, and it is
    what the shared AphiaID exists to make expressible. The first writer defines the row -
    WoRMS, by the ordering above, so the taxonomy is WoRMS's - and a later source may only
    fill in what is still missing. In practice that means Wikidata contributes the common
    name to a record WoRMS supplied the identity for, which is exactly the division of
    labour the two-source design was chosen for.
    """
    existing = merged.get(result.aphia_id)
    if existing is None:
        merged[result.aphia_id] = result
        return

    merged[result.aphia_id] = existing.model_copy(
        update={
            "common_name": existing.common_name or result.common_name,
            "matched_name": existing.matched_name or result.matched_name,
            # A "unknown" rank from the Wikidata path is a placeholder, not a claim, so a
            # real one from either source displaces it.
            "rank": existing.rank if existing.rank != "unknown" else result.rank,
            "status": existing.status if existing.status != "unknown" else result.status,
        }
    )


def _ordered(results: list[SpeciesSearchResult], query: str) -> list[SpeciesSearchResult]:
    """Exact matches first, then prefix matches, then everything else - stably.

    The same ranking the local query applies in SQL, so a catalog row and a remote row that
    matched equally well end up next to each other rather than in two differently-sorted
    halves. "Matched" is judged against every name the result carries, since a hit whose
    reason is a synonym should still rank as the exact match it was.
    """

    def rank(result: SpeciesSearchResult) -> tuple[int, str]:
        names = [n.casefold() for n in (result.scientific_name, result.common_name, result.matched_name) if n]
        if any(name == query for name in names):
            return 0, result.scientific_name
        if any(name.startswith(query) for name in names):
            return 1, result.scientific_name
        return 2, result.scientific_name

    return sorted(results, key=rank)


async def _local_search(db: AsyncSession, query: str) -> tuple[list[SpeciesSearchResult], bool]:
    """The catalog's own matches, which are what keep the picker useful with Redis cold and
    both registers unreachable: a species anyone has ever logged stays findable.

    A plain escaped `ILIKE`, no `pg_trgm`. A leading-wildcard pattern cannot use a btree
    index whatever else is done, and the catalog is small by construction - it only grows
    when a diver picks something new. The recorded escalation is a `pg_trgm` GIN index on
    `species_name.name`, not a different query shape; see DECISIONS.md.

    `Species.scientific_name` is matched alongside the name index, through an outer join, so
    a row whose `species_name` rows failed to write is still findable by its own name.
    """
    escaped = escape_like(query)
    contains = f"%{escaped}%"
    starts_with = f"{escaped}%"

    name_lower = func.lower(SpeciesName.name)
    scientific_lower = func.lower(Species.scientific_name)
    match_rank = case(
        (or_(name_lower == query, scientific_lower == query), 0),
        (
            or_(
                name_lower.like(starts_with, escape=LIKE_ESCAPE_CHAR),
                scientific_lower.like(starts_with, escape=LIKE_ESCAPE_CHAR),
            ),
            1,
        ),
        else_=2,
    )
    matched_name = case((SpeciesName.name.ilike(contains, escape=LIKE_ESCAPE_CHAR), SpeciesName.name), else_=None)

    statement = (
        select(
            Species.uuid,
            Species.aphia_id,
            Species.scientific_name,
            Species.common_name,
            Species.rank,
            Species.status,
            func.min(match_rank).label("match_rank"),
            # An arbitrary-but-deterministic one of the names that matched. `matched_name` is
            # a hint about *why* a row is in the list, not an identity, so picking the
            # alphabetically first of several matching aliases is enough - and it is nulled
            # below whenever it merely repeats a name the row already shows.
            func.min(matched_name).label("matched_name"),
        )
        .outerjoin(SpeciesName, SpeciesName.species_id == Species.id)
        .where(
            or_(
                SpeciesName.name.ilike(contains, escape=LIKE_ESCAPE_CHAR),
                Species.scientific_name.ilike(contains, escape=LIKE_ESCAPE_CHAR),
            )
        )
        # Grouped by the primary key alone: every other selected column is functionally
        # dependent on it, which Postgres understands. The join can produce several rows per
        # species (one per matching alias) and the diver wants one.
        .group_by(Species.id)
        .order_by("match_rank", Species.scientific_name)
        .limit(_MAX_RESULTS)
    )
    rows = (await db.execute(statement)).all()

    results = []
    for row in rows:
        hint = row.matched_name
        if hint is not None and hint.casefold() in {
            row.scientific_name.casefold(),
            (row.common_name or "").casefold(),
        }:
            hint = None
        results.append(
            SpeciesSearchResult(
                aphia_id=row.aphia_id,
                uuid=row.uuid,
                scientific_name=row.scientific_name,
                common_name=row.common_name,
                rank=row.rank,
                status=row.status,
                matched_name=hint,
                source="catalog",
                attribution=_WORMS_ATTRIBUTION,
            )
        )
    return results, len(rows) >= _MAX_RESULTS


async def _attach_catalog_uuids(db: AsyncSession, results: list[SpeciesSearchResult]) -> list[SpeciesSearchResult]:
    """Give a remote result the local `uuid` of the row it turned out to already be.

    A species can be in the catalog and still miss the local name query - the diver typed a
    vernacular in a language nobody has stored for it, or a synonym WoRMS knows and we never
    persisted. Without this the picker would offer to resolve a species it already has, which
    works (resolve is idempotent) but spends a round trip and a provider call to be told
    something one indexed query answers here.

    Re-sourced as `catalog` along with the uuid, because that field is what the client
    branches on: a row it can attach to a dive right now is a catalog row, whichever register
    surfaced it this time.
    """
    remote_ids = [result.aphia_id for result in results if result.uuid is None]
    if not remote_ids:
        return results

    rows = await db.execute(select(Species.aphia_id, Species.uuid).where(Species.aphia_id.in_(remote_ids)))
    known = {row.aphia_id: row.uuid for row in rows}
    if not known:
        return results

    return [
        result
        if result.uuid is not None or result.aphia_id not in known
        else result.model_copy(update={"uuid": known[result.aphia_id], "source": "catalog"})
        for result in results
    ]


async def _release_read_transaction(db: AsyncSession) -> None:
    """End the read-only transaction the local lookups opened, before going out to a register.

    Every function here that touches `db` does so *before* the slow part, and SQLAlchemy's
    `AsyncSession` autobegins on the first `execute()` - so without this the connection that
    ran a sub-millisecond `SELECT` sits idle-in-transaction for the whole outbound call. That
    is up to `_SEARCH_BUDGET_SECONDS` on search and `_RESOLVE_BUDGET_SECONDS` on resolve,
    against a pool of five plus ten overflow: on the order of fifteen concurrent "add species"
    clicks would park every connection doing nothing, and unrelated endpoints then wait out
    `pool_timeout` and fail. The event loop is free the whole time, which is exactly what
    makes it invisible until the pool runs dry.

    The geocoder this module is otherwise modelled on cannot have this problem - its routes
    take no `db` dependency at all. This one needs the session for the local catalog, so the
    release has to be explicit.

    `rollback` rather than `commit` because it states what is true here: nothing is being
    persisted. Safe at every call site below because nothing has been written yet, and because
    each one releases only on a path where the preceding lookup returned **no ORM instance** -
    either rows already converted to Pydantic models, or a `None`. That matters: `rollback`
    expires live ORM objects regardless of `expire_on_commit=False`, which applies to commit
    only, so releasing while holding a `Species` would turn its next attribute access into a
    silent reload.

    `services/dive_files.py` has the same helper for the same reason (a `run_in_threadpool`
    hop rather than an HTTP call) - see *"Uploaded files are parsed in a thread"* in
    DECISIONS.md. Two copies is a coincidence worth tolerating; a third wants a shared util.
    """
    await db.rollback()


async def search_species(db: AsyncSession, query: str) -> SpeciesSearchResponse:
    """Search the catalog and both registers at once, merged into one ranked list.

    The local catalog rides in front so an outage degrades the answer rather than emptying
    it, and it is also the only half that can hand back a `uuid` - which is what tells the
    client whether it may attach the species to a dive immediately or has to resolve it
    first.

    Never raises for provider trouble. The caller's own rate limit is enforced at the route,
    not here.
    """
    normalized = " ".join(query.split()).casefold()
    if len(normalized) < 2:
        return SpeciesSearchResponse(results=[], has_more=False)

    local, local_was_full = await _local_search(db, normalized)
    # `_local_search` has already turned its rows into `SpeciesSearchResult`s, so there is no
    # ORM state to expire and nothing to preserve - see `_release_read_transaction`.
    await _release_read_transaction(db)

    remote = await _remote_search(normalized)

    merged: dict[int, SpeciesSearchResult] = {result.aphia_id: result for result in local}
    for result in remote.results:
        # Catalog rows win outright rather than merging: their fields went through
        # `resolve_species`'s choices once already, and letting a live provider overwrite
        # them would make a dive's species card disagree with the picker that filled it.
        merged.setdefault(result.aphia_id, result)

    ordered = _ordered(list(merged.values()), normalized)
    return SpeciesSearchResponse(
        results=await _attach_catalog_uuids(db, ordered[:_MAX_RESULTS]),
        has_more=remote.has_more or local_was_full or len(ordered) > _MAX_RESULTS,
    )


# -------------- resolve --------------


async def _species_by_aphia_id(db: AsyncSession, aphia_id: int) -> Species | None:
    return (await db.execute(select(Species).where(Species.aphia_id == aphia_id))).scalar_one_or_none()


async def _worms_vernaculars(aphia_id: int) -> list[tuple[str, str | None]]:
    """Every common name WoRMS has for a taxon, as `(name, language_code)`.

    All languages, not just English. They are few and they arrive language-tagged, and a
    Japanese vernacular the UI will never render still earns its row by making カクレクマノミ
    find the clownfish.
    """
    rows = await _worms("AphiaVernacularsByAphiaID", aphia_id)
    if not isinstance(rows, list):
        return []

    vernaculars = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if (name := _text(row.get("vernacular"))) is not None:
            vernaculars.append((name, _text(row.get("language_code"), _LANGUAGE_CODE_MAX_LENGTH)))
    return vernaculars


async def _worms_synonyms(aphia_id: int) -> list[str]:
    """Superseded names for a taxon, so a diver who learned *Manta birostris* still finds it."""
    rows = await _worms("AphiaSynonymsByAphiaID", aphia_id)
    if not isinstance(rows, list):
        return []
    return [name for row in rows if isinstance(row, dict) and (name := _text(row.get("scientificname"))) is not None]


async def _wikidata_by_aphia_id(aphia_id: int) -> _WikidataEntity | None:
    """The Wikidata entity for a known AphiaID, or `None`.

    An exact statement match rather than a text search: the AphiaID is already in hand, so
    there is nothing to disambiguate and no chance of matching the wrong taxon by name.
    """
    payload = await _wikidata(
        {
            "action": "query",
            "list": "search",
            "srsearch": f"haswbstatement:{_APHIA_PROPERTY}={aphia_id}",
            "srlimit": 1,
        }
    )
    # `None` and `[]` both mean "no entity to enrich with" on this path - unlike search, resolve
    # degrades to a row without a qid either way, so the two need no separating here.
    qids = _wikidata_qids(payload)
    if not qids:
        return None
    entities, _ = await _wikidata_entities(qids[:1])
    return entities[0] if entities else None


def _name_rows(
    *,
    scientific_name: str,
    synonyms: list[str],
    vernaculars: list[tuple[str, str | None]],
    entity: _WikidataEntity | None,
) -> list[tuple[str, str, str, str | None]]:
    """The full `species_name` set for a taxon, as `(name, kind, source, language_code)`.

    Deduplicated on `(casefolded name, kind)` before it ever reaches the database. The unique
    constraint is case-*sensitive*, so it would happily accept "Clownfish" next to
    "clownfish" - two rows that match identically under `ILIKE` and buy the search nothing.
    The first occurrence wins, which is why the order below is deliberate rather than
    incidental: scientific name, then synonyms, then WoRMS's language-tagged vernaculars,
    then Wikidata's English names last, since a name that arrives tagged is worth more to
    keep than the same name untagged.
    """
    candidates: list[tuple[str, str, str, str | None]] = [(scientific_name, "scientific", "worms", None)]
    candidates += [(name, "synonym", "worms", None) for name in synonyms]
    candidates += [(name, "common", "worms", language) for name, language in vernaculars]
    if entity is not None:
        english = [name for name in (entity.label, *entity.aliases) if name]
        candidates += [(name, "common", "wikidata", "eng") for name in english]

    seen: set[tuple[str, str]] = set()
    rows = []
    for name, kind, source, language in candidates:
        key = (name.casefold(), kind)
        if key in seen:
            continue
        seen.add(key)
        rows.append((name, kind, source, language))
    return rows


async def resolve_species(db: AsyncSession, aphia_id: int) -> Species:
    """Return the catalog row for an AphiaID, creating it from WoRMS and Wikidata if needed.

    Idempotent, and answering 200 either way: "resolve" rather than "create" because the
    caller is naming a taxon that already exists in the world, and whether this instance has
    seen it before is not their concern.

    **The one place in this module that can fail loudly.** If WoRMS cannot be reached, this
    503s instead of degrading, because the alternative is writing a row into a table shared
    by every account on the strength of a guess. Wikidata failing is a different matter and
    degrades silently: it contributes the common name and the qid, and a row without either
    is a perfectly good row that falls back to the scientific name.

    Handed a synonym's id - a diver picked *Manta birostris* - it follows `valid_AphiaID` and
    stores the accepted taxon, re-checking for an existing row under the accepted id first.

    **This is allowed to be slow, and has to be.** `AphiaRecordByAphiaID` was measured at
    eleven seconds against the live register, so a keystroke-sized budget here does not mean
    a fast endpoint - it means an endpoint that always 503s and a catalog that can never be
    filled. The client is showing a spinner against a deliberate click, which is the one
    place in this feature where waiting is the right answer.
    """
    if (existing := await _species_by_aphia_id(db, aphia_id)) is not None:
        return existing

    # Nothing found, so the lookup above is holding a transaction open over no ORM state and
    # no writes - and the next line can take twenty-five seconds. See
    # `_release_read_transaction`; the early return above deliberately precedes it, because
    # that path *does* hold a live `Species` and must not have it expired underneath the
    # caller.
    await _release_read_transaction(db)

    # Initialized before the scope, not after it: `move_on_after` cancels the body wherever it
    # happens to be, so an assignment inside it is not guaranteed to have run.
    taxon: _Taxon | None = None
    with anyio.move_on_after(_RESOLVE_BUDGET_SECONDS):
        taxon = _worms_taxon(await _worms("AphiaRecordByAphiaID", aphia_id))
    if taxon is None:
        # A raw `HTTPException`: `core/exceptions/http_exceptions.py` has no class for 503,
        # the same reason `parse_dive` raises 415 and 409 raw. One message for both "the
        # register is down" and "the register has no such id" - the second is unreachable
        # through the picker, which only ever offers ids WoRMS just returned, and telling an
        # unknown id apart from an outage would mean trusting the failure mode to be honest.
        raise HTTPException(status_code=503, detail="Species lookup is temporarily unavailable.")

    if taxon.valid_aphia_id is not None and taxon.valid_aphia_id != taxon.aphia_id:
        if (existing := await _species_by_aphia_id(db, taxon.valid_aphia_id)) is not None:
            return existing
        # Same shape, and the branch that makes this endpoint's worst case two budgets rather
        # than one - so the second read must not hold a connection across the second fetch.
        await _release_read_transaction(db)
        valid_taxon: _Taxon | None = None
        with anyio.move_on_after(_RESOLVE_BUDGET_SECONDS):
            valid_taxon = _worms_taxon(await _worms("AphiaRecordByAphiaID", taxon.valid_aphia_id))
        if valid_taxon is None:
            raise HTTPException(status_code=503, detail="Species lookup is temporarily unavailable.")
        taxon = valid_taxon

    # Enrichment, all three concurrently and none of it load-bearing: a failure in any of
    # them costs names or a qid, never the row. Each collects into its own slot rather than
    # returning, since a task group's tasks cannot return values.
    #
    # Budgeted well below the record fetch above, and separately from it: the diver is already
    # several seconds into a spinner by the time this runs, and a synonym list is not worth
    # another twenty. Whatever arrived is what gets indexed - a species that lands with fewer
    # search aliases is still a species the diver can attach to the dive.
    synonyms: list[str] = []
    vernaculars: list[tuple[str, str | None]] = []
    entity: _WikidataEntity | None = None
    accepted_id = taxon.aphia_id

    async def load_synonyms() -> None:
        nonlocal synonyms
        synonyms = await _worms_synonyms(accepted_id)

    async def load_vernaculars() -> None:
        nonlocal vernaculars
        vernaculars = await _worms_vernaculars(accepted_id)

    async def load_entity() -> None:
        nonlocal entity
        entity = await _wikidata_by_aphia_id(accepted_id)

    with anyio.move_on_after(_ENRICHMENT_BUDGET_SECONDS):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(load_synonyms)
            tasks.start_soon(load_vernaculars)
            tasks.start_soon(load_entity)

    common_name = _choose_common_name(
        scientific_name=taxon.scientific_name,
        label=entity.label if entity is not None else None,
        aliases=entity.aliases if entity is not None else (),
        # English only for the *display* name, while every language goes into the index
        # below. The app has no i18n, so a Japanese common name on a dive card would be a
        # bug; the same name in the search index is a feature.
        vernaculars=tuple(name for name, language in vernaculars if language in (None, "eng")),
    )

    species = Species(
        aphia_id=taxon.aphia_id,
        scientific_name=taxon.scientific_name,
        rank=taxon.rank,
        status=taxon.status,
        authority=taxon.authority,
        kingdom=taxon.kingdom,
        phylum=taxon.phylum,
        class_name=taxon.class_name,
        order_name=taxon.order_name,
        family=taxon.family,
        genus=taxon.genus,
        is_marine=taxon.is_marine,
        is_brackish=taxon.is_brackish,
        is_freshwater=taxon.is_freshwater,
        common_name=common_name,
        wikidata_qid=entity.qid if entity is not None else None,
    )
    db.add(species)
    try:
        # Flushed before the names so `species.id` exists to hang them on, and so the unique
        # `aphia_id` violation below surfaces here rather than after a pile of name rows.
        await db.flush()
        for name, kind, source, language in _name_rows(
            scientific_name=taxon.scientific_name, synonyms=synonyms, vernaculars=vernaculars, entity=entity
        ):
            db.add(SpeciesName(species_id=species.id, name=name, kind=kind, source=source, language_code=language))
        await db.commit()
    except IntegrityError:
        # Two divers resolving the same new species at once. The unique `aphia_id` is what
        # makes this a collision rather than a duplicate taxon, and the loser simply adopts
        # the winner's row - both callers asked for the same thing and both get it.
        await db.rollback()
        winner = await _species_by_aphia_id(db, taxon.aphia_id)
        if winner is None:
            raise
        return winner

    await db.refresh(species)
    return species
