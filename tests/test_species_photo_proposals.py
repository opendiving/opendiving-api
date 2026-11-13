"""Species photo proposals through the routes, as a proposer, a second account and the operator.

Wikimedia is faked: the pin's fetch and the review's metadata are patched where the routes reach
them, and every proposing test runs with outbound HTTP refused outright, since proposing asks
Wikimedia nothing.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import base64
import uuid as uuid_pkg
from collections.abc import AsyncGenerator, Generator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.security import API_KEY_SHOWN_LENGTH, generate_api_key
from src.app.core.setup import create_application
from src.app.core.utils import cache as cache_module
from src.app.crud.crud_users import read_account
from src.app.models.api_key import ApiKey
from src.app.models.species import Species
from src.app.models.species_photo_proposal import SpeciesPhotoProposal
from src.app.models.user import User
from src.app.schemas.species import PhotoCuration
from src.app.services import blob_store, species_photos, species_service
from src.app.services.species_photos import PhotoCredit
from src.app.services.species_service import PhotoAttempt, PhotoCandidate
from tests.conftest import db_available
from tests.helpers.fake_redis import FakeRedis
from tests.helpers.generators import create_species, create_user

pytestmark = [
    pytest.mark.skipif(not db_available(), reason="No database connection available"),
    pytest.mark.xdist_group("species_photo_proposals"),
]


@dataclass
class Sign:
    """Who the app reads as signed in, switched between requests."""

    account: dict[str, Any]
    accounts: dict[int, dict[str, Any]]

    def as_(self, user: User) -> None:
        self.account = self.accounts[user.id]


@pytest.fixture(scope="module")
def proposals_app() -> Any:
    return create_application(router=router, settings=settings, apply_migrations_on_start=False)


@pytest.fixture
def operator(db: Session) -> User:
    return create_user(db, is_super_user=True)


@pytest.fixture
def volume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(blob_store.settings, "FILE_STORAGE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def no_wikimedia() -> Generator[None]:
    """Every outbound request fails the test: proposing asks Wikimedia nothing."""

    def refuse(**_: Any) -> httpx.AsyncClient:
        raise AssertionError("proposing went outbound")

    with patch("src.app.core.utils.outbound.httpx.AsyncClient", side_effect=refuse):
        yield


@pytest_asyncio.fixture
async def per_request(
    proposals_app: Any, async_sessions: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[None]:
    monkeypatch.setattr(cache_module, "client", FakeRedis())

    async def session() -> AsyncGenerator[AsyncSession]:
        async with async_sessions() as db:
            yield db

    proposals_app.dependency_overrides[async_get_db] = session
    yield
    proposals_app.dependency_overrides = {}


@pytest_asyncio.fixture
async def signed(
    proposals_app: Any,
    per_request: None,
    async_sessions: async_sessionmaker[AsyncSession],
    diver: User,
    other_diver: User,
    operator: User,
) -> AsyncGenerator[Sign]:
    async with async_sessions() as db:
        accounts = {
            user.id: cast(dict[str, Any], await read_account(db, uuid=user.uuid))
            for user in (diver, other_diver, operator)
        }
    sign = Sign(account=accounts[diver.id], accounts=accounts)
    proposals_app.dependency_overrides[get_current_user] = lambda: sign.account
    yield sign


@pytest_asyncio.fixture
async def http(proposals_app: Any, signed: Sign) -> AsyncGenerator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proposals_app), base_url="http://test/api/v1"
    ) as client:
        yield client


@pytest_asyncio.fixture
async def bearer_http(proposals_app: Any, per_request: None) -> AsyncGenerator[httpx.AsyncClient]:
    """The real credential path: `get_current_user` resolves whatever bearer is sent."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proposals_app), base_url="http://test/api/v1"
    ) as client:
        yield client


def _species(db: Session, **overrides: Any) -> Species:
    return create_species(db, **overrides)


def _key(db: Session, user: User, *, scope: str = "write") -> dict[str, str]:
    secret, token_hash = generate_api_key()
    db.add(
        ApiKey(
            user_id=user.id,
            name="rater",
            prefix=secret[:API_KEY_SHOWN_LENGTH],
            token_hash=token_hash,
            scope=scope,
            expires_at=None,
        )
    )
    db.commit()
    return {"Authorization": f"Bearer {secret}"}


def _row(db: Session, uuid: Any) -> SpeciesPhotoProposal:
    db.expire_all()
    return db.scalars(select(SpeciesPhotoProposal).where(SpeciesPhotoProposal.uuid == uuid)).one()


def _statuses(db: Session, species: Species) -> list[tuple[str, str]]:
    db.expire_all()
    return [
        (row.file, row.status)
        for row in db.execute(
            select(SpeciesPhotoProposal.file, SpeciesPhotoProposal.status)
            .where(SpeciesPhotoProposal.species_id == species.id)
            .order_by(SpeciesPhotoProposal.id)
        )
    ]


async def _propose(http: httpx.AsyncClient, species: Species, file: str, **body: Any) -> dict[str, Any]:
    response = await http.post(f"/species/{species.uuid}/photo-proposals", json={"file": file, **body})
    assert response.status_code == 201, response.text
    proposal: dict[str, Any] = response.json()
    return proposal


def _found(file: str) -> AsyncMock:
    photo = species_photos.fetched_photo(
        photo=species_photos.NormalizedPhoto(data=b"webp", width=600, height=400),
        file=file,
        credit=PhotoCredit(author="A. Diver", license_name="CC BY 4.0", license_url=None, source_url=None),
    )
    return AsyncMock(return_value=PhotoAttempt.found(photo))


async def _admin_row(http: httpx.AsyncClient, species: Species) -> dict[str, Any]:
    response = await http.get("/admin/species", params={"search": species.scientific_name})
    assert response.status_code == 200, response.text
    rows: list[dict[str, Any]] = response.json()["data"]
    assert len(rows) == 1
    return rows[0]


class TestProposing:
    @pytest.mark.asyncio
    async def test_it_waits_and_proposing_again_replaces_it(
        self, http: httpx.AsyncClient, db: Session, no_wikimedia: None
    ) -> None:
        species = _species(db)

        first = await _propose(http, species, "File:Pterois_miles_1.jpg", note="  Whole animal  ")
        second = await _propose(http, species, "https://commons.wikimedia.org/wiki/File:Pterois_miles_2.jpg")

        assert first["status"] == "waiting"
        assert (first["file"], first["note"]) == ("Pterois miles 1.jpg", "Whole animal")
        assert first["species"] == {
            "uuid": str(species.uuid),
            "scientific_name": species.scientific_name,
            "common_name": species.common_name,
        }
        assert second["uuid"] != first["uuid"]
        assert _statuses(db, species) == [("Pterois miles 2.jpg", "waiting")]
        listed = (await http.get("/species-photo-proposals", params={"status": "waiting"})).json()["data"]
        assert [(row["species"]["uuid"], row["file"]) for row in listed] == [(str(species.uuid), "Pterois miles 2.jpg")]

    @pytest.mark.asyncio
    async def test_two_accounts_each_have_one_waiting(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, other_diver: User, no_wikimedia: None
    ) -> None:
        species = _species(db)

        await _propose(http, species, "A.jpg")
        signed.as_(other_diver)
        await _propose(http, species, "B.jpg")

        assert _statuses(db, species) == [("A.jpg", "waiting"), ("B.jpg", "waiting")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("file", ["Range map.svg", "not a file", "https://example.org/wiki/File:Fish.jpg"])
    async def test_a_title_that_names_no_raster_file_is_a_422(
        self, http: httpx.AsyncClient, db: Session, no_wikimedia: None, file: str
    ) -> None:
        species = _species(db)

        response = await http.post(f"/species/{species.uuid}/photo-proposals", json={"file": file})

        assert response.status_code == 422
        assert _statuses(db, species) == []

    @pytest.mark.asyncio
    async def test_a_note_past_500_characters_is_a_422(self, http: httpx.AsyncClient, db: Session) -> None:
        response = await http.post(
            f"/species/{_species(db).uuid}/photo-proposals", json={"file": "A.jpg", "note": "x" * 501}
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_an_unknown_species_is_a_404(self, http: httpx.AsyncClient, no_wikimedia: None) -> None:
        response = await http.post(f"/species/{uuid_pkg.uuid4()}/photo-proposals", json={"file": "A.jpg"})

        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("curation", [PhotoCuration.PINNED, PhotoCuration.HIDDEN])
    async def test_a_curated_species_refuses_it_and_says_so_on_its_read(
        self, http: httpx.AsyncClient, db: Session, no_wikimedia: None, curation: PhotoCuration
    ) -> None:
        species = _species(db, photo_curation=curation)

        response = await http.post(f"/species/{species.uuid}/photo-proposals", json={"file": "A.jpg"})

        assert response.status_code == 409
        read = await http.get(f"/species/{species.uuid}")
        assert read.json()["accepts_photo_proposals"] is False

    @pytest.mark.asyncio
    async def test_the_stored_photo_is_refused_in_any_spelling(
        self, http: httpx.AsyncClient, db: Session, no_wikimedia: None
    ) -> None:
        species = _species(db, photo_file="Pterois miles 1.jpg")
        assert (await http.get(f"/species/{species.uuid}")).json()["accepts_photo_proposals"] is True

        response = await http.post(
            f"/species/{species.uuid}/photo-proposals", json={"file": "File:Pterois_miles_1.jpg"}
        )

        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_an_account_is_bounded_per_day(
        self, http: httpx.AsyncClient, db: Session, monkeypatch: pytest.MonkeyPatch, no_wikimedia: None
    ) -> None:
        monkeypatch.setattr(settings, "SPECIES_PHOTO_PROPOSAL_RATE_LIMIT_PER_USER", 1)
        await _propose(http, _species(db), "A.jpg")

        response = await http.post(f"/species/{_species(db).uuid}/photo-proposals", json={"file": "A.jpg"})

        assert response.status_code == 429

    def test_the_daily_limit_defaults_to_a_hundred(self) -> None:
        assert settings.SPECIES_PHOTO_PROPOSAL_RATE_LIMIT_PER_USER == 100
        assert settings.SPECIES_PHOTO_PROPOSAL_RATE_LIMIT_WINDOW_SECONDS == 86400


class TestAReplacedProposal:
    @pytest.mark.asyncio
    async def test_the_uuid_the_operator_was_shown_never_pins_the_file_that_replaced_it(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, diver: User, operator: User, volume: Path
    ) -> None:
        species = _species(db)
        shown = await _propose(http, species, "A.jpg")
        signed.as_(operator)
        with patch.object(species_service, "proposed_files", AsyncMock(return_value={})):
            review = (await http.get(f"/admin/species/{species.uuid}/photo-proposals")).json()
        signed.as_(diver)
        replacement = await _propose(http, species, "B.jpg")
        signed.as_(operator)
        fetch = _found("B.jpg")

        with patch.object(species_service, "fetch_pinned_photo", fetch):
            stale = await http.post(f"/admin/species-photo-proposal/{shown['uuid']}/accept")
            stale_decline = await http.post(f"/admin/species-photo-proposal/{shown['uuid']}/decline")

        assert [row["uuid"] for row in review] == [shown["uuid"]]
        assert (stale.status_code, stale_decline.status_code) == (404, 404)
        fetch.assert_not_awaited()
        assert _row(db, replacement["uuid"]).status == "waiting"
        db.refresh(species)
        assert species.photo_curation is None

    @pytest.mark.asyncio
    async def test_it_goes_to_the_back_of_the_review(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, diver: User, other_diver: User, operator: User
    ) -> None:
        species = _species(db)
        await _propose(http, species, "A.jpg")
        signed.as_(other_diver)
        theirs = await _propose(http, species, "B.jpg")
        signed.as_(diver)
        replacement = await _propose(http, species, "C.jpg")
        signed.as_(operator)

        with patch.object(species_service, "proposed_files", AsyncMock(return_value={})):
            review = (await http.get(f"/admin/species/{species.uuid}/photo-proposals")).json()

        assert [row["uuid"] for row in review] == [theirs["uuid"], replacement["uuid"]]


class TestADeclinedFile:
    @pytest.mark.asyncio
    async def test_it_never_waits_again_whoever_proposes_it(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, diver: User, other_diver: User, operator: User
    ) -> None:
        species = _species(db)
        proposal = await _propose(http, species, "A.jpg")
        signed.as_(operator)
        assert (await http.post(f"/admin/species-photo-proposal/{proposal['uuid']}/decline")).status_code == 200

        for proposer in (diver, other_diver):
            signed.as_(proposer)
            response = await http.post(f"/species/{species.uuid}/photo-proposals", json={"file": "File:A.jpg"})
            assert response.status_code == 409

        assert (await _propose(http, species, "B.jpg"))["status"] == "waiting"

    @pytest.mark.asyncio
    async def test_a_withdrawn_or_closed_file_may_wait_again(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, diver: User, operator: User, volume: Path
    ) -> None:
        species = _species(db)
        withdrawn = await _propose(http, species, "A.jpg")
        assert (await http.delete(f"/species-photo-proposal/{withdrawn['uuid']}")).status_code == 200
        await _propose(http, species, "A.jpg")

        signed.as_(operator)
        assert (await http.delete(f"/admin/species/{species.uuid}/photo")).status_code == 200
        with patch.object(species_service, "fetch_photo_for_species", AsyncMock(return_value=PhotoAttempt.declined())):
            assert (await http.post(f"/admin/species/{species.uuid}/photo/refetch")).status_code == 200
        signed.as_(diver)

        assert _statuses(db, species) == [("A.jpg", "withdrawn"), ("A.jpg", "closed")]
        assert (await _propose(http, species, "A.jpg"))["status"] == "waiting"


class TestTheCuratedWritesSettleWhatWaits:
    @pytest.mark.asyncio
    async def test_accepting_pins_the_file_accepts_its_twins_and_closes_the_rest(
        self,
        http: httpx.AsyncClient,
        db: Session,
        signed: Sign,
        other_diver: User,
        operator: User,
        volume: Path,
    ) -> None:
        species = _species(db)
        accepted = await _propose(http, species, "A.jpg")
        signed.as_(other_diver)
        twin = await _propose(http, species, "A.jpg")
        other_species = _species(db)
        elsewhere = await _propose(http, other_species, "B.jpg")
        signed.as_(operator)
        other = await _propose(http, species, "C.jpg")
        fetch = _found("A.jpg")

        with patch.object(species_service, "fetch_pinned_photo", fetch):
            response = await http.post(f"/admin/species-photo-proposal/{accepted['uuid']}/accept")

        assert response.status_code == 200, response.text
        fetch.assert_awaited_once_with("A.jpg")
        row = response.json()
        assert (row["photo_curation"], row["photo_file"], row["photo_proposals_waiting"]) == ("pinned", "A.jpg", 0)
        assert row["accepts_photo_proposals"] is False
        assert _row(db, accepted["uuid"]).status == "accepted"
        assert _row(db, twin["uuid"]).status == "accepted"
        assert _row(db, other["uuid"]).status == "closed"
        assert _row(db, other["uuid"]).decided_at is not None
        assert _row(db, elsewhere["uuid"]).status == "waiting"

    @pytest.mark.asyncio
    async def test_a_pin_by_hand_accepts_what_names_its_file_and_closes_the_rest(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, other_diver: User, operator: User, volume: Path
    ) -> None:
        species = _species(db)
        named = await _propose(http, species, "A.jpg")
        signed.as_(other_diver)
        unnamed = await _propose(http, species, "B.jpg")
        signed.as_(operator)

        with patch.object(species_service, "fetch_pinned_photo", _found("A.jpg")):
            response = await http.put(f"/admin/species/{species.uuid}/photo", json={"file": "File:A.jpg"})

        assert response.status_code == 200, response.text
        assert response.json()["photo_proposals_waiting"] == 0
        assert (_row(db, named["uuid"]).status, _row(db, unnamed["uuid"]).status) == ("accepted", "closed")

    @pytest.mark.asyncio
    async def test_a_hide_closes_them_all(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, other_diver: User, operator: User, volume: Path
    ) -> None:
        species = _species(db)
        await _propose(http, species, "A.jpg")
        signed.as_(other_diver)
        await _propose(http, species, "B.jpg")
        signed.as_(operator)

        response = await http.delete(f"/admin/species/{species.uuid}/photo")

        assert response.status_code == 200, response.text
        assert response.json()["photo_proposals_waiting"] == 0
        assert _statuses(db, species) == [("A.jpg", "closed"), ("B.jpg", "closed")]

    @pytest.mark.asyncio
    async def test_a_refetch_hands_the_species_back_and_settles_nothing(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, operator: User, volume: Path
    ) -> None:
        species = _species(db)
        await _propose(http, species, "A.jpg")
        signed.as_(operator)

        with patch.object(species_service, "fetch_photo_for_species", AsyncMock(return_value=PhotoAttempt.declined())):
            response = await http.post(f"/admin/species/{species.uuid}/photo/refetch")

        assert response.status_code == 200, response.text
        assert (response.json()["accepts_photo_proposals"], response.json()["photo_proposals_waiting"]) == (True, 1)
        assert _statuses(db, species) == [("A.jpg", "waiting")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("failure", "status"),
        [
            (AsyncMock(side_effect=species_service.CommonsFileMissingError("A.jpg")), 422),
            (AsyncMock(side_effect=species_photos.UnsupportedPhotoImageError("not an image")), 422),
            (AsyncMock(return_value=PhotoAttempt.unavailable()), 503),
            (AsyncMock(return_value=PhotoAttempt.timed_out()), 503),
        ],
    )
    async def test_an_accept_the_pin_would_refuse_refuses_and_leaves_it_waiting(
        self,
        http: httpx.AsyncClient,
        db: Session,
        signed: Sign,
        operator: User,
        volume: Path,
        failure: AsyncMock,
        status: int,
    ) -> None:
        species = _species(db)
        proposal = await _propose(http, species, "A.jpg")
        signed.as_(operator)

        with patch.object(species_service, "fetch_pinned_photo", failure):
            response = await http.post(f"/admin/species-photo-proposal/{proposal['uuid']}/accept")

        assert response.status_code == status
        assert _row(db, proposal["uuid"]).status == "waiting"
        db.refresh(species)
        assert species.photo_curation is None


class TestDeciding:
    @pytest.mark.asyncio
    async def test_a_decline_takes_an_optional_reason_and_answers_the_row(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, other_diver: User, operator: User
    ) -> None:
        species = _species(db)
        bare = await _propose(http, species, "A.jpg")
        signed.as_(other_diver)
        reasoned = await _propose(http, species, "B.jpg")
        signed.as_(operator)

        first = await http.post(f"/admin/species-photo-proposal/{bare['uuid']}/decline")
        second = await http.post(
            f"/admin/species-photo-proposal/{reasoned['uuid']}/decline", json={"reason": " A goatfish "}
        )

        assert first.status_code == 200, first.text
        assert first.json()["photo_proposals_waiting"] == 1
        assert second.json()["photo_proposals_waiting"] == 0
        assert (_row(db, bare["uuid"]).status, _row(db, bare["uuid"]).reason) == ("declined", None)
        assert (_row(db, reasoned["uuid"]).reason, second.json()["photo_curation"]) == ("A goatfish", None)
        signed.as_(other_diver)
        mine = (await http.get("/species-photo-proposals")).json()["data"]
        assert [(row["status"], row["reason"]) for row in mine] == [("declined", "A goatfish")]
        assert mine[0]["decided_at"] is not None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["accept", "decline"])
    async def test_a_decided_proposal_is_a_409(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, operator: User, action: str
    ) -> None:
        proposal = await _propose(http, _species(db), "A.jpg")
        assert (await http.delete(f"/species-photo-proposal/{proposal['uuid']}")).status_code == 200
        signed.as_(operator)

        response = await http.post(f"/admin/species-photo-proposal/{proposal['uuid']}/{action}")

        assert response.status_code == 409

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["accept", "decline"])
    async def test_an_unknown_proposal_is_a_404(
        self, http: httpx.AsyncClient, signed: Sign, operator: User, action: str
    ) -> None:
        signed.as_(operator)

        response = await http.post(f"/admin/species-photo-proposal/{uuid_pkg.uuid4()}/{action}")

        assert response.status_code == 404


class TestWithdrawing:
    @pytest.mark.asyncio
    async def test_it_stays_on_the_list_as_withdrawn_and_twice_is_a_409(
        self, http: httpx.AsyncClient, db: Session
    ) -> None:
        proposal = await _propose(http, _species(db), "A.jpg")

        first = await http.delete(f"/species-photo-proposal/{proposal['uuid']}")
        second = await http.delete(f"/species-photo-proposal/{proposal['uuid']}")

        assert (first.status_code, second.status_code) == (200, 409)
        listed = (await http.get("/species-photo-proposals")).json()["data"]
        assert [row["status"] for row in listed if row["uuid"] == proposal["uuid"]] == ["withdrawn"]

    @pytest.mark.asyncio
    async def test_someone_elses_is_a_404_and_stays_waiting(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, other_diver: User
    ) -> None:
        proposal = await _propose(http, _species(db), "A.jpg")
        signed.as_(other_diver)

        response = await http.delete(f"/species-photo-proposal/{proposal['uuid']}")

        assert response.status_code == 404
        assert _row(db, proposal["uuid"]).status == "waiting"


class TestTheProposersList:
    @pytest.mark.asyncio
    async def test_newest_first_their_own_only_and_filtered_by_status(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, diver: User, other_diver: User
    ) -> None:
        older = await _propose(http, _species(db), "A.jpg")
        newer = await _propose(http, _species(db), "B.jpg")
        await http.delete(f"/species-photo-proposal/{older['uuid']}")
        signed.as_(other_diver)
        await _propose(http, _species(db), "C.jpg")
        signed.as_(diver)

        everything = (await http.get("/species-photo-proposals")).json()
        waiting = (await http.get("/species-photo-proposals", params={"status": "waiting"})).json()

        assert [row["uuid"] for row in everything["data"]] == [newer["uuid"], older["uuid"]]
        assert everything["total_count"] == 2
        assert [row["uuid"] for row in waiting["data"]] == [newer["uuid"]]
        assert (await http.get("/species-photo-proposals", params={"status": "pending"})).status_code == 422


class TestTheOperatorsReview:
    @pytest.mark.asyncio
    async def test_the_list_counts_and_filters_on_what_waits(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, other_diver: User, operator: User
    ) -> None:
        proposed = _species(db)
        quiet = _species(db, scientific_name=f"{proposed.scientific_name} quiet")
        await _propose(http, proposed, "A.jpg")
        signed.as_(other_diver)
        await _propose(http, proposed, "B.jpg")
        signed.as_(operator)

        response = await http.get("/admin/species", params={"search": proposed.scientific_name, "filter": "proposed"})

        assert [row["uuid"] for row in response.json()["data"]] == [str(proposed.uuid)]
        assert response.json()["data"][0]["photo_proposals_waiting"] == 2
        assert (await _admin_row(http, quiet))["photo_proposals_waiting"] == 0

    @pytest.mark.asyncio
    async def test_the_species_list_asks_wikimedia_nothing(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, operator: User, no_wikimedia: None
    ) -> None:
        await _propose(http, _species(db), "A.jpg")
        signed.as_(operator)

        assert (await http.get("/admin/species", params={"filter": "proposed"})).status_code == 200

    @pytest.mark.asyncio
    async def test_the_species_read_is_the_oldest_two_dozen_with_what_commons_says(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, operator: User
    ) -> None:
        species = _species(db)
        proposers = [create_user(db) for _ in range(species_service.CANDIDATE_LIMIT + 1)]
        db.add_all(
            SpeciesPhotoProposal(user_id=user.id, species_id=species.id, file=f"Fish {index}.jpg", note=f"n{index}")
            for index, user in enumerate(proposers)
        )
        db.commit()
        signed.as_(operator)
        found = {
            "Fish 0.jpg": PhotoCandidate(
                file="Fish 0.jpg",
                width=1200,
                height=800,
                credit=PhotoCredit(
                    author="A. Diver", license_name="CC BY 4.0", license_url=None, source_url="https://c/File:F0"
                ),
                preview="data:image/jpeg;base64,AA==",
                is_current=False,
            )
        }
        described = AsyncMock(return_value=found)

        with patch.object(species_service, "proposed_files", described):
            response = await http.get(f"/admin/species/{species.uuid}/photo-proposals")

        assert response.status_code == 200, response.text
        rows = response.json()
        expected = [f"Fish {index}.jpg" for index in range(species_service.CANDIDATE_LIMIT)]
        described.assert_awaited_once_with(expected)
        assert [row["file"] for row in rows] == expected
        assert {key: rows[0][key] for key in ("note", "proposer_name", "width", "license", "author", "preview")} == {
            "note": "n0",
            "proposer_name": proposers[0].name,
            "width": 1200,
            "license": "CC BY 4.0",
            "author": "A. Diver",
            "preview": "data:image/jpeg;base64,AA==",
        }
        assert rows[0]["source_url"] == "https://c/File:F0"
        assert {key: rows[1][key] for key in ("width", "height", "license", "author", "source_url", "preview")} == (
            dict.fromkeys(("width", "height", "license", "author", "source_url", "preview"))
        )

    @pytest.mark.asyncio
    async def test_the_species_read_of_an_unknown_species_is_a_404(
        self, http: httpx.AsyncClient, signed: Sign, operator: User
    ) -> None:
        signed.as_(operator)

        assert (await http.get(f"/admin/species/{uuid_pkg.uuid4()}/photo-proposals")).status_code == 404

    @pytest.mark.asyncio
    async def test_a_deleting_accounts_proposal_is_out_of_sight_and_out_of_reach(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, diver: User, operator: User
    ) -> None:
        species = _species(db)
        proposal = await _propose(http, species, "A.jpg")
        db.execute(update(User).where(User.id == diver.id).values(is_deleted=True))
        db.commit()
        signed.as_(operator)

        with patch.object(species_service, "proposed_files", AsyncMock(return_value={})):
            review = await http.get(f"/admin/species/{species.uuid}/photo-proposals")
        listed = await http.get("/admin/species", params={"search": species.scientific_name, "filter": "proposed"})
        accept = await http.post(f"/admin/species-photo-proposal/{proposal['uuid']}/accept")
        decline = await http.post(f"/admin/species-photo-proposal/{proposal['uuid']}/decline")

        assert review.json() == []
        assert listed.json()["data"] == []
        assert (await _admin_row(http, species))["photo_proposals_waiting"] == 0
        assert (accept.status_code, decline.status_code) == (404, 404)

        db.execute(update(User).where(User.id == diver.id).values(is_deleted=False))
        db.commit()
        assert (await _admin_row(http, species))["photo_proposals_waiting"] == 1

    @pytest.mark.asyncio
    async def test_the_purge_takes_them_and_a_pin_from_one_stays(
        self, http: httpx.AsyncClient, db: Session, signed: Sign, diver: User, operator: User, volume: Path
    ) -> None:
        species = _species(db)
        proposal = await _propose(http, species, "A.jpg")
        signed.as_(operator)
        with patch.object(species_service, "fetch_pinned_photo", _found("A.jpg")):
            assert (await http.post(f"/admin/species-photo-proposal/{proposal['uuid']}/accept")).status_code == 200

        db.execute(text('DELETE FROM "user" WHERE id = :id'), {"id": diver.id})
        db.commit()

        assert _statuses(db, species) == []
        db.refresh(species)
        assert (species.photo_curation, species.photo_file) == ("pinned", "A.jpg")


class TestAKey:
    @pytest.mark.asyncio
    async def test_a_write_key_proposes_and_lists_its_own(
        self, bearer_http: httpx.AsyncClient, db: Session, diver: User, no_wikimedia: None
    ) -> None:
        key = _key(db, diver)
        species = _species(db)

        response = await bearer_http.post(
            f"/species/{species.uuid}/photo-proposals", json={"file": "A.jpg"}, headers=key
        )
        listed = await bearer_http.get("/species-photo-proposals", headers=key)

        assert response.status_code == 201, response.text
        assert [row["uuid"] for row in listed.json()["data"]] == [response.json()["uuid"]]

    @pytest.mark.asyncio
    async def test_a_read_key_cannot_propose(self, bearer_http: httpx.AsyncClient, db: Session, diver: User) -> None:
        key = _key(db, diver, scope="read")

        response = await bearer_http.post(
            f"/species/{_species(db).uuid}/photo-proposals", json={"file": "A.jpg"}, headers=key
        )

        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_no_key_decides_or_lists_the_catalog_even_the_operators(
        self, bearer_http: httpx.AsyncClient, db: Session, diver: User, operator: User
    ) -> None:
        species = _species(db)
        proposal = SpeciesPhotoProposal(user_id=diver.id, species_id=species.id, file="A.jpg")
        db.add(proposal)
        db.commit()
        key = _key(db, operator)

        answers = [
            await bearer_http.get("/admin/species", params={"filter": "proposed"}, headers=key),
            await bearer_http.get(f"/admin/species/{species.uuid}/photo-proposals", headers=key),
            await bearer_http.post(f"/admin/species-photo-proposal/{proposal.uuid}/accept", headers=key),
            await bearer_http.post(f"/admin/species-photo-proposal/{proposal.uuid}/decline", headers=key),
        ]

        assert [answer.status_code for answer in answers] == [403, 403, 403, 403]
        assert _row(db, proposal.uuid).status == "waiting"


class TestTheProposedFilesAtCommons:
    @pytest.mark.asyncio
    async def test_one_metadata_call_previews_inline_and_a_missing_file_absent(self) -> None:
        calls: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            if request.url.host == "upload.wikimedia.org":
                return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})
            assert request.url.params["prop"] == "imageinfo"
            assert request.url.params["iiurlwidth"] == "250"
            return httpx.Response(
                200,
                json={
                    "query": {
                        "pages": [
                            {"title": "File:Gone.jpg", "missing": True},
                            {
                                "title": "File:Fish.jpg",
                                "imageinfo": [
                                    {
                                        "width": 900,
                                        "height": 600,
                                        "thumburl": "https://upload.wikimedia.org/x/250px-Fish.jpg",
                                        "descriptionurl": "https://commons.wikimedia.org/wiki/File:Fish.jpg",
                                        "extmetadata": {"LicenseShortName": {"value": "CC0"}},
                                    }
                                ],
                            },
                        ]
                    }
                },
            )

        real = httpx.AsyncClient
        with (
            patch(
                "src.app.core.utils.outbound.httpx.AsyncClient",
                side_effect=lambda **kwargs: real(transport=httpx.MockTransport(handle), **kwargs),
            ),
            patch.object(species_service, "_claim_provider_slot", AsyncMock(return_value=True)) as slot,
        ):
            found = await species_service.proposed_files(["Fish.jpg", "Gone.jpg", "Fish.jpg"])

        assert list(found) == ["Fish.jpg"]
        assert (found["Fish.jpg"].width, found["Fish.jpg"].credit.license_name) == (900, "CC0")
        assert found["Fish.jpg"].preview == f"data:image/jpeg;base64,{base64.b64encode(b'\xff\xd8jpeg').decode()}"
        assert calls[0].url.params["titles"] == "File:Fish.jpg|File:Gone.jpg"
        assert slot.await_count == 1

    @pytest.mark.asyncio
    async def test_nothing_proposed_asks_nobody(self) -> None:
        with patch("src.app.core.utils.outbound.httpx.AsyncClient", side_effect=AssertionError):
            assert await species_service.proposed_files([]) == {}
