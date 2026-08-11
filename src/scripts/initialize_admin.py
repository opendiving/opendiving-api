"""One-shot setup for the CRUDAdmin panel's own tables and its initial admin user.

Run once before the API starts:

    python -m src.scripts.initialize_admin

`docker-compose.yml` wires this up as the `admin_init` service, which `web` waits on via
`service_completed_successfully`, so local development still needs no manual step.

Why this isn't in the app's lifespan: it used to be, and the lifespan runs once *per
worker*. Under `gunicorn -w 4` the four workers raced to create the same tables and insert
the same initial admin row; the losers crashed and took the container with them. Creating
a schema and seeding a row is a deployment step, not something each process should attempt
on boot - the same reasoning that keeps `create_first_superuser` out of the lifespan.

A no-op (exit 0) when `CRUD_ADMIN_ENABLED` is false, so it can sit unconditionally in a
compose file or entrypoint without the panel having to be turned on.
"""

import asyncio
import logging

from ..app.admin.initialize import create_admin_interface
from ..app.core.config import settings

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def main() -> None:
    admin = create_admin_interface()
    if admin is None:
        logger.info("CRUD_ADMIN_ENABLED is false - nothing to initialize.")
        return

    if settings.CRUD_ADMIN_DB_URL is None:
        # The default SQLite file is created relative to *this* process's working
        # directory, so in a one-shot container it lands somewhere the API container
        # cannot read. Worth saying out loud rather than leaving someone to wonder why
        # their admin login fails against an apparently-initialized panel.
        logger.warning(
            "CRUD_ADMIN_DB_URL is unset, so the panel is using a local SQLite file. "
            "That is single-process only: set it to a shared database (e.g. the app's "
            "Postgres) if the API runs more than one worker or container."
        )

    await admin.initialize()
    logger.info("Admin interface initialized (tables ready, initial admin ensured).")


if __name__ == "__main__":
    asyncio.run(main())
