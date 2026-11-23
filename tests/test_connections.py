"""What a tie between a person and an account shows the other side: *Added you* with its Hide
and Disconnect, a person's own Disconnect, and the three routes that serve a connected
account's picture to somebody who is not its owner.

All against Postgres: what is listed, who may see a picture and what a cut leaves behind are
`WHERE` clauses and a transaction, which a mock evaluates none of. Making a tie by a person
write is `test_people.py`'s; making one at sign-up is `test_connect_links.py`'s.
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1 import connect_links as connect_links_module
from src.app.api.v1 import people as people_module
from src.app.core.exceptions.http_exceptions import NotFoundException
from src.app.core.worker.functions import _purge_one_account
from src.app.models.account_disconnect import AccountDisconnect
from src.app.models.connect_link import ConnectLink
from src.app.models.person import Person
from src.app.models.user import User
from src.app.services.connect_links import new_connect_link_token
from tests.conftest import db_available
from tests.helpers.generators import create_person, create_user, create_user_picture
from tests.helpers.mocks import fake_request

pytestmark = pytest.mark.skipif(not db_available(), reason="No database connection available")

BYTES = b"RIFF....WEBP"


def _as(user: User) -> dict[str, Any]:
    return {"id": user.id, "uuid": user.uuid, "username": user.username, "is_superuser": False}


def _request(etag: str | None = None) -> MagicMock:
    request = MagicMock()
    request.headers = {} if etag is None else {"if-none-match": etag}
    return request


def _leave(db: Session, user: User) -> None:
    user.is_deleted = True
    user.deleted_at = datetime.now(UTC)
    db.commit()


@pytest.fixture(autouse=True)
def _bytes() -> Any:
    """The rows `create_user_picture` writes name keys nothing has stored."""
    with patch("src.app.api.account_avatar.read_picture_bytes", new_callable=AsyncMock, return_value=BYTES):
        yield


async def _added_you(async_db: AsyncSession, user: User) -> list[dict[str, Any]]:
    page = await people_module.read_added_you(request=MagicMock(), current_user=_as(user), db=async_db)
    return list(page["data"])


class TestAddedYou:
    @pytest.mark.asyncio
    async def test_it_lists_who_has_you_by_name_with_username_and_picture(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        zed, amy = create_user(db), create_user(db)
        zed.name, amy.name = "Zed Diver", "Amy Diver"
        db.commit()
        create_user_picture(db, amy)
        create_person(db, zed, connected_to=diver)
        create_person(db, amy, connected_to=diver)
        expected = [
            {"uuid": amy.uuid, "name": "Amy Diver", "username": amy.username, "avatar_sha256": "a" * 64},
            {"uuid": zed.uuid, "name": "Zed Diver", "username": zed.username, "avatar_sha256": None},
        ]

        assert await _added_you(async_db, diver) == expected

    @pytest.mark.asyncio
    async def test_it_leaves_out_who_you_added_who_you_hid_and_who_is_leaving(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        added_back, hidden, leaving, listed = (create_user(db) for _ in range(4))
        for account in (added_back, hidden, leaving, listed):
            create_person(db, account, connected_to=diver)
        create_person(db, diver, connected_to=added_back)
        _leave(db, leaving)
        listed_uuid, hidden_uuid = listed.uuid, hidden.uuid

        await people_module.hide_added_you(fake_request(), hidden_uuid, _as(diver), async_db)

        assert [row["uuid"] for row in await _added_you(async_db, diver)] == [listed_uuid]

    @pytest.mark.asyncio
    async def test_hide_changes_nothing_on_the_other_side_and_a_second_hide_changes_nothing(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        theirs = create_person(db, other_diver, connected_to=diver)
        theirs_id, other_uuid, diver_id = theirs.id, other_diver.uuid, diver.id

        await people_module.hide_added_you(fake_request(), other_uuid, _as(diver), async_db)
        db.expire_all()
        first = db.get(Person, theirs_id)
        assert first is not None
        hidden_at = first.connection_hidden_at
        await people_module.hide_added_you(fake_request(), other_uuid, _as(diver), async_db)

        db.expire_all()
        stored = db.get(Person, theirs_id)
        assert stored is not None
        assert (stored.connected_user_id, stored.connection_hidden_at) == (diver_id, hidden_at)
        assert hidden_at is not None

    @pytest.mark.asyncio
    async def test_a_tie_cut_and_made_afresh_shows_again(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        """Hide sits on the tie it hides."""
        theirs = create_person(db, other_diver, connected_to=diver)
        theirs_uuid, other_uuid = theirs.uuid, other_diver.uuid
        token = new_connect_link_token()
        db.add(ConnectLink(user_id=diver.id, token=token))
        db.commit()

        await people_module.hide_added_you(fake_request(), other_uuid, _as(diver), async_db)
        await people_module.disconnect_person(fake_request(), theirs_uuid, _as(other_diver), async_db)
        await people_module.patch_person(
            request=MagicMock(),
            uuid=theirs_uuid,
            values=people_module.PersonUpdateRequest.model_validate({"connect_link": token}),
            current_user=_as(other_diver),
            db=async_db,
        )

        assert [row["uuid"] for row in await _added_you(async_db, diver)] == [other_uuid]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("action", ["hide_added_you", "disconnect_added_you"])
    async def test_an_account_with_no_tie_to_you_is_a_404(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User, action: str
    ) -> None:
        """Your own tie to them is not theirs to you."""
        create_person(db, diver, connected_to=other_diver)
        other_uuid = other_diver.uuid

        for account in (other_uuid, uuid7()):
            with pytest.raises(NotFoundException):
                await getattr(people_module, action)(fake_request(), account, _as(diver), async_db)


class TestDisconnect:
    @pytest.mark.asyncio
    async def test_from_a_row_cuts_their_tie_keeps_their_person_and_records_it_in_one_commit(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        theirs = create_person(db, other_diver, connected_to=diver)
        theirs_id, theirs_name, other_id, diver_id = theirs.id, theirs.name, other_diver.id, diver.id

        await people_module.hide_added_you(fake_request(), other_diver.uuid, _as(diver), async_db)
        await people_module.disconnect_added_you(fake_request(), other_diver.uuid, _as(diver), async_db)
        # A second is a 404: there is no tie left to cut.
        with pytest.raises(NotFoundException):
            await people_module.disconnect_added_you(fake_request(), other_diver.uuid, _as(diver), async_db)

        db.expire_all()
        stored = db.get(Person, theirs_id)
        assert stored is not None
        assert (stored.name, stored.connected_user_id, stored.connection_hidden_at) == (theirs_name, None, None)
        recorded = db.scalars(
            select(AccountDisconnect).where(
                AccountDisconnect.user_id == diver_id, AccountDisconnect.disconnected_user_id == other_id
            )
        ).all()
        assert len(recorded) == 1
        read = await people_module._read(async_db, theirs_id)
        assert (read.username, read.avatar_sha256) == (None, None)

    @pytest.mark.asyncio
    async def test_twice_over_a_tie_made_again_keeps_one_record(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        create_person(db, other_diver, connected_to=diver)
        db.add(AccountDisconnect(user_id=diver.id, disconnected_user_id=other_diver.id))
        db.commit()
        diver_id = diver.id

        await people_module.disconnect_added_you(fake_request(), other_diver.uuid, _as(diver), async_db)

        db.expire_all()
        assert len(db.scalars(select(AccountDisconnect).where(AccountDisconnect.user_id == diver_id)).all()) == 1

    @pytest.mark.asyncio
    async def test_on_your_own_person_keeps_it_and_records_nothing(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        mine = create_person(db, diver, connected_to=other_diver)
        mine_uuid, diver_id, other_uuid = mine.uuid, diver.id, other_diver.uuid
        create_user_picture(db, other_diver)

        read = await people_module.disconnect_person(fake_request(), mine_uuid, _as(diver), async_db)
        again = await people_module.disconnect_person(fake_request(), mine_uuid, _as(diver), async_db)

        assert (read.username, read.avatar_sha256) == (None, None)
        assert again == read
        assert await _added_you(async_db, other_diver) == []
        db.expire_all()
        assert db.scalar(select(AccountDisconnect.id).where(AccountDisconnect.disconnected_user_id == diver_id)) is None
        assert db.scalar(select(AccountDisconnect.id).where(AccountDisconnect.user_id == diver_id)) is None
        assert other_uuid is not None


class TestThePurge:
    @pytest.mark.asyncio
    async def test_it_clears_a_hide_the_purged_account_left_on_another_divers_tie(
        self, db: Session, async_sessions: async_sessionmaker[AsyncSession], diver: User
    ) -> None:
        leaving = create_user(db)
        theirs = create_person(db, diver, connected_to=leaving)
        theirs.connection_hidden_at = datetime.now(UTC)
        leaving.is_deleted = True
        leaving.deleted_at = datetime.now(UTC) - timedelta(days=30)
        db.commit()
        theirs_id, leaving_id, email = theirs.id, leaving.id, leaving.email

        async with async_sessions() as session:
            gone = await _purge_one_account(session, user_id=leaving_id, email=email, cutoff=datetime.now(UTC))

        assert gone is True
        db.expire_all()
        stored = db.get(Person, theirs_id)
        assert stored is not None
        assert (stored.connected_user_id, stored.connection_hidden_at) == (None, None)


def _served(answer: Response) -> tuple[int, str | None]:
    assert answer.headers["Cache-Control"] == "private, no-store"
    return answer.status_code, answer.headers.get("ETag")


class TestThePictures:
    """Served only while the tie stands, and each of a cut, a reset and a deletion request
    stops the very next request."""

    @pytest.mark.asyncio
    async def test_a_connected_persons_picture_stops_at_either_sides_cut_and_at_a_deletion_request(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        cutting, dropped, leaving = create_user(db), create_user(db), create_user(db)
        for account in (cutting, dropped, leaving):
            create_user_picture(db, account)
        people = [create_person(db, diver, connected_to=account) for account in (cutting, dropped, leaving)]
        cut_by_them, cut_by_you, kept = (person.uuid for person in people)
        kept_id, diver_uuid = people[2].id, diver.uuid

        async def ask(person_uuid: Any, etag: str | None = None) -> tuple[int, str | None]:
            return _served(await people_module.read_person_avatar(_request(etag), person_uuid, _as(diver), async_db))

        assert await ask(cut_by_them) == (200, f'"{"a" * 64}"')
        assert await ask(cut_by_them, f'"{"a" * 64}"') == (304, f'"{"a" * 64}"')
        await people_module.disconnect_added_you(fake_request(), diver_uuid, _as(cutting), async_db)
        await people_module.disconnect_person(fake_request(), cut_by_you, _as(diver), async_db)
        assert (await ask(cut_by_them))[0] == 404
        assert (await ask(cut_by_you))[0] == 404
        assert (await ask(kept))[0] == 200
        _leave(db, leaving)
        assert (await ask(kept))[0] == 404
        read = await people_module._read(async_db, kept_id)
        # The username stays until the purge clears the tie, as a restore would bring it back.
        assert (read.avatar_sha256, read.username is not None) == (None, True)

    @pytest.mark.asyncio
    async def test_someone_elses_person_is_a_404(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        theirs = create_person(db, other_diver, connected_to=create_user(db))

        with pytest.raises(NotFoundException):
            await people_module.read_person_avatar(_request(), theirs.uuid, _as(diver), async_db)

    @pytest.mark.asyncio
    async def test_an_added_you_picture_stops_when_they_cut_the_tie_or_ask_to_be_deleted(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        cutting, leaving = create_user(db), create_user(db)
        for account in (cutting, leaving):
            create_user_picture(db, account)
        cutting_person = create_person(db, cutting, connected_to=diver)
        create_person(db, leaving, connected_to=diver)
        cutting_uuid, leaving_uuid, person_uuid = cutting.uuid, leaving.uuid, cutting_person.uuid

        async def ask(account_uuid: Any) -> int:
            answer = await people_module.read_added_you_avatar(_request(), account_uuid, _as(diver), async_db)
            return _served(answer)[0]

        assert (await ask(cutting_uuid), await ask(leaving_uuid), await ask(uuid7())) == (200, 200, 404)
        await people_module.disconnect_person(fake_request(), person_uuid, _as(cutting), async_db)
        _leave(db, leaving)
        assert (await ask(cutting_uuid), await ask(leaving_uuid)) == (404, 404)

    @pytest.mark.asyncio
    async def test_a_link_owners_picture_stops_at_a_reset_and_at_a_deletion_request(
        self, db: Session, async_db: AsyncSession, diver: User
    ) -> None:
        create_user_picture(db, diver)
        first = new_connect_link_token()
        db.add(ConnectLink(user_id=diver.id, token=first))
        db.commit()

        async def ask(token: str) -> int:
            answer = await connect_links_module.read_connect_link_avatar(_request(), token, async_db)
            return _served(answer)[0]

        assert await ask(first) == 200
        second = (await connect_links_module.make_connect_link(fake_request(), _as(diver), async_db)).token
        assert (await ask(first), await ask(second)) == (404, 200)
        _leave(db, diver)
        assert await ask(second) == 404
        resolved = await connect_links_module.resolve_connect_link(second, Response(), async_db)
        assert isinstance(resolved, JSONResponse)


class TestTheSignedInRead:
    @pytest.mark.asyncio
    async def test_it_answers_the_username_your_own_link_and_the_person_you_have(
        self, db: Session, async_db: AsyncSession, diver: User, other_diver: User
    ) -> None:
        token = new_connect_link_token()
        db.add(ConnectLink(user_id=other_diver.id, token=token))
        db.commit()
        mine = create_person(db, diver, connected_to=other_diver)
        mine_uuid, username, name = mine.uuid, other_diver.username, other_diver.name

        mine_read = await connect_links_module.read_connect_link_owner(token, Response(), _as(diver), async_db)
        theirs_read = await connect_links_module.read_connect_link_owner(token, Response(), _as(other_diver), async_db)
        stranger_read = await connect_links_module.read_connect_link_owner(
            token, Response(), _as(create_user(db)), async_db
        )

        assert mine_read.model_dump() == {  # type: ignore[union-attr]
            "name": name,
            "username": username,
            "avatar_sha256": None,
            "is_yours": False,
            "person_uuid": mine_uuid,
        }
        assert (theirs_read.is_yours, theirs_read.person_uuid) == (True, None)  # type: ignore[union-attr]
        assert (stranger_read.is_yours, stranger_read.person_uuid) == (False, None)  # type: ignore[union-attr]
