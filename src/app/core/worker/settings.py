from arq.connections import RedisSettings
from arq.cron import cron

from ...core.config import settings
from .functions import purge_expired_tokens, send_gear_service_digests, shutdown, startup

REDIS_QUEUE_HOST = settings.REDIS_QUEUE_HOST
REDIS_QUEUE_PORT = settings.REDIS_QUEUE_PORT


class WorkerSettings:
    functions: list = []
    cron_jobs = [
        cron(purge_expired_tokens, minute=0, run_at_startup=True),
        # Deliberately no `run_at_startup` here, unlike the purge above: that one is
        # idempotent housekeeping, this one sends email, and a worker restart must never
        # blast a round of reminders out. Once a day is plenty - `should_notify` means
        # most runs send nothing at all.
        cron(send_gear_service_digests, hour=settings.GEAR_SERVICE_DIGEST_HOUR, minute=0),
    ]
    redis_settings = RedisSettings(host=REDIS_QUEUE_HOST, port=REDIS_QUEUE_PORT, password=settings.REDIS_PASSWORD)
    # `arq --check` reads a sentinel key that the worker rewrites every
    # `health_check_interval` seconds with a TTL of interval + 1, so the interval is also
    # how stale a passing check may be. Arq's default of an hour would let a dead worker
    # look alive until the next morning; 15s makes `docker-compose.yml`'s healthcheck for
    # the `worker` service mean "running now" without costing more than one SETEX a
    # quarter-minute.
    health_check_interval = 15
    on_startup = startup
    on_shutdown = shutdown
    handle_signals = False
