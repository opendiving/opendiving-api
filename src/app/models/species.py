from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class Species(Base, PublicUUIDMixin, TimestampMixin):
    """One marine (or freshwater) taxon a diver can record having seen, shared by every
    account on the instance.

    **The first table here that belongs to nobody.** Every other domain table carries a
    `user_id`, because everything else a diver enters is a fact about their own logbook. A
    species is a fact about the ocean: two divers who both saw *Mobula birostris* saw the
    same animal, and the row says so. That is what makes the sightings count, a future life
    list and iteration 2's photos possible at all - and it is why nothing here goes through
    `OwnedResourceCache` or `fetch_owned_or_raise`, both of which require a `user_id`. See
    "Species are a global catalog, filled one pick at a time" in DECISIONS.md.

    **The taxonomy is written once and never updated.** There is no PATCH endpoint and no
    refresh job. A global rename would have to invalidate the cached dives of *every* user
    that ever logged the species, and a cache sweep across all users is something this
    codebase has never needed - `services.cache_invalidation` speaks in `user_{id}_*`
    patterns. v1 avoids needing it. The consequence is bounded and documented: a row edited
    by hand in psql goes stale in already-cached dive reads for at most that cache's TTL.

    **The `photo_*` columns are the one exception, and they are exempt rather than a
    contradiction.** The immutability argument is about *invalidation* - a rename cannot be
    expressed in a pattern vocabulary that only speaks `user_{id}_*`. Filling a photo column
    inherits exactly the same bounded staleness and nothing worse: the only long-lived cached
    surface carrying one is the single-dive response, whose `SpeciesInfo` gains
    `photo_sha256`, and it self-heals within that same TTL. Nothing else caches a photo
    field. See "Filling a photo column is exempt from the immutability argument" in
    DECISIONS.md.

    **Identity is the accepted WoRMS AphiaID**, never a synonym's. Resolving an unaccepted
    id follows `valid_AphiaID` and stores the accepted taxon, with the synonym kept as a
    `SpeciesName` row so a search for it still finds this record. `rank` is not restricted to
    "Species" - "a moray eel" is an honest log entry, so a genus or family row is legal.

    No `SoftDeleteMixin`: nothing deletes a species. There is no endpoint that could, and a
    row nobody references costs a few hundred bytes.
    """

    __tablename__ = "species"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    # The World Register of Marine Species' own identifier, and this table's real identity.
    # Unique so two divers resolving the same species concurrently collide in the database
    # rather than creating a duplicate taxon - `services.species_service.resolve_species`
    # catches that `IntegrityError` and returns the winner's row.
    aphia_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    scientific_name: Mapped[str] = mapped_column(String(255))
    # WoRMS's open vocabularies, stored as plain strings and passed through: unlike
    # `GearItem.type` or `Certification.agency` these are somebody else's lists, and they
    # grow without asking us. A `StrEnum` here would turn a new WoRMS rank into a failed
    # resolve. `rank` is "Species", "Genus", "Family", ...; `status` is "accepted" for every
    # row this app writes, and is stored anyway so a record whose status later changes can
    # be told apart from one that never had it checked.
    rank: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(64))
    # The taxon's naming authority, e.g. "(Walbaum, 1792)". Displayed nowhere in v1; kept
    # because it is free at resolve time and is what makes a scientific name citable.
    authority: Mapped[str | None] = mapped_column(String(255), default=None)

    # WoRMS's classification, flat, exactly as it sends it. `class_name`/`order_name` rather
    # than `class`/`order`: the first is a Python keyword and the second is a reserved word
    # in SQL, and naming the columns for the ranks they hold sidesteps both without needing
    # a mapped_column alias or a quoted identifier. The wire schema uses the same two names.
    kingdom: Mapped[str | None] = mapped_column(String(255), default=None)
    phylum: Mapped[str | None] = mapped_column(String(255), default=None)
    class_name: Mapped[str | None] = mapped_column(String(255), default=None)
    order_name: Mapped[str | None] = mapped_column(String(255), default=None)
    family: Mapped[str | None] = mapped_column(String(255), default=None)
    genus: Mapped[str | None] = mapped_column(String(255), default=None)

    # WoRMS's habitat flags. Three-valued on purpose: `None` means WoRMS did not say, which
    # is different from "no". Nothing reads them yet - they are here because they arrive in
    # the same response as everything above, and a freshwater filter would otherwise need a
    # backfill across every row.
    is_marine: Mapped[bool | None] = mapped_column(Boolean, default=None)
    is_brackish: Mapped[bool | None] = mapped_column(Boolean, default=None)
    is_freshwater: Mapped[bool | None] = mapped_column(Boolean, default=None)

    # The one English name to show. Chosen at resolve time (see
    # `services.species_service._choose_common_name`) and null when no source offered one,
    # in which case every surface falls back to the scientific name. Deliberately singular
    # and deliberately English: the app has no i18n, while `species_name` keeps every
    # language it is given so a search in one still finds the row.
    common_name: Mapped[str | None] = mapped_column(String(255), default=None)

    # The Wikidata entity this was matched to, e.g. "Q1126155". Iteration 2's hook: the same
    # entity carries P18 (image), so storing the qid now is what makes photos cheap later.
    wikidata_qid: Mapped[str | None] = mapped_column(String(32), default=None)

    # One Wikimedia Commons photograph, fetched once and stored on this instance's files
    # volume - never hotlinked. Written by `services.species_photos`, served by
    # `GET /species/{uuid}/photo`, and all nullable because most of the register has no
    # usable image and this app refuses to guess (see that module's selection rule).
    #
    # The credit is stored as **parts, not as one pre-composed string**, which reverses
    # iteration 1's `attribution` precedent on purpose: a compliant credit needs two
    # different hyperlinks - the licence and the source - and one string can carry at most
    # one. The clients compose it.
    #
    # Unique like `user_picture`'s key columns and for the same reason: two rows naming one
    # key would let either one's replacement unlink the other's bytes. Nullable, and Postgres
    # allows any number of NULLs in a unique index, so the photo-less majority is unaffected.
    photo_storage_key: Mapped[str | None] = mapped_column(String(255), default=None)
    # Doubles as the ETag on the download route and as the whole client contract: non-null
    # means "there is a photo, and this is which one", and the client builds the URL itself.
    photo_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    # The Commons file title the bytes came from. Provenance, and what a re-fetch needs.
    photo_file: Mapped[str | None] = mapped_column(String(255), default=None)
    # The **creator**, never the uploader, and plain text: Commons' `Artist` field is HTML,
    # and it is parsed rather than stripped before it lands here.
    photo_author: Mapped[str | None] = mapped_column(String(255), default=None)
    # The licence's short name exactly as Commons reports it, e.g. "CC BY-SA 3.0". Somebody
    # else's open vocabulary, so a plain string for the same reason `rank` is one.
    photo_license: Mapped[str | None] = mapped_column(String(128), default=None)
    photo_license_url: Mapped[str | None] = mapped_column(String(512), default=None)
    # The Commons file description page - the "source" element every one of these licences
    # asks for, and the page a credit line links to.
    photo_source_url: Mapped[str | None] = mapped_column(String(512), default=None)
    # **Stamped on every attempt**, including one that found nothing and one that refused an
    # ambiguous P18 - which is what makes the backfill's re-run cheap. A predicate keyed on
    # the absence of stored bytes would never shrink, because "no photo" is the permanent
    # outcome for most of the catalog, and every re-run would re-query the whole photo-less
    # tail forever. See `src/scripts/backfill_species_photos.py`.
    photo_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        # The same unique index `dive_file`, `certification_file` and `user` carry on their
        # own key columns, for the same reason: two rows naming one key would let either
        # one's replacement unlink the other's bytes.
        Index("ux_species_photo_storage_key", "photo_storage_key", unique=True),
    )
