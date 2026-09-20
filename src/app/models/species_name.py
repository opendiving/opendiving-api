from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class SpeciesName(Base):
    """Every name a species is findable by - its scientific name, its synonyms, and its
    common names in whatever languages the sources supplied.

    This is the search index, not a display source. `Species.common_name` is what any
    surface shows; these rows exist so that typing "clownfish", "カクレクマノミ" or the
    superseded *Manta birostris* all land on the same catalog row. They are value objects
    like `TripPart`: no public `uuid`, no ownership, nothing references one.

    **Multilingual on the way in, English on the way out.** WoRMS vernaculars arrive
    language-tagged and there are only a handful per taxon, so they are all kept - a name
    the UI will never render still earns its row by being searchable. Wikidata contributes
    its English label and aliases only; mirroring its full alias set across dozens of
    languages waits for the app to have an i18n story.

    **No index on `name`.** Search is a leading-wildcard `ILIKE`, which no btree can serve
    (see DECISIONS.md), so an index here would cost writes and buy nothing. The table is
    small by construction - the catalog only grows when a diver picks a species that isn't
    in it yet - and the recorded escalation is `pg_trgm` + a GIN index, not a different
    query shape.
    """

    __tablename__ = "species_name"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    # Indexed on its own because every read of this table is "the names of this species" -
    # unlike the dive join tables, there is no position to order by and so no composite
    # index whose leading column would already cover it. The FK's cascade is the only way a
    # row here is ever removed, and it is dormant in v1: nothing deletes a species.
    species_id: Mapped[int] = mapped_column(ForeignKey("species.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    # `scientific` | `synonym` | `common`. A closed vocabulary with no DB `CHECK`, matching
    # `GearItem.type`: the only writer is `services.species_service`, so a constraint would
    # duplicate a rule that already has exactly one enforcement point.
    kind: Mapped[str] = mapped_column(String(16))
    # `worms` | `wikidata` - which source supplied this spelling. Kept so a future refresh
    # can replace one source's names without touching the other's.
    source: Mapped[str] = mapped_column(String(16))
    # ISO 639 language code as the source tagged it, e.g. "jpn", "eng". Null for scientific
    # names and synonyms, which belong to no language, and for any common name that arrived
    # untagged.
    language_code: Mapped[str | None] = mapped_column(String(3), default=None)

    __table_args__ = (
        # Two languages can spell a common name identically ("barracuda"), and a synonym can
        # equal another taxon's accepted name; both are fine, and the first writer's row
        # keeps the slot. The service dedups case-insensitively before inserting, so this
        # constraint is the backstop rather than the mechanism - it only catches spellings
        # that differ solely in case.
        UniqueConstraint("species_id", "name", "kind", name="ux_species_name_species_id_name_kind"),
    )
