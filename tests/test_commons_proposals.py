"""Matches, replacing, proposals and the queue, through the routes as three accounts.

A leak between accounts only shows as a second account reading, so `Sign` switches who the app
reads as signed in. Every pin is random and every registry entry unique: Commons rows outlive
their test, and a neighbour left by another would be a match here.

Automatically skipped if no database is reachable - see `test_dive_check_constraints.py`.
"""

import json
import random
import uuid as uuid_pkg
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import date
from typing import Any, cast
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from divejson import SPEC_VERSION
from divejson.validate import validate_document
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from src.app.api import router
from src.app.api.dependencies import get_current_user
from src.app.api.v1 import dive_sites as dive_sites_module
from src.app.core.config import settings
from src.app.core.db.database import async_get_db
from src.app.core.setup import create_application
from src.app.core.utils import cache as cache_module
from src.app.crud.crud_users import read_account
from src.app.models.dive import Dive
from src.app.models.dive_dive_site import DiveDiveSite
from src.app.models.dive_site import DiveSite
from src.app.models.dive_site_proposal import DiveSiteProposal
from src.app.models.dive_site_tag import DiveSiteTag
from src.app.models.saved_site import SavedSite
from src.app.models.tag import Tag
from src.app.models.user import User
from src.app.schemas.dive_site import DiveSiteProposalKind, DiveSiteReadInternal
from src.app.schemas.dive_site_proposal import ATTESTATIONS
from src.app.services import commons_proposals, commons_records
from src.app.services.commons_records import CURATED_SOURCES, CommonsBundle, sync_commons
from tests.conftest import db_available
from tests.helpers.fake_redis import FakeRedis
from tests.helpers.generators import (
    create_commons_site,
    create_curated_site,
    create_dive,
    create_dive_site,
    create_tag,
    create_user,
)
from tests.helpers.generators import save_dive_site as saved_row

pytestmark = [
    pytest.mark.skipif(not db_available(), reason="No database connection available"),
    pytest.mark.xdist_group("commons_proposals"),
]

NEW_SITE = ATTESTATIONS[DiveSiteProposalKind.NEW_SITE]
CHANGE = ATTESTATIONS[DiveSiteProposalKind.CHANGE]
CONTRIBUTION_CREDIT = (
    "[Data from OpenDiving Commons contributors, CC0 1.0.](https://github.com/opendiving/opendiving-commons)"
)


def _spot() -> tuple[float, float]:
    return round(random.uniform(-60, 60), 6), round(random.uniform(-170, 170), 6)


def _entry() -> dict[str, str]:
    return {"registry": "openstreetmap", "identifier": f"node/{random.randrange(10**11, 10**12)}"}


def _north(latitude: float, metres: float) -> float:
    return latitude + metres / 111_195


def _marked(label: str) -> str:
    return f"{label} {uuid_pkg.uuid4().hex[:10]}"


def _members(**changes: Any) -> dict[str, Any]:
    latitude, longitude = _spot()
    return {
        "name": _marked("Reviewed"),
        "other_names": [],
        "latitude": latitude,
        "longitude": longitude,
        "location_name": "Dahab, Egypt",
        "external_ids": [],
        "depth_from": None,
        "depth_to": None,
        "water_type": None,
        "altitude": None,
        "entry_types": [],
        **changes,
    }


def _own_site(db: Session, user: User, *, notes: str = "", **values: Any) -> DiveSite:
    latitude, longitude = _spot()
    site = DiveSite(
        user_id=user.id,
        name=values.pop("name", _marked("Own")),
        latitude=values.pop("latitude", latitude),
        longitude=values.pop("longitude", longitude),
        **values,
    )
    db.add(site)
    db.commit()
    db.refresh(site)
    saved_row(db, site, notes=notes)
    return site


def _record(db: Session, **values: Any) -> DiveSite:
    latitude, longitude = _spot()
    return create_commons_site(
        db, latitude=values.pop("latitude", latitude), longitude=values.pop("longitude", longitude), **values
    )


def _on_a_dive(db: Session, dive: Dive, *sites: DiveSite) -> None:
    for position, site in enumerate(sites):
        db.add(DiveDiveSite(dive_id=dive.id, dive_site_id=site.id, position=position))
    db.commit()


def _sites_of(db: Session, dive: Dive) -> list[int]:
    db.expire_all()
    return list(
        db.scalars(
            select(DiveDiveSite.dive_site_id).where(DiveDiveSite.dive_id == dive.id).order_by(DiveDiveSite.position)
        )
    )


def _tag(db: Session, site: DiveSite, tag: Tag, *, position: int = 0) -> None:
    db.add(DiveSiteTag(dive_site_id=site.id, tag_id=tag.id, position=position))
    db.commit()


def _row(db: Session, uuid: Any) -> DiveSite | None:
    db.expire_all()
    return db.scalars(select(DiveSite).where(DiveSite.uuid == uuid)).one_or_none()


def _proposal(db: Session, uuid: Any) -> DiveSiteProposal:
    db.expire_all()
    return db.scalars(select(DiveSiteProposal).where(DiveSiteProposal.uuid == uuid)).one()


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
def reviewer(db: Session) -> User:
    return create_user(db, is_super_user=True)


@pytest_asyncio.fixture
async def signed(
    proposals_app: Any,
    async_sessions: async_sessionmaker[AsyncSession],
    diver: User,
    other_diver: User,
    reviewer: User,
) -> AsyncGenerator[Sign]:
    async with async_sessions() as db:
        accounts = {
            user.id: cast(dict[str, Any], await read_account(db, uuid=user.uuid))
            for user in (diver, other_diver, reviewer)
        }
    sign = Sign(account=accounts[diver.id], accounts=accounts)
    proposals_app.dependency_overrides[get_current_user] = lambda: sign.account
    yield sign
    proposals_app.dependency_overrides = {}


@pytest_asyncio.fixture
async def http(
    proposals_app: Any,
    async_sessions: async_sessionmaker[AsyncSession],
    db: Session,
    signed: Sign,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[httpx.AsyncClient]:
    monkeypatch.setattr(cache_module, "client", FakeRedis())
    monkeypatch.setattr(settings, "COMMONS_PRIMARY", True)
    # The queue is every account's, and these rows outlive their test.
    db.execute(delete(DiveSiteProposal))
    db.commit()

    async def per_request() -> AsyncGenerator[AsyncSession]:
        async with async_sessions() as session:
            yield session

    proposals_app.dependency_overrides[async_get_db] = per_request
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proposals_app), base_url="http://test/api/v1"
    ) as client:
        yield client


async def _read(http: httpx.AsyncClient, uuid: Any) -> dict[str, Any]:
    response = await http.get(f"/dive-site/{uuid}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _propose_site(http: httpx.AsyncClient, site: DiveSite, **body: Any) -> dict[str, Any]:
    response = await http.post(f"/dive-site/{site.uuid}/proposals", json={"attestation": NEW_SITE, **body})
    assert response.status_code == 201, response.text
    proposal: dict[str, Any] = response.json()
    return proposal


async def _propose_change(http: httpx.AsyncClient, site: DiveSite, **members: Any) -> dict[str, Any]:
    response = await http.post(f"/dive-site/{site.uuid}/proposals", json={"attestation": CHANGE, "members": members})
    assert response.status_code == 201, response.text
    proposal: dict[str, Any] = response.json()
    return proposal


async def _queue(http: httpx.AsyncClient) -> list[dict[str, Any]]:
    response = await http.get("/admin/dive-site-proposals", params={"items_per_page": 100})
    assert response.status_code == 200, response.text
    rows: list[dict[str, Any]] = response.json()["data"]
    return rows


async def _accept(http: httpx.AsyncClient, proposal: dict[str, Any], **body: Any) -> httpx.Response:
    return await http.post(f"/admin/dive-site-proposal/{proposal['uuid']}/accept", json=body)


async def _accepted(http: httpx.AsyncClient, proposal: dict[str, Any], **body: Any) -> dict[str, Any]:
    response = await _accept(http, proposal, **body)
    assert response.status_code == 200, response.text
    read: dict[str, Any] = response.json()
    return read


async def _exported(http: httpx.AsyncClient, uuid: Any) -> dict[str, Any]:
    response = await http.get("/commons/dive-sites")
    assert response.status_code == 200, response.text
    return next(f for f in response.json()["features"] if f["properties"]["uuid"] == str(uuid))


class TestTheMatches:
    @pytest.mark.asyncio
    async def test_they_come_strongest_first_each_with_its_rule_and_distance(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        latitude, longitude = _spot()
        name = _marked("Canyon")
        entry = _entry()
        site = _own_site(db, diver, name=name, latitude=latitude, longitude=longitude, external_ids=[entry])
        far_copy = _record(db, external_ids=[entry])
        near = _record(db, latitude=_north(latitude, 15), longitude=longitude)
        similar = _record(db, name=f"{name} Wreck", latitude=_north(latitude, 150), longitude=longitude)
        same = _record(db, name=name, latitude=_north(latitude, 2_000), longitude=longitude)
        _record(db, latitude=_north(latitude, 60), longitude=longitude)

        response = await http.get(f"/dive-site/{site.uuid}/commons-matches")

        assert response.status_code == 200, response.text
        matches = response.json()
        assert [(match["uuid"], match["rule"]) for match in matches] == [
            (str(far_copy.uuid), "identity"),
            (str(near.uuid), "within_20_m"),
            (str(similar.uuid), "similar_name_within_200_m"),
            (str(same.uuid), "same_name_within_5_km"),
        ]
        assert matches[1]["distance_metres"] == pytest.approx(15, abs=0.2)
        assert matches[1]["attribution"] is not None

    @pytest.mark.asyncio
    async def test_a_site_with_no_pin_can_meet_identity_alone(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        entry = _entry()
        site = create_dive_site(db, diver)
        site.external_ids = [entry]
        db.commit()
        record = _record(db, external_ids=[entry])

        matches = (await http.get(f"/dive-site/{site.uuid}/commons-matches")).json()

        assert [(match["uuid"], match["distance_metres"]) for match in matches] == [(str(record.uuid), None)]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("mine", "theirs"),
        [
            ((10.0, 179.99995), (10.0, -179.99995)),
            ((10.0, -179.99995), (10.0, 179.99995)),
            ((89.9999, 0.0), (89.9999, 120.0)),
            ((-89.9999, -170.0), (-89.9999, -50.0)),
        ],
        ids=["east-of-the-antimeridian", "west-of-it", "north-pole", "south-pole"],
    )
    async def test_a_neighbour_twenty_metres_away_is_found_at_the_edges_of_the_map(
        self, http: httpx.AsyncClient, db: Session, diver: User, mine: tuple[float, float], theirs: tuple[float, float]
    ) -> None:
        site = _own_site(db, diver, latitude=mine[0], longitude=mine[1])
        record = _record(db, latitude=theirs[0], longitude=theirs[1])

        matches = (await http.get(f"/dive-site/{site.uuid}/commons-matches")).json()

        assert (str(record.uuid), "within_20_m") in [(match["uuid"], match["rule"]) for match in matches]

    @pytest.mark.asyncio
    async def test_it_answers_for_the_caller_s_own_site_alone(
        self, http: httpx.AsyncClient, db: Session, other_diver: User
    ) -> None:
        for uuid in (_own_site(db, other_diver).uuid, _record(db).uuid):
            assert (await http.get(f"/dive-site/{uuid}/commons-matches")).status_code == 404


class TestCommonsMatchOnTheRead:
    @pytest.mark.asyncio
    async def test_the_read_and_the_list_name_the_record_sharing_an_entry(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        entry = _entry()
        site = _own_site(db, diver, external_ids=[entry])
        record = _record(db, external_ids=[entry])
        expected = {"uuid": str(record.uuid), "name": record.name}

        assert (await _read(http, site.uuid))["commons_match"] == expected
        listed = (await http.get("/dive-sites", params={"search": site.name})).json()["data"]
        assert listed[0]["commons_match"] == expected

    @pytest.mark.asyncio
    async def test_creating_a_copy_says_so_at_once(self, http: httpx.AsyncClient, db: Session) -> None:
        entry = _entry()
        record = _record(db, external_ids=[entry])

        response = await http.post("/dive-site", json={"name": _marked("Copy"), "external_ids": [entry]})

        assert response.status_code == 201, response.text
        assert response.json()["commons_match"]["uuid"] == str(record.uuid)

    @pytest.mark.asyncio
    async def test_a_site_that_shares_nothing_and_a_saved_record_have_none(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        entry = _entry()
        record = _record(db, external_ids=[entry])
        saved_row(db, record, user=diver)

        assert (await _read(http, _own_site(db, diver).uuid))["commons_match"] is None
        assert (await _read(http, record.uuid))["commons_match"] is None

    @pytest.mark.asyncio
    async def test_the_filter_narrows_the_list_and_its_total_and_keeps_its_own_cache_entry(
        self, http: httpx.AsyncClient, db: Session, other_diver: User, signed: Sign
    ) -> None:
        signed.as_(other_diver)
        entry = _entry()
        copy = _own_site(db, other_diver, external_ids=[entry])
        _own_site(db, other_diver, external_ids=[_entry()])
        _own_site(db, other_diver)
        record = _record(db, external_ids=[entry])
        saved_row(db, record, user=other_diver)

        everything = (await http.get("/dive-sites", params={"items_per_page": 100})).json()
        narrowed = (await http.get("/dive-sites", params={"items_per_page": 100, "commons_match": True})).json()

        assert everything["total_count"] > 1
        assert [row["uuid"] for row in narrowed["data"]] == [str(copy.uuid)]
        assert narrowed["total_count"] == 1


class TestReplacing:
    @pytest.mark.asyncio
    async def test_dives_notes_tags_and_the_list_move_to_the_record_and_the_site_goes(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        site = _own_site(db, diver, notes="Mine alone")
        uuid = site.uuid
        other = _own_site(db, diver)
        record = _record(db)
        tag = create_tag(db, diver)
        _tag(db, site, tag)
        dive = create_dive(db, diver)
        _on_a_dive(db, dive, other, site)
        before = db.scalar(select(SavedSite.created_at).where(SavedSite.dive_site_id == site.id))

        response = await http.post(f"/dive-site/{uuid}/replace", json={"commons_site_uuid": str(record.uuid)})

        assert response.status_code == 200, response.text
        read = response.json()
        assert (read["uuid"], read["saved"], read["notes"]) == (str(record.uuid), True, "Mine alone")
        assert read["tags"] == [tag.name]
        assert read["dive_count"] == 1
        assert _sites_of(db, dive) == [other.id, record.id]
        assert _row(db, uuid) is None
        assert db.scalar(select(SavedSite.created_at).where(SavedSite.dive_site_id == record.id)) == before

    @pytest.mark.asyncio
    async def test_notes_join_those_on_the_record_and_tags_are_the_union(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        site = _own_site(db, diver, notes="From the copy")
        record = _record(db)
        saved_row(db, record, user=diver, notes="From the record")
        shared, on_the_record, on_the_site = (create_tag(db, diver) for _ in range(3))
        _tag(db, record, on_the_record, position=0)
        _tag(db, record, shared, position=1)
        _tag(db, site, shared, position=0)
        _tag(db, site, on_the_site, position=1)

        response = await http.post(f"/dive-site/{site.uuid}/replace", json={"commons_site_uuid": str(record.uuid)})

        read = response.json()
        assert read["notes"] == "From the record\n\nFrom the copy"
        assert read["tags"] == [on_the_record.name, shared.name, on_the_site.name]

    @pytest.mark.asyncio
    async def test_a_dive_naming_both_holds_the_record_once(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        site = _own_site(db, diver)
        record = _record(db)
        saved_row(db, record, user=diver)
        dive = create_dive(db, diver)
        _on_a_dive(db, dive, site, record)

        await http.post(f"/dive-site/{site.uuid}/replace", json={"commons_site_uuid": str(record.uuid)})

        assert _sites_of(db, dive) == [record.id]

    @pytest.mark.asyncio
    async def test_what_the_diver_read_before_is_not_served_after(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        site = _own_site(db, diver)
        record = _record(db)
        dive = create_dive(db, diver)
        _on_a_dive(db, dive, site)
        assert (await http.get(f"/dive/{dive.uuid}")).json()["dive_sites"][0]["uuid"] == str(site.uuid)
        await http.get("/dive-sites", params={"search": site.name})

        await http.post(f"/dive-site/{site.uuid}/replace", json={"commons_site_uuid": str(record.uuid)})

        assert (await http.get(f"/dive/{dive.uuid}")).json()["dive_sites"][0]["uuid"] == str(record.uuid)
        assert (await http.get("/dive-sites", params={"search": site.name})).json()["total_count"] == 0
        assert (await http.get(f"/dive-site/{site.uuid}")).status_code == 404

    @pytest.mark.asyncio
    async def test_a_waiting_proposal_goes_with_the_site(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        site = _own_site(db, diver)
        proposal = await _propose_site(http, site)

        await http.post(f"/dive-site/{site.uuid}/replace", json={"commons_site_uuid": str(_record(db).uuid)})

        assert db.scalar(select(DiveSiteProposal.id).where(DiveSiteProposal.uuid == proposal["uuid"])) is None

    @pytest.mark.asyncio
    async def test_it_takes_a_commons_record_and_the_caller_s_own_site(
        self, http: httpx.AsyncClient, db: Session, diver: User, other_diver: User
    ) -> None:
        site = _own_site(db, diver)
        record = _record(db)

        refused = await http.post(
            f"/dive-site/{site.uuid}/replace", json={"commons_site_uuid": str(_own_site(db, diver).uuid)}
        )
        theirs = await http.post(
            f"/dive-site/{_own_site(db, other_diver).uuid}/replace", json={"commons_site_uuid": str(record.uuid)}
        )
        a_record = await http.post(
            f"/dive-site/{_record(db).uuid}/replace", json={"commons_site_uuid": str(record.uuid)}
        )

        assert (refused.status_code, theirs.status_code, a_record.status_code) == (422, 404, 404)
        assert _row(db, site.uuid) is not None


class TestOnlyOnThePrimary:
    @pytest.mark.asyncio
    async def test_elsewhere_every_proposal_route_is_a_404(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign, monkeypatch: Any
    ) -> None:
        site = _own_site(db, diver)
        uuid = uuid_pkg.uuid4()
        monkeypatch.setattr(settings, "COMMONS_PRIMARY", False)

        answers = [
            await http.post(f"/dive-site/{site.uuid}/proposals", json={"attestation": NEW_SITE}),
            await http.get("/dive-site-proposals"),
            await http.delete(f"/dive-site-proposal/{uuid}"),
        ]
        signed.as_(reviewer)
        answers += [
            await http.get("/admin/dive-site-proposals"),
            await http.post(f"/admin/dive-site-proposal/{uuid}/accept", json={"members": _members()}),
            await http.post(f"/admin/dive-site-proposal/{uuid}/decline", json={"reason": "No"}),
        ]

        assert [answer.status_code for answer in answers] == [404] * 6

    @pytest.mark.asyncio
    async def test_matches_and_replacing_work_everywhere(
        self, http: httpx.AsyncClient, db: Session, diver: User, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(settings, "COMMONS_PRIMARY", False)
        site = _own_site(db, diver)

        assert (await http.get(f"/dive-site/{site.uuid}/commons-matches")).status_code == 200
        replaced = await http.post(f"/dive-site/{site.uuid}/replace", json={"commons_site_uuid": str(_record(db).uuid)})
        assert replaced.status_code == 200


class TestProposingANewSite:
    @pytest.mark.asyncio
    async def test_it_keeps_the_site_as_it_stands_with_the_attestation_and_the_time(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        site = _own_site(db, diver, notes="gate code 1234", location_name="Dahab", location_latitude=28.5)

        proposal = await _propose_site(http, site, message="Found on a liveaboard")

        assert (proposal["kind"], proposal["status"], proposal["reason"]) == ("new_site", "waiting", None)
        assert proposal["site"] == {"uuid": str(site.uuid), "name": site.name}
        row = _proposal(db, proposal["uuid"])
        assert (row.attestation, row.message) == (NEW_SITE, "Found on a liveaboard")
        assert row.attested_at is not None
        assert row.members["name"] == site.name
        assert row.members["location_name"] == "Dahab"
        assert set(row.members) == {
            "name",
            "other_names",
            "latitude",
            "longitude",
            "location_name",
            "external_ids",
            "depth_from",
            "depth_to",
            "water_type",
            "altitude",
            "entry_types",
        }

    @pytest.mark.asyncio
    async def test_the_read_says_it_waits_and_the_site_stays_private(
        self, http: httpx.AsyncClient, db: Session, diver: User, other_diver: User, signed: Sign
    ) -> None:
        site = _own_site(db, diver)
        before = await _read(http, site.uuid)

        proposal = await _propose_site(http, site)

        read = await _read(http, site.uuid)
        assert before["proposal"] is None
        assert (read["proposal"]["uuid"], read["proposal"]["status"]) == (proposal["uuid"], "waiting")
        assert read["owner"] == "account"
        signed.as_(other_diver)
        assert (await http.get(f"/dive-site/{site.uuid}")).status_code == 404

    @pytest.mark.asyncio
    async def test_a_site_with_no_pin_is_refused(self, http: httpx.AsyncClient, db: Session, diver: User) -> None:
        site = create_dive_site(db, diver)

        response = await http.post(f"/dive-site/{site.uuid}/proposals", json={"attestation": NEW_SITE})

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_a_copy_of_a_record_is_refused(self, http: httpx.AsyncClient, db: Session, diver: User) -> None:
        entry = _entry()
        site = _own_site(db, diver, external_ids=[entry])
        _record(db, external_ids=[entry])

        response = await http.post(f"/dive-site/{site.uuid}/proposals", json={"attestation": NEW_SITE})

        assert response.status_code == 409

    @pytest.mark.asyncio
    @pytest.mark.parametrize("attestation", ["", "I agree", CHANGE])
    async def test_any_other_attestation_is_refused(
        self, http: httpx.AsyncClient, db: Session, diver: User, attestation: str
    ) -> None:
        site = _own_site(db, diver)

        response = await http.post(f"/dive-site/{site.uuid}/proposals", json={"attestation": attestation})

        assert response.status_code == 422
        assert NEW_SITE in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_members_are_refused_on_the_diver_s_own_site(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        site = _own_site(db, diver)

        response = await http.post(
            f"/dive-site/{site.uuid}/proposals", json={"attestation": NEW_SITE, "members": {"name": "Other"}}
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_proposing_again_replaces_what_waits(self, http: httpx.AsyncClient, db: Session, diver: User) -> None:
        site = _own_site(db, diver)
        first = await _propose_site(http, site, message="First")
        patched = await http.patch(f"/dive-site/{site.uuid}", json={"name": _marked("Renamed")})
        assert patched.status_code == 200, patched.text

        second = await _propose_site(http, site, message="Second")

        assert second["uuid"] == first["uuid"]
        rows = db.scalars(select(DiveSiteProposal).where(DiveSiteProposal.dive_site_id == site.id)).all()
        assert [(row.message, row.members["name"]) for row in rows] == [("Second", second["site"]["name"])]

    @pytest.mark.asyncio
    async def test_an_account_is_bounded_per_day(
        self, http: httpx.AsyncClient, db: Session, diver: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "COMMONS_PROPOSAL_RATE_LIMIT_PER_USER", 1)
        await _propose_site(http, _own_site(db, diver))

        response = await http.post(f"/dive-site/{_own_site(db, diver).uuid}/proposals", json={"attestation": NEW_SITE})

        assert response.status_code == 429


class TestProposingAChange:
    @pytest.mark.asyncio
    async def test_it_keeps_what_the_diver_wants_different_and_the_record_stays(
        self, http: httpx.AsyncClient, db: Session, diver: User, other_diver: User, signed: Sign
    ) -> None:
        record = _record(db, depth_to=30.0)

        proposal = await _propose_change(http, record, depth_to=40.0, name=record.name)

        assert proposal["kind"] == "change"
        assert _proposal(db, proposal["uuid"]).members == {"depth_to": 40.0}
        assert _proposal(db, proposal["uuid"]).attestation == CHANGE
        read = await _read(http, record.uuid)
        assert (read["depth_to"], read["proposal"]["status"], read["saved"]) == (30.0, "waiting", False)
        signed.as_(other_diver)
        read = await _read(http, record.uuid)
        assert (read["depth_to"], read["proposal"]) == (30.0, None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "members",
        [
            None,
            {},
            {"latitude": None, "longitude": None},
            {"depth_from": 50.0},
            {"depth_to": 30.0},
        ],
        ids=["no-members", "empty", "clears-the-pin", "past-the-stored-end", "changes-nothing"],
    )
    async def test_a_change_that_cannot_stand_is_refused(
        self, http: httpx.AsyncClient, db: Session, members: dict[str, Any] | None
    ) -> None:
        record = _record(db, depth_to=30.0)

        response = await http.post(
            f"/dive-site/{record.uuid}/proposals", json={"attestation": CHANGE, "members": members}
        )

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_a_seed_s_own_entry_cannot_be_changed_or_removed(self, http: httpx.AsyncClient, db: Session) -> None:
        record = _record(db)

        for external_ids in ([], [_entry()]):
            response = await http.post(
                f"/dive-site/{record.uuid}/proposals",
                json={"attestation": CHANGE, "members": {"external_ids": external_ids}},
            )

            assert response.status_code == 422
            assert "seeded from" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_another_account_s_private_site_is_a_404(
        self, http: httpx.AsyncClient, db: Session, other_diver: User
    ) -> None:
        site = _own_site(db, other_diver)

        response = await http.post(f"/dive-site/{site.uuid}/proposals", json={"attestation": NEW_SITE})

        assert response.status_code == 404


class TestTheCallersProposals:
    @pytest.mark.asyncio
    async def test_they_list_their_own_alone_newest_first(
        self, http: httpx.AsyncClient, db: Session, diver: User, other_diver: User, signed: Sign
    ) -> None:
        first = await _propose_site(http, _own_site(db, diver))
        second = await _propose_change(http, _record(db), depth_to=12.0)
        signed.as_(other_diver)
        await _propose_site(http, _own_site(db, other_diver))
        signed.as_(diver)

        body = (await http.get("/dive-site-proposals")).json()

        assert [row["uuid"] for row in body["data"]] == [second["uuid"], first["uuid"]]
        assert body["total_count"] == 2

    @pytest.mark.asyncio
    async def test_withdrawing_stops_it_waiting_and_keeps_it_on_the_list(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        site = _own_site(db, diver)
        proposal = await _propose_site(http, site)
        await _read(http, site.uuid)

        response = await http.delete(f"/dive-site-proposal/{proposal['uuid']}")

        assert response.status_code == 200, response.text
        assert (await _read(http, site.uuid))["proposal"] is None
        assert (await http.get("/dive-site-proposals")).json()["data"][0]["status"] == "withdrawn"
        assert (await http.delete(f"/dive-site-proposal/{proposal['uuid']}")).status_code == 409

    @pytest.mark.asyncio
    async def test_another_account_cannot_withdraw_it_or_learn_it_exists(
        self, http: httpx.AsyncClient, db: Session, diver: User, other_diver: User, signed: Sign
    ) -> None:
        proposal = await _propose_site(http, _own_site(db, diver))
        signed.as_(other_diver)

        assert (await http.delete(f"/dive-site-proposal/{proposal['uuid']}")).status_code == 404


class TestWhatDeletingTakes:
    @pytest.mark.asyncio
    async def test_deleting_a_private_site_deletes_the_proposals_on_it(
        self, http: httpx.AsyncClient, db: Session, diver: User
    ) -> None:
        site = _own_site(db, diver)
        await _propose_site(http, site)
        site_id = site.id

        assert (await http.delete(f"/dive-site/{site.uuid}")).status_code == 200

        db.expire_all()
        assert db.scalar(select(DiveSiteProposal.id).where(DiveSiteProposal.dive_site_id == site_id)) is None

    @pytest.mark.asyncio
    async def test_deleting_an_account_deletes_its_proposals_and_an_accepted_record_stays_marked(
        self, http: httpx.AsyncClient, db: Session, reviewer: User, signed: Sign
    ) -> None:
        leaving = create_user(db)
        signed.accounts[leaving.id] = {**signed.accounts[reviewer.id], "id": leaving.id, "is_superuser": False}
        site = _own_site(db, leaving)
        signed.as_(leaving)
        proposal = await _propose_site(http, site)
        waiting = await _propose_change(http, _record(db), depth_to=9.0)
        signed.as_(reviewer)
        await _accepted(http, proposal, members=_members())
        uuid = site.uuid

        db.execute(delete(User).where(User.id == leaving.id))
        db.commit()

        assert db.scalar(select(DiveSiteProposal.id).where(DiveSiteProposal.uuid == proposal["uuid"])) is None
        assert db.scalar(select(DiveSiteProposal.id).where(DiveSiteProposal.uuid == waiting["uuid"])) is None
        row = _row(db, uuid)
        assert row is not None and row.user_id is None and row.commons_reviewed_at is not None


class TestAnAccountBeingDeleted:
    """The account has gone dark and cannot withdraw, and a fortnight passes before the purge."""

    def _flagged(self, db: Session, diver: User, *, deleted: bool) -> None:
        db.execute(update(User).where(User.id == diver.id).values(is_deleted=deleted))
        db.commit()

    @pytest.mark.asyncio
    async def test_its_proposals_leave_the_queue_and_cannot_be_decided_and_a_restore_brings_them_back(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        site = _own_site(db, diver)
        proposal = await _propose_site(http, site)
        signed.as_(reviewer)
        self._flagged(db, diver, deleted=True)

        body = (await http.get("/admin/dive-site-proposals")).json()
        accepted = await _accept(http, proposal, members=_members())
        declined = await http.post(f"/admin/dive-site-proposal/{proposal['uuid']}/decline", json={"reason": "No"})

        assert (body["data"], body["total_count"]) == ([], 0)
        assert (accepted.status_code, declined.status_code) == (404, 404)
        row = _row(db, site.uuid)
        assert row is not None and row.user_id == diver.id

        self._flagged(db, diver, deleted=False)

        assert [row["uuid"] for row in await _queue(http)] == [proposal["uuid"]]


class TestTheQueue:
    @pytest.mark.asyncio
    async def test_it_is_a_superuser_s(self, http: httpx.AsyncClient, db: Session, diver: User) -> None:
        proposal = await _propose_site(http, _own_site(db, diver))

        answers = [
            await http.get("/admin/dive-site-proposals"),
            await _accept(http, proposal, members=_members()),
            await http.post(f"/admin/dive-site-proposal/{proposal['uuid']}/decline", json={"reason": "No"}),
        ]

        assert [answer.status_code for answer in answers] == [403] * 3

    @pytest.mark.asyncio
    async def test_a_row_shows_what_a_record_would_carry_and_nothing_of_the_diver_s_own(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        latitude, longitude = _spot()
        site = _own_site(
            db,
            diver,
            notes="gate code 1234",
            latitude=latitude,
            longitude=longitude,
            location_name="Dahab",
            location_latitude=28.49,
            location_longitude=34.51,
        )
        _tag(db, site, create_tag(db, diver, name=_marked("secret-tag")))
        neighbour = _record(db, latitude=_north(latitude, 15), longitude=longitude)
        proposal = await _propose_site(http, site, message="Worth a look")
        signed.as_(reviewer)

        (row,) = await _queue(http)

        assert row["uuid"] == proposal["uuid"]
        assert row["site_uuid"] == str(site.uuid)
        assert row["message"] == "Worth a look"
        assert row["proposer"] == {"name": diver.name, "accepted_count": 0, "declined_count": 0}
        assert row["current"]["name"] == row["proposed"]["name"] == site.name
        assert row["current"]["location_name"] == "Dahab"
        assert [match["uuid"] for match in row["matches"]] == [str(neighbour.uuid)]
        text_of_it = json.dumps(row)
        for private in ("gate code 1234", "secret-tag", "28.49", "34.51", diver.email, diver.username):
            assert private not in text_of_it

    @pytest.mark.asyncio
    async def test_a_change_shows_the_record_beside_it_and_never_the_record_as_its_own_match(
        self, http: httpx.AsyncClient, db: Session, reviewer: User, signed: Sign
    ) -> None:
        record = _record(db, depth_to=30.0)
        await _propose_change(http, record, depth_to=40.0)
        signed.as_(reviewer)

        (row,) = await _queue(http)

        assert (row["kind"], row["changed"]) == ("change", ["depth_to"])
        assert (row["current"]["depth_to"], row["proposed"]["depth_to"]) == (30.0, 40.0)
        assert row["proposed"]["name"] == record.name
        assert row["matches"] == []

    @pytest.mark.asyncio
    async def test_it_runs_oldest_first_holds_only_what_waits_and_counts_the_proposer_s_record(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        accepted = await _propose_change(http, _record(db), depth_to=1.0)
        declined = await _propose_change(http, _record(db), depth_to=2.0)
        withdrawn = await _propose_change(http, _record(db), depth_to=3.0)
        await http.delete(f"/dive-site-proposal/{withdrawn['uuid']}")
        older = await _propose_change(http, _record(db), depth_to=4.0)
        newer = await _propose_change(http, _record(db), depth_to=5.0)
        signed.as_(reviewer)
        site = _proposal(db, accepted["uuid"]).dive_site_id
        record = db.scalars(select(DiveSite).where(DiveSite.id == site)).one()
        await _accepted(
            http,
            accepted,
            members=_members(
                name=record.name, latitude=record.latitude, longitude=record.longitude, external_ids=record.external_ids
            ),
        )
        await http.post(f"/admin/dive-site-proposal/{declined['uuid']}/decline", json={"reason": "No"})

        rows = await _queue(http)

        assert [row["uuid"] for row in rows] == [older["uuid"], newer["uuid"]]
        assert rows[0]["proposer"] == {"name": diver.name, "accepted_count": 1, "declined_count": 1}
        assert (await http.get("/admin/dive-site-proposals")).json()["total_count"] == 2


class TestAcceptingANewSite:
    @pytest.mark.asyncio
    async def test_the_row_changes_hands_and_keeps_its_uuid_the_dives_and_what_is_the_proposer_s(
        self, http: httpx.AsyncClient, db: Session, diver: User, other_diver: User, reviewer: User, signed: Sign
    ) -> None:
        site = _own_site(
            db,
            diver,
            notes="gate code 1234",
            location_name="Dahab",
            location_latitude=28.49,
            location_longitude=34.51,
            location_bbox_south=28.4,
            location_bbox_north=28.6,
            location_bbox_west=34.4,
            location_bbox_east=34.6,
        )
        created_at = site.created_at
        tag = create_tag(db, diver)
        _tag(db, site, tag)
        dive = create_dive(db, diver)
        _on_a_dive(db, dive, site)
        proposal = await _propose_site(http, site)
        await http.get(f"/dive/{dive.uuid}")
        members = _members(depth_to=18.0, entry_types=["shore"], water_type="salt")
        signed.as_(reviewer)

        read = await _accepted(http, proposal, members=members)

        assert read["status"] == "accepted"
        assert read["site"] == {"uuid": str(site.uuid), "name": members["name"]}
        row = _row(db, site.uuid)
        assert row is not None
        assert (row.user_id, row.commons_source, row.commons_license) == (None, "contribution", "CC0-1.0")
        assert row.commons_reviewed_at is not None
        assert row.created_at > created_at
        assert row.location_name == "Dahab, Egypt"
        assert (row.location_latitude, row.location_longitude, row.location_bbox_south) == (None, None, None)

        signed.as_(diver)
        mine = await _read(http, site.uuid)
        assert (mine["owner"], mine["saved"], mine["notes"], mine["tags"]) == (
            "opendivingcommons",
            True,
            "gate code 1234",
            [tag.name],
        )
        assert mine["attribution"] == CONTRIBUTION_CREDIT
        assert (mine["name"], mine["dive_count"], mine["proposal"]) == (members["name"], 1, None)
        assert (await http.get(f"/dive/{dive.uuid}")).json()["dive_sites"][0]["name"] == members["name"]

        signed.as_(other_diver)
        theirs = await _read(http, site.uuid)
        assert (theirs["saved"], theirs["notes"], theirs["tags"], theirs["dive_count"]) == (False, "", [], 0)
        assert theirs["created_at"] != mine["created_at"]
        assert theirs["location"] == {
            "name": "Dahab, Egypt",
            "latitude": None,
            "longitude": None,
            "bbox_south": None,
            "bbox_north": None,
            "bbox_west": None,
            "bbox_east": None,
        }

    @pytest.mark.asyncio
    async def test_what_is_published_is_what_the_reviewer_sent_whatever_the_diver_edited_meanwhile(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        site = _own_site(db, diver)
        proposal = await _propose_site(http, site)
        await http.patch(
            f"/dive-site/{site.uuid}", json={"name": _marked("Edited"), "other_names": ["Meanwhile"], "depth_to": 99.0}
        )
        members = _members()
        signed.as_(reviewer)

        await _accepted(http, proposal, members=members)

        properties = (await _exported(http, site.uuid))["properties"]
        assert properties["name"] == members["name"]
        assert "other_names" not in properties
        assert "depth_to" not in properties
        assert properties["extensions"]["opendivingcommons"] == {"source": "contribution", "license": "CC0-1.0"}

    @pytest.mark.asyncio
    async def test_the_published_record_is_valid_divejson_and_carries_nothing_of_the_diver_s_own(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        site = _own_site(db, diver, notes="gate code 1234")
        proposal = await _propose_site(http, site, message="A private word")
        signed.as_(reviewer)

        await _accepted(
            http,
            proposal,
            members=_members(name=_marked("Published"), other_names=["Elsewhere"], depth_from=3.0, depth_to=18.0),
        )

        feature = await _exported(http, site.uuid)
        longitude, latitude = feature["geometry"]["coordinates"]
        record = {**feature["properties"], "position": {"latitude": latitude, "longitude": longitude}}
        document = {
            "format": "divejson",
            "version": SPEC_VERSION,
            "exported_at": "2026-01-01T00:00:00Z",
            "sites": [record],
        }
        assert validate_document(document) == []
        text_of_it = json.dumps(feature)
        for private in ("gate code 1234", "A private word", "commons_reviewed_at"):
            assert private not in text_of_it

    @pytest.mark.asyncio
    async def test_a_pin_typed_with_more_decimals_is_rounded_to_six(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        site = _own_site(db, diver)
        proposal = await _propose_site(http, site)
        signed.as_(reviewer)

        await _accepted(http, proposal, members=_members(latitude=27.81409249, longitude=33.92004851))

        assert (await _exported(http, site.uuid))["geometry"]["coordinates"] == [33.920049, 27.814092]


class TestAPatchRacingAnAcceptance:
    @pytest.mark.asyncio
    async def test_a_patch_that_read_the_row_before_the_acceptance_writes_nothing_to_the_record(
        self,
        http: httpx.AsyncClient,
        db: Session,
        diver: User,
        reviewer: User,
        signed: Sign,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        site = _own_site(db, diver)
        uuid = site.uuid
        stale = DiveSiteReadInternal.model_validate(site, from_attributes=True)
        proposal = await _propose_site(http, site)
        members = _members()
        signed.as_(reviewer)
        await _accepted(http, proposal, members=members)
        signed.as_(diver)
        monkeypatch.setattr(dive_sites_module, "_get_readable_dive_site", AsyncMock(return_value=stale))

        response = await http.patch(f"/dive-site/{uuid}", json={"name": _marked("Too late")})

        assert response.status_code == 403
        row = _row(db, uuid)
        assert row is not None and row.name == members["name"]


class TestTheRecordRules:
    async def _refusal(self, http: httpx.AsyncClient, proposal: dict[str, Any], **changes: Any) -> str:
        response = await _accept(http, proposal, members=_members(**changes))
        assert response.status_code == 422, response.text
        detail: str = response.json()["detail"]
        return detail

    async def _proposed(self, http: httpx.AsyncClient, db: Session, diver: User, signed: Sign, to: User) -> Any:
        site = _own_site(db, diver)
        proposal = await _propose_site(http, site)
        signed.as_(to)
        return site, proposal

    @pytest.mark.asyncio
    async def test_a_pin_is_required(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        _, proposal = await self._proposed(http, db, diver, signed, reviewer)

        assert "keeps its pin" in await self._refusal(http, proposal, latitude=None, longitude=None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("latitude", "longitude"), [(0.0, 0.0), (0.0000001, -0.0000002)], ids=["typed", "rounded"])
    async def test_a_pin_at_nought_nought_is_refused_after_rounding(
        self,
        http: httpx.AsyncClient,
        db: Session,
        diver: User,
        reviewer: User,
        signed: Sign,
        latitude: float,
        longitude: float,
    ) -> None:
        _, proposal = await self._proposed(http, db, diver, signed, reviewer)

        assert "0, 0" in await self._refusal(http, proposal, latitude=latitude, longitude=longitude)

    @pytest.mark.asyncio
    async def test_a_registry_entry_another_record_carries_is_refused_naming_it(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        entry = _entry()
        record = _record(db, external_ids=[entry])
        _, proposal = await self._proposed(http, db, diver, signed, reviewer)

        detail = await self._refusal(http, proposal, external_ids=[entry])

        assert entry["identifier"] in detail
        assert record.name in detail

    @pytest.mark.asyncio
    async def test_an_entry_repeated_in_the_body_is_kept_once(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        site, proposal = await self._proposed(http, db, diver, signed, reviewer)
        entry = _entry()

        await _accepted(http, proposal, members=_members(external_ids=[entry, entry]))

        row = _row(db, site.uuid)
        assert row is not None and row.external_ids == [entry]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("registry", sorted(CURATED_SOURCES))
    async def test_an_entry_naming_a_curated_source_is_refused_by_name(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign, registry: str
    ) -> None:
        _, proposal = await self._proposed(http, db, diver, signed, reviewer)

        detail = await self._refusal(http, proposal, external_ids=[{"registry": registry, "identifier": "french-reef"}])

        assert registry in detail and "curated source" in detail

    @pytest.mark.asyncio
    async def test_a_record_divejson_would_refuse_is_refused(
        self,
        http: httpx.AsyncClient,
        db: Session,
        diver: User,
        reviewer: User,
        signed: Sign,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Nothing the body's own schema lets through breaks the format today, so the format's
        verdict is stood in for: this pins that it is asked, and that it decides."""
        _, proposal = await self._proposed(http, db, diver, signed, reviewer)
        monkeypatch.setattr(commons_proposals, "validate_document", lambda document: ["sites/0/name: too long"])

        assert "DiveJSON: sites/0/name: too long" in await self._refusal(http, proposal)

    @pytest.mark.asyncio
    async def test_an_entry_naming_the_record_itself_is_refused(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        site, proposal = await self._proposed(http, db, diver, signed, reviewer)

        detail = await self._refusal(
            http, proposal, external_ids=[{"registry": "opendivingcommons", "identifier": str(site.uuid)}]
        )

        assert "own uuid" in detail

    @pytest.mark.asyncio
    async def test_an_entry_naming_a_record_that_exists_is_refused(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        record = _record(db)
        _, proposal = await self._proposed(http, db, diver, signed, reviewer)

        detail = await self._refusal(
            http, proposal, external_ids=[{"registry": "opendivingcommons", "identifier": str(record.uuid)}]
        )

        assert record.name in detail

    @pytest.mark.asyncio
    async def test_an_entry_naming_a_record_that_does_not_exist_is_kept(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        """What a merge leaves behind, and the repository's rules allow it."""
        _, proposal = await self._proposed(http, db, diver, signed, reviewer)
        entry = {"registry": "opendivingcommons", "identifier": str(uuid_pkg.uuid4())}

        await _accepted(http, proposal, members=_members(external_ids=[entry]))

    @pytest.mark.asyncio
    async def test_a_member_left_out_is_a_refusal_and_not_a_value_cleared(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        site, proposal = await self._proposed(http, db, diver, signed, reviewer)
        members = _members()
        del members["depth_to"]

        response = await _accept(http, proposal, members=members)

        assert response.status_code == 422
        row = _row(db, site.uuid)
        assert row is not None and row.user_id is not None

    @pytest.mark.asyncio
    async def test_a_refused_body_changes_nothing(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        site, proposal = await self._proposed(http, db, diver, signed, reviewer)

        await self._refusal(http, proposal, latitude=0.0, longitude=0.0)

        row = _row(db, site.uuid)
        assert row is not None and (row.user_id, row.commons_reviewed_at) == (diver.id, None)
        assert _proposal(db, proposal["uuid"]).status == "waiting"


class TestAnsweringAReportOnASeed:
    """A report against a record is answered by a reviewer proposing a change and accepting it."""

    @pytest.mark.asyncio
    async def test_a_superuser_coarsens_the_pin_and_renames_a_seed_and_its_provenance_stands(
        self, http: httpx.AsyncClient, db: Session, reviewer: User, signed: Sign
    ) -> None:
        record = _record(db)
        seed = (record.commons_seed_registry, record.commons_seed_identifier)
        external_ids = record.external_ids
        signed.as_(reviewer)
        proposal = await _propose_change(http, record, latitude=28.5, longitude=34.5, name="Coarsened")

        await _accepted(
            http,
            proposal,
            members=_members(name="Coarsened", latitude=28.5, longitude=34.5, external_ids=external_ids),
        )

        row = _row(db, record.uuid)
        assert row is not None
        assert (row.name, row.latitude, row.longitude) == ("Coarsened", 28.5, 34.5)
        assert (row.commons_source, row.commons_license) == ("openstreetmap", "ODbL-1.0")
        assert (row.commons_seed_registry, row.commons_seed_identifier) == seed
        assert row.commons_retrieved == date(2026, 10, 17)
        assert row.commons_reviewed_at is not None

    @pytest.mark.asyncio
    async def test_a_curated_record_takes_a_change_with_no_entry_and_its_provenance_stands(
        self, http: httpx.AsyncClient, db: Session, reviewer: User, signed: Sign
    ) -> None:
        record = create_curated_site(db)
        seed = (record.commons_seed_registry, record.commons_seed_identifier)
        signed.as_(reviewer)
        proposal = await _propose_change(http, record, depth_to=12.0)

        members = _members(depth_to=12.0)

        await _accepted(http, proposal, members=members)

        row = _row(db, record.uuid)
        assert row is not None
        assert (row.name, row.depth_to, row.external_ids) == (members["name"], 12.0, [])
        assert (row.commons_source, row.commons_license) == ("noaa-florida-keys-moorings", "LicenseRef-US-Gov-PD")
        assert (row.commons_seed_registry, row.commons_seed_identifier) == seed

    @pytest.mark.asyncio
    async def test_a_curated_record_is_not_handed_an_entry_of_its_own_source(
        self, http: httpx.AsyncClient, db: Session, reviewer: User, signed: Sign
    ) -> None:
        record = create_curated_site(db)
        entry = {"registry": record.commons_seed_registry, "identifier": record.commons_seed_identifier}
        signed.as_(reviewer)
        proposal = await _propose_change(http, record, depth_to=12.0)

        response = await _accept(http, proposal, members=_members(external_ids=[entry]))

        assert response.status_code == 422
        assert "curated source" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_an_acceptance_cannot_take_a_seed_s_own_entry(
        self, http: httpx.AsyncClient, db: Session, reviewer: User, signed: Sign
    ) -> None:
        record = _record(db)
        signed.as_(reviewer)
        proposal = await _propose_change(http, record, depth_to=8.0)

        response = await _accept(http, proposal, members=_members(external_ids=[]))

        assert response.status_code == 422
        assert "seeded from" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_every_account_s_read_and_every_saver_s_list_and_dives_are_dropped(
        self, http: httpx.AsyncClient, db: Session, diver: User, other_diver: User, reviewer: User, signed: Sign
    ) -> None:
        record = _record(db, depth_to=30.0)
        saved_row(db, record, user=diver)
        dive = create_dive(db, diver)
        _on_a_dive(db, dive, record)
        members = _members(external_ids=record.external_ids, depth_to=40.0)
        proposal = await _propose_change(http, record, depth_to=40.0)
        await _read(http, record.uuid)
        await http.get(f"/dive/{dive.uuid}")
        await http.get("/dive-sites", params={"search": record.name})
        signed.as_(other_diver)
        await _read(http, record.uuid)
        signed.as_(reviewer)

        await _accepted(http, proposal, members=members)

        signed.as_(other_diver)
        assert (await _read(http, record.uuid))["depth_to"] == 40.0
        signed.as_(diver)
        assert (await _read(http, record.uuid))["name"] == members["name"]
        assert (await http.get(f"/dive/{dive.uuid}")).json()["dive_sites"][0]["name"] == members["name"]
        assert (await http.get("/dive-sites", params={"search": record.name})).json()["total_count"] == 0


class TestAcceptingIntoARecord:
    @pytest.mark.asyncio
    async def test_the_site_is_replaced_and_the_proposer_still_reads_the_outcome(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        site = _own_site(db, diver, notes="My note")
        tag = create_tag(db, diver)
        _tag(db, site, tag)
        dive = create_dive(db, diver)
        _on_a_dive(db, dive, site)
        record = _record(db)
        proposal = await _propose_site(http, site)
        assert (await http.get(f"/dive/{dive.uuid}")).json()["dive_sites"][0]["uuid"] == str(site.uuid)
        signed.as_(reviewer)

        read = await _accepted(http, proposal, into=str(record.uuid))

        assert (read["status"], read["site"]["uuid"]) == ("accepted", str(record.uuid))
        assert _row(db, site.uuid) is None
        assert _sites_of(db, dive) == [record.id]
        signed.as_(diver)
        mine = await _read(http, record.uuid)
        assert (mine["saved"], mine["notes"], mine["tags"], mine["dive_count"]) == (True, "My note", [tag.name], 1)
        assert (await http.get(f"/dive/{dive.uuid}")).json()["dive_sites"][0]["uuid"] == str(record.uuid)
        (listed,) = (await http.get("/dive-site-proposals")).json()["data"]
        assert (listed["uuid"], listed["status"], listed["site"]["name"]) == (
            proposal["uuid"],
            "accepted",
            record.name,
        )

    @pytest.mark.asyncio
    async def test_the_body_may_change_the_record(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        record = _record(db)
        proposal = await _propose_site(http, _own_site(db, diver))
        signed.as_(reviewer)

        await _accepted(
            http, proposal, into=str(record.uuid), members=_members(name="Better", external_ids=record.external_ids)
        )

        row = _row(db, record.uuid)
        assert row is not None and row.name == "Better" and row.commons_reviewed_at is not None

    @pytest.mark.asyncio
    async def test_without_a_body_the_record_is_not_marked(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        record = _record(db)
        proposal = await _propose_site(http, _own_site(db, diver))
        signed.as_(reviewer)

        await _accepted(http, proposal, into=str(record.uuid))

        row = _row(db, record.uuid)
        assert row is not None and row.commons_reviewed_at is None

    @pytest.mark.asyncio
    async def test_only_a_new_site_folds_and_only_into_a_commons_record(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        change = await _propose_change(http, _record(db), depth_to=7.0)
        site = _own_site(db, diver)
        proposal = await _propose_site(http, site)
        signed.as_(reviewer)

        answers = [
            await _accept(http, change, into=str(_record(db).uuid)),
            await _accept(http, proposal, into=str(_own_site(db, diver).uuid)),
            await _accept(http, proposal, into=str(uuid_pkg.uuid4())),
            await _accept(http, proposal),
        ]

        assert [answer.status_code for answer in answers] == [422] * 4
        assert _row(db, site.uuid) is not None


class TestDeclining:
    @pytest.mark.asyncio
    async def test_the_proposer_reads_the_reason_until_they_propose_again(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        site = _own_site(db, diver)
        proposal = await _propose_site(http, site)
        await _read(http, site.uuid)
        signed.as_(reviewer)

        response = await http.post(
            f"/admin/dive-site-proposal/{proposal['uuid']}/decline", json={"reason": "  Pin is on land  "}
        )

        assert response.status_code == 200, response.text
        assert (response.json()["status"], response.json()["reason"]) == ("declined", "Pin is on land")
        signed.as_(diver)
        read = await _read(http, site.uuid)
        assert (read["proposal"]["status"], read["proposal"]["reason"]) == ("declined", "Pin is on land")
        assert read["owner"] == "account"
        listed = (await http.get("/dive-site-proposals")).json()["data"]
        assert listed[0]["reason"] == "Pin is on land"

        again = await _propose_site(http, site)

        assert again["uuid"] != proposal["uuid"]
        read = await _read(http, site.uuid)
        assert (read["proposal"]["uuid"], read["proposal"]["reason"]) == (again["uuid"], None)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "   "}], ids=["none", "empty", "blank"])
    async def test_a_reason_is_required(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign, body: dict[str, Any]
    ) -> None:
        proposal = await _propose_site(http, _own_site(db, diver))
        signed.as_(reviewer)

        response = await http.post(f"/admin/dive-site-proposal/{proposal['uuid']}/decline", json=body)

        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_a_decided_proposal_cannot_be_decided_again(
        self, http: httpx.AsyncClient, db: Session, diver: User, reviewer: User, signed: Sign
    ) -> None:
        proposal = await _propose_site(http, _own_site(db, diver))
        signed.as_(reviewer)
        decline = f"/admin/dive-site-proposal/{proposal['uuid']}/decline"
        await http.post(decline, json={"reason": "No"})

        answers = [
            await http.post(decline, json={"reason": "Still no"}),
            await _accept(http, proposal, members=_members()),
        ]

        assert [answer.status_code for answer in answers] == [409, 409]

    @pytest.mark.asyncio
    async def test_no_such_proposal_is_a_404(self, http: httpx.AsyncClient, reviewer: User, signed: Sign) -> None:
        signed.as_(reviewer)

        response = await http.post(f"/admin/dive-site-proposal/{uuid_pkg.uuid4()}/decline", json={"reason": "No"})

        assert response.status_code == 404


class TestTheSyncGuard:
    @pytest_asyncio.fixture
    async def sync_engine(self, monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[Any]:
        """The sync opens connections of its own, so it gets an engine of this test's event loop."""
        engine = create_async_engine(settings.POSTGRES_ASYNC_PREFIX + settings.POSTGRES_URI.get_secret_value())
        monkeypatch.setattr(commons_records, "engine", engine)
        yield engine
        await engine.dispose()

    async def _sync(self, monkeypatch: pytest.MonkeyPatch, record: DiveSite, *, name: str) -> None:
        columns = {
            "uuid": record.uuid,
            "name": name,
            "other_names": [],
            "latitude": record.latitude,
            "longitude": record.longitude,
            "location_name": record.location_name,
            "external_ids": record.external_ids,
            "depth_from": None,
            "depth_to": None,
            "water_type": None,
            "altitude": None,
            "entry_types": [],
            "commons_source": record.commons_source,
            "commons_license": record.commons_license,
            "commons_seed_registry": record.commons_seed_registry,
            "commons_seed_identifier": record.commons_seed_identifier,
            "commons_retrieved": record.commons_retrieved,
        }
        bundle = CommonsBundle(
            commit="f" * 40,
            licence="ODbL 1.0",
            licence_url="https://opendatacommons.org/licenses/odbl/1-0/",
            notice="The notice",
            attributions={"openstreetmap": "[Credit](https://example.org)"},
            records=(columns,),
        )
        monkeypatch.setattr(commons_records, "_loaded", bundle)
        await sync_commons()

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("sync_engine")
    @pytest.mark.parametrize("primary", [True, False], ids=["primary", "setting-lost"])
    async def test_a_row_a_review_wrote_stays_what_the_review_wrote(
        self,
        http: httpx.AsyncClient,
        db: Session,
        reviewer: User,
        signed: Sign,
        monkeypatch: pytest.MonkeyPatch,
        primary: bool,
    ) -> None:
        record = _record(db)
        signed.as_(reviewer)
        proposal = await _propose_change(http, record, depth_to=11.0)
        members = _members(external_ids=record.external_ids, depth_to=11.0)
        await _accepted(http, proposal, members=members)
        monkeypatch.setattr(settings, "COMMONS_PRIMARY", primary)
        db.expire_all()

        await self._sync(monkeypatch, record, name="What the bundle says")

        row = _row(db, record.uuid)
        assert row is not None and (row.name, row.depth_to) == (members["name"], 11.0)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("sync_engine")
    async def test_a_row_no_review_wrote_goes_on_updating_on_a_copy(
        self, db: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "COMMONS_PRIMARY", False)
        record = _record(db)

        await self._sync(monkeypatch, record, name="What the bundle says")

        row = _row(db, record.uuid)
        assert row is not None and row.name == "What the bundle says"

    @pytest.mark.asyncio
    async def test_the_mark_is_on_no_read_and_in_no_export(
        self, http: httpx.AsyncClient, db: Session, reviewer: User, signed: Sign
    ) -> None:
        record = _record(db)
        signed.as_(reviewer)
        proposal = await _propose_change(http, record, depth_to=11.0)
        await _accepted(http, proposal, members=_members(external_ids=record.external_ids))

        assert "commons_reviewed_at" not in await _read(http, record.uuid)
        assert "commons_reviewed_at" not in json.dumps(await _exported(http, record.uuid))


class TestTheTable:
    def test_one_proposal_waits_per_account_and_site(self, db: Session, diver: User) -> None:
        site = _own_site(db, diver)
        insert = text(
            "INSERT INTO dive_site_proposal (user_id, dive_site_id, kind, members, attestation, attested_at, "
            "status, message, uuid, created_at) VALUES (:user, :site, 'new_site', '{}', 'x', now(), :status, '', "
            "gen_random_uuid(), now())"
        )
        db.execute(insert, {"user": diver.id, "site": site.id, "status": "withdrawn"})
        db.execute(insert, {"user": diver.id, "site": site.id, "status": "waiting"})
        db.commit()

        with pytest.raises(IntegrityError, match="ux_dive_site_proposal_one_waiting"):
            db.execute(insert, {"user": diver.id, "site": site.id, "status": "waiting"})
        db.rollback()
