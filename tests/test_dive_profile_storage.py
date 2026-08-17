"""Integration tests for the `dive_profile` summary columns against a live Postgres.

The rest of the profile suite is pure and DB-free (`test_dive_profiles.py`), and
`get_gas_attribution_for_dives` is covered there against a mocked session - which pins
every shape the *reader* has to survive but cannot check the one thing a mock never
touches: that what `store_profile` writes into a JSONB column is what comes back out of
it. `gas_attribution` is the first summary column whose stored shape can drift, since it
is a list of objects rather than an integer, so the round trip is worth a test of its own.

Same skip-if-unreachable guard and same write-real-rows-and-leave-them convention as
`test_dive_check_constraints.py`; see the note there.
"""

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio.session import AsyncSession
from sqlalchemy.orm import Session

from src.app.models.dive import Dive
from src.app.models.dive_profile import DiveProfile
from src.app.models.user import User
from src.app.schemas.dive_profile import GasAttribution
from src.app.services.dive_profiles import (
    NormalizedProfile,
    ProfileSeries,
    get_gas_attribution_for_dives,
    store_profile,
)
from tests.conftest import db_available
from tests.helpers.generators import create_user

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")


@pytest.fixture
def dive(db: Session) -> Dive:
    """A dive row to hang a profile off, with an owner of its own so nothing this test
    leaves behind can collide with a later run's `dive_number`."""
    owner: User = create_user(db)
    row = Dive(user_id=owner.id, dive_number=1, start_time=datetime.now(UTC), duration=1800, notes="")
    db.add(row)
    db.commit()
    return row


def _profile(*attribution: GasAttribution) -> NormalizedProfile:
    return NormalizedProfile(
        depth=ProfileSeries(t=[0, 100], v=[3000, 600]),
        gas_attribution=list(attribution),
    )


class TestGasAttributionRoundTrip:
    @pytest.mark.asyncio
    async def test_what_is_written_is_what_comes_back(self, async_db: AsyncSession, dive: Dive) -> None:
        """The whole point of the column: `store_profile` serializes with `model_dump()`
        and `get_gas_attribution_for_dives` revives with `model_validate`, and the two have
        to agree through Postgres's own JSONB encoding - including that a `gas_number` of
        **0** survives as 0 rather than as anything falsy-adjacent, which is the number a
        Suunto Ocean's back gas really carries.
        """
        entries = [
            GasAttribution(gas_number=0, seconds=2075, mean_depth_cm=3399),
            GasAttribution(gas_number=1, seconds=2225, mean_depth_cm=579),
        ]

        await store_profile(
            async_db,
            dive_id=dive.id,
            profile=_profile(*entries),
            source_sha256="a" * 64,
            parser_key="suunto_json",
            commit=True,
        )
        attribution = await get_gas_attribution_for_dives(async_db, dive_ids=[dive.id])

        assert attribution[dive.id].entries == entries
        # The span the fraction is taken against comes off the same row, so it has to
        # survive the same trip: the profile's own duration, not the dive's 1800.
        assert attribution[dive.id].duration_seconds == 100

    @pytest.mark.asyncio
    async def test_a_profile_with_nothing_to_attribute_stores_an_empty_list_not_null(
        self, db: Session, async_db: AsyncSession, dive: Dive
    ) -> None:
        """`[]` and NULL mean different things in this column - "this extractor looked and
        found nothing" against "no extractor has looked yet", the second being what a
        backfill is still owed. Asserted against the raw column rather than through the
        reader, which deliberately flattens both to an empty result.
        """
        await store_profile(
            async_db,
            dive_id=dive.id,
            profile=_profile(),
            source_sha256="b" * 64,
            parser_key="suunto_xml",
            commit=True,
        )

        db.rollback()  # end the fixture's transaction, so this read sees the write above
        stored = db.execute(select(DiveProfile.gas_attribution).where(DiveProfile.dive_id == dive.id)).scalar_one()

        assert stored == []
