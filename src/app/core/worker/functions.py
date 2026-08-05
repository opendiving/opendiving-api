import asyncio
import logging
from datetime import datetime
from typing import Any

import uvloop
from arq.worker import Worker

from ..db.crud_token_blacklist import crud_token_blacklist
from ..db.database import local_session

asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")


# -------- background tasks --------
async def purge_expired_tokens(ctx: dict[Any, Any]) -> str:
    """Delete rows from `token_blacklist` whose `expires_at` is in the past.

    Blacklist entries only need to be kept until the token they reference
    would have expired naturally, since an expired JWT is already rejected
    on its own. Without this cleanup the table grows unbounded, as every
    logout/account-deletion inserts new rows and nothing ever removes them.
    """
    async with local_session() as db:
        now = datetime.now()
        expired_count = await crud_token_blacklist.count(db, expires_at__lt=now)
        if expired_count == 0:
            logging.info("No expired blacklisted tokens to purge")
            return "No expired tokens to purge"

        await crud_token_blacklist.delete(db, allow_multiple=True, expires_at__lt=now)
        logging.info("Purged %d expired blacklisted token(s)", expired_count)
        return f"Purged {expired_count} expired token(s)"


# -------- base functions --------
async def startup(ctx: Worker) -> None:
    logging.info("Worker Started")


async def shutdown(ctx: Worker) -> None:
    logging.info("Worker end")
