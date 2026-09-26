"""The check-in link: the diver's three routes under `/user/checkin-link`, the three anonymous
ones under `/checkin/{token}`, and the sweep that deletes dead links.

The Postgres half drives the real app over ASGI in the test's own event loop, so what it asserts
- status, body and `Cache-Control` - is what the middleware stack actually sends.
"""

import hashlib
import io
from collections.abc import AsyncGenerator, Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi import UploadFile
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api import router as api_router
from src.app.api.dependencies import get_current_user
from src.app.api.v1.certifications import _cached_read_certifications
from src.app.core.config import settings
from src.app.core.db.database import async_engine, async_get_db
from src.app.core.setup import create_application
from src.app.core.worker.functions import purge_expired_checkin_links
from src.app.core.worker.settings import WorkerSettings
from src.app.crud.crud_users import read_account
from src.app.main import app
from src.app.models.certification import Certification
from src.app.models.checkin_link import CheckinLink
from src.app.models.user import User
from src.app.models.user_dive_stats import UserDiveStats
from src.app.schemas.certification import CertificationSide
from src.app.schemas.checkin_link import (
    POSTGRES_INTEGER_MAX,
    CheckinCertification,
    CheckinDiver,
    CheckinLinkCreate,
)
from src.app.schemas.user import UserRead
from src.app.schemas.user_picture import PictureCrop
from src.app.services.certification_files import store_certification_file
from src.app.services.checkin_links import CHECKIN_LINK_TTL, CardFront
from src.app.services.user_pictures import AVATAR_FRAME, PORTRAIT_FRAME, store_picture
from tests.conftest import db_available
from tests.helpers.generators import create_contact, create_dive, create_user
from tests.helpers.images import plain_png
from tests.helpers.routes import iter_api_routes

NO_STORE = "private, no-store"
PDF_BYTES = b"%PDF-1.7\n" + b"\x00" * 32
FIGURES = {"total_dives": 212, "max_depth": 41.5, "last_dive_on": "2026-09-01"}


class TestTheMintBody:
    """The figures as the page shows them: each nullable, the count and depth bounded by their
    columns, and the date by its format alone."""

    def test_every_figure_may_be_null(self) -> None:
        body = CheckinLinkCreate.model_validate({"total_dives": None, "max_depth": None, "last_dive_on": None})

        assert (body.total_dives, body.max_depth, body.last_dive_on) == (None, None, None)

    def test_a_last_dive_years_ahead_is_accepted(self) -> None:
        """The log holds future-dated dives, and an uncorrected page carries whichever is newest."""
        body = CheckinLinkCreate.model_validate(FIGURES | {"last_dive_on": "2031-02-14"})

        assert body.last_dive_on == date(2031, 2, 14)

    def test_the_count_stops_at_the_integer_ceiling(self) -> None:
        assert CheckinLinkCreate.model_validate(FIGURES | {"total_dives": POSTGRES_INTEGER_MAX}).total_dives
        with pytest.raises(ValidationError):
            CheckinLinkCreate.model_validate(FIGURES | {"total_dives": POSTGRES_INTEGER_MAX + 1})

    @pytest.mark.parametrize(
        "change",
        [
            {"total_dives": -1},
            {"max_depth": -0.5},
            {"max_depth": float("inf")},
            {"max_depth": float("nan")},
            {"last_dive_on": "2026-13-01"},
            {"notes": "extra"},
        ],
    )
    def test_what_no_page_could_show_is_refused(self, change: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            CheckinLinkCreate.model_validate(FIGURES | change)

    def test_a_figure_left_out_is_refused_rather_than_read_as_null(self) -> None:
        with pytest.raises(ValidationError):
            CheckinLinkCreate.model_validate({"total_dives": 3, "max_depth": 12.0})


class TestTheAnonymousRoutes:
    def test_they_are_the_summary_the_portrait_and_a_cards_front(self) -> None:
        """No route under `/checkin` serves a back, an original or the avatar."""
        paths = {route.path for route in iter_api_routes(app) if route.path.startswith("/api/v1/checkin/")}

        assert paths == {
            "/api/v1/checkin/{token}",
            "/api/v1/checkin/{token}/portrait",
            "/api/v1/checkin/{token}/certification/{uuid}/front",
        }


class TestTheSweepIsOnTheHour:
    def test_it_runs_hourly_and_at_startup_like_the_other_sweeps(self) -> None:
        (job,) = [job for job in WorkerSettings.cron_jobs if job.coroutine is purge_expired_checkin_links]

        assert (job.minute, job.hour, job.run_at_startup) == (0, None, True)


class _Session:
    """`local_session()` stand-in that records the one statement the sweep sends."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        return MagicMock(rowcount=3)

    async def commit(self) -> None:
        return None


class TestTheSweepStatement:
    @pytest.mark.asyncio
    async def test_it_deletes_the_expired_and_the_revoked_in_one_statement(self) -> None:
        session = _Session()

        with patch("src.app.core.worker.functions.local_session", return_value=session):
            result = await purge_expired_checkin_links({})

        (statement,) = session.statements
        compiled = str(statement)
        assert compiled.startswith("DELETE FROM checkin_link")
        assert "checkin_link.revoked_at IS NOT NULL OR checkin_link.expires_at <=" in compiled
        assert "3" in result


# -------------- against Postgres --------------


@pytest.fixture(scope="module")
def checkin_app() -> Any:
    return create_application(router=api_router, settings=settings, apply_migrations_on_start=False)


@pytest_asyncio.fixture
async def http(checkin_app: Any, async_db: AsyncSession) -> AsyncGenerator[httpx.AsyncClient]:
    """No credential on any request: the diver's own routes are reached through
    `get_current_user`'s override, and the anonymous ones through nothing at all."""

    async def the_tests_session() -> AsyncGenerator[AsyncSession]:
        yield async_db

    checkin_app.dependency_overrides[async_get_db] = the_tests_session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=checkin_app), base_url="http://test") as client:
        yield client
    checkin_app.dependency_overrides = {}


SignIn = Callable[[User], Awaitable[None]]


@pytest.fixture
def sign_in(checkin_app: Any, async_db: AsyncSession) -> SignIn:
    async def as_diver(diver: User) -> None:
        account = await read_account(async_db, uuid=diver.uuid)
        checkin_app.dependency_overrides[get_current_user] = lambda: account

    return as_diver


async def _mint(http: httpx.AsyncClient, sign_in: SignIn, diver: User, figures: dict[str, Any] = FIGURES) -> str:
    await sign_in(diver)
    response = await http.post("/api/v1/user/checkin-link", json=figures)
    assert response.status_code == 201, response.text
    return cast(str, response.json()["token"])


def _upload(data: bytes, filename: str) -> UploadFile:
    return UploadFile(filename=filename, file=io.BytesIO(data))


def _dead(response: httpx.Response) -> tuple[int, bytes, str | None]:
    return response.status_code, response.content, response.headers.get("cache-control")


async def _with_a_portrait_and_a_card_front(db: Session, async_db: AsyncSession, diver: User) -> Any:
    """Store both of the images a live link serves, answering the card's uuid."""
    card = Certification(user_id=diver.id, agency="padi", name="Open Water")
    db.add(card)
    db.commit()
    await store_certification_file(
        async_db,
        certification_id=card.id,
        side=CertificationSide.FRONT,
        upload=_upload(plain_png(size=(9, 6)), "c.png"),
    )
    await store_picture(
        async_db,
        user_id=diver.id,
        frame=PORTRAIT_FRAME,
        upload=_upload(plain_png(size=(140, 180)), "me.png"),
        crop=PictureCrop(x=0, y=0, width=140, height=180),
    )
    return card.uuid


@pytest.mark.skipif(not db_available(), reason="No database connection available")
class TestAgainstPostgres:
    @pytest_asyncio.fixture(autouse=True)
    async def _dispose_the_app_engine(self) -> AsyncGenerator[None]:
        """The sweep opens its own session from the app's module-level engine, and a pooled
        asyncpg connection belongs to the loop that opened it."""
        await async_engine.dispose()
        yield
        await async_engine.dispose()

    @pytest.mark.asyncio
    async def test_the_token_is_answered_once_and_stored_only_as_its_hash(
        self, db: Session, http: httpx.AsyncClient, sign_in: SignIn
    ) -> None:
        diver = create_user(db)
        before = datetime.now(UTC)

        token = await _mint(http, sign_in, diver)

        row = db.execute(select(CheckinLink).where(CheckinLink.user_id == diver.id)).scalar_one()
        assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert token not in {str(value) for value in vars(row).values()}
        assert before + CHECKIN_LINK_TTL <= row.expires_at <= datetime.now(UTC) + CHECKIN_LINK_TTL
        assert (row.total_dives, row.max_depth, row.last_dive_on) == (212, 41.5, date(2026, 9, 1))

        live = await http.get("/api/v1/user/checkin-link")
        assert live.status_code == 200
        assert live.json().keys() == {"expires_at"}
        assert datetime.fromisoformat(live.json()["expires_at"]) == row.expires_at

    @pytest.mark.asyncio
    async def test_minting_again_retires_the_previous_link(
        self, db: Session, http: httpx.AsyncClient, sign_in: SignIn
    ) -> None:
        diver = create_user(db)
        first = await _mint(http, sign_in, diver)

        second = await _mint(http, sign_in, diver)

        assert (await http.get(f"/api/v1/checkin/{first}")).status_code == 404
        assert (await http.get(f"/api/v1/checkin/{second}")).status_code == 200
        unrevoked = db.execute(
            select(CheckinLink.id).where(CheckinLink.user_id == diver.id, CheckinLink.revoked_at.is_(None))
        ).all()
        assert len(unrevoked) == 1

    @pytest.mark.asyncio
    async def test_a_revoke_ends_the_link_and_a_second_one_still_succeeds(
        self, db: Session, http: httpx.AsyncClient, sign_in: SignIn
    ) -> None:
        diver = create_user(db)
        token = await _mint(http, sign_in, diver)

        assert (await http.delete("/api/v1/user/checkin-link")).status_code == 200

        assert (await http.get(f"/api/v1/checkin/{token}")).status_code == 404
        assert (await http.get("/api/v1/user/checkin-link")).status_code == 404
        assert (await http.delete("/api/v1/user/checkin-link")).status_code == 200

    @pytest.mark.asyncio
    async def test_every_dead_token_is_the_same_404(
        self, db: Session, async_db: AsyncSession, http: httpx.AsyncClient, sign_in: SignIn
    ) -> None:
        """Unknown, expired, revoked and a diver who asked for deletion, on all three routes -
        for divers whose portrait and card front a live link does serve."""
        paths: dict[str, list[str]] = {}
        for case in ("expired", "revoked", "deleted"):
            diver = create_user(db)
            card_uuid = await _with_a_portrait_and_a_card_front(db, async_db, diver)
            token = await _mint(http, sign_in, diver)
            paths[case] = [
                f"/api/v1/checkin/{token}",
                f"/api/v1/checkin/{token}/portrait",
                f"/api/v1/checkin/{token}/certification/{card_uuid}/front",
            ]
            assert {(await http.get(path)).status_code for path in paths[case]} == {200}, case
            if case == "expired":
                db.execute(
                    update(CheckinLink)
                    .where(CheckinLink.user_id == diver.id)
                    .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
                )
            elif case == "revoked":
                await http.delete("/api/v1/user/checkin-link")
            else:
                db.execute(
                    update(User).where(User.id == diver.id).values(is_deleted=True, deleted_at=datetime.now(UTC))
                )
            db.commit()
        paths["unknown"] = [
            path.replace(paths["deleted"][0], "/api/v1/checkin/never-minted") for path in paths["deleted"]
        ]

        answers = {_dead(await http.get(path)) for case_paths in paths.values() for path in case_paths}

        assert answers == {(404, b'{"detail":"Not found"}', NO_STORE)}

    @pytest.mark.asyncio
    async def test_the_summary_is_what_the_signed_in_endpoints_say(
        self, db: Session, async_db: AsyncSession, http: httpx.AsyncClient, sign_in: SignIn
    ) -> None:
        """Every field the check-in page prints, off `GET /user` and `GET /certifications` for
        the same diver - in the list's order, with each card's dive centre by name, and its
        front's type and nothing of the back. A deleted card is not there."""
        diver = create_user(db)
        diver.phone, diver.date_of_birth = "+20 100 000 0000", date(1990, 4, 2)
        diver.insurance_provider, diver.insurance_policy_number = "DAN Europe", "DE-1234"
        diver.emergency_contact_name, diver.emergency_contact_phone = "Sam", "+44 20 0000 0000"
        centre = create_contact(db, diver)
        newest = Certification(
            user_id=diver.id, agency="padi", name="Rescue", certified_on=date(2025, 5, 1), contact_id=centre.id
        )
        older = Certification(
            user_id=diver.id, agency="other", agency_other="CMAS Egypt", name="2 Star", certified_on=date(2019, 1, 1)
        )
        dateless = Certification(user_id=diver.id, agency="ssi", name="Nitrox", certification_number="N-9")
        deleted = Certification(user_id=diver.id, agency="padi", name="Gone", is_deleted=True)
        db.add_all([newest, older, dateless, deleted])
        db.commit()
        await store_certification_file(
            async_db,
            certification_id=newest.id,
            side=CertificationSide.FRONT,
            upload=_upload(plain_png(size=(8, 5)), "f.png"),
        )
        await store_certification_file(
            async_db, certification_id=newest.id, side=CertificationSide.BACK, upload=_upload(PDF_BYTES, "b.pdf")
        )
        await store_certification_file(
            async_db, certification_id=older.id, side=CertificationSide.FRONT, upload=_upload(PDF_BYTES, "f.pdf")
        )
        await store_picture(
            async_db,
            user_id=diver.id,
            frame=PORTRAIT_FRAME,
            upload=_upload(plain_png(size=(70, 90)), "me.png"),
            crop=PictureCrop(x=0, y=0, width=70, height=90),
        )
        token = await _mint(http, sign_in, diver)

        response = await http.get(f"/api/v1/checkin/{token}")

        assert response.status_code == 200
        assert response.headers["cache-control"] == NO_STORE
        summary = response.json()
        account = UserRead.model_validate(await read_account(async_db, uuid=diver.uuid)).model_dump(mode="json")
        assert summary["diver"] == {field: account[field] for field in CheckinDiver.model_fields}
        assert summary["diver"]["portrait_sha256"] is not None

        listed = await cast(Any, _cached_read_certifications).__wrapped__(
            request=None,
            user_id=diver.id,
            user_uuid=diver.uuid,
            db=async_db,
            page=1,
            items_per_page=100,
            course_id=None,
        )
        shared_fields = set(CheckinCertification.model_fields) - {"contact_name", "front_content_type"}
        expected = [
            {field: card[field] for field in shared_fields}
            | {
                "contact_name": centre.name if card["contact_uuid"] == centre.uuid else None,
                "front_content_type": next(
                    (file["content_type"] for file in card["files"] if file["side"] == CertificationSide.FRONT), None
                ),
            }
            for card in listed["data"]
        ]
        assert [CheckinCertification.model_validate(card).model_dump() for card in summary["certifications"]] == [
            CheckinCertification.model_validate(card).model_dump() for card in expected
        ]
        assert [card["name"] for card in summary["certifications"]] == ["Rescue", "2 Star", "Nitrox"]
        assert [card["front_content_type"] for card in summary["certifications"]] == [
            "image/png",
            "application/pdf",
            None,
        ]

    @pytest.mark.asyncio
    async def test_the_figures_are_the_minted_ones_and_a_dive_logged_later_moves_nothing(
        self, db: Session, http: httpx.AsyncClient, sign_in: SignIn
    ) -> None:
        diver = create_user(db)
        token = await _mint(http, sign_in, diver, {"total_dives": 40, "max_depth": None, "last_dive_on": "2031-02-14"})

        create_dive(db, diver)
        db.add(UserDiveStats(user_id=diver.id, total_dives=41, max_depth=55.0, total_time=60, species_seen=0))
        db.commit()

        summary = (await http.get(f"/api/v1/checkin/{token}")).json()
        assert summary["diving"] == {"total_dives": 40, "max_depth": None, "last_dive_on": "2031-02-14"}

    @pytest.mark.asyncio
    async def test_all_three_figures_may_be_null(self, db: Session, http: httpx.AsyncClient, sign_in: SignIn) -> None:
        diver = create_user(db)
        token = await _mint(http, sign_in, diver, {"total_dives": None, "max_depth": None, "last_dive_on": None})

        summary = (await http.get(f"/api/v1/checkin/{token}")).json()
        assert summary["diving"] == {"total_dives": None, "max_depth": None, "last_dive_on": None}

    @pytest.mark.asyncio
    async def test_the_bytes_are_the_owner_routes_bytes(
        self, db: Session, async_db: AsyncSession, http: httpx.AsyncClient, sign_in: SignIn
    ) -> None:
        diver = create_user(db)
        card_uuid = await _with_a_portrait_and_a_card_front(db, async_db, diver)
        token = await _mint(http, sign_in, diver)

        pairs = [
            ("/api/v1/user/portrait", f"/api/v1/checkin/{token}/portrait"),
            (
                f"/api/v1/certification/{card_uuid}/file/front",
                f"/api/v1/checkin/{token}/certification/{card_uuid}/front",
            ),
        ]
        for owner_path, shared_path in pairs:
            owner, shared = await http.get(owner_path), await http.get(shared_path)
            assert shared.status_code == 200, shared_path
            assert shared.content == owner.content
            assert shared.headers["content-type"] == owner.headers["content-type"]
            assert shared.headers["etag"] == owner.headers["etag"]
            assert shared.headers["content-disposition"] == "inline"
            assert shared.headers["cache-control"] == NO_STORE
            assert shared.headers["x-content-type-options"] == "nosniff"

            revalidated = await http.get(shared_path, headers={"If-None-Match": shared.headers["etag"]})
            assert (revalidated.status_code, revalidated.headers["cache-control"]) == (304, NO_STORE)

    @pytest.mark.asyncio
    async def test_an_avatar_is_never_the_portrait(
        self, db: Session, async_db: AsyncSession, http: httpx.AsyncClient, sign_in: SignIn
    ) -> None:
        diver = create_user(db)
        await store_picture(
            async_db, user_id=diver.id, frame=AVATAR_FRAME, upload=_upload(plain_png(size=(32, 32)), "a.png"), crop=None
        )
        token = await _mint(http, sign_in, diver)

        summary = (await http.get(f"/api/v1/checkin/{token}")).json()
        portrait = await http.get(f"/api/v1/checkin/{token}/portrait")

        assert summary["diver"]["portrait_sha256"] is None
        assert _dead(portrait) == _dead(await http.get("/api/v1/checkin/never-minted/portrait"))

    @pytest.mark.asyncio
    async def test_only_the_divers_own_image_fronts_are_served(
        self, db: Session, async_db: AsyncSession, http: httpx.AsyncClient, sign_in: SignIn
    ) -> None:
        """A PDF front, a card with no front, a deleted card, another diver's card and a
        malformed uuid are all the dead link's 404."""
        diver, stranger = create_user(db), create_user(db)
        pdf_card = Certification(user_id=diver.id, agency="padi", name="Scanned")
        bare_card = Certification(user_id=diver.id, agency="padi", name="Bare")
        deleted_card = Certification(user_id=diver.id, agency="padi", name="Deleted", is_deleted=True)
        strangers_card = Certification(user_id=stranger.id, agency="padi", name="Theirs")
        db.add_all([pdf_card, bare_card, deleted_card, strangers_card])
        db.commit()
        image = plain_png(size=(9, 6))
        await store_certification_file(
            async_db, certification_id=pdf_card.id, side=CertificationSide.FRONT, upload=_upload(PDF_BYTES, "f.pdf")
        )
        await store_certification_file(
            async_db, certification_id=bare_card.id, side=CertificationSide.BACK, upload=_upload(image, "b.png")
        )
        for card in (deleted_card, strangers_card):
            await store_certification_file(
                async_db, certification_id=card.id, side=CertificationSide.FRONT, upload=_upload(image, "f.png")
            )
        token = await _mint(http, sign_in, diver)
        dead = _dead(await http.get("/api/v1/checkin/never-minted/portrait"))

        for card_uuid in (pdf_card.uuid, bare_card.uuid, deleted_card.uuid, strangers_card.uuid, "not-a-uuid"):
            assert _dead(await http.get(f"/api/v1/checkin/{token}/certification/{card_uuid}/front")) == dead
        assert (await http.get(f"/api/v1/checkin/{token}/certification/{bare_card.uuid}/back")).status_code == 404

        # A front that was an image when it was looked up and is a PDF by the time its bytes are
        # read is refused on what is actually about to be served.
        stale = CardFront(certification_id=pdf_card.id, sha256="0" * 64, content_type="image/png")
        with patch("src.app.api.v1.checkin_links.find_card_front", AsyncMock(return_value=stale)):
            replaced = await http.get(f"/api/v1/checkin/{token}/certification/{pdf_card.uuid}/front")
        assert _dead(replaced) == dead

    @pytest.mark.asyncio
    async def test_the_sweep_deletes_the_dead_and_keeps_the_live(self, db: Session) -> None:
        diver = create_user(db)
        now = datetime.now(UTC)

        def link(**stamps: Any) -> int:
            row = CheckinLink(
                user_id=diver.id,
                token_hash=hashlib.sha256(uuid7().bytes).hexdigest(),
                expires_at=stamps.pop("expires_at", now + timedelta(hours=1)),
                total_dives=None,
                max_depth=None,
                last_dive_on=None,
                **stamps,
            )
            db.add(row)
            db.commit()
            return row.id

        live = link()
        expired = link(expires_at=now - timedelta(seconds=1))
        revoked = link(revoked_at=now)

        await purge_expired_checkin_links({})

        remaining = set(db.execute(select(CheckinLink.id).where(CheckinLink.user_id == diver.id)).scalars())
        assert remaining == {live}
        assert expired not in remaining and revoked not in remaining
