"""The tags every account starts with: the registration seed and the revision's backfill.

`tests/test_auth.py` asserts the seed is called inside the account transaction; this is where
the rows are asserted to arrive, against Postgres.
"""

from typing import Any

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from src.app.core.db.migrations import alembic_config
from src.app.models.tag import Tag
from src.app.schemas.tag import tag_key
from src.app.services.default_tags import DEFAULT_TAGS, seed_default_tags
from tests.conftest import db_available
from tests.helpers.generators import create_tag, create_user

BACKFILL_REVISION = "7025991d9229"


async def _stored_names(async_db: AsyncSession, user_id: int) -> list[str]:
    rows = await async_db.execute(select(Tag.name).where(Tag.user_id == user_id).order_by(Tag.name))
    return list(rows.scalars())


def _backfill_module() -> Any:
    return ScriptDirectory.from_config(alembic_config()).get_revision(BACKFILL_REVISION).module


def test_no_two_defaults_are_one_tag() -> None:
    """Two that fold to one would seed a single row, and the list would promise one more."""
    assert len({tag_key(name) for name in DEFAULT_TAGS}) == len(DEFAULT_TAGS)


@pytest.mark.skipif(not db_available(), reason="Requires PostgreSQL")
class TestTheSeed:
    @pytest.mark.asyncio
    async def test_a_new_account_gets_every_default(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)

        await seed_default_tags(async_db, user_id=diver.id)
        await async_db.commit()

        assert await _stored_names(async_db, diver.id) == sorted(DEFAULT_TAGS)

    @pytest.mark.asyncio
    async def test_a_tag_the_account_already_holds_keeps_its_spelling(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = create_user(db)
        create_tag(db, diver, name="NIGHT")

        await seed_default_tags(async_db, user_id=diver.id)
        await async_db.commit()

        names = await _stored_names(async_db, diver.id)
        assert "NIGHT" in names
        assert "night" not in names
        assert len(names) == len(DEFAULT_TAGS)


@pytest.mark.skipif(not db_available(), reason="Requires PostgreSQL")
class TestTheBackfill:
    """The revision's data step, driven directly: `conftest.py` migrates to head before any
    test body runs, so no account can exist "before the revision"."""

    @pytest.mark.asyncio
    async def test_an_account_gets_the_frozen_defaults_it_lacks(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        create_tag(db, diver, name="Night")

        written = _backfill_module()._backfill_default_tags(db.connection())
        db.commit()

        frozen = _backfill_module()._DEFAULT_TAGS
        assert written >= len(frozen) - 1
        names = await _stored_names(async_db, diver.id)
        assert "Night" in names
        assert {tag_key(name) for name in names} == {tag_key(name) for name in frozen}

    @pytest.mark.asyncio
    async def test_the_rows_carry_a_uuid_and_a_timestamp(self, db: Session, async_db: AsyncSession) -> None:
        """Neither column has a server default, and a migration constructs no models."""
        diver = create_user(db)

        _backfill_module()._backfill_default_tags(db.connection())
        db.commit()

        rows = (await async_db.execute(select(Tag).where(Tag.user_id == diver.id))).scalars().all()
        assert len(rows) == len(_backfill_module()._DEFAULT_TAGS)
        assert all(row.uuid is not None and row.created_at is not None for row in rows)
        assert len({row.uuid for row in rows}) == len(rows)

    @pytest.mark.asyncio
    async def test_a_second_run_writes_nothing_for_an_account_it_already_seeded(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = create_user(db)
        backfill = _backfill_module()._backfill_default_tags

        backfill(db.connection())
        db.commit()
        backfill(db.connection())
        db.commit()

        assert len(await _stored_names(async_db, diver.id)) == len(_backfill_module()._DEFAULT_TAGS)
