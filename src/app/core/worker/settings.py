from arq.connections import RedisSettings
from arq.cron import cron

from ...core.config import settings
from .functions import purge_expired_tokens, shutdown, startup

REDIS_QUEUE_HOST = settings.REDIS_QUEUE_HOST
REDIS_QUEUE_PORT = settings.REDIS_QUEUE_PORT


class WorkerSettings:
    functions: list = []
    cron_jobs = [cron(purge_expired_tokens, minute=0, run_at_startup=True)]
    redis_settings = RedisSettings(host=REDIS_QUEUE_HOST, port=REDIS_QUEUE_PORT)
    on_startup = startup
    on_shutdown = shutdown
    handle_signals = False
