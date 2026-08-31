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

**A third provider, and it is not a third source of names.** Wikimedia Commons is asked for
one thing only - the credit and the scaled bytes of the photograph a Wikidata item already
named - and it is asked once per *new* species rather than per keystroke. It never sees
anything a diver typed. The bytes it returns go on this instance's files volume and are served
from this instance's own API, so no browser ever contacts Wikimedia; see
`services/species_photos.py`, which owns everything done with the answer.

Every provider here is throttled instance-wide and every answer is cached, for the reason
Nominatim's policy makes load-bearing next door: a courtesy that is only observed when
traffic is low is not one.
"""

import json
import logging
import re
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
from ..core.db.database import release_read_transaction
from ..core.exceptions.http_exceptions import RateLimitException
from ..core.utils import cache
from ..core.utils.rate_limit import enforce_rate_limit
from ..core.utils.search import LIKE_ESCAPE_CHAR, escape_like
from ..models.species import Species
from ..models.species_name import SpeciesName
from ..schemas.species import SpeciesSearchResponse, SpeciesSearchResult
from . import species_photos

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

# What the vername annotation inside `_worms_by_vernacular` may spend before the rows go out
# unexplained. It needs a bound of its own because `_request`'s bounds are both far above
# `_SEARCH_BUDGET_SECONDS`: without this, one hung ajax call would have the *whole* WoRMS
# by-vernacular source cancelled by the search budget, losing the records this endpoint only
# decorates. Calibrated against a bound rather than a median, because the latency itself
# moves - three twenty-call samples of the same URL measured medians of 0.5-0.9 s and maxima
# of 1.9-3.8 s. Four seconds fits every sample; an 8.5 s outlier in a separate burst says
# over-budget is a live case rather than an eliminated one, which is why expiry degrades
# (rows without hints, and the short TTL) instead of failing.
_AJAX_ANNOTATION_BUDGET_SECONDS = 4.0

# What `resolve_species` is willing to spend on the record fetch it cannot do without. Far
# longer than a search budget, because this is a deliberate "add this species" click with a
# spinner against it rather than a keystroke, and because the alternative to waiting is a 503
# that leaves the diver unable to log what they saw.
#
# The enrichment fan-out that follows gets its own, shorter budget, and it is doing two jobs
# rather than one. Most of what it collects is best-effort - names and a Wikidata id are worth
# a moment, not a stall. But the synonym list in that same fan-out vets the display name, so
# this figure is also a 503 boundary: expire it and `resolve_species` refuses rather than
# storing a name nothing checked. It is the *only* bound on `_worms_synonyms`' page walk,
# which is deliberate and explained there - so lowering it shortens that walk rather than
# merely trimming a fan-out.
_RESOLVE_BUDGET_SECONDS = 25.0
_ENRICHMENT_BUDGET_SECONDS = 10.0

# What a picker can usefully show before "keep typing" is better advice than another row.
_MAX_RESULTS = 25

# WoRMS pages its list endpoints at 50. Search never asks for page 2: more than fifty raw
# hits for one typed fragment is what `has_more` exists to say. Resolve is the exception -
# `_worms_synonyms` walks every page, because the list it returns vets a name this app then
# stores forever. Wikidata's search is asked for the same fifty, and used to be asked for ten:
# that was 2% of what CirrusSearch will rank, and the candidates a diver actually means sat
# past the cut, which was the whole of "the good rows are missing" on the Wikidata side. Fifty
# is affordable only because the search asks for *names* rather than claims - the expensive
# half is cut back to `_WIKIDATA_ENRICH_LIMIT` before a single entity is fetched.
_WORMS_PAGE_SIZE = 50
_WIKIDATA_SEARCH_LIMIT = 50

# The ajax endpoint's row budget, and it has nothing to do with `_WORMS_PAGE_SIZE` above
# beyond happening to be the same number. **It must be sent, and it must not be exceeded**:
# omitted, the endpoint answers with its default of twenty rows over a quarter of the taxa;
# above fifty, the value is *discarded* rather than clamped, so `max_matches=100` returns
# fewer rows than `max_matches=50`. Measured across five variants of the same call.
_AJAX_MAX_MATCHES = 50

# How many entities to ask `wbgetentities` for at once. Small on purpose - see
# `_wikidata_entities`: a taxon entity with all its claims runs ~50 KB, so a batch of ten
# regularly exceeds `_MAX_RESPONSE_BYTES` and costs the whole Wikidata contribution. Four
# keeps the heaviest realistic chunk under the cap, and no margin is quoted here on purpose:
# how much room is left depends entirely on which taxa land in the chunk, and two measurements
# of the worst realistic four-QID chunk came out at different fractions of the cap on
# different days. A figure here would only be a second place to be wrong about something that
# moves.
_WIKIDATA_ENTITY_BATCH = 4

# How many Phase-1 candidates survive to be enriched. Written as a product because the batch
# above is the decision this one derives from: four chunks of entity fetches is the budget, so
# a change there carries through rather than leaving two numbers to reconcile by hand.
#
# It is also the knob that trades breadth against Wikimedia's *anonymous* request ceiling,
# which is an order of magnitude below this app's own self-imposed Wikidata cap and is reached
# by heavy `props=claims` calls rather than by searches - so an instance seeing 429s should
# lower this rather than `_WIKIDATA_SEARCH_LIMIT`, which costs one light call however wide it
# is set.
_WIKIDATA_ENRICH_LIMIT = 4 * _WIKIDATA_ENTITY_BATCH

# A taxon's name does not change - that is rather the point of a nomenclatural register - so
# a hit is held for a month. A *miss* is held for an hour, because an empty answer is far
# more likely to be provider weirdness than a fact about the sea, and pinning it for a month
# would make one bad afternoon look like a broken feature.
_HIT_TTL_SECONDS = 30 * 24 * 60 * 60
_MISS_TTL_SECONDS = 60 * 60

# Bumped whenever the cached shape or the way it is composed changes. What is cached is the
# *normalized, merged* remote list rather than raw provider payloads, so a change to the
# normalizer has to invalidate the old entries - a new prefix does that without a flush.
_CACHE_VERSION = "v5"

# Wikidata's "WoRMS AphiaID" property. The single hinge the whole two-source design turns
# on: without a shared key there would be nothing to merge two registers *on*.
_APHIA_PROPERTY = "P850"
# Wikidata's "taxon name" property - the scientific name. A search row requires it: an item
# tagged with an AphiaID but no taxon name is not a taxon this app can stand behind, and
# `?q=orca` used to open with two indistinguishable bare "Orca" rows to prove it.
_TAXON_NAME_PROPERTY = "P225"
# Wikidata's "taxon rank" property, whose value is an item rather than a string - so it is
# read through `_claim_entity_id` and translated by the map below.
_TAXON_RANK_PROPERTY = "P105"
# Wikidata's "image" property: the Commons file title of the item's lead image. **The only
# image property consulted**, deliberately. P181 is a distribution map and P2716 a collage,
# and rendering either where a photograph belongs is the same failure that made GBIF's media
# unusable. P373 (Commons category) is more common than P18 across the register and is the
# recorded escalation if the no-photo rate ever becomes the complaint - it is not built,
# because a category's first member is not a curated lead image.
_IMAGE_PROPERTY = "P18"

# P105's item to the rank string this app ships. **Spelled WoRMS's way wherever WoRMS has a
# spelling**, so one rank never reaches a client under two names: a client displays this
# string verbatim, and a Wikidata-only row and the WoRMS row beside it have to agree.
#
# The keys are enumerated against WoRMS's own closed rank vocabulary rather than collected as
# they turn up - `AphiaTaxonRanksByID` lists thirty distinct names across its kingdoms, and
# every one of them is here except *Mutatio*, for which Wikidata has no taxonomic-rank item
# at all (the full set of items carrying `instance of: taxonomic rank` was enumerated and
# searched). Two entries sit outside that vocabulary on purpose: **Parvorder**, which
# Wikidata uses and WoRMS does not (*Mysticeti* carries it), and **Clade**, the common
# non-Linnaean rank, so the likeliest escape from a register-derived list still lands
# somewhere rather than falling through.
#
# Falling through is the recorded residual, and it is not neutral: an unmapped item leaves
# the row on the `"unknown"` sentinel, which is the value that would sit *between* the ranks
# under an order that tiers them rather than below them - see the `"unknown"` section in
# DECISIONS.md, which also records the second way this field moves between two identical
# searches. The map is sized to make the fall-through rare, not impossible.
_WIKIDATA_RANK_BY_QID = {
    "Q36732": "Kingdom",
    "Q2752679": "Subkingdom",
    "Q38348": "Phylum",
    # WoRMS renders the botanical rank as "Phylum (Division)" - measured on *Rhodophyta*,
    # AphiaID 852 - and its own spelling is the one that has to win here.
    "Q334460": "Phylum (Division)",
    "Q1153785": "Subphylum",
    "Q3491997": "Subphylum (Subdivision)",
    "Q3504061": "Superclass",
    "Q37517": "Class",
    "Q5867051": "Subclass",
    "Q2007442": "Infraclass",
    "Q5868144": "Superorder",
    "Q36602": "Order",
    "Q5867959": "Suborder",
    "Q2889003": "Infraorder",
    "Q6311258": "Parvorder",
    "Q2136103": "Superfamily",
    "Q35409": "Family",
    "Q164280": "Subfamily",
    "Q227936": "Tribe",
    "Q3965313": "Subtribe",
    "Q34740": "Genus",
    "Q3238261": "Subgenus",
    # Botanical ranks: WoRMS scopes Section and Subsection to Plantae, Fungi and Chromista,
    # and Wikidata's zoological homonyms are a different rank entirely rather than the same
    # one spelled twice - so only the botanical items are here.
    "Q3181348": "Section",
    "Q5998839": "Subsection",
    "Q7432": "Species",
    "Q68947": "Subspecies",
    "Q767728": "Variety",
    "Q630771": "Subvariety",
    # Wikidata labels this one "form"; WoRMS spells it "Forma", and WoRMS wins.
    "Q279749": "Forma",
    "Q12774043": "Subforma",
    "Q713623": "Clade",
}

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
_PROVIDER_COMMONS = "commons"


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
    """One Wikidata entity, reduced to what this app wants from it.

    No count is quoted, deliberately: this sentence used to carry one, the field list had
    already outgrown it before the rank arrived, and a number here is only ever a second
    place to be wrong about something the fields below state exactly.
    """

    qid: str
    aphia_id: int
    scientific_name: str | None
    label: str | None
    aliases: tuple[str, ...]
    # P105 translated through `_WIKIDATA_RANK_BY_QID`; `None` for an entity with no P105 or
    # one whose rank item is not in the map, both of which become the `"unknown"` sentinel on
    # the way out.
    rank: str | None
    # Every non-deprecated P18 value **and its statement rank**, in serialization order. The
    # rank rides along because `species_photos.choose_photo_file` needs it, and the order does
    # because that rule's last tie-break is "the first in statement order". These bytes are
    # already in the `props=claims` response every caller here makes, so carrying them costs
    # no extra request - which is the whole reason the photo's *identity* is free.
    images: tuple[species_photos.ImageCandidate, ...]


@dataclass(frozen=True, slots=True)
class _WikidataPage:
    """One search candidate, before anything has been fetched about it.

    The search asks for `entityterms` rather than claims, which is the whole reason it can ask
    for fifty candidates at all - the same fifty *with* their claims run to hundreds of times
    the payload. What comes back per candidate is a QID and the entity's English names, and
    those names are enough to do the two jobs that have to happen before the expensive fetch:
    decide which candidates are worth enriching, and remember which name the diver's query
    actually hit. Everything else about the taxon - its binomial, its rank - still needs the
    entity.
    """

    qid: str
    # Label first, then aliases in the order Wikidata listed them. The order is load-bearing:
    # it breaks ties between equally good matches, and it is measured identical to the order
    # `wbgetentities` returns, so a term chosen here is the same string the entity would have
    # offered. Either key can be absent from a page independently, so this can be empty.
    terms: tuple[str, ...]


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
        _PROVIDER_COMMONS: (
            settings.SPECIES_COMMONS_RATE_LIMIT_REQUESTS,
            settings.SPECIES_COMMONS_RATE_LIMIT_WINDOW_SECONDS,
        ),
    }
    # Indexed rather than `.get`-ed on purpose: a provider added above without a pair here is a
    # `KeyError` at the first call, which is loud, where a default would be an unthrottled
    # third party nobody notices. Every caller of this passes one of the three constants.
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


async def _commons(params: dict[str, Any]) -> Any | None:
    """Ask Wikimedia Commons' Action API.

    `formatversion=2` because this one is read for its *content* rather than for a list of
    ids: version 2 gives `query.pages` as an array and drops the "*" wrappers, which is what
    makes `extmetadata` readable without a layer of unwrapping. The Wikidata calls above stay
    on version 1 because their readers are written against its shapes and have measured
    comments about them.
    """
    return await _request(_PROVIDER_COMMONS, settings.COMMONS_API_URL, {**params, "format": "json", "formatversion": 2})


# -------------- matching --------------


# How well a name answers what the diver typed, ascending, so 0 is the best. Named rather
# than written as bare integers because they are compared, minimised and sorted on in four
# places, and `2` says nothing at any of them.
_MATCH_EXACT = 0
_MATCH_PREFIX = 1
_MATCH_WORD = 2
_MATCH_SUBSTRING = 3
_MATCH_NONE = 4


def _match_bucket(query: str, name: str) -> int:
    """How well `name` matches an already-normalized `query` - `_MATCH_EXACT` down to
    `_MATCH_NONE`.

    One predicate for every place that asks the question, so the ranking, the choice of which
    vernacular explains a row, and anything that later has to cut candidates before the full
    row exists all agree about what "a better match" means.

    **Only the name side is casefolded here; `query` is expected to arrive that way**, from
    `search_species`, which also collapses its whitespace. Handing this a raw query fails
    silently and completely rather than loudly: every comparison below is against a folded
    name, so a single capital puts *every* row in `_MATCH_NONE` and the page comes back
    ordered by nothing at all, with every hint still attached because nothing looked
    redundant. Worth knowing before debugging a page reached by calling `_remote_search`
    directly.

    **Prefix beats word boundary, and that is a decision with a known cost.** "Whale shark"
    outranks "blue whale" for `?q=whale`, and so does "Whale louse family" - divers log whale
    sharks constantly and baleen whales almost never, so the first-word compounds people
    actually mean stay on top, and the intruder displays the very name that earned its
    position. The word-boundary bucket underneath is what still separates "blue whale" from
    *Barbourisia rufa*, and `\\b` handles multi-word queries and hyphenated names for free.

    A plural is deliberately *not* a word-boundary match: "whales" lands in the substring
    bucket for `?q=whale`, which is the right place - above the unmatched mass, below the
    exact word.
    """
    folded = name.casefold()
    if folded == query:
        return _MATCH_EXACT
    if folded.startswith(query):
        return _MATCH_PREFIX
    if re.search(rf"\b{re.escape(query)}\b", folded):
        return _MATCH_WORD
    if query in folded:
        return _MATCH_SUBSTRING
    return _MATCH_NONE


def _best_matching_term(terms: tuple[str, ...], query: str) -> tuple[int, str | None]:
    """The best bucket over `terms`, and the term that earned it.

    Both answers come from one pass because the Wikidata search needs both and they have to
    agree: the bucket decides which candidates survive the cut before enrichment, and the term
    is what the surviving row quotes back as `matched_name`. Asking twice would let a candidate
    be admitted on the strength of one name and then explained by a different one.

    **Term order is the tie-break**, which is why this replaces only on a strictly better
    bucket: callers pass the label first and the aliases in Wikidata's own order, so the most
    representative name wins among equals. `(_MATCH_NONE, None)` means nothing here answers the
    query at all - a candidate carried by breadth that has nothing to say for itself.
    """
    best_bucket = _MATCH_NONE
    best_term: str | None = None
    for term in terms:
        bucket = _match_bucket(query, term)
        if bucket < best_bucket:
            best_bucket, best_term = bucket, term
    return best_bucket, best_term


# Every rank at or below species in WoRMS's own closed vocabulary (`AphiaTaxonRanksByID`),
# casefolded. Closed because WoRMS's list is closed - this is an enumeration of a finite set,
# not a growing collection of things seen in the wild.
_SPECIES_TIER_RANKS = frozenset(
    {"species", "subspecies", "variety", "subvariety", "forma", "subforma", "form", "mutatio"}
)


def _rank_tier(rank: str) -> int:
    """Species and below, then "we do not know", then genus and above.

    A diver is most interested in the species they spotted, so a species outranks its own
    genus and family on the page. Coarse on purpose: three tiers, not a rank ladder, because
    the registers disagree about the exact rank of the same taxon often enough that a fine
    order would move between two identical searches (see the `"unknown"` section in
    DECISIONS.md).

    **A rule, not two enumerations, and the direction matters.** Membership in the closed
    species set decides the top tier; the sentinel sits in the middle; *everything else known*
    falls to the bottom by default. Enumerating the higher tier instead would put every rank
    left out of that list - superclass, infraorder, subtribe, section - into the middle,
    above the genus and family rows it is supposed to sit below. Defaulting downward cannot
    invert that way. What it cannot defend against is a rank that never becomes a string at
    all: an unmapped Wikidata rank item leaves the row on `"unknown"` and lands middle-tier,
    which is why `_WIKIDATA_RANK_BY_QID` is enumerated up front rather than grown.
    """
    folded = rank.casefold()
    if folded in _SPECIES_TIER_RANKS:
        return 0
    if folded == "unknown":
        return 1
    return 2


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


def _vername_map(rows: Any, query: str) -> dict[int, str] | None:
    """AphiaID to the one vernacular that best explains why the taxon matched `query`, or
    `None` when the annotation call did not answer.

    Built from `AjaxAphiaRecordsByNamePart`, which is the only WoRMS endpoint that says
    *which* vernacular a hit matched on - an `AphiaRecord` carries no vernacular field at all,
    which is why by-vernacular rows have no explanation of their own.

    **Every language, not just English.** The English-only rule governs the *display* name; a
    foreign word that accounts for a row is information rather than noise. `?q=orca` returns a
    shad (*Alosa alosa*) because its Spanish vernacular is "samborca", and `matched
    "samborca"` is precisely what makes that row make sense. So no `languages[]` filter is
    sent, and `eng` only wins as a tie-break.

    **Keyed by the raw, unfolded id WoRMS sent**, because these rows carry no `valid_AphiaID`
    and no `status`: the ajax answer for `whale` contains two "blue whale" rows under
    different ids, one of them an unaccepted homonym. The caller looks this up with the
    record's own id, *before* its fold, so the wrong-taxon lookup cannot happen.

    A taxon with several vernames - *Balaena mysticetus* has nine for `whale` - keeps the one
    that matched best, English first at equal quality, then casefolded alphabetical order.
    Name-intrinsic tie-breaks rather than response order, because ajax rows are ordered
    alphabetically by `displayname` and nothing else, and the chosen vername reaches the sort
    key: letting arrival order pick it would let WoRMS decide page order between two identical
    searches.
    """
    if not isinstance(rows, list):
        return None

    best: dict[int, tuple[int, int, str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        aphia_id = row.get("id")
        vername = _text(row.get("vername"))
        # `vername` is null on every scientific-name match, which is most of the answer for a
        # query like `orca` - those rows explain nothing and are simply not in the map.
        if not isinstance(aphia_id, int) or aphia_id <= 0 or vername is None:
            continue
        language = _text(row.get("language"), _LANGUAGE_CODE_MAX_LENGTH)
        candidate = (_match_bucket(query, vername), 0 if language == "eng" else 1, vername.casefold(), vername)
        if (current := best.get(aphia_id)) is None or candidate < current:
            best[aphia_id] = candidate
    return {aphia_id: chosen[-1] for aphia_id, chosen in best.items()}


def _worms_result(row: Any, vernames: dict[int, str] | None = None) -> SpeciesSearchResult | None:
    """One WoRMS record as a search hit, folded onto its accepted taxon.

    This is where a diver typing *Manta birostris* gets *Mobula birostris* back. WoRMS sends
    `valid_AphiaID`/`valid_name` inline on an unaccepted record, so the fold costs no second
    request - and the superseded name the diver actually typed becomes `matched_name`, since
    being silently handed a different binomial is baffling.

    `vernames` is the by-vernacular source's annotation map, and it fills the same field for
    rows the fold has nothing to say about: a row that matched on a common name WoRMS will not
    tell us about otherwise. **The fold wins any collision**, because it is the truer account
    of what the diver's query actually hit - the ajax row for the unaccepted "blue whale"
    (AphiaID 380449) folds to the fin whale, and reporting "blue whale" there would be a
    different animal's name.

    Neither hint is guaranteed to survive: whatever is set here is nulled downstream on any
    row whose visible names already match the query, which is `SpeciesSearchResult`'s own
    contract for the field. That test needs the merged `common_name` and cannot be made here,
    where every row's is `None`.
    """
    taxon = _worms_taxon(row)
    if taxon is None:
        return None

    matched_name: str | None = None
    scientific_name = taxon.scientific_name
    aphia_id = taxon.aphia_id
    folded = False
    if taxon.valid_aphia_id is not None and taxon.valid_aphia_id != taxon.aphia_id:
        valid_name = _text(row.get("valid_name"))
        if valid_name is not None:
            folded = True
            matched_name = taxon.scientific_name
            scientific_name = valid_name
            aphia_id = taxon.valid_aphia_id

    if matched_name is None and vernames is not None:
        # `taxon.aphia_id` rather than `aphia_id`: the map is keyed by the id WoRMS put on the
        # record, which is the pre-fold one.
        vername = vernames.get(taxon.aphia_id)
        if vername is not None and vername.casefold() != scientific_name.casefold():
            matched_name = vername

    return SpeciesSearchResult(
        aphia_id=aphia_id,
        uuid=None,
        scientific_name=scientific_name,
        common_name=None,
        rank=taxon.rank,
        # "accepted" rather than the record's own status whenever the fold above happened:
        # the row now describes the accepted taxon, and reporting the synonym's status would
        # label the wrong thing. Keyed on the fold rather than on `matched_name`, which a
        # vername can now also fill without any of that being true.
        status="accepted" if folded else taxon.status,
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

    rank_qid = _claim_entity_id(claims, _TAXON_RANK_PROPERTY)
    return _WikidataEntity(
        qid=qid[:_QID_MAX_LENGTH],
        aphia_id=aphia_id,
        scientific_name=_claim_value(claims, _TAXON_NAME_PROPERTY),
        label=label,
        aliases=tuple(aliases),
        rank=_WIKIDATA_RANK_BY_QID.get(rank_qid) if rank_qid is not None else None,
        images=_image_candidates(claims),
    )


def _image_candidates(claims: dict[str, Any]) -> tuple[species_photos.ImageCandidate, ...]:
    """Every P18 value on an entity, paired with its statement rank.

    Untouched otherwise: no format filter and no choosing happens here, both of which are
    `services.species_photos`' job. This reads the claim, that decides what to do with it.
    """
    return tuple(
        species_photos.ImageCandidate(file=title, rank=rank)
        for value, rank in _ranked_claim_values(claims, _IMAGE_PROPERTY)
        if (title := _text(value)) is not None
    )


def _ranked_claim_values(claims: dict[str, Any], prop: str) -> list[tuple[Any, str]]:
    """Every usable value of a Wikidata property with its statement rank, **in serialization
    order**, with `deprecated` statements dropped.

    Wikidata nests every claim four levels deep and any level can be missing or be a type
    this cares nothing about, so each step is checked rather than assumed - a malformed
    entity should cost its own row, never the search.

    `deprecated` is dropped here rather than by the callers because it is Wikidata's own
    marker for a value the community has ruled wrong, and no reader in this app wants one.
    The other two ranks are *carried* rather than resolved, because the two callers want
    different things: `_claim_values` below wants preferred first, while the photo selection
    rule wants the untouched statement order and asks about `preferred` itself.
    """
    statements = claims.get(prop)
    if not isinstance(statements, list):
        return []
    values: list[tuple[Any, str]] = []
    for statement in statements:
        if not isinstance(statement, dict):
            continue
        rank = statement.get("rank")
        if rank == "deprecated":
            continue
        snak = statement.get("mainsnak")
        if not isinstance(snak, dict):
            continue
        datavalue = snak.get("datavalue")
        if not isinstance(datavalue, dict):
            continue
        # An absent or unrecognized rank reads as "normal" - Wikidata's own default, and the
        # safe direction, since a value is then dropped only when explicitly disowned.
        values.append((datavalue.get("value"), rank if isinstance(rank, str) else "normal"))
    return values


def _claim_values(claims: dict[str, Any], prop: str) -> list[Any]:
    """Every usable value of a Wikidata property, best statement first.

    **Statement rank is honoured, and that is not decoration.** A property here is
    multi-valued more often than it looks - P105 carries both a parvorder and a suborder on
    *Mysticeti* - and Wikidata's own answer to "which of these is current" is the statement's
    `rank`: `deprecated` marks a value the community has ruled wrong, `preferred` the one to
    use when several are true. Reading in serialization order and taking the first, as this
    did, let a deprecated AphiaID or a superseded binomial win purely by sitting earlier in
    the JSON. So deprecated statements are dropped, preferred ones come first, and the rest
    keep serialization order - deterministic for a given entity revision, and one rule for
    every property rather than one for the rank and another for the identifier.
    """
    ranked = _ranked_claim_values(claims, prop)
    return [value for value, rank in ranked if rank == "preferred"] + [
        value for value, rank in ranked if rank != "preferred"
    ]


def _claim_value(claims: dict[str, Any], prop: str) -> str | None:
    """The best plain-string value of a Wikidata property, if it has one."""
    for value in _claim_values(claims, prop):
        if (text := _text(value)) is not None:
            return text
    return None


def _claim_entity_id(claims: dict[str, Any], prop: str) -> str | None:
    """The best item-valued QID of a Wikidata property, if it has one.

    Separate from `_claim_value` because the two datatypes are shaped differently: an
    external identifier arrives as a bare string, an item reference as a dict whose `id`
    carries the QID. Both go through the statement-rank ordering above.
    """
    for value in _claim_values(claims, prop):
        if isinstance(value, dict) and (qid := _text(value.get("id"), _QID_MAX_LENGTH)) is not None:
            return qid
    return None


def _wikidata_result(entity: _WikidataEntity, matched_name: str | None = None) -> SpeciesSearchResult | None:
    """A Wikidata entity as a search hit, or `None` for an item carrying no taxon name.

    **`matched_name` is the term the search matched on**, handed down from the candidate's
    `entityterms` because that is the only place it exists - an entity fetch says what the
    taxon is called but not which of those names the diver's query hit. It is what keeps
    *Orcinus orca* honest on `?q=whale`: the row displays "Orca gladiator", which accounts for
    nothing, and `matched "orca whale"` is why it is on the page. Under the ranking key a hint
    places a row only where the visible names place it nowhere, so explaining a row never
    promotes it.

    **P225 or no row.** The English label used to stand in when the entity had no taxon name,
    and the row it built was one this app could not stand behind: `?q=orca` opened with two
    indistinguishable bare "Orca" rows, one of them Q61884050 - an item with an AphiaID, no
    P225, and a label that says nothing a diver can act on. Dropping it costs something real
    and known: a dropped entity also leaves the merge, so a WoRMS row at the same AphiaID
    loses the Wikidata name it would otherwise have gained. That is the accepted price, and
    the exposure measured small - one P225-less item among the entities behind the flagship
    queries, and no WoRMS row carried its AphiaID.

    The name that survives is therefore always P225, and for most taxa the English label is
    that same binomial - which is exactly why `_choose_common_name` prefers a label that
    differs from it.

    **Resolve is unaffected, by construction.** The fallback lived here and nowhere else, this
    function is reached only from `_wikidata_search`, and `resolve_species` takes its binomial
    from the WoRMS record and asks an entity only for its qid and its English names - so
    nothing that gets *written* changes shape, and no label-for-binomial fallback survives
    anywhere in this module.

    **The name here is unvetted, and that is a knowing limitation rather than an oversight.**
    Resolve passes `_choose_common_name` the taxon's synonym list so a junior scientific
    synonym cannot become a display name; search cannot, because that list is a separate WoRMS
    call *per row* - fifty of them against a six-second keystroke budget, on a path that has
    already released its read transaction to go outbound. So `?q=orca` shows "Orca gladiator"
    for as long as the taxon is not in the catalog, and picking that row stores "Orca whale".
    The disagreement is real, one-directional and self-healing: the wrong name is never
    written, and the first resolve replaces it for everyone. Do not close it by adding a fetch
    here - see the common-name section in DECISIONS.md.
    """
    scientific_name = entity.scientific_name
    if scientific_name is None:
        return None

    common_name = _choose_common_name(scientific_name=scientific_name, label=entity.label, aliases=entity.aliases)

    # A term that merely repeats what the row already displays explains nothing, which is the
    # schema's own contract for the field. This catches what a single source can see; the
    # merged-list pass catches what it cannot - a `common_name` some other source supplied.
    displayed = {scientific_name.casefold()}
    if common_name is not None:
        displayed.add(common_name.casefold())
    if matched_name is not None and matched_name.casefold() in displayed:
        matched_name = None

    return SpeciesSearchResult(
        aphia_id=entity.aphia_id,
        uuid=None,
        scientific_name=scientific_name,
        common_name=common_name,
        # P105, translated by `_WIKIDATA_RANK_BY_QID`. This reverses the refusal that stood
        # here - no rank at all rather than one derived from claims - and what changed is the
        # premise rather than the appetite for guessing: rank was picker context that nothing
        # read, and `_ordered` now tiers on it through `_rank_tier`, where a sentinel on every
        # Wikidata-only row misplaces the row instead of merely leaving a caption blank. The
        # fill landed one release ahead of the order deliberately, so that order never ran
        # against an all-sentinel Wikidata side. The argument is in the `"unknown"` section of
        # DECISIONS.md. Still the sentinel for an entity with no P105 or an unmapped rank
        # item, and a hit WoRMS also returned takes WoRMS's rank at the merge - which is also
        # how two identical searches can show two different real ranks for a taxon the
        # registers disagree about, recorded in that same section.
        rank=entity.rank or "unknown",
        # Wikidata has nothing to say about nomenclatural status, so this one stays a
        # sentinel outright - the merge fills it from WoRMS wherever WoRMS answered. It is
        # deliberately *not* keyed on `matched_name`: the two say different things, and the
        # WoRMS side of this module had that coupling and had to have it taken out.
        status="unknown",
        matched_name=matched_name,
        source="wikidata",
        attribution=_WIKIDATA_ATTRIBUTION,
    )


def _choose_common_name(
    *,
    scientific_name: str,
    label: str | None,
    aliases: tuple[str, ...],
    vernaculars: tuple[str, ...] = (),
    rejected: tuple[str, ...] = (),
) -> str | None:
    """The one English name to display, capitalised, or `None` to fall back to the scientific
    name.

    The order is forced by what the sources actually contain. A taxon's English Wikidata
    *label* is very often the binomial itself, which would make "common name" a duplicate of
    the column next to it - so a label is only taken when it differs, and the first English
    alias ("Ocellaris clownfish") is what usually carries the real name. WoRMS vernaculars
    come last because their English coverage is the thin part; they are still tried, because
    a taxon Wikidata has never heard of may well have one. Neither register orders its
    vernaculars by preference, which is why this rule never reaches for one over an alias:
    WoRMS returns its six English names for *Orcinus orca* alphabetically, so "first" there
    means "grampus".

    **The binomial test is a case-insensitive *prefix* test rather than equality.** Both
    halves of that were forced by real answers. "Amphiprion Ocellaris" as a label is the
    scientific name wearing a capital; and Wikidata labels obscure taxa with the binomial plus
    its authority - resolving one returned the label
    "Leptasterias (Leptasterias) muelleri muelleri (M. Sars, 1846)", which under an equality
    test is "different from the scientific name" and would have been displayed as that taxon's
    common name. A name that begins with the binomial is the binomial with decoration on it,
    not something a diver would ever call the animal.

    **`rejected` catches what the prefix test cannot: a junior *scientific* synonym wearing a
    different genus.** *Orcinus orca*'s label is its binomial and its first English alias is
    "Orca gladiator" - a superseded scientific name, which shares no prefix with the accepted
    one and so sailed through as this app's display name for the killer whale. Resolve passes
    the taxon's WoRMS synonym list, and a candidate that casefold-*equals* an entry is
    skipped. Equality rather than a prefix here on purpose: a synonym list runs to dozens of
    names, and prefix-matching against all of them would start eating real vernaculars.

    The same restraint rules out the lexical shortcut that keeps suggesting itself - reject
    "a capitalised word followed by lowercase words" and you also reject *Hippocampus kuda*'s
    "Common seahorse" and *Chelonia mydas*'s "Green sea turtle", which are correct.

    **Only the first character is uppercased.** Neither Wikidata field is normalised at
    source - "whale shark" and "Blacktip reef shark" are both *labels* - so the app has to
    settle the case itself, and it can only settle the first letter: lowercasing the rest
    would destroy "Red Sea clownfish" and "Sibbold's Rorqual".
    """
    folded = scientific_name.casefold()
    banned = {name.casefold() for name in rejected}

    def is_a_name_for_the_animal(candidate: str) -> bool:
        cased = candidate.casefold()
        return not cased.startswith(folded) and cased not in banned

    for candidate in (label, *aliases, *vernaculars):
        if candidate is not None and is_a_name_for_the_animal(candidate):
            return candidate[:1].upper() + candidate[1:]
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
    """Common names, as far as WoRMS has them - which is not far, hence Wikidata.

    **Two calls, concurrently, and only one of them makes rows.** `AphiaRecordsByVernacular`
    is the row source, exactly as before; `AjaxAphiaRecordsByNamePart` chains alongside it
    purely to learn *which* vernacular matched, the way `_wikidata_entities` chains inside
    `_wikidata_search`. It is an annotation and never a source, for three measured reasons:
    its taxa are a subset of the record endpoint's everywhere sampled, it is useless for some
    queries (`orca` spends its whole row budget on scientific-name prefixes and omits the
    animal), and its rows are raw unfolded ids with no `valid_AphiaID` - so using them as rows
    would need a second fold call and would ship the duplicate taxa that fold away here. The
    one thing it uniquely knows is the vername string, and that is all this takes.

    Because it contributes no rows, `has_more` never sees it and the source count is unchanged.

    **A slow annotation must not cost the records.** `_request`'s own bounds sit far above
    `_SEARCH_BUDGET_SECONDS`, so without an inner bound a hung ajax call would have this whole
    source cancelled by the search budget - losing rows that had already arrived. The ajax leg
    therefore runs under `_AJAX_ANNOTATION_BUDGET_SECONDS`, and expiry degrades exactly like a
    fast failure: the records go out unannotated with `ok` false, so the entry keeps the short
    TTL and the next cold ask tries again. The wait is real and priced - the task group cannot
    exit until the hung leg is reaped, so a record page that answered in half a second still
    holds the source for the inner budget - and it is bought with the alternative being a
    month-long cache entry of unexplained rows.
    """
    rows: Any = None
    vernames: dict[int, str] | None = None

    async def records() -> None:
        nonlocal rows
        rows = await _worms("AphiaRecordsByVernacular", query, {"like": "true"})

    async def annotations() -> None:
        nonlocal vernames
        with anyio.move_on_after(_AJAX_ANNOTATION_BUDGET_SECONDS):
            payload = await _worms(
                "AjaxAphiaRecordsByNamePart",
                query,
                # `marine_only` is *not* inert on this endpoint whatever the record calls do:
                # `Astyanax` returns 11 rows under `true` and 50 under `false`, and the
                # documented default disagrees with the observed one - so it is sent
                # explicitly, matching `_worms_by_name`'s freshwater rule.
                {
                    "combine_vernaculars": "true",
                    "marine_only": "false",
                    "max_matches": _AJAX_MAX_MATCHES,
                },
            )
            vernames = _vername_map(payload, query)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(records)
        tasks.start_soon(annotations)

    page = _worms_page(rows, vernames)
    # An *empty* ajax answer is a complete one - WoRMS says "no match" with a 204 that
    # `_request` maps to `[]` - so only `None`, the failure and the budget expiry, drops `ok`.
    return _SourceAnswer(page.results, page.page_was_full, ok=page.ok and vernames is not None)


def _worms_page(rows: Any, vernames: dict[int, str] | None = None) -> _SourceAnswer:
    """A page of WoRMS records as results, plus whether the page was full and whether WoRMS
    answered at all.

    `_request` hands back `None` for every way of not getting an answer - unreachable, an
    error status, a body over the size cap, a body that did not parse - and `[]` only when
    the register genuinely said "no such name". That distinction is preserved here rather
    than collapsed, because it decides how long the merged answer is cached for.

    `vernames` is `_worms_by_vernacular`'s annotation map; the by-name source has no such
    thing and passes nothing. It is threaded through here rather than applied afterwards
    because the lookup needs each record's *pre-fold* id, which stops existing the moment
    `_worms_result` has returned.
    """
    if rows is None:
        return _SourceAnswer([], False, ok=False)
    if not isinstance(rows, list):
        # Valid JSON that is not an array is not WoRMS answering - a proxy or an error page.
        return _SourceAnswer([], False, ok=False)
    results = [result for row in rows if (result := _worms_result(row, vernames)) is not None]
    return _SourceAnswer(results, len(rows) >= _WORMS_PAGE_SIZE, ok=True)


async def _wikidata_search(query: str) -> _SourceAnswer:
    """Common names and aliases, via CirrusSearch filtered to entities that carry an AphiaID.

    Two chained phases, counting as one provider against the throttle, and the split between
    them is what makes breadth affordable at all.

    **Phase 1 asks for names, not claims.** `generator=search` with `prop=entityterms` returns
    fifty candidates and their English label and aliases in about eight kilobytes; the same
    fifty entities *with* their claims run to well over a megabyte - samples have ranged from
    roughly two hundred to over three hundred times the payload. There is no server-side claim
    filter to split the difference with, so the shape of the call is the only lever, and this
    is it: the search can be wide precisely because it asks for so little per hit.

    **Phase 2 enriches only the candidates worth enriching.** The terms Phase 1 carries are
    enough to score a candidate with the same `_match_bucket` the page ordering uses, so the
    fifty are pre-ranked and cut to `_WIKIDATA_ENRICH_LIMIT` before a single entity is fetched.

    That cut is *aligned* with `_ordered` rather than identical to it, and the difference is
    worth knowing before trusting it. `_ordered` also scores `scientific_name`, which is P225
    and does not exist until Phase 2 - so a taxon whose terms never mention its binomial can be
    cut here even though the final key would have ranked it first (Q472616 is the live shape:
    its terms say "Clownfish", its P225 says "Amphiprioninae"). The exposure is bounded by the
    division of labour rather than by luck: a scientific-name match is WoRMS by-name's job, so
    the row still arrives - what the cut can cost is that row's Wikidata *name*, not the row.
    It runs the other way too, harmlessly: the cut scores every term while the final key sees
    only the *chosen* display name, so a candidate can survive on an alias the row never shows
    and then rank a bucket lower than it was admitted at.

    The `haswbstatement:P850` filter is what keeps the result set to taxa WoRMS also knows,
    which is what makes the merge possible.
    """
    payload = await _wikidata(
        {
            "action": "query",
            "generator": "search",
            "gsrsearch": f"{query} haswbstatement:{_APHIA_PROPERTY}",
            "gsrlimit": _WIKIDATA_SEARCH_LIMIT,
            "prop": "entityterms",
            "wbetlanguage": "en",
            "wbetterms": "label|alias",
        }
    )
    pages = _wikidata_search_pages(payload)
    if pages is None:
        # Not a search response at all: transport failure, or a 200 carrying an error. Either
        # way this source learned nothing, which is not the same as finding nothing.
        return _SourceAnswer([], False, ok=False)
    if not pages:
        return _SourceAnswer([], False, ok=True)

    # **Two ways this page can be truncated, and Wikidata knows about only one of them.** It
    # sets `continue` when it held hits back; the cut below is this app's own truncation and is
    # invisible from there. `nudibranch` is what makes the second clause non-theoretical -
    # fifty candidates with no `continue` at all, because fifty is the whole result set, and
    # thirty-four of them discarded here. On a `continue`-only flag that page would claim to be
    # complete while most of it had been thrown away.
    truncated = (isinstance(payload, dict) and "continue" in payload) or len(pages) > _WIKIDATA_ENRICH_LIMIT

    # Stable, so CirrusSearch's own relevance order survives *inside* a bucket. That order is
    # better than anything this app could compute from a label and a handful of aliases, and
    # the bucket only overrules it where a candidate plainly answers the typed query better.
    scored = [(_best_matching_term(page.terms, query), page) for page in pages]
    scored.sort(key=lambda entry: entry[0][0])
    survivors = scored[:_WIKIDATA_ENRICH_LIMIT]

    hints = {page.qid: term for (_, term), page in survivors}
    entities, entities_ok = await _wikidata_entities([page.qid for _, page in survivors])
    results = [result for entity in entities if (result := _wikidata_result(entity, hints.get(entity.qid))) is not None]
    return _SourceAnswer(results, truncated, ok=entities_ok)


def _wikidata_search_pages(payload: Any) -> list[_WikidataPage] | None:
    """The candidates a search returned, in CirrusSearch's order - `[]` for a search that
    matched nothing, or `None` for a response that was not a search result at all.

    **The same three outcomes `_wikidata_qids` has, for the same reason**, and this exists as a
    second reader rather than as a change to that one because `generator=search` and
    `list=search` are differently shaped answers to differently shaped questions. Resolve still
    asks `list=search` for one entity by exact statement and keeps its own reader.

    Three measured shapes drive everything here:

    - **`query.pages` is an object keyed by stringified pageid, and that key order is not the
      relevance order.** Each page carries `index`, contiguous from 1, and it is the only thing
      that can put them back in the order CirrusSearch ranked them - iterating the object gets
      pageid order, which is arbitrary. Measured on `whale`: the first three keys carry indices
      31, 30 and 1.
    - **`entityterms` omits a key rather than emptying it.** Q733595, the `nudibranch`
      front-runner, has no English label at all - its page is
      `{"entityterms": {"alias": ["Nudibranchs"]}}` - and 22 of that query's 50 pages carry no
      `alias` key. A page with no `entityterms` whatsoever was never observed across the
      sampled payloads, and is handled anyway rather than assumed away.
    - **An empty result is `{"batchcomplete": ""}` with no `query` key at all** - twenty bytes,
      measured stable across distinct no-match queries. This is exactly where the two search
      shapes diverge: `list=search` answers a miss with `query.search` *present* and empty, so
      a reader that treats a missing key as "nothing found" would call every one of this
      variant's failures an empty register. Hence "no `query`, but `batchcomplete`" is the only
      shape that means empty, and anything else unrecognizable is a failure.

    Errors arrive as **HTTP 200 with a top-level `error` object**, exactly as on the other
    variant, so nothing about the transport says anything went wrong.
    """
    if not isinstance(payload, dict):
        return None
    if "error" in payload:
        error = payload["error"]
        code = error.get("code") if isinstance(error, dict) else None
        logger.warning("Wikidata refused a search (%s).", code or "unknown")
        return None
    if "query" not in payload:
        return [] if "batchcomplete" in payload else None

    query = payload["query"]
    if not isinstance(query, dict) or not isinstance(pages := query.get("pages"), dict):
        # A 200 that is neither an error nor a generator result - a proxy, a CDN page rendered
        # as JSON, or an API change. Not this app's business to interpret, and definitely not
        # an empty register.
        return None

    ranked: list[tuple[int, int, _WikidataPage]] = []
    for page in pages.values():
        if not isinstance(page, dict):
            continue
        qid = _text(page.get("title"), _QID_MAX_LENGTH)
        if qid is None:
            continue
        index = page.get("index")
        # A page with no usable `index` cannot be placed in the relevance order at all, so it
        # sorts last rather than being dropped or landing wherever the JSON happened to put it.
        unplaced, position = (0, index) if isinstance(index, int) else (1, 0)
        ranked.append((unplaced, position, _WikidataPage(qid=qid, terms=_page_terms(page))))

    ranked.sort(key=lambda entry: (entry[0], entry[1]))
    return [page for _, _, page in ranked]


def _page_terms(page: dict[str, Any]) -> tuple[str, ...]:
    """One candidate's English names, label first and aliases after.

    The order is fixed here rather than left to however the payload iterated, because it is the
    tie-break for which term ends up explaining a row. Both keys are optional independently -
    see `_wikidata_search_pages` - and neither is trusted to be a list of strings.
    """
    entityterms = page.get("entityterms")
    if not isinstance(entityterms, dict):
        return ()
    terms: list[str] = []
    for key in ("label", "alias"):
        values = entityterms.get(key)
        if isinstance(values, list):
            terms.extend(term for value in values if (term := _text(value)) is not None)
    return tuple(terms)


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

    **Only `resolve_species`'s exact-statement lookup reaches this now.** Search asks
    `generator=search` and reads it with `_wikidata_search_pages`, so the thirty-day cache
    entry described above is that reader's problem rather than this one's; what is left here is
    `_wikidata_by_aphia_id`, which collapses `None` and `[]` into "no entity to enrich with"
    because a resolve degrades to a row without a qid either way. The three outcomes still earn
    their keep on that path for the middle paragraph's reason - a 200 carrying an error must
    not read as a QID, or resolve would store a row pointing at whatever came back - and that
    is what the resolve-path test pins.
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

    # Each entry carries its source's position in `sources` below, which is the only thing
    # that can put the answers back in a fixed order afterwards - see the sort further down.
    collected: list[tuple[int, _SourceAnswer]] = []

    async def fetch(priority: int, source: str, call: Any) -> None:
        try:
            collected.append((priority, await call(query)))
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
            for priority, (source, call) in enumerate(sources):
                tasks.start_soon(fetch, priority, source, call)

    # **Two different ways of not being complete, and both have to count.** A source can run
    # out of *time* - still in flight when the budget expired, so it is not in `collected` at
    # all - or it can come back having *failed*: unreachable, an error status, a body over the
    # size cap, a body that did not parse. The second kind is the dangerous one, because it
    # returns fast and looks exactly like "this register had nothing", so an answer missing
    # half its sources would otherwise be stored for thirty days as though it were the whole
    # truth. `_SourceAnswer.ok` is what tells them apart.
    answered = len(collected) == len(sources)
    complete = answered and all(answer.ok for _, answer in collected)
    if not complete:
        logger.info(
            "A species search was incomplete: %d of %d sources answered, %d of those failed.",
            len(collected),
            len(sources),
            sum(1 for _, answer in collected if not answer.ok),
        )

    # Re-sorted before merging because a task group completes in whatever order the network
    # allowed, and the merge below is first-writer-wins: without this, which register defines
    # a shared row would depend on the weather. **On the source's own index**, not on where its
    # first row came from: that older key could only see `_PROVIDER_WORMS`, so the two WoRMS
    # sources tied and their relative order was completion order. Harmless while their rows
    # were identical in shape - and not harmless now that by-vernacular rows carry vernames,
    # since network weather would decide whether a folded row's hint reads as a synonym or as a
    # common name. By-name, then by-vernacular, then Wikidata: WoRMS first, since it owns the
    # taxonomy, and its two halves in a fixed order.
    collected.sort(key=lambda entry: entry[0])

    merged: dict[int, SpeciesSearchResult] = {}
    truncated = False
    for _, answer in collected:
        truncated = truncated or answer.page_was_full
        for result in answer.results:
            _merge_result(merged, result)

    # After the merge, because whether a hint is redundant depends on the `common_name` the
    # merge just supplied - and before the cache, so the stored entry keeps the schema's
    # contract rather than repairing it on every read.
    ordered = _ordered(_drop_redundant_hints(list(merged.values()), query), query)
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


def _visible_bucket(result: SpeciesSearchResult, query: str) -> int:
    """The best match over the names the diver can actually read on this row.

    `default` because the generator can in principle be empty - a catalog row whose
    `scientific_name` column holds an empty string - and a bare `min()` would raise there,
    which on this path means a 500 out of the one function in this module that promises never
    to fail for anything a provider or a stored row did.
    """
    return min(
        (_match_bucket(query, name) for name in (result.scientific_name, result.common_name) if name),
        default=_MATCH_NONE,
    )


def _drop_redundant_hints(results: list[SpeciesSearchResult], query: str) -> list[SpeciesSearchResult]:
    """Null `matched_name` wherever the row's own visible names already explain the match.

    `SpeciesSearchResult` has promised this all along - "null when the display name already
    explains the match" - and no single source can keep the promise, because none of them sees
    the finished row. `_worms_result` can only compare a vername against the scientific name;
    its `common_name` is always `None` and arrives from Wikidata at the merge. So `?q=swordfish`
    would ship *Xiphias gladius* as `Swordfish · matched "swordfish"`, and simulating `?q=whale`
    over live payloads put a redundant hint on four of the first sixteen rows ("Bowhead whale ·
    matched \\"whale-fish\\"").

    **Any bucket counts, not just equality.** A hint exists to account for a row nothing else
    accounts for, and "Bowhead whale" accounts for `whale` perfectly well. That is the same
    test `_ordered` uses to decide whether a row is hint-placed, which is what keeps the wire
    and the ranking saying the same thing: a hint survives exactly where the ranking would
    have to read one.

    It runs over the merged list rather than inside a source for the reason above, and it
    covers the catalog rows too - `_local_search`'s own SQL-side de-noise is equality-only, so
    a catalog row can reach here with a visible match and a live hint.
    """
    return [
        result
        if result.matched_name is None or _visible_bucket(result, query) == _MATCH_NONE
        else result.model_copy(update={"matched_name": None})
        for result in results
    ]


def _ordered(results: list[SpeciesSearchResult], query: str) -> list[SpeciesSearchResult]:
    """Rank by how well each row answers the query, best first.

    **Rows rank by the names the diver can see; a hidden name places a row only when the
    visible names place it nowhere.** That is the governing rule and the first key term. A row
    the diver can read something relevant on - `scientific_name` or `common_name` - outranks
    every row placed only by its `matched_name` hint, however good that hint is. Otherwise the
    genus *Orcinus*, which contains no "orca" anywhere a diver can see and is placed purely by
    its synonym "Orca", would beat the animal itself on `?q=orca`. Ranking on the best bucket
    over *all* the names was the first attempt and it failed the same way: *Balaena mysticetus*
    carries nine "whale" vernaculars, two of them prefix matches, so a row displaying "Bowhead
    whale" jumped over "Blue whale" explained by a name nobody typed.

    Below that, in order: the match bucket of whichever name placed the row (`_match_bucket`,
    five buckets rather than the three this used to have); `_rank_tier`, so the species a diver
    spotted outranks its genus and family inside a bucket; named rows ahead of bare binomials,
    which is what stops the two indistinguishable bare "Orcadia" genus rows from opening
    `?q=orca` above the killer whale; then the *displayed* name casefolded, which is the string
    the diver is actually reading and which kills the capitals-first artefact of comparing raw
    `str`s; then `aphia_id`, so the order is total and cannot depend on merge order even in
    theory.

    **This is no longer the ranking the local catalog's SQL applies**, and the divergence is
    named rather than left for the next reader to assume away. `_local_search` still orders by
    its own three-way `match_rank` and, worse, applies `LIMIT _MAX_RESULTS` under it - so the
    moment one instance's catalog holds more than `_MAX_RESULTS` matches for a single query,
    the SQL can cut a row this key would have ranked first, and nothing here can put it back.
    The catalog is small by construction and a long way from that threshold; the fix belongs
    with the `pg_trgm` escalation in DECISIONS.md whenever that reopens.
    """

    def key(result: SpeciesSearchResult) -> tuple[int, int, int, int, str, int]:
        visible = _visible_bucket(result, query)
        hint = _match_bucket(query, result.matched_name) if result.matched_name else _MATCH_NONE
        placed_visibly = visible < _MATCH_NONE
        return (
            0 if placed_visibly else 1,
            visible if placed_visibly else hint,
            _rank_tier(result.rank),
            0 if result.common_name else 1,
            (result.common_name or result.scientific_name).casefold(),
            result.aphia_id,
        )

    return sorted(results, key=key)


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
    # ORM state to expire and nothing to preserve - see `release_read_transaction`.
    await release_read_transaction(db)

    remote = await _remote_search(normalized)

    merged: dict[int, SpeciesSearchResult] = {result.aphia_id: result for result in local}
    for result in remote.results:
        # Catalog rows win outright rather than merging: their fields went through
        # `resolve_species`'s choices once already, and letting a live provider overwrite
        # them would make a dive's species card disagree with the picker that filled it.
        merged.setdefault(result.aphia_id, result)

    # Again over the merged list, because the catalog half has not been through it: a cached
    # remote entry is already clean, but `_local_search` de-noises by equality alone, so a
    # catalog row can arrive showing a name that matches the query *and* a hint repeating it.
    ordered = _ordered(_drop_redundant_hints(list(merged.values()), normalized), normalized)
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


async def _worms_synonyms(aphia_id: int) -> list[tuple[str, int | None]] | None:
    """Superseded names for a taxon and the AphiaIDs they are filed under, so a diver who
    learned *Manta birostris* still finds it - or `None` when WoRMS could not be asked for
    **all** of them.

    **The id is carried because the photo path needs it and it is free.** It sits right beside
    the name in the same response, and it is the key the synonym retry searches Wikidata by:
    the zebra shark's accepted id 313100 reaches an item with no image, while its unaccepted
    220032 reaches the item that has one. Discarding it here, as this used to, would mean a
    second WoRMS call to get back what was already in hand. `None` for a row WoRMS sent without
    a usable id - the name still vets the display name, which is this list's first job.

    **The `None` is the point, and it is why this one enrichment call is load-bearing.**
    `resolve_species` hands the list to `_choose_common_name` as its reject list, which is the
    only thing standing between a junior scientific synonym and a permanent display name (see
    that function, and *Rows are immutable in v1* in DECISIONS.md). A list that failed to
    arrive is not an empty list: returning `[]` for both would let a rate-limit drop-out or a
    timeout be read as "this taxon has no synonyms" and the wrong name written blind, with no
    re-resolve to correct it. Callers must treat `None` as a refusal, not a degradation.

    **And a truncated list vets nothing**, which is why this pages rather than reading the
    first page and stopping. WoRMS pages synonyms at `_WORMS_PAGE_SIZE` like its other list
    endpoints, and multi-page lists are ordinary - *Fucus vesiculosus* (145548) has 55, two of
    the overflow five being different-genus junior synonyms, exactly the shape the reject list
    exists to catch. A short page ends the list; a full one means there is more, and there is
    nothing in a full page that distinguishes it from a complete answer.

    **No page cap: the caller's enrichment budget is the bound.** A hard cap forces a choice
    between refusing a taxon with a very long list *forever* and vetting with a partial one,
    and both are worse than what a budget gives - at the measured quarter-second a page it
    spans far more pages than any real taxon needs, and a register pathological enough to
    exhaust it expires the budget into the ordinary transient 503. Any page that fails fails
    the whole list, for the same reason a truncation does.
    """
    synonyms: list[tuple[str, int | None]] = []
    offset = 1
    while True:
        rows = await _worms("AphiaSynonymsByAphiaID", aphia_id, {"offset": offset})
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict) or (name := _text(row.get("scientificname"))) is None:
                continue
            row_id = row.get("AphiaID")
            synonyms.append((name, row_id if isinstance(row_id, int) and row_id > 0 else None))
        if len(rows) < _WORMS_PAGE_SIZE:
            return synonyms
        offset += _WORMS_PAGE_SIZE


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
    synonyms: list[tuple[str, int | None]],
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
    # The synonyms' AphiaIDs are the photo path's business, not the search index's: a
    # `species_name` row is a string to match a typed query against.
    candidates += [(name, "synonym", "worms", None) for name, _ in synonyms]
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


# -------------- photos --------------


# What the whole photo pipeline may spend, and it is a **scope of its own** rather than a
# share of `_ENRICHMENT_BUDGET_SECONDS`. That is the sharpest hazard in this feature:
# `move_on_after` cancels the entire task group it wraps, so a slow Commons call sharing the
# enrichment scope would cancel the synonym walk beside it, `synonyms` would come back `None`,
# and `resolve_species` would answer **503** - on the only route by which a species enters the
# catalog at all, so divers could not add species to dives. Photos are decoration; that route
# is not. The invariant, pinned by a test: no failure or slowness of Commons can change the
# status code of `POST /species/resolve`.
#
# Twelve seconds covers up to four sequential calls - the synonym-item search, one entity
# chunk, the `imageinfo` lookup and the byte fetch - against hosts that answer in well under a
# second each. Expiry is not an error: the row is already committed and `photo_fetched_at` is
# left unstamped, so the backfill picks the species up on its next run.
_PHOTO_BUDGET_SECONDS = 12.0

# How many Wikidata items the synonym retry will look at. The search ORs every synonym id into
# one query whatever the count - some taxa have 55 - so this bounds the *entity* fetches that
# follow rather than the search: two chunks of `_WIKIDATA_ENTITY_BATCH`. A taxon with more than
# eight distinct items carrying one of its ids is a data problem rather than a case to serve.
_SYNONYM_ITEM_SEARCH_LIMIT = 8


async def _commons_imageinfo(file_title: str) -> tuple[str, species_photos.PhotoCredit] | None:
    """The URL of one Commons file's 500 px rendition and its credit, or `None`.

    **`iiurlwidth` rather than a hand-built thumbnail URL**, which is what keeps this clear of
    the bucketing trap: Commons serves thumbnails only at 120/250/330/500/960 and refuses
    anything else outright, so asking the API to name the URL means never constructing an
    off-bucket one. 500 is a real bucket, so 500 is what comes back.

    `thumburl` is absent when the source file is *narrower* than the width asked for - Commons
    does not upscale - and the full-size `url` is the right answer there, because a file under
    500 px wide is already thumbnail-sized. Both go through the same host fence downstream.
    """
    payload = await _commons(
        {
            "action": "query",
            "prop": "imageinfo",
            "titles": f"File:{file_title}",
            "iiprop": "extmetadata|url",
            "iiurlwidth": species_photos.COMMONS_THUMBNAIL_WIDTH,
        }
    )
    if not isinstance(payload, dict) or not isinstance(query := payload.get("query"), dict):
        return None
    pages = query.get("pages")
    if not isinstance(pages, list) or not pages:
        return None
    page = pages[0]
    if not isinstance(page, dict) or page.get("missing"):
        return None
    infos = page.get("imageinfo")
    if not isinstance(infos, list) or not infos or not isinstance(info := infos[0], dict):
        return None

    url = info.get("thumburl") or info.get("url")
    if not isinstance(url, str) or not url:
        return None
    return url, species_photos.credit_from_imageinfo(info)


async def _fetch_photo_bytes(url: str) -> bytes | None:
    """Fetch the image bytes, or `None` for every way of not getting them.

    Modelled on `user_avatars.import_google_avatar`: redirects are not followed, the read is
    capped, and the host is checked against an allowlist before anything leaves. The allowlist
    is the SSRF fence and it is hard-coded in `species_photos`, unlike the API endpoint beside
    it, which is a setting.

    **The `User-Agent` is not optional here.** Wikimedia's policy blocks generic and empty
    ones, and an empty header returns 403 on `upload.wikimedia.org` just as it does on
    `api.php` - which bites servers rather than `<img>` tags, because a browser always sends
    one. The same string the register calls already send does the job.
    """
    if not species_photos.is_photo_byte_source(url):
        logger.warning("Refusing to fetch species photo bytes from an unexpected host.")
        return None
    if not await _claim_provider_slot(_PROVIDER_COMMONS):
        logger.warning("Skipping a species photo fetch: this instance is over its Commons rate limit.")
        return None

    try:
        async with httpx.AsyncClient(follow_redirects=False, timeout=_TIMEOUT) as client:
            async with client.stream("GET", url, headers={"User-Agent": settings.SPECIES_USER_AGENT}) as response:
                if response.status_code != 200:
                    logger.info("A species photo fetch answered %s; the species keeps no photo.", response.status_code)
                    return None
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > species_photos.MAX_PHOTO_DOWNLOAD_BYTES:
                        logger.warning("A species photo exceeded %d bytes.", species_photos.MAX_PHOTO_DOWNLOAD_BYTES)
                        return None
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        logger.warning("A species photo fetch failed (%s).", type(exc).__name__)
        return None
    return bytes(body)


async def _wikidata_items_by_aphia_ids(aphia_ids: list[int]) -> list[_WikidataEntity]:
    """Every Wikidata item carrying any of these AphiaIDs, in **one** search.

    `haswbstatement` ORs its values inside a single query, verified against the live API, so
    "try a synonym" needs no per-synonym request however long the list is - the flagship case
    has 22 synonym ids and some taxa have 55. Whatever comes back feeds the existing
    four-at-a-time `wbgetentities` chunking.
    """
    if not aphia_ids:
        return []

    clause = "|".join(f"{_APHIA_PROPERTY}={aphia_id}" for aphia_id in aphia_ids)
    payload = await _wikidata(
        {
            "action": "query",
            "list": "search",
            "srsearch": f"haswbstatement:{clause}",
            "srlimit": _SYNONYM_ITEM_SEARCH_LIMIT,
        }
    )
    qids = _wikidata_qids(payload)
    if not qids:
        return []
    entities, _ = await _wikidata_entities(qids[:_SYNONYM_ITEM_SEARCH_LIMIT])
    return entities


def _choose_from_synonym_items(
    items: list[_WikidataEntity], *, scientific_name: str, accepted_aphia_id: int
) -> str | None:
    """The Commons file a synonym's item offers, or `None` to decline.

    Each item is judged by the same conservative rule as the accepted one, against **its own**
    P225 - which is the whole reason this path recovers the zebra shark: `Q169468`'s taxon name
    is *Stegostoma fasciatum* while this instance stores *Stegostoma tigrinum*, so matching the
    stored name would keep neither of its two candidates.

    Where more than one item offers a photo, prefer the one whose P225 *is* the accepted name,
    then the one holding the accepted id, and otherwise decline - the same refuse-when-ambiguous
    rule as everywhere else here. The second tie-break is live rather than defensive: the search
    that found the accepted item asked for one result, so a taxon whose accepted id is carried
    by two items can reach here with the other one.
    """
    offered = [
        (item, chosen)
        for item in items
        if (
            chosen := species_photos.choose_photo_file(
                species_photos.photograph_candidates(item.images), taxon_name=item.scientific_name
            )
        )
        is not None
    ]
    if not offered:
        return None
    if len(offered) == 1:
        return offered[0][1]

    folded = scientific_name.casefold()
    by_name = [chosen for item, chosen in offered if (item.scientific_name or "").casefold() == folded]
    if len(by_name) == 1:
        return by_name[0]
    by_id = [chosen for item, chosen in offered if item.aphia_id == accepted_aphia_id]
    if len(by_id) == 1:
        return by_id[0]
    return None


async def fetch_species_photo(
    *,
    scientific_name: str,
    aphia_id: int,
    entity: _WikidataEntity | None,
    synonym_aphia_ids: list[int],
) -> species_photos.FetchedPhoto | None:
    """Choose, fetch and normalize one species photo - or `None` for every way of not having
    one, which is most of them.

    **`None` is a first-class answer, not a failure**, and the caller stamps
    `photo_fetched_at` either way. Across the whole register only 11.7% of Wikidata items
    carrying a WoRMS id have a P18 at all, and this rule then refuses some of those on purpose,
    so "no photo" is the ordinary outcome and every surface has to look deliberate without one.

    The synonym retry fires **only when the accepted item offered no candidate at all**, never
    when the rule looked at candidates and refused them. That distinction is load-bearing:
    *Triaenodon obesus* carries a silvertip shark beside a correct photo at equal rank and
    neither title names the taxon, so the rule declines - and a retry that fired there would
    hand it a photo from a synonym's item, undoing the one refusal this design exists to make.

    Guarded end to end, `import_google_avatar`-style: whatever goes wrong out here, the caller
    is mid-way through an operation the diver asked for and a picture must not be able to fail
    it.
    """
    try:
        candidates = species_photos.photograph_candidates(entity.images) if entity is not None else []
        file_title = (
            species_photos.choose_photo_file(candidates, taxon_name=entity.scientific_name)
            if entity is not None
            else None
        )

        if file_title is None and not candidates and synonym_aphia_ids:
            # Gated on there being a *synonym* to try, so the very common "this taxon has no
            # Wikidata item at all" path costs no second search. The accepted id then rides
            # along with the synonyms rather than being left out: it is what makes the "prefer
            # the item holding the accepted id" tie-break reachable, and the accepted item
            # having no candidates is this branch's own precondition, so it can never win on
            # its own account.
            items = await _wikidata_items_by_aphia_ids(list(dict.fromkeys([aphia_id, *synonym_aphia_ids])))
            file_title = _choose_from_synonym_items(items, scientific_name=scientific_name, accepted_aphia_id=aphia_id)

        if file_title is None:
            return None

        found = await _commons_imageinfo(file_title)
        if found is None:
            return None
        url, credit = found

        data = await _fetch_photo_bytes(url)
        if data is None:
            return None

        return species_photos.fetched_photo(
            data=await species_photos.process_photo(data), file=file_title, credit=credit
        )
    except Exception:
        logger.warning("Could not fetch a species photo for %s; storing the row without one.", aphia_id, exc_info=True)
        return None


async def fetch_photo_for_species(*, scientific_name: str, aphia_id: int) -> species_photos.FetchedPhoto | None:
    """`fetch_species_photo` for a species already in the catalog, loading its own inputs.

    The entry point for `src/scripts/backfill_species_photos.py`, which has a stored row rather
    than the enrichment `resolve_species` happens to be holding. It pays for the two lookups
    that path gets free - the Wikidata entity and the synonym list - which is the right trade
    for a script that runs once and paces itself between species.

    A synonym list that failed to arrive degrades to no retry here rather than refusing, which
    is the opposite of what `resolve_species` does with the same `None`. The reason is what the
    list is *for* in each place: there it vets a display name about to be written forever, and
    here it is a second chance at a photograph that the next run will take again anyway.
    """
    entity: _WikidataEntity | None = None
    synonyms: list[tuple[str, int | None]] | None = None

    async def load_entity() -> None:
        nonlocal entity
        entity = await _wikidata_by_aphia_id(aphia_id)

    async def load_synonyms() -> None:
        nonlocal synonyms
        synonyms = await _worms_synonyms(aphia_id)

    with anyio.move_on_after(_ENRICHMENT_BUDGET_SECONDS):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(load_entity)
            tasks.start_soon(load_synonyms)

    return await fetch_species_photo(
        scientific_name=scientific_name,
        aphia_id=aphia_id,
        entity=entity,
        synonym_aphia_ids=[synonym_id for _, synonym_id in (synonyms or []) if synonym_id is not None],
    )


async def resolve_species(db: AsyncSession, aphia_id: int) -> Species:
    """Return the catalog row for an AphiaID, creating it from WoRMS and Wikidata if needed.

    Idempotent, and answering 200 either way: "resolve" rather than "create" because the
    caller is naming a taxon that already exists in the world, and whether this instance has
    seen it before is not their concern.

    **The one place in this module that can fail loudly.** Two WoRMS calls are mandatory and
    503 rather than degrading, because the alternative is writing a row into a table shared by
    every account on the strength of a guess: the record itself, and the synonym list that
    vets the display name (`_worms_synonyms`). Everything else here is enrichment and degrades
    silently - Wikidata contributes the common name and the qid, WoRMS's vernaculars the rest
    of the index, and a row without any of them is a perfectly good row that falls back to the
    scientific name.

    Handed a synonym's id - a diver picked *Manta birostris* - it follows `valid_AphiaID` and
    stores the accepted taxon, re-checking for an existing row under the accepted id first.

    **This is allowed to be slow, and has to be.** `AphiaRecordByAphiaID` was measured at
    eleven seconds against the live register, so a keystroke-sized budget here does not mean
    a fast endpoint - it means an endpoint that always 503s and a catalog that can never be
    filled. The client is showing a spinner against a deliberate click, which is the one
    place in this feature where waiting is the right answer.

    **The photo runs last, after the row is committed, in a timeout scope of its own**, and
    that placement is a correctness requirement rather than an ordering preference: sharing
    the enrichment scope would let a slow Commons cancel the synonym walk beside it and turn a
    photo provider's bad day into this endpoint answering 503. Nothing about a picture can
    change what this returns.
    """
    if (existing := await _species_by_aphia_id(db, aphia_id)) is not None:
        return existing

    # Nothing found, so the lookup above is holding a transaction open over no ORM state and
    # no writes - and the next line can take twenty-five seconds. See
    # `release_read_transaction`; the early return above deliberately precedes it, because
    # that path *does* hold a live `Species` and must not have it expired underneath the
    # caller.
    await release_read_transaction(db)

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
        await release_read_transaction(db)
        valid_taxon: _Taxon | None = None
        with anyio.move_on_after(_RESOLVE_BUDGET_SECONDS):
            valid_taxon = _worms_taxon(await _worms("AphiaRecordByAphiaID", taxon.valid_aphia_id))
        if valid_taxon is None:
            raise HTTPException(status_code=503, detail="Species lookup is temporarily unavailable.")
        taxon = valid_taxon

    # Enrichment, all three concurrently, and the three are not equals. Losing the
    # vernaculars or the entity costs names or a qid and never the row: both can only push
    # `_choose_common_name` toward `None` and the binomial fallback, which is degradation.
    # **The synonym list is load-bearing**, because it is that function's reject list, and a
    # missing one is the single absence here that can select a *wrong* name - permanently,
    # since rows are immutable and there is no re-resolve. So it is the one leg whose failure
    # is fatal: `_worms_synonyms` returns `None` rather than `[]` when it could not read the
    # whole list, the slot below starts at `None` so a budget expiry mid-walk is
    # indistinguishable from that, and the check after the group refuses rather than storing a
    # name nothing vetted. Each leg collects into its own slot rather than returning, since a
    # task group's tasks cannot return values.
    #
    # Budgeted well below the record fetch above, and separately from it: the diver is already
    # several seconds into a spinner by the time this runs, and enrichment is not worth another
    # twenty. That budget is also the only bound on the synonym walk - see `_worms_synonyms`.
    synonyms: list[tuple[str, int | None]] | None = None
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

    if synonyms is None:
        # The same 503 the record fetch raises, and for the same reason: a row here is a claim
        # shared with every account on the instance and never rewritten, so refusing costs the
        # diver a retry while guessing costs them the wrong name forever. One message for
        # every way of not having the list - a failed page, a denied rate slot, the budget
        # expiring mid-walk - because none of them is a distinction the diver can act on.
        raise HTTPException(status_code=503, detail="Species lookup is temporarily unavailable.")

    common_name = _choose_common_name(
        scientific_name=taxon.scientific_name,
        label=entity.label if entity is not None else None,
        aliases=entity.aliases if entity is not None else (),
        # English only for the *display* name, while every language goes into the index
        # below. The app has no i18n, so a Japanese common name on a dive card would be a
        # bug; the same name in the search index is a feature.
        vernaculars=tuple(name for name, language in vernaculars if language in (None, "eng")),
        # A superseded scientific name is not what this animal is called, however plausibly
        # it reads next to the accepted binomial.
        rejected=tuple(name for name, _ in synonyms),
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
        # No photo attempt on this branch: the winner's own resolve is making one, or has
        # already made it, and `photo_fetched_at IS NULL` is what the backfill picks up if the
        # winner died in between. Two callers racing to write the same photo would be two
        # blobs, one of them orphaned.
        return winner

    # **After the enrichment task group and in a scope of its own**, which is the point rather
    # than the tidy shape - see `_PHOTO_BUDGET_SECONDS`. The row above is committed by now, so
    # the species is already in the catalog and attachable to a dive whatever happens here.
    #
    # `taxon.scientific_name` rather than the entity's: this is the accepted binomial this
    # instance stores, and it is what the synonym retry's tie-breaks compare against. The
    # *selection rule* uses each examined item's own P225 instead, which is a different
    # question and is answered inside `fetch_species_photo`.
    photo: species_photos.FetchedPhoto | None = None
    try:
        with anyio.move_on_after(_PHOTO_BUDGET_SECONDS):
            photo = await fetch_species_photo(
                scientific_name=taxon.scientific_name,
                aphia_id=taxon.aphia_id,
                entity=entity,
                synonym_aphia_ids=[synonym_id for _, synonym_id in synonyms if synonym_id is not None],
            )
        # Outside the scope on purpose: the budget bounds what leaves this server, and a
        # cancellation landing part-way through the write would be a torn row rather than a
        # missing photo.
        await species_photos.save_photo_attempt(db, species_id=species.id, photo=photo)
    except Exception:
        # The row is committed and shared with every account on the instance, so nothing about
        # a picture may turn a resolve that succeeded into an error the client reads as
        # failure. `photo_fetched_at` stays null and the backfill collects it.
        logger.warning("Could not store a species photo for %s.", taxon.aphia_id, exc_info=True)
        await db.rollback()

    # After the photo, not before: `expire_on_commit=False` means the in-memory row still
    # carries the nulls it was constructed with, so a refresh placed above would return a
    # `SpeciesRead` claiming no photo for a species that has one.
    await db.refresh(species)
    return species
