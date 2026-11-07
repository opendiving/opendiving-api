import uuid as uuid_pkg

from sqlalchemy import ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from ..core.db.database import Base


class DiveSiteAlias(Base):
    """For one account, the uuid of a site it replaced, and the Commons record that uuid now means.

    Written when a site is replaced with a record or accepted into one, so what a phone queued
    against the old uuid still lands. Private to the account and tied to it: both keys cascade.
    The uuid is deliberately not a key into `dive_site`, whose row is gone, and may name another
    account's row, which an import of a shared export makes under that very uuid.
    """

    __tablename__ = "dive_site_alias"

    id: Mapped[int] = mapped_column("id", autoincrement=True, nullable=False, primary_key=True, init=False)
    # No standalone index: the unique constraint below leads with it.
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    alias_uuid: Mapped[uuid_pkg.UUID] = mapped_column(UUID(as_uuid=True))
    dive_site_id: Mapped[int] = mapped_column(ForeignKey("dive_site.id", ondelete="CASCADE"), index=True)

    __table_args__ = (UniqueConstraint("user_id", "alias_uuid", name="ux_dive_site_alias_user_id_alias_uuid"),)
