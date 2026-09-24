"""Wire and admin-panel schemas for the species catalog.

Both, in one module, unlike `schemas/trip_part.py` - which is admin-only and says so.
`species` is a public resource with its own endpoints *and* three tables the panel renders,
so `SpeciesRead` (what `GET /species/{uuid}` returns) and `SpeciesReadInternal` (the row
shape) coexist here the way `TripPartRead`/`TripPartReadInternal` do across their
two modules.

Every string a provider wrote is bounded here, for the reason `GeocodeResult`'s are: they
are third-party text, cached for a month and handed to every client. The bounds match the
column widths in `models/species.py`, since that is where they are headed.
"""

import uuid as uuid_pkg
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..core.schemas import PublicUUIDSchema

# The source that supplied a search result. `catalog` means the row already exists locally
# and so carries a `uuid` a dive can reference immediately; the other two mean the diver has
# to resolve it first.
SpeciesSource = Literal["catalog", "worms", "wikidata"]


class SpeciesBase(BaseModel):
    """The taxon itself, as WoRMS describes it."""

    aphia_id: Annotated[int, Field(gt=0, examples=[278400], description="The species' WoRMS AphiaID")]
    scientific_name: Annotated[str, Field(max_length=255, examples=["Amphiprion ocellaris"])]
    common_name: Annotated[
        str | None,
        Field(
            default=None,
            max_length=255,
            examples=["Ocellaris clownfish"],
            description="One English display name, or null when no source offered one",
        ),
    ]
    authority: Annotated[str | None, Field(default=None, max_length=255, examples=["Cuvier, 1830"])]
    # WoRMS's own vocabularies, passed through rather than mapped onto an enum of ours -
    # they are somebody else's lists and they grow. See `models/species.py`.
    rank: Annotated[str, Field(max_length=64, examples=["Species"])]
    status: Annotated[str, Field(max_length=64, examples=["accepted"])]
    kingdom: Annotated[str | None, Field(default=None, max_length=255, examples=["Animalia"])]
    phylum: Annotated[str | None, Field(default=None, max_length=255, examples=["Chordata"])]
    # `class_name`/`order_name` rather than `class`/`order`: the first is a Python keyword,
    # the second a reserved SQL word. The columns are named the same, so nothing aliases.
    class_name: Annotated[str | None, Field(default=None, max_length=255, examples=["Teleostei"])]
    order_name: Annotated[str | None, Field(default=None, max_length=255, examples=["Perciformes"])]
    family: Annotated[str | None, Field(default=None, max_length=255, examples=["Pomacentridae"])]
    genus: Annotated[str | None, Field(default=None, max_length=255, examples=["Amphiprion"])]
    is_marine: Annotated[bool | None, Field(default=None)]
    is_brackish: Annotated[bool | None, Field(default=None)]
    is_freshwater: Annotated[bool | None, Field(default=None)]
    wikidata_qid: Annotated[
        str | None,
        Field(default=None, max_length=32, examples=["Q1126155"], description="Wikidata entity id, when one matched"),
    ]


class SpeciesPhotoCredit(BaseModel):
    """The stored photo's provenance, as the parts a compliant credit line is built from.

    **Parts rather than one pre-composed string**, which is the opposite of what
    `SpeciesSearchResult.attribution` does and is a deliberate departure: a credit here needs
    two different hyperlinks - the licence and the source page - and a single string can
    carry at most one of them. The client composes; nothing here is markup, and none of it
    may ever be interpolated as HTML (`photo_author` is parsed out of Commons' HTML `Artist`
    field, so it is plain text by the time it reaches this).

    Every field is nullable independently. Measured across a 40-file sample, `descriptionurl`
    was present on all forty and `Artist` was absent from one, so a photo whose author is
    unknown is a real state rather than a defensive one.
    """

    photo_file: Annotated[
        str | None,
        Field(default=None, max_length=255, examples=["Clownfisch (Amphiprion ocellaris).jpg"]),
    ]
    photo_author: Annotated[str | None, Field(default=None, max_length=255, examples=["Raimond Spekking"])]
    photo_license: Annotated[str | None, Field(default=None, max_length=128, examples=["CC BY-SA 4.0"])]
    photo_license_url: Annotated[
        str | None,
        Field(default=None, max_length=512, examples=["https://creativecommons.org/licenses/by-sa/4.0"]),
    ]
    photo_source_url: Annotated[
        str | None,
        Field(
            default=None,
            max_length=512,
            examples=["https://commons.wikimedia.org/wiki/File:Clownfisch%20(Amphiprion%20ocellaris).jpg"],
            description="The Commons file description page - the 'source' the licence asks for",
        ),
    ]


class SpeciesRead(SpeciesBase, SpeciesPhotoCredit, PublicUUIDSchema):
    """Public representation of a catalog row, keyed by its opaque `uuid`.

    No `user_uuid`, unlike every other read schema here: the catalog belongs to nobody. The
    `aphia_id` is exposed deliberately - it is the identifier that makes a row meaningful
    outside this database, which is also why the export carries it.

    Carries the whole credit because the species page renders it. **`photo_storage_key` and
    `photo_fetched_at` are deliberately absent**, exactly as `UserRead` carries
    `avatar_sha256` and never the rendition's storage key: an internal blob key and an
    operational timestamp are not part of any client contract.
    """

    created_at: datetime
    # The same digest `SpeciesInfo` carries, and the same contract - see there. Non-null
    # means there is a photo; the client builds `/species/{uuid}/photo?v=<prefix>` itself.
    photo_sha256: Annotated[str | None, Field(default=None, max_length=64)]


class SpeciesSearchResult(BaseModel):
    """One hit from `GET /species/search`, whether it came from the local catalog or from a
    provider.

    `uuid` is the field that separates the two: set means the species is already in the
    catalog and a dive can reference it now, null means the client must
    `POST /species/resolve` with the `aphia_id` first. Keyed on `aphia_id` rather than
    `uuid` precisely so a remote hit is expressible at all.

    `matched_name` says *why* this row matched when the reason isn't visible - a synonym or
    a foreign-language vernacular the diver typed. Null when the display name already
    explains the match.
    """

    aphia_id: Annotated[int, Field(gt=0, examples=[278400])]
    uuid: Annotated[
        uuid_pkg.UUID | None,
        Field(default=None, description="Set when this species is already in the local catalog"),
    ]
    scientific_name: Annotated[str, Field(max_length=255, examples=["Amphiprion ocellaris"])]
    common_name: Annotated[str | None, Field(default=None, max_length=255, examples=["Ocellaris clownfish"])]
    rank: Annotated[str, Field(max_length=64, examples=["Species"])]
    status: Annotated[str, Field(max_length=64, examples=["accepted"])]
    matched_name: Annotated[
        str | None,
        Field(default=None, max_length=255, examples=["Manta birostris"], description="The alias or synonym that hit"),
    ]
    source: Annotated[SpeciesSource, Field(examples=["worms"])]
    # Carried per-result rather than in an envelope, for the reason `GeocodeResult` does it:
    # attribution is a licence condition of the data itself, so it travels with the row it
    # describes. A wire format, not display copy - the clients dedupe these strings and
    # render them as one credit line, reading `[label](href)` as a link. See DECISIONS.md.
    attribution: Annotated[
        str,
        Field(
            max_length=255,
            examples=["[World Register of Marine Species](https://www.marinespecies.org) (CC BY)"],
        ),
    ]


class SpeciesSearchResponse(BaseModel):
    """A capped list, not a paginated envelope.

    This is a picker feed like `GET /geocode/search`, not a browsable collection: there is
    no page to ask for and no total to report, because the answer is assembled from three
    sources whose totals mean different things. `has_more` is the one thing a client needs -
    it renders "keep typing to narrow" and stops.
    """

    results: Annotated[list[SpeciesSearchResult], Field(default_factory=list)]
    has_more: Annotated[
        bool, Field(default=False, description="True when matches were cut, or a source returned a full page")
    ]


class SpeciesLifeListEntry(PublicUUIDSchema):
    """One row of `GET /user/species` - a species this diver has logged, and their history
    with it.

    Not a `SpeciesRead` with extras: the three aggregate fields are facts about *this
    caller's* logbook rather than about the taxon, which is the whole reason this route lives
    under `/user/` rather than under the ownerless `/species/`. The taxon half is deliberately
    the same subset `SpeciesInfo` carries, plus the digest, so a card renders without a second
    request per row.

    `first_seen`/`last_seen` are dive **start times**, so they carry the offset the diver
    logged the dive in - the same values every other dive-derived surface reports.
    """

    scientific_name: Annotated[str, Field(max_length=255, examples=["Amphiprion ocellaris"])]
    common_name: Annotated[str | None, Field(default=None, max_length=255, examples=["Ocellaris clownfish"])]
    rank: Annotated[str, Field(max_length=64, examples=["Species"])]
    photo_sha256: Annotated[str | None, Field(default=None, max_length=64)]
    dive_count: Annotated[int, Field(ge=1, examples=[7], description="How many live dives recorded this species")]
    first_seen: datetime
    last_seen: datetime


class SpeciesResolveRequest(BaseModel):
    """Body for `POST /species/resolve` - the one field that identifies a taxon globally.

    `extra="forbid"` so a client that sends a scientific name alongside gets told, rather
    than having it silently ignored: the name is not what this resolves on, and a caller
    that thinks it is has a bug.
    """

    model_config = ConfigDict(extra="forbid")

    aphia_id: Annotated[int, Field(gt=0, examples=[278400], description="WoRMS AphiaID of the species to resolve")]


# -------------- admin-panel schemas --------------
# Registered in `admin/views.py`. `Species` and `SpeciesName` are registered without
# "delete" - see there for why - so their `*Delete` schemas exist only for completeness,
# matching every other table's set.


class SpeciesCreate(SpeciesBase):
    model_config = ConfigDict(extra="forbid")


class SpeciesReadInternal(SpeciesBase, SpeciesPhotoCredit):
    """Mirrors the actual `species` table columns. Never returned over the API - the public
    shape is `SpeciesRead`, which carries the `uuid` instead of the internal `id`.

    The two photo fields `SpeciesRead` withholds are here, since this shape is the row."""

    id: int
    uuid: uuid_pkg.UUID
    created_at: datetime
    photo_sha256: Annotated[str | None, Field(default=None, max_length=64)]
    photo_storage_key: Annotated[str | None, Field(default=None, max_length=255)]
    photo_fetched_at: datetime | None = None


class SpeciesUpdate(BaseModel):
    """Admin-only, and the panel is the only way to edit a catalog row at all: there is no
    PATCH endpoint, by design (see `models/species.py`). An edit here goes stale in already
    cached dive reads for at most that cache's TTL, since global invalidation is not
    something this codebase can express.

    **The `photo_*` columns are deliberately absent from this schema altogether**, the same
    call `UserUpdate` makes about the pictures and for the same reason: they are written
    by `services.species_photos`, which owns the blob beside them, and an edit that could
    null the key while leaving the file on the volume is exactly the orphan this app has a
    sweeper for. Re-fetching a photo is a backfill run, not a form field.
    """

    model_config = ConfigDict(extra="forbid")

    aphia_id: Annotated[int | None, Field(default=None, gt=0)]
    scientific_name: Annotated[str | None, Field(default=None, max_length=255)]
    common_name: Annotated[str | None, Field(default=None, max_length=255)]
    authority: Annotated[str | None, Field(default=None, max_length=255)]
    rank: Annotated[str | None, Field(default=None, max_length=64)]
    status: Annotated[str | None, Field(default=None, max_length=64)]
    kingdom: Annotated[str | None, Field(default=None, max_length=255)]
    phylum: Annotated[str | None, Field(default=None, max_length=255)]
    class_name: Annotated[str | None, Field(default=None, max_length=255)]
    order_name: Annotated[str | None, Field(default=None, max_length=255)]
    family: Annotated[str | None, Field(default=None, max_length=255)]
    genus: Annotated[str | None, Field(default=None, max_length=255)]
    is_marine: Annotated[bool | None, Field(default=None)]
    is_brackish: Annotated[bool | None, Field(default=None)]
    is_freshwater: Annotated[bool | None, Field(default=None)]
    wikidata_qid: Annotated[str | None, Field(default=None, max_length=32)]


class SpeciesDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SpeciesNameBase(BaseModel):
    species_id: Annotated[int, Field(examples=[1], description="ID of the species")]
    name: Annotated[str, Field(min_length=1, max_length=255, examples=["clownfish"])]
    kind: Annotated[str, Field(max_length=16, examples=["common"], description="scientific | synonym | common")]
    source: Annotated[str, Field(max_length=16, examples=["wikidata"], description="worms | wikidata")]
    language_code: Annotated[str | None, Field(default=None, max_length=3, examples=["eng"])]


class SpeciesNameCreate(SpeciesNameBase):
    model_config = ConfigDict(extra="forbid")


class SpeciesNameReadInternal(SpeciesNameBase):
    """Mirrors the actual `species_name` table columns. These rows have no public shape:
    they are a search index, and nothing outside `services.species_service` reads them."""

    id: int


class SpeciesNameUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    species_id: Annotated[int | None, Field(default=None, description="ID of the species")]
    name: Annotated[str | None, Field(default=None, min_length=1, max_length=255)]
    kind: Annotated[str | None, Field(default=None, max_length=16)]
    source: Annotated[str | None, Field(default=None, max_length=16)]
    language_code: Annotated[str | None, Field(default=None, max_length=3)]


class SpeciesNameDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiveSpeciesBase(BaseModel):
    dive_id: Annotated[int, Field(examples=[1], description="ID of the dive")]
    species_id: Annotated[int, Field(examples=[1], description="ID of the species")]
    position: Annotated[int, Field(default=0, examples=[0], description="Order the species was listed in")]


class DiveSpeciesRead(DiveSpeciesBase):
    id: int


class DiveSpeciesCreate(DiveSpeciesBase):
    model_config = ConfigDict(extra="forbid")


class DiveSpeciesUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dive_id: Annotated[int | None, Field(default=None, description="ID of the dive")]
    species_id: Annotated[int | None, Field(default=None, description="ID of the species")]
    position: Annotated[int | None, Field(default=None, description="Order the species was listed in")]


class DiveSpeciesDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
