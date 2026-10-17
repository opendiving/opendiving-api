"""The shapes a route turns an `IntegrityError` into a 422 with, and what each leaves alone."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError

from src.app.api.integrity import refuse_vanished_reference, write_or_422
from src.app.core.exceptions.http_exceptions import DuplicateValueException, UnprocessableEntityException
from src.app.crud.unique_keys import write_or_duplicate


def _violating(constraint: str) -> IntegrityError:
    return IntegrityError("INSERT", {}, Exception(f'violates foreign key constraint "{constraint}"'))


async def _raising(error: Exception) -> None:
    raise error


class TestWriteOr422:
    @pytest.mark.asyncio
    async def test_a_write_that_holds_is_flushed_and_returned(self) -> None:
        db = AsyncMock()

        async def write() -> int:
            return 7

        assert await write_or_422(db, write(), "unused") == 7
        db.flush.assert_awaited_once()
        db.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_broken_constraint_rolls_back_and_is_the_sentence_given(self) -> None:
        db = AsyncMock()

        with pytest.raises(UnprocessableEntityException, match="Tag not found."):
            await write_or_422(db, _raising(_violating("dive_site_tag_tag_id_fkey")), "Tag not found.")

        db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_detail_function_is_handed_the_error(self) -> None:
        db = AsyncMock()

        with pytest.raises(UnprocessableEntityException, match="dive_trip_id_fkey"):
            await write_or_422(db, _raising(_violating("dive_trip_id_fkey")), lambda e: str(e.orig))

    @pytest.mark.asyncio
    async def test_a_constraint_the_flush_breaks_is_caught_too(self) -> None:
        db = AsyncMock()
        db.flush.side_effect = _violating("gear_set_item_gear_item_id_fkey")

        async def write() -> None:
            return None

        with pytest.raises(UnprocessableEntityException, match="Gear item not found."):
            await write_or_422(db, write(), "Gear item not found.")

        db.rollback.assert_awaited_once()


class TestRefuseVanishedReference:
    @pytest.mark.asyncio
    async def test_a_named_constraint_is_its_sentence_after_a_rollback(self) -> None:
        db = AsyncMock()

        with pytest.raises(UnprocessableEntityException, match="Contact not found."):
            await refuse_vanished_reference(
                db, _violating("course_contact_id_fkey"), {"course_contact_id_fkey": "Contact not found."}
            )

        db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_any_other_constraint_is_raised_as_it_came(self) -> None:
        db = AsyncMock()
        error = _violating("ck_course_date_range")

        with pytest.raises(IntegrityError) as raised:
            await refuse_vanished_reference(db, error, {"course_contact_id_fkey": "Contact not found."})

        assert raised.value is error
        db.rollback.assert_awaited_once()


class TestWriteOrDuplicate:
    @pytest.mark.asyncio
    async def test_the_named_index_is_the_duplicate_sentence_after_a_rollback(self) -> None:
        db = AsyncMock()
        error = IntegrityError(
            "INSERT", {}, Exception('duplicate key value violates unique constraint "ux_trip_user_id_name_lower"')
        )

        with pytest.raises(DuplicateValueException, match="A trip with this name already exists"):
            await write_or_duplicate(
                db, _raising(error), "ux_trip_user_id_name_lower", "A trip with this name already exists"
            )

        db.rollback.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_any_other_constraint_is_raised_as_it_came(self) -> None:
        db = AsyncMock()
        error = _violating("trip_user_id_fkey")

        with pytest.raises(IntegrityError) as raised:
            await write_or_duplicate(db, _raising(error), "ux_trip_user_id_name_lower", "unused")

        assert raised.value is error
        db.rollback.assert_awaited_once()
