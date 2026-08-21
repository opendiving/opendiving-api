import asyncio
import logging
from datetime import UTC, datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, MetaData, String, Table, insert, select
from sqlalchemy.dialects.postgresql import UUID
from uuid6 import uuid7  # 126

from ..app.core.config import settings
from ..app.core.db.database import AsyncSession, async_engine, local_session
from ..app.models.user import User

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# A hand-built Core mirror of the two tables this script inserts into, rather than the ORM
# models - it runs before the app does and inserts through a bare connection.
#
# **It is a copy, and a copy drifts.** Every column named here has to exist on the real
# table, because SQLAlchemy puts a column with a client-side `default=` into the INSERT
# whether or not `data` mentions it - so a column dropped from the model turns this into an
# `UndefinedColumn` on every fresh install, swallowed by the bare `except` below and logged
# as a line nobody reads. That is not hypothetical: `profile_image_url` was named here and
# was dropped when avatars arrived. `tests/test_create_first_superuser.py` checks both
# directions of the drift, which is the only thing standing between the next drop and a
# silent bootstrap failure. Lifted to module scope so it can.
_metadata = MetaData()

USER_TABLE = Table(
    "user",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True, nullable=False),
    Column("name", String(30), nullable=False),
    Column("username", String(20), nullable=False, unique=True, index=True),
    Column("email", String(50), nullable=False, unique=True, index=True),
    Column("uuid", UUID(as_uuid=True), default=uuid7, unique=True),
    Column("created_at", DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False),
    Column("updated_at", DateTime),
    Column("deleted_at", DateTime),
    Column("is_deleted", Boolean, default=False, index=True),
    Column("is_superuser", Boolean, default=False),
)

AUTHENTICATION_PROVIDER_TABLE = Table(
    "authentication_provider",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True, nullable=False),
    Column("user_id", Integer, ForeignKey("user.id", ondelete="CASCADE"), nullable=False),
    Column("provider", String(20), nullable=False),
    Column("provider_user_id", String, nullable=True),
    Column("created_at", DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False),
)


async def create_first_user(session: AsyncSession) -> None:
    """Creates the `is_superuser` account this instance's operator signs in as.

    `ADMIN_EMAIL` has no default on purpose (`core.config.FirstUserSettings`), so unset is
    a configuration mistake rather than a shape to work around: sign-in is passwordless
    and keyed on the address, and a superuser row created against a guessed one hands its
    magic link to whoever owns that domain.
    """
    if not settings.ADMIN_EMAIL:
        logger.error("ADMIN_EMAIL is not set; no admin user created. Sign-in is keyed on it, so there is no default.")
        return

    try:
        name = settings.ADMIN_NAME
        email = settings.ADMIN_EMAIL
        username = settings.ADMIN_USERNAME

        query = select(User).filter_by(email=email)
        result = await session.execute(query)
        user = result.scalar_one_or_none()

        if user is None:
            data = {
                "name": name,
                "email": email,
                "username": username,
                "is_superuser": True,
            }

            async with async_engine.connect() as conn:
                result = await conn.execute(insert(USER_TABLE).values(data).returning(USER_TABLE.c.id))
                user_id = result.scalar_one()
                # No password anywhere - the admin authenticates the same way as any
                # other user, via the email-magic-link flow (see `AuthenticationProvider`).
                await conn.execute(insert(AUTHENTICATION_PROVIDER_TABLE).values(user_id=user_id, provider="email"))
                await conn.commit()

            logger.info(f"Admin user {username} created successfully.")

        else:
            logger.info(f"Admin user {username} already exists.")

    except Exception as e:
        logger.error(f"Error creating admin user: {e}")


async def main():
    async with local_session() as session:
        await create_first_user(session)


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
