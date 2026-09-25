"""What a merge does to the rows: which recordings survive, where the files end up, and what
the surviving dive comes out claiming.

`test_dive_merge.py` pins the arithmetic, which is pure. This pins the writes, and every one
of them needs a real database to be true at all: the ordinal's unique index as recordings
move between dives, the `ON DELETE CASCADE` that takes a folded recording's profile with it,
the `(dive_id, x_id)` uniqueness that decides which links can move, and the soft delete on
the dive that goes.

Same skip-if-unreachable guard and same write-real-rows-and-leave-them convention as
`test_dive_recordings_rows.py`; see the note there.
"""

import hashlib
import io
import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.api.v1 import dives as dives_module
from src.app.api.v1.dives import _cached_read_dive, merge_two_dives
from src.app.core.exceptions.http_exceptions import NotFoundException, UnprocessableEntityException
from src.app.core.security import create_dive_file_token
from src.app.models.dive import Dive
from src.app.models.dive_dive_site import DiveDiveSite
from src.app.models.dive_file import DiveFile
from src.app.models.dive_gear_item import DiveGearItem
from src.app.models.dive_mixture import DiveMixture
from src.app.models.dive_profile import DiveProfile
from src.app.models.dive_recording import DiveRecording
from src.app.models.dive_species import DiveSpecies
from src.app.models.user import User
from src.app.schemas.dive import DiveMergeRequest
from src.app.services import blob_store
from src.app.services.dive_files import store_recording_file
from src.app.services.dive_parsers.suunto_xml import SuuntoXmlParser
from src.app.services.dive_profiles import MERGE_PARSER_KEY
from tests.conftest import db_available
from tests.helpers.generators import (
    create_dive,
    create_dive_site,
    create_gear_item,
    create_species,
    create_user,
)

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

SUUNTO_NS = "http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal"

# The two halves of the interrupted Perdix dive, as this repository can actually express
# them: `opendiving-api` has no UDDF reader, so the pair is built from the numbers rather
# than from the file. The Suunto XML parser is the cheapest of the three and the only one
# whose bytes can be written inline.
FIRST_START = "2026-09-08T15:17:38+03:00"
SECOND_START = "2026-09-08T15:21:21+03:00"  # 223 s later
RESTART_DELTA = 223
FIRST_PART_SAMPLES = 19
SECOND_PART_SAMPLES = 295


def _samples(count: int, *, step: int = 10, depth_cm: int = 500, pressure_mbar: int | None = None) -> str:
    pressure = "" if pressure_mbar is None else f"<Pressure>{pressure_mbar}</Pressure>"
    inner = "".join(
        f"<Dive.Sample><Time>{index * step}</Time><Depth>{depth_cm / 100}</Depth>{pressure}</Dive.Sample>"
        for index in range(count)
    )
    return f"<DiveSamples>{inner}</DiveSamples>"


def _export(
    *,
    start: str,
    serial: str = "253810000400",
    duration: int = 1800,
    max_depth: str = "19.04",
    samples: str = "",
    cylinder: str = "",
) -> bytes:
    mixtures = "" if not cylinder else f"<DiveMixtures><DiveMixture>{cylinder}</DiveMixture></DiveMixtures>"
    return f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}"><StartTime>{start}</StartTime><Duration>{duration}</Duration>
<MaxDepth>{max_depth}</MaxDepth><SerialNumber>{serial}</SerialNumber>
{mixtures}{samples}</Dive>
""".encode()


@pytest.fixture
def volume(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def diver(db: Session) -> User:
    return create_user(db)


@pytest.fixture
def merging(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drive the real handler with Redis out of the way.

    The route itself is thin - two ownership checks, the service, the recalculations - but
    it ends by dropping the caches and reading the survivor back through a `@cache`d helper,
    neither of which has a Redis to talk to here. `__wrapped__` is the undecorated read, the
    same idiom `test_owned_read_scoping.py` uses, so what comes back is the real response
    body rather than a stub.
    """
    monkeypatch.setattr(dives_module, "invalidate_dive_caches", AsyncMock())
    monkeypatch.setattr(dives_module, "invalidate_gear_caches", AsyncMock())
    monkeypatch.setattr(dives_module, "_cached_read_dive", _cached_read_dive.__wrapped__)  # type: ignore[attr-defined]


def _dive_at(db: Session, diver: User, when: datetime, *, number: int = 1, **columns: Any) -> Dive:
    """A dive at a chosen instant. `create_dive` fixes one, and which of two dives is earlier
    is the whole question here."""
    dive = create_dive(db, diver)
    dive.dive_number = number
    dive.start_time = when
    dive.utc_offset_minutes = 180
    for name, value in columns.items():
        setattr(dive, name, value)
    db.commit()
    return dive


async def _attach(db: AsyncSession, diver: User, dive: Dive, content: bytes, *, filename: str) -> Any:
    return await store_recording_file(
        db,
        user_id=diver.id,
        user_uuid=diver.uuid,
        dive_id=dive.id,
        upload=UploadFile(filename=filename, file=io.BytesIO(content)),
        file_token=create_dive_file_token(
            user_uuid=diver.uuid, sha256=hashlib.sha256(content).hexdigest(), parser_key=SuuntoXmlParser.key
        ),
    )


async def _merge(db: AsyncSession, diver: User, first: Dive, second: Dive) -> Any:
    return await merge_two_dives(
        request=Mock(),
        values=DiveMergeRequest(dive_uuids=[first.uuid, second.uuid]),
        current_user={"id": diver.id, "uuid": diver.uuid, "is_superuser": False},
        db=db,
    )


async def _recordings(db: AsyncSession, dive: Dive) -> list[DiveRecording]:
    rows = await db.execute(
        select(DiveRecording).where(DiveRecording.dive_id == dive.id).order_by(DiveRecording.ordinal)
    )
    return list(rows.scalars().all())


async def _profile(db: AsyncSession, recording_id: int) -> Any:
    """Explicit columns, `data` included. The payload is `deferred` on the model, so reading
    it off an entity lazy-loads - which in an async session is a `MissingGreenlet` rather
    than a query."""
    return (
        await db.execute(
            select(
                DiveProfile.parser_key,
                DiveProfile.duration,
                DiveProfile.depth_sample_count,
                DiveProfile.dive_id,
                DiveProfile.data,
            ).where(DiveProfile.recording_id == recording_id)
        )
    ).one()


async def _row(db: AsyncSession, dive: Dive) -> Any:
    return (
        await db.execute(select(Dive.duration, Dive.max_depth, Dive.notes, Dive.is_deleted).where(Dive.id == dive.id))
    ).one()


async def _two_halves(db: AsyncSession, sync_db: Session, diver: User) -> tuple[Dive, Dive]:
    """One computer's two records of one dive, as two dives - which is how they reach the app.

    The second dive is logged four minutes after the first, which is the diver's own entry;
    the two *recordings* are 223 seconds apart, which is what the fold uses. The two numbers
    are deliberately different, because using the dives' delta is the mistake this feature
    can make silently.
    """
    first = _dive_at(sync_db, diver, datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC), number=214)
    second = _dive_at(sync_db, diver, datetime(2026, 9, 8, 12, 21, 38, tzinfo=UTC), number=215)
    await _attach(
        db,
        diver,
        first,
        _export(start=FIRST_START, duration=180, max_depth="5.0", samples=_samples(FIRST_PART_SAMPLES, depth_cm=500)),
        filename="part-one.xml",
    )
    await _attach(
        db,
        diver,
        second,
        _export(
            start=SECOND_START, duration=2921, max_depth="19.04", samples=_samples(SECOND_PART_SAMPLES, depth_cm=1904)
        ),
        filename="part-two.xml",
    )
    return first, second


class TestOneComputersTwoRecordsFoldIntoOne:
    @pytest.mark.asyncio
    async def test_the_two_recordings_become_one(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        first, second = await _two_halves(async_db, db, diver)

        result = await _merge(async_db, diver, first, second)

        assert result.folded is True
        assert result.removed_dive_uuid == second.uuid
        assert [row.ordinal for row in await _recordings(async_db, first)] == [0]

    @pytest.mark.asyncio
    async def test_the_samples_land_on_one_axis_with_the_gap_intact(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """The whole point, end to end: 19 samples then 295, offset by the **recordings'**
        223-second delta and not by the dives' 240, with the 43 seconds the computer was off
        left empty.
        """
        first, second = await _two_halves(async_db, db, diver)

        await _merge(async_db, diver, first, second)

        recording = (await _recordings(async_db, first))[0]
        profile = await _profile(async_db, recording.id)
        times = profile.data["depth"]["t"]
        assert len(times) == FIRST_PART_SAMPLES + SECOND_PART_SAMPLES == 314
        assert times[FIRST_PART_SAMPLES] == RESTART_DELTA * 1000
        assert times[FIRST_PART_SAMPLES] - times[FIRST_PART_SAMPLES - 1] == 43_000
        assert profile.duration == 3_163_000

    @pytest.mark.asyncio
    async def test_the_folded_samples_are_marked_as_a_merge(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """`merge` is what stops `backfill_profiles` overwriting the fold with one half of
        itself, read back off the very files the merge kept - which is exactly the case that
        made the provenance live on the profile rather than on a file.
        """
        first, second = await _two_halves(async_db, db, diver)

        await _merge(async_db, diver, first, second)

        recording = (await _recordings(async_db, first))[0]
        assert (await _profile(async_db, recording.id)).parser_key == MERGE_PARSER_KEY

    @pytest.mark.asyncio
    async def test_both_halves_files_stay_downloadable_on_the_one_recording(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """Nothing on this server can produce folded samples again, so the files either half
        held are the only evidence left of what the computer wrote."""
        first, second = await _two_halves(async_db, db, diver)

        await _merge(async_db, diver, first, second)

        recording = (await _recordings(async_db, first))[0]
        files = (await async_db.execute(select(DiveFile).where(DiveFile.recording_id == recording.id))).scalars().all()
        assert sorted(row.original_filename for row in files) == ["part-one.xml", "part-two.xml"]
        assert all(row.dive_id == first.id for row in files)
        assert all((volume / row.storage_key).exists() for row in files)

    @pytest.mark.asyncio
    async def test_the_surviving_recordings_own_gate_figures_are_recomputed(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """`duration` and `max_depth` on the recording are the match gates' columns. After a
        fold the recording describes both records, so leaving them at the first half's 180
        and 5.0 would have every later gate comparing an incoming file against half a dive.
        """
        first, second = await _two_halves(async_db, db, diver)

        await _merge(async_db, diver, first, second)

        recording = (await _recordings(async_db, first))[0]
        assert recording.duration == 3163
        assert recording.max_depth == 19.04
        # The axis origin, which is the earlier of the two *records*.
        assert recording.start_time == datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_the_dives_figures_are_re_seeded_from_the_merged_profile(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """223 plus the second record's sampled 2 940. Its *logged* 2 921 would give 3 144,
        and the samples are what the merged profile contains."""
        first, second = await _two_halves(async_db, db, diver)

        await _merge(async_db, diver, first, second)

        row = await _row(async_db, first)
        assert row.duration == 3163
        assert row.max_depth == 19.04

    @pytest.mark.asyncio
    async def test_the_later_dive_is_soft_deleted(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        first, second = await _two_halves(async_db, db, diver)

        await _merge(async_db, diver, first, second)

        assert (await _row(async_db, second)).is_deleted is True
        assert await _recordings(async_db, second) == []

    @pytest.mark.asyncio
    async def test_naming_the_two_dives_the_other_way_round_merges_the_same_way(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """The earlier dive survives, and which one that is is the server's answer rather
        than a property of the request body's order."""
        first, second = await _two_halves(async_db, db, diver)

        result = await _merge(async_db, diver, second, first)

        assert result.dive.uuid == first.uuid
        assert result.removed_dive_uuid == second.uuid


class TestTwoDifferentComputers:
    @pytest.mark.asyncio
    async def test_the_recordings_are_appended_rather_than_folded(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """Subsurface's *join*. Both machines were recording the whole time, so there is
        nothing to fold - their samples describe the same seconds from two wrists."""
        first = _dive_at(db, diver, datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC), number=214)
        second = _dive_at(db, diver, datetime(2026, 9, 8, 12, 18, 10, tzinfo=UTC), number=215)
        await _attach(async_db, diver, first, _export(start=FIRST_START, samples=_samples(5)), filename="suunto.xml")
        await _attach(
            async_db,
            diver,
            second,
            _export(start="2026-09-08T15:18:10+03:00", serial="D9772626", samples=_samples(5)),
            filename="perdix.xml",
        )

        result = await _merge(async_db, diver, first, second)

        assert result.folded is False
        recordings = await _recordings(async_db, first)
        assert [row.ordinal for row in recordings] == [0, 1]
        assert [row.device_serial for row in recordings] == ["253810000400", "D9772626"]

    @pytest.mark.asyncio
    async def test_the_appended_recording_keeps_its_own_samples_and_start(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """A recording's `times` are elapsed from its own start, which moves with it, so
        nothing about its profile changes when it changes dive."""
        first = _dive_at(db, diver, datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC), number=214)
        second = _dive_at(db, diver, datetime(2026, 9, 8, 12, 18, 10, tzinfo=UTC), number=215)
        await _attach(async_db, diver, first, _export(start=FIRST_START, samples=_samples(5)), filename="a.xml")
        await _attach(
            async_db,
            diver,
            second,
            _export(start="2026-09-08T15:18:10+03:00", serial="D9772626", samples=_samples(5)),
            filename="b.xml",
        )

        await _merge(async_db, diver, first, second)

        appended = (await _recordings(async_db, first))[1]
        assert appended.start_time == datetime(2026, 9, 8, 12, 18, 10, tzinfo=UTC)
        profile = await _profile(async_db, appended.id)
        assert profile.data["depth"]["t"] == [0, 10_000, 20_000, 30_000, 40_000]
        assert profile.dive_id == first.id


class TestWhatTheMergeRefuses:
    @pytest.mark.asyncio
    async def test_a_hand_entered_dive_is_refused_by_number(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """A dive with no recording has nothing this could fold, and the message says which
        of the two it was - the diver is looking at both."""
        recorded = _dive_at(db, diver, datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC), number=214)
        by_hand = _dive_at(db, diver, datetime(2026, 9, 8, 12, 21, 38, tzinfo=UTC), number=215)
        await _attach(async_db, diver, recorded, _export(start=FIRST_START), filename="a.xml")

        with pytest.raises(UnprocessableEntityException, match="Dive 215"):
            await _merge(async_db, diver, recorded, by_hand)

    @pytest.mark.asyncio
    async def test_neither_dive_is_touched_by_a_refusal(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        recorded = _dive_at(db, diver, datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC), number=214)
        by_hand = _dive_at(db, diver, datetime(2026, 9, 8, 12, 21, 38, tzinfo=UTC), number=215)
        await _attach(async_db, diver, recorded, _export(start=FIRST_START), filename="a.xml")

        with pytest.raises(UnprocessableEntityException):
            await _merge(async_db, diver, recorded, by_hand)

        assert (await _row(async_db, by_hand)).is_deleted is False
        assert len(await _recordings(async_db, recorded)) == 1

    @pytest.mark.asyncio
    async def test_another_divers_dive_is_a_404(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """Indistinguishable from a dive that does not exist, so someone else's uuid stays
        unprobeable - and it is refused before anything is read off either dive."""
        stranger = create_user(db)
        mine = _dive_at(db, diver, datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC), number=214)
        theirs = _dive_at(db, stranger, datetime(2026, 9, 8, 12, 21, 38, tzinfo=UTC), number=1)
        await _attach(async_db, diver, mine, _export(start=FIRST_START), filename="a.xml")

        with pytest.raises(NotFoundException, match="Dive not found"):
            await _merge(async_db, diver, mine, theirs)

    @pytest.mark.asyncio
    async def test_a_deeper_average_than_the_recordings_reached_is_refused(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """`ck_dive_avg_depth_within_max` would otherwise refuse the write from inside the
        transaction, where an `IntegrityError` is a 500 rather than anything a diver can act
        on. The number to change is the average, which is theirs and on a form they can
        reach.
        """
        first, second = await _two_halves(async_db, db, diver)
        first.avg_depth = 30.0
        first.max_depth = 45.0
        db.commit()

        with pytest.raises(UnprocessableEntityException, match="average depth"):
            await _merge(async_db, diver, first, second)

    def test_merging_a_dive_with_itself_is_refused_by_the_schema(self) -> None:
        """Before the route is entered at all: one uuid twice would soft-delete the dive it
        had just merged into."""
        one = uuid_pkg.uuid4()

        with pytest.raises(ValueError, match="cannot be merged with itself"):
            DiveMergeRequest(dive_uuids=[one, one])


class TestWhatElseMoves:
    @pytest.mark.asyncio
    async def test_sites_gear_and_species_arrive_without_duplicating_what_is_there(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """Each of the three carries a `(dive_id, x_id)` uniqueness constraint, and two halves
        of one dive name the same site and the same wing by construction - so the shared rows
        stay where they are and go down with the dive, and only what is new moves.
        """
        first, second = await _two_halves(async_db, db, diver)
        shared_site, other_site = create_dive_site(db, diver), create_dive_site(db, diver)
        shared_wing = create_gear_item(db, diver)
        fish = create_species(db)
        db.add_all(
            [
                DiveDiveSite(dive_id=first.id, dive_site_id=shared_site.id, position=0),
                DiveDiveSite(dive_id=second.id, dive_site_id=shared_site.id, position=0),
                DiveDiveSite(dive_id=second.id, dive_site_id=other_site.id, position=1),
                DiveGearItem(dive_id=first.id, gear_item_id=shared_wing.id, position=0),
                DiveGearItem(dive_id=second.id, gear_item_id=shared_wing.id, position=0),
                DiveSpecies(dive_id=second.id, species_id=fish.id, position=0),
            ]
        )
        db.commit()

        await _merge(async_db, diver, first, second)

        sites = (
            (
                await async_db.execute(
                    select(DiveDiveSite.dive_site_id)
                    .where(DiveDiveSite.dive_id == first.id)
                    .order_by(DiveDiveSite.position)
                )
            )
            .scalars()
            .all()
        )
        assert list(sites) == [shared_site.id, other_site.id]
        gear = (
            (await async_db.execute(select(DiveGearItem.gear_item_id).where(DiveGearItem.dive_id == first.id)))
            .scalars()
            .all()
        )
        assert list(gear) == [shared_wing.id]
        species = (
            (await async_db.execute(select(DiveSpecies.species_id).where(DiveSpecies.dive_id == first.id)))
            .scalars()
            .all()
        )
        assert list(species) == [fish.id]

    @pytest.mark.asyncio
    async def test_the_other_dives_notes_arrive_under_a_heading(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        first, second = await _two_halves(async_db, db, diver)
        first.notes = "Dropped on the mooring."
        second.notes = "Computer died, restarted on the reef."
        db.commit()

        await _merge(async_db, diver, first, second)

        notes = (await _row(async_db, first)).notes
        assert notes.startswith("Dropped on the mooring.")
        assert "Notes from dive 215, merged into this one:" in notes
        assert notes.endswith("Computer died, restarted on the reef.")

    @pytest.mark.asyncio
    async def test_a_cylinder_the_other_dive_had_and_this_one_did_not_is_appended(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """`gas_number` is dive-scoped, so a cylinder arriving from the other dive takes the
        next free label rather than its own - and one the two dives agree on by mix is the
        same tank and is not duplicated.
        """
        first, second = await _two_halves(async_db, db, diver)
        db.add_all(
            [
                DiveMixture(dive_id=first.id, gas_number=0, oxygen=21.0, helium=0.0, start_pressure=200.0),
                DiveMixture(dive_id=second.id, gas_number=0, oxygen=21.0, helium=0.0, start_pressure=120.0),
                DiveMixture(dive_id=second.id, gas_number=1, oxygen=50.0, helium=0.0, start_pressure=200.0),
            ]
        )
        db.commit()

        await _merge(async_db, diver, first, second)

        rows = (
            (
                await async_db.execute(
                    select(DiveMixture).where(DiveMixture.dive_id == first.id).order_by(DiveMixture.id)
                )
            )
            .scalars()
            .all()
        )
        assert [(row.oxygen, row.gas_number) for row in rows] == [(21.0, 0), (50.0, 1)]

    @pytest.mark.asyncio
    async def test_a_second_computers_pressure_channels_are_relabelled_as_they_move(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """The appended branch, where the labelling genuinely differs: this dive calls its
        back gas 7 and the arriving computer calls its own tank 0, so the channel has to be
        renumbered or the chart reads that tank's pressure off nothing.
        """
        first = _dive_at(db, diver, datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC), number=214)
        second = _dive_at(db, diver, datetime(2026, 9, 8, 12, 18, 10, tzinfo=UTC), number=215)
        db.add_all(
            [
                DiveMixture(dive_id=first.id, gas_number=7, oxygen=21.0, helium=0.0),
                # The second computer's own numbering, which is what its stored profile's
                # pressure channel is labelled under: this format counts its cylinders
                # from 1.
                DiveMixture(dive_id=second.id, gas_number=1, oxygen=21.0, helium=0.0),
            ]
        )
        db.commit()
        await _attach(async_db, diver, first, _export(start=FIRST_START, samples=_samples(5)), filename="a.xml")
        await _attach(
            async_db,
            diver,
            second,
            _export(
                start="2026-09-08T15:18:10+03:00",
                serial="D9772626",
                samples=_samples(5, pressure_mbar=200000),
                cylinder="<TransmitterId>2b</TransmitterId><StartPressure>200000</StartPressure>",
            ),
            filename="b.xml",
        )
        before = await _profile(async_db, (await _recordings(async_db, second))[0].id)
        assert [series["gas_number"] for series in before.data["pressure"]] == [1]

        await _merge(async_db, diver, first, second)

        appended = (await _recordings(async_db, first))[1]
        profile = await _profile(async_db, appended.id)
        assert [series["gas_number"] for series in profile.data["pressure"]] == [7]


class TestTheSurvivingDiveReadsBackWhole:
    @pytest.mark.asyncio
    async def test_the_response_is_the_merged_dive(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """The route answers with the survivor read back rather than with the row it wrote,
        so a client renders the merged dive without a second round trip."""
        first, second = await _two_halves(async_db, db, diver)

        result = await _merge(async_db, diver, first, second)

        assert result.dive.uuid == first.uuid
        assert result.dive.duration == 3163
        assert len(result.dive.recordings) == 1
        assert result.dive.recordings[0].profile is not None
        assert result.dive.recordings[0].profile.duration == 3_163_000
        assert result.dive.recordings[0].profile.depth_sample_count == 314
        assert [file.original_filename for file in result.dive.recordings[0].files] == [
            "part-one.xml",
            "part-two.xml",
        ]


class TestWhenTheSurvivingDivesOwnRecordStartedLater:
    @pytest.mark.asyncio
    async def test_the_axis_is_the_earlier_record_whichever_dive_holds_it(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """A dive's start is the diver's logbook entry and a recording's is the device's own
        stamp, so the two can disagree about which half came first. Offsetting by the dives'
        delta - or assuming the surviving dive holds the earlier record - would put the
        folded samples at negative seconds.
        """
        earlier_logged = _dive_at(db, diver, datetime(2026, 9, 8, 12, 17, 0, tzinfo=UTC), number=214)
        later_logged = _dive_at(db, diver, datetime(2026, 9, 8, 12, 30, 0, tzinfo=UTC), number=215)
        # The surviving dive's own record is the *later* of the two records.
        await _attach(
            async_db, diver, earlier_logged, _export(start=SECOND_START, samples=_samples(3)), filename="late.xml"
        )
        await _attach(
            async_db, diver, later_logged, _export(start=FIRST_START, samples=_samples(3)), filename="early.xml"
        )

        await _merge(async_db, diver, earlier_logged, later_logged)

        recording = (await _recordings(async_db, earlier_logged))[0]
        assert recording.start_time == datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC)
        times = (await _profile(async_db, recording.id)).data["depth"]["t"]
        assert min(times) == 0
        assert max(times) == (RESTART_DELTA + 20) * 1000


class TestASameDeviceParedWithNoStartToPlaceIt:
    @pytest.mark.asyncio
    async def test_it_is_appended_rather_than_refused(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """A header-only export with no timestamp gets a recording with a NULL start, and two
        of those read as the same device by the absent rule. There is no delta to fold them
        by, so they go side by side: nothing is lost, both records keep their samples, and
        the sites, gear and notes still merge - where refusing would block all of that over a
        clock reading nobody can supply.
        """
        first = _dive_at(db, diver, datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC), number=214)
        second = _dive_at(db, diver, datetime(2026, 9, 8, 12, 21, 38, tzinfo=UTC), number=215)
        await _attach(async_db, diver, first, _export(start=FIRST_START), filename="a.xml")
        await _attach(async_db, diver, second, _export(start=SECOND_START, max_depth="19.05"), filename="b.xml")
        # Both records lose their start, which is what a timestamp-less export produces.
        for dive in (first, second):
            recording = (await _recordings(async_db, dive))[0]
            recording_row = await async_db.get(DiveRecording, recording.id)
            assert recording_row is not None
            recording_row.start_time = None
            recording_row.utc_offset_minutes = None
        await async_db.commit()

        result = await _merge(async_db, diver, first, second)

        assert result.folded is False
        assert [row.ordinal for row in await _recordings(async_db, first)] == [0, 1]


class TestTheDivesOwnStartIsLeftAlone:
    @pytest.mark.asyncio
    async def test_the_surviving_dive_keeps_the_start_the_diver_logged(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """What stays on the dive is the diver's logbook entry. The recordings carry the
        devices' own stamps, and the merge moves those - never this."""
        first, second = await _two_halves(async_db, db, diver)
        logged = (await async_db.execute(select(Dive.start_time).where(Dive.id == first.id))).scalar_one()

        await _merge(async_db, diver, first, second)

        assert (await async_db.execute(select(Dive.start_time).where(Dive.id == first.id))).scalar_one() == logged


class TestADiveWithNothingToReSeedFrom:
    @pytest.mark.asyncio
    async def test_a_merge_of_two_sample_less_records_leaves_the_diver_figures_alone(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """`ck_dive_duration_positive` is one reason and the diver is the other: a merge that
        found no samples has nothing to say about how long the dive was, and writing a zero
        would be the row refusing the write rather than the merge admitting it learned
        nothing.
        """
        first = _dive_at(db, diver, datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC), number=214, duration=2400)
        second = _dive_at(db, diver, datetime(2026, 9, 8, 12, 21, 38, tzinfo=UTC), number=215)
        await _attach(async_db, diver, first, _export(start=FIRST_START), filename="a.xml")
        await _attach(async_db, diver, second, _export(start=SECOND_START), filename="b.xml")

        await _merge(async_db, diver, first, second)

        row = await _row(async_db, first)
        assert row.duration == 2400
        recording = (await _recordings(async_db, first))[0]
        assert await async_db.scalar(select(DiveProfile.id).where(DiveProfile.recording_id == recording.id)) is None


class TestTheStatsAndCachesFollow:
    @pytest.mark.asyncio
    async def test_the_dive_caches_are_dropped_for_the_owner(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User, monkeypatch: Any
    ) -> None:
        """One dive fewer and one dive rewritten, so both the list pages and the two dives'
        own entries are stale - and the gear items' `dive_count` with them."""
        dropped = AsyncMock()
        gear_dropped = AsyncMock()
        monkeypatch.setattr(dives_module, "invalidate_dive_caches", dropped)
        monkeypatch.setattr(dives_module, "invalidate_gear_caches", gear_dropped)
        first, second = await _two_halves(async_db, db, diver)

        await _merge(async_db, diver, first, second)

        dropped.assert_awaited_once_with(diver.id)
        gear_dropped.assert_awaited_once_with(diver.id)


class TestTheTimeSpanWhenTheGapIsLonger:
    @pytest.mark.asyncio
    async def test_a_long_surface_interval_between_the_records_still_holds(
        self, volume: Any, merging: None, async_db: AsyncSession, db: Session, diver: User
    ) -> None:
        """Not a dive a diver would merge, but the arithmetic must not depend on the gap
        being small: the offset is a delta in seconds and nothing here bounds it."""
        first = _dive_at(db, diver, datetime(2026, 9, 8, 12, 17, 38, tzinfo=UTC), number=214)
        second = _dive_at(db, diver, datetime(2026, 9, 8, 12, 40, 0, tzinfo=UTC), number=215)
        await _attach(async_db, diver, first, _export(start=FIRST_START, samples=_samples(3)), filename="a.xml")
        later = (datetime(2026, 9, 8, 15, 17, 38) + timedelta(minutes=30)).isoformat() + "+03:00"
        await _attach(async_db, diver, second, _export(start=later, samples=_samples(3)), filename="b.xml")

        await _merge(async_db, diver, first, second)

        recording = (await _recordings(async_db, first))[0]
        times = (await _profile(async_db, recording.id)).data["depth"]["t"]
        assert times[3] == 30 * 60 * 1000
        assert (await _row(async_db, first)).duration == 30 * 60 + 20
