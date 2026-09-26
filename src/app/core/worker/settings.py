from arq.connections import RedisSettings
from arq.cron import cron

from ...core.config import settings
from .functions import (
    purge_deleted_accounts,
    purge_expired_auth_audit_events,
    purge_expired_authentication_requests,
    purge_expired_checkin_links,
    purge_expired_invitations,
    purge_expired_invite_requests,
    purge_expired_tokens,
    purge_expired_user_sessions,
    send_gear_service_digests,
    shutdown,
    startup,
)

REDIS_QUEUE_HOST = settings.REDIS_QUEUE_HOST
REDIS_QUEUE_PORT = settings.REDIS_QUEUE_PORT


class WorkerSettings:
    functions: list = []
    cron_jobs = [
        cron(purge_expired_tokens, minute=0, run_at_startup=True),
        # Same shape as the sweep above, and for the same reason: both are idempotent
        # housekeeping that deletes only rows already past their own expiry, so a
        # restart loop costs nothing but a no-op DELETE. Two tiny statements against
        # different tables don't contend, so they share the hour mark.
        cron(purge_expired_authentication_requests, minute=0, run_at_startup=True),
        # Both new sweeps join the same hour mark on the same criterion the comment above
        # states: idempotent housekeeping that deletes only rows already past their own
        # expiry (or, for a session, already revoked), so a restart loop costs a no-op
        # `DELETE`. Neither destroys anything a diver could ask for back - a dead session
        # cannot authenticate and an expired audit row has aged out of its retention - which
        # is what keeps them on this side of the line `purge_deleted_accounts` sits on.
        cron(purge_expired_user_sessions, minute=0, run_at_startup=True),
        cron(purge_expired_auth_audit_events, minute=0, run_at_startup=True),
        # A dead check-in link opens nothing either, and joins them on the same criterion.
        cron(purge_expired_checkin_links, minute=0, run_at_startup=True),
        # The two invitation sweeps join the hour mark on the same criterion, with one
        # difference worth naming: these delete rows that are *not* past an expiry of their
        # own, because neither table has one - a live invitation admits its address until
        # this takes it. What keeps them on this side of the line `purge_deleted_accounts`
        # sits on is what they destroy: an allow-list entry nobody used in three months and
        # a request nobody acted on, neither of which is a thing a diver could ask for back.
        # A restart loop still costs a no-op `DELETE`, since the cutoff is absolute.
        cron(purge_expired_invitations, minute=0, run_at_startup=True),
        cron(purge_expired_invite_requests, minute=0, run_at_startup=True),
        # Hourly, so `ACCOUNT_DELETION_GRACE_DAYS=0` behaves the way an operator setting
        # it to zero expects, and at :30 so it doesn't contend with the two sweeps on the
        # hour mark. No `run_at_startup`, and that is the difference that matters: those
        # two delete rows already past their own expiry, this one destroys logbooks, and a
        # restart loop must never be what decides an account's fate a few minutes early.
        cron(purge_deleted_accounts, minute=30),
        # Deliberately no `run_at_startup` here, unlike the two purges above: those are
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
