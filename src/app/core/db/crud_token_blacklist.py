from fastcrud import FastCRUD
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db.token_blacklist import TokenBlacklist
from ..schemas import TokenBlacklistCreate, TokenBlacklistRead, TokenBlacklistUpdate


class CRUDTokenBlacklist(
    FastCRUD[
        TokenBlacklist,
        TokenBlacklistCreate,
        TokenBlacklistUpdate,
        TokenBlacklistUpdate,
        TokenBlacklistUpdate,
        TokenBlacklistRead,
    ]
):
    async def revoke(self, db: AsyncSession, object: TokenBlacklistCreate) -> bool:
        """Insert the row unless its token already has one, commit, and say whether this
        call inserted it.

        `ON CONFLICT (token) DO NOTHING` rather than `create`, because the same token
        arriving twice is ordinary - two tabs rotating one refresh cookie, a double-clicked
        sign-out or account deletion - and `create` meets the unique index as an
        `IntegrityError`. A concurrent second insert waits for the first's transaction and
        then inserts nothing, so `False` is exact rather than a guess.
        """
        inserted = (
            await db.execute(
                insert(TokenBlacklist)
                .values(**object.model_dump())
                .on_conflict_do_nothing(index_elements=[TokenBlacklist.token])
                .returning(TokenBlacklist.id)
            )
        ).scalar_one_or_none()
        await db.commit()
        return inserted is not None


crud_token_blacklist = CRUDTokenBlacklist(TokenBlacklist)
