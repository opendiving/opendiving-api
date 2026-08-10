from sqlalchemy import ForeignKey, Index, Integer, LargeBinary, String
from sqlalchemy.orm import Mapped, declared_attr, deferred, mapped_column

from ..core.db.database import Base
from ..core.db.models import PublicUUIDMixin, TimestampMixin


class DiveFile(Base, PublicUUIDMixin, TimestampMixin):
    """The dive-computer export a dive was imported from.

    Kept so that new parsing features can be developed and backfilled against real data.
    The parsers currently read a handful of header fields (see `services/dive_parsers/`);
    the files themselves carry per-sample depth and temperature profiles, deco stops,
    surface intervals and device metadata that nothing persists yet. Extracting any of
    that later is only testable against a corpus of the exports divers actually upload -
    and only *backfillable* if each file is still attached to the dive it produced.

    Only files that became a dive are stored. `POST /dive/parse` stays parse-only and
    in-memory; the bytes arrive here from `PUT /dive/{uuid}/file` after the dive exists,
    carrying a signed token from that parse (see `create_dive_file_token` in
    `core/security.py`) which proves this server parsed these exact bytes for this user.
    Without it the endpoint would accept any blob shaped vaguely like an export, and a
    stored file could not be trusted to be the one that pre-filled the dive's form.

    Bytes live in Postgres (`data`, a `bytea`) rather than object storage, for the same
    reasons as `CertificationFile`: exports are a few hundred KB, a diver logs a few
    hundred dives, and the existing database and its backups cover the lot with no new
    infrastructure. `services/dive_files.py` is the only module that touches `data` and
    is the seam to rewrite if that ever stops being true.

    A separate table rather than a `bytea` on `dive`: `dive` is read by `get_multi` on
    the hot list path, and a blob column there is one careless `schema_to_select` away
    from being dragged into every page of every dive list. Out of reach is better than
    deferred.

    No `SoftDeleteMixin`. A soft-deleted blob occupies its bytes forever with nothing
    able to read it; these are hard-deleted, including when their dive is soft-deleted
    (see `erase_dive`) - which also frees both unique slots below, so re-importing the
    same export into a new dive isn't blocked by a dive the diver can no longer see.
    """

    __tablename__ = "dive_file"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, unique=True, primary_key=True, init=False)
    # The owner is denormalized off `dive` rather than joined for it, because the dedupe
    # index below is per-user and has to be enforceable in one table.
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    dive_id: Mapped[int] = mapped_column(ForeignKey("dive.id", ondelete="CASCADE"))
    # Hex SHA-256 of `data`. Three jobs: the `ETag` on the download endpoint, the dedupe
    # key below, and the value the upload token is checked against - it is what ties a
    # set of bytes to a parse this server performed.
    sha256: Mapped[str] = mapped_column(String(64))
    # Taken from the parser that successfully read the file (`DiveParser.content_type`),
    # never from the client's claimed `Content-Type` - it is what the download route
    # serves the bytes back as.
    content_type: Mapped[str] = mapped_column(String(32))
    byte_size: Mapped[int] = mapped_column(Integer)
    original_filename: Mapped[str] = mapped_column(String(255))
    # `DiveParser.key` of the parser that produced this dive's values. Records what read
    # the file at import time - not a promise the same parser would still claim it - so
    # that a later backfill can select the subset it knows how to re-read.
    parser_key: Mapped[str] = mapped_column(String(32))
    # `deferred` so any query for a file row returns metadata only unless the bytes are
    # asked for explicitly with `undefer`.
    #
    # `nullable=False` is spelled out because wrapping the column in `deferred()` hides
    # the `Mapped[bytes]` annotation from SQLAlchemy's nullability inference, which would
    # otherwise emit a nullable column - and a file row with no bytes is meaningless.
    data: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))

    @declared_attr.directive
    @classmethod
    def __table_args__(cls) -> tuple:
        return (
            # One source file per dive. Re-importing replaces it rather than
            # accumulating versions: a diver who imports the wrong export and fixes it
            # wants the fix, not both. Enforced here as well as in `store_dive_file` so
            # two concurrent uploads can't both win.
            Index("ux_dive_file_dive_id", "dive_id", unique=True),
            # Byte-identical content is stored once per diver. An export normally holds
            # a single dive, so the same bytes turning up against a second dive means a
            # duplicate import - which `store_dive_file` reports rather than silently
            # storing twice or moving the file off the dive that already has it.
            #
            # Also the index a backfill reads by: `user_id` leads, so no separate
            # single-column index on it is needed.
            Index("ux_dive_file_user_id_sha256", "user_id", "sha256", unique=True),
        )
