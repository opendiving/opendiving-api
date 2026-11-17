"""Writing and reading `daily_external_request`.

A move adds what it took to the stored row, in one upsert on `(day, provider, kind, outcome)`:
the counts and the total durations add up, and the slowest is the greater of the two.
"""

from collections.abc import Sequence
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.utils.external_requests import Buffered
from ..models.daily_external_request import DailyExternalRequest

_IDENTITY = [
    DailyExternalRequest.day,
    DailyExternalRequest.provider,
    DailyExternalRequest.kind,
    DailyExternalRequest.outcome,
]


async def add_external_requests(db: AsyncSession, rows: Sequence[Buffered]) -> None:
    """Add `rows` to the stored counts. Commits nothing; writes nothing for no rows."""
    if not rows:
        return
    statement = insert(DailyExternalRequest).values(
        [
            {
                "day": row.day,
                "provider": row.provider,
                "kind": row.kind,
                "outcome": row.outcome,
                "count": row.count,
                "duration_ms_total": row.duration_ms_total,
                "duration_ms_max": row.duration_ms_max,
            }
            for row in rows
        ]
    )
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=_IDENTITY,
            set_={
                "count": DailyExternalRequest.count + statement.excluded.count,
                "duration_ms_total": DailyExternalRequest.duration_ms_total + statement.excluded.duration_ms_total,
                "duration_ms_max": func.greatest(
                    DailyExternalRequest.duration_ms_max, statement.excluded.duration_ms_max
                ),
            },
        )
    )


async def external_requests_between(db: AsyncSession, *, first: date, last: date) -> Sequence[DailyExternalRequest]:
    """Every stored row from `first` to `last` inclusive, in no particular order."""
    result = await db.execute(
        select(DailyExternalRequest).where(DailyExternalRequest.day >= first, DailyExternalRequest.day <= last)
    )
    return result.scalars().all()
