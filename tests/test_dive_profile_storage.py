"""What `dive_profile` needs a live Postgres to say: the summary round trip, and the query
that selects a profile for re-extraction.

The rest of the profile suite is pure and DB-free (`test_dive_profiles.py`), and
`get_gas_attribution_for_dives` is covered there against a mocked session - which pins
every shape the *reader* has to survive but cannot check the one thing a mock never
touches: that what `store_profile` writes into a JSONB column is what comes back out of
it. `gas_attribution` is the first summary column whose stored shape can drift, since it
is a list of objects rather than an integer, so the round trip is worth a test of its own.

`backfill_profiles`' candidate selection is here for the neighbouring reason: the criterion
lives in a `WHERE` clause, so a mocked session could only assert the SQL that was written
rather than the rows it comes back with - which is exactly the difference that let the
digest term go missing.

Same skip-if-unreachable guard and same write-real-rows-and-leave-them convention as
`test_dive_check_constraints.py`; see the note there.
"""

import hashlib
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio.session import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.models.dive import Dive
from src.app.models.dive_file import DiveFile
from src.app.models.dive_profile import DiveProfile
from src.app.models.dive_recording import DiveRecording
from src.app.models.user import User
from src.app.schemas.dive_profile import GasAttribution
from src.app.services.dive_profiles import (
    NormalizedProfile,
    ProfileSeries,
    backfill_profiles,
    get_gas_attribution_for_dives,
    store_profile,
)
from tests.conftest import db_available
from tests.helpers.generators import create_user

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")


@pytest.fixture
def dive(db: Session) -> Dive:
    """A dive row to hang a recording off, with an owner of its own so nothing this test
    leaves behind can collide with a later run's `dive_number`."""
    owner: User = create_user(db)
    row = Dive(user_id=owner.id, dive_number=1, start_time=datetime.now(UTC), duration=1800, notes="")
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def recording(db: Session, dive: Dive) -> DiveRecording:
    """The row a profile actually hangs off. `dive_profile.recording_id` is `NOT NULL`, so a
    profile cannot be stored against a dive that has no record of being recorded."""
    row = DiveRecording(dive_id=dive.id, user_id=dive.user_id, ordinal=0, start_time=dive.start_time)
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
    async def test_what_is_written_is_what_comes_back(
        self, async_db: AsyncSession, dive: Dive, recording: DiveRecording
    ) -> None:
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
            recording_id=recording.id,
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
        assert attribution[dive.id].duration == 100

    @pytest.mark.asyncio
    async def test_a_profile_with_nothing_to_attribute_stores_an_empty_list_not_null(
        self, db: Session, async_db: AsyncSession, dive: Dive, recording: DiveRecording
    ) -> None:
        """`[]` and NULL mean different things in this column - "this extractor looked and
        found nothing" against "no extractor has looked yet", the second being what a
        backfill is still owed. Asserted against the raw column rather than through the
        reader, which deliberately flattens both to an empty result.
        """
        await store_profile(
            async_db,
            recording_id=recording.id,
            dive_id=dive.id,
            profile=_profile(),
            source_sha256="b" * 64,
            parser_key="suunto_xml",
            commit=True,
        )

        db.rollback()  # end the fixture's transaction, so this read sees the write above
        stored = db.execute(select(DiveProfile.gas_attribution).where(DiveProfile.dive_id == dive.id)).scalar_one()

        assert stored == []


class TestTheBackfillSeesADigestThatDrifted:
    """A recording whose stored profile came out of different bytes is a candidate again.

    The criterion the old query spelled `DiveProfile.source_sha256 != DiveFile.sha256`, and
    the one both `backfill_profiles`' docstring and the script's `--help` promise. Against a
    real database because it is a *query* under test: the term lives in the `WHERE`, and a
    mocked session would assert the SQL rather than what it selects.

    Measured as a **delta over one run either way** rather than as an absolute count. The
    suite's database is shared and other modules leave recordings behind, so the only number
    attributable to this test is the change its own edit produces. `failed` is the counter to
    watch: this recording's file has a `storage_key` naming bytes that were never written, so
    being selected costs exactly one `BlobMissingError` - which proves selection without
    needing a blob, a parser or a profile that extracts.
    """

    @pytest.mark.asyncio
    async def test_a_current_version_profile_from_other_bytes_becomes_a_candidate(
        self, async_db: AsyncSession, db: Session, dive: Dive, recording: DiveRecording
    ) -> None:
        stored = b"<Dive/>"
        digest = hashlib.sha256(stored).hexdigest()
        db.add(
            DiveFile(
                user_id=dive.user_id,
                recording_id=recording.id,
                dive_id=dive.id,
                sha256=digest,
                content_type="application/xml",
                byte_size=len(stored),
                original_filename="export.xml",
                parser_key="suunto_xml",
                storage_key=f"dive-files/ab/{uuid7()}_{digest}",
            )
        )
        db.commit()
        # Current extractor version and a digest that names the file the recording holds:
        # nothing to do, and the query must not select it.
        await store_profile(
            async_db,
            recording_id=recording.id,
            dive_id=dive.id,
            profile=_profile(),
            source_sha256=digest,
            parser_key="suunto_xml",
            commit=True,
        )
        agreeing = (await backfill_profiles(async_db, dry_run=True)).failed

        # The same row, its profile now claiming bytes the recording does not hold.
        await async_db.execute(
            update(DiveProfile).where(DiveProfile.recording_id == recording.id).values(source_sha256="f" * 64)
        )
        await async_db.commit()
        drifted = (await backfill_profiles(async_db, dry_run=True)).failed

        assert drifted == agreeing + 1, (
            "a recording whose stored profile came out of different bytes has to be a candidate "
            "without --force; the digest term is what selects it"
        )
