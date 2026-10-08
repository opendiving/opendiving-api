"""A logbook dive that states no duration or no average depth takes its profile's time in the
water, as a reader with no stated figure does - the span only where no sample was in the water.
"""

import json
import uuid as uuid_pkg
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.models.dive import Dive
from src.app.schemas.logbook_import import ImportNoteCode
from src.app.services.logbook_import import plan_import, write_import
from tests.conftest import db_available
from tests.helpers.generators import create_user
from tests.helpers.import_parts import load_one

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

# Down a minute in, eighteen metres for 28 minutes, five more coming up, and the
# surface for five: in the water from the minute to the surfacing, 1 980 s of the 2 340.
PROFILE = {
    "depth": {
        "times": [0, 60_000, 1_740_000, 2_040_000, 2_340_000],
        "values": [0, 1800, 1800, 0, 0],
    }
}
IN_WATER = 1680 + 300
MEAN = round((1680 * 18.0 + 300 * 9.0) / IN_WATER, 2)


async def _imported(db: AsyncSession, user_id: int, profile: dict[str, Any] = PROFILE, **dive: Any) -> tuple[Any, Dive]:
    body = {
        "format": "divejson",
        "version": "1.0",
        "exported_at": "2026-09-09T10:00:00+00:00",
        "dives": [
            {
                "uuid": str(uuid_pkg.uuid4()),
                "number": 1,
                "started_at": "2026-08-01T10:00:00+02:00",
                "recordings": [{"profile": profile}],
                **dive,
            }
        ],
    }
    with await load_one(json.dumps(body).encode(), "logbook.divejson") as loaded:
        plan = await plan_import(db, user_id=user_id, loaded=loaded, resolution_ran=True)
        await write_import(db, user_id=user_id, loaded=loaded, plan=plan)
        await db.commit()
    stored = (await db.execute(select(Dive).where(Dive.user_id == user_id))).scalar_one()
    return plan, stored


def _derived(plan: Any) -> list[str]:
    return [note.message for note in plan.notes if note.code is ImportNoteCode.VALUE_DERIVED]


class TestADiveWithNoStatedFigures:
    @pytest.mark.asyncio
    async def test_its_duration_and_average_are_its_time_in_the_water(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        user = create_user(db)

        plan, dive = await _imported(async_db, user.id)

        assert (dive.duration, dive.avg_depth) == (IN_WATER, MEAN)
        assert [message for message in _derived(plan) if "1.2 m" in message] == [
            "This dive records no duration, so its length was taken as the time its own profile spends deeper "
            "than 1.2 m.",
            "This dive records no average depth, so it was taken as the mean depth of its own profile over the "
            "time it spends deeper than 1.2 m.",
        ]

    @pytest.mark.asyncio
    async def test_a_stated_figure_is_kept_beside_a_derived_one(self, db: Session, async_db: AsyncSession) -> None:
        user = create_user(db)

        plan, dive = await _imported(async_db, user.id, duration=2100, avg_depth=12.5)

        assert (dive.duration, dive.avg_depth) == (2100, 12.5)
        assert not [message for message in _derived(plan) if "1.2 m" in message]

    @pytest.mark.asyncio
    async def test_a_profile_never_in_the_water_falls_back_to_its_span(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """Nothing deeper than the threshold that another sample follows: the span is the only
        number the document offers, and no average is made up."""
        user = create_user(db)

        plan, dive = await _imported(
            async_db, user.id, profile={"depth": {"times": [0, 600_000, 1_200_000], "values": [100, 120, 0]}}
        )

        assert (dive.duration, dive.avg_depth) == (1200, None)
        assert _derived(plan) == [
            "This dive records no duration, so its length was taken from the span of its own profile."
        ]
