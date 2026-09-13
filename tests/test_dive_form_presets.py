"""The dive form's hidden-field vocabulary, its presets, and the seed that plants three.

Three kinds of test live here.

The pure ones need no database: the vocabulary guard, which is what keeps the enum honest
against the dive schemas it names, and the canonicalization rules, which are Pydantic
validators.

The Postgres-backed ones - the seed, restore, and the revision's backfill - are guarded by
the shared `skipif(not db_available())` and skip silently without a database. On a
developer's machine that means `POSTGRES_SERVER=localhost`; CI sets it and fails the job if
anything skips. See CONTRIBUTING.md.

What is deliberately *not* here: the ownership 404s, which
`test_ownership.py::TestEveryUuidRouteIsAccountedFor` derives from the real route table for
every `{uuid}` route at once, and the hard-delete behaviour, which
`test_hard_delete.py`'s three classes run over its registry. Both are registrations this
resource joined rather than cases it writes again.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from uuid6 import uuid7

from src.app.api.v1.dive_form_presets import (
    _to_public,
    erase_dive_form_preset,
    patch_dive_form_preset,
    read_dive_form_preset,
    read_dive_form_presets,
    restore_default_dive_form_presets,
    write_dive_form_preset,
)
from src.app.core.db.migrations import alembic_config
from src.app.core.exceptions.http_exceptions import DuplicateValueException, ForbiddenException
from src.app.models.dive_form_preset import DiveFormPreset
from src.app.models.user import User
from src.app.schemas.dive import DiveCreateRequest
from src.app.schemas.dive_form_preset import (
    MIXTURE_FIELD_PREFIX,
    DiveFormField,
    DiveFormPresetCreate,
    DiveFormPresetRead,
    DiveFormPresetReadInternal,
    DiveFormPresetUpdate,
    canonical_hidden_fields,
)
from src.app.schemas.dive_mixture import DiveMixtureCreate
from src.app.schemas.user import UserRead, UserUpdate
from src.app.services.dive_form_presets import (
    DEFAULT_PRESETS,
    missing_default_presets,
    seed_default_presets,
)
from tests.conftest import db_available
from tests.helpers.generators import create_user
from tests.helpers.mocks import awaited_kwargs

# The revision that creates the table and backfills the accounts predating it. Reached
# through Alembic's own script directory rather than by importing a path: the module's
# filename carries a slug nobody should have to spell twice, and the revision id is the
# stable name for it.
BACKFILL_REVISION = "e0cfbd603859"


def _internal(**overrides: Any) -> DiveFormPresetReadInternal:
    defaults: dict[str, Any] = {
        "id": 3,
        "user_id": 1,
        "uuid": uuid7(),
        "name": "Warm water",
        "hidden_fields": [DiveFormField.ALTITUDE],
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    return DiveFormPresetReadInternal(**{**defaults, **overrides})


def _caller(user_id: int = 1, user_uuid: uuid_pkg.UUID | None = None) -> dict:
    return {"id": user_id, "uuid": user_uuid or uuid7(), "username": "ada", "is_superuser": False}


class TestTheVocabularyNamesRealFields:
    """The api half of the two-sided guard on the field vocabulary.

    A `DiveFormField` value is a *key of the dive resource*, not a UI label, and this is what
    holds it to that: every member has to name a field the API itself accepts and does not
    require. The web half is an equality check against its form schema, minus the keys that
    side exempts by name, so between them the hand-kept mirror is pinned from both ends with
    no cross-repo test.

    Subset here, equality there, deliberately: API-optional is wider than what is hideable.
    `gas_number` has no input at all, and a cylinder's `volume` and `oxygen` are optional on
    both sides but always shown, exempt by name on the web side - so the API has optional
    fields with no business being hideable, while every hideable field must be one the API
    will accept omitted.
    """

    def test_every_value_names_a_field_of_the_create_request(self) -> None:
        top_level = {value for value in DiveFormField if not value.startswith(MIXTURE_FIELD_PREFIX)}

        unknown = sorted(value for value in top_level if value not in DiveCreateRequest.model_fields)

        assert not unknown, f"not fields of DiveCreateRequest: {unknown}"

    def test_every_prefixed_value_names_a_field_of_the_cylinder_schema(self) -> None:
        per_cylinder = {value for value in DiveFormField if value.startswith(MIXTURE_FIELD_PREFIX)}

        unknown = sorted(
            value
            for value in per_cylinder
            if value.removeprefix(MIXTURE_FIELD_PREFIX) not in DiveMixtureCreate.model_fields
        )

        assert not unknown, f"not fields of DiveMixtureCreate: {unknown}"

    def test_no_value_names_a_field_the_api_requires(self) -> None:
        """A required field cannot be hidden - there would be no way to submit the form -
        so a member naming one is a vocabulary that promises something the API refuses.
        """
        required = []
        for value in DiveFormField:
            if value.startswith(MIXTURE_FIELD_PREFIX):
                field = DiveMixtureCreate.model_fields[value.removeprefix(MIXTURE_FIELD_PREFIX)]
            else:
                field = DiveCreateRequest.model_fields[value]
            if field.is_required():
                required.append(value)

        assert not required, f"required on the wire, so not hideable: {required}"

    def test_the_prefix_is_the_only_thing_that_makes_a_key_per_cylinder(self) -> None:
        """`mixtures` hides the whole section and is a top-level key; `mixture.role` hides
        one input on every tank card. The singular/plural pair is easy to typo into each
        other, and the two mean different things.
        """
        assert DiveFormField.MIXTURES == "mixtures"
        assert not DiveFormField.MIXTURES.startswith(MIXTURE_FIELD_PREFIX)
        assert DiveFormField.MIXTURE_ROLE.startswith(MIXTURE_FIELD_PREFIX)


class TestTheCanonicalForm:
    """What is stored is a set spelled as a list, and this is what makes that true.

    The client compares the account's current state against each preset's list to decide
    which one to mark. That is a loop over two lists, which is only correct if two equal sets
    are always the same list - so every write is rewritten into form order with duplicates
    collapsed, on a preset and on the user column alike.
    """

    def test_declaration_order_is_form_order(self) -> None:
        """Not alphabetical, and not the order anything was added: a client takes its panel
        rows from this, so the enum's order is part of the contract.
        """
        assert list(DiveFormField)[:4] == [
            DiveFormField.TRIP_UUID,
            DiveFormField.COURSE_UUID,
            DiveFormField.DIVE_SITE_UUIDS,
            DiveFormField.MAX_DEPTH,
        ]
        assert list(DiveFormField)[-1] == DiveFormField.MIXTURE_USAGE

    def test_any_input_order_becomes_form_order(self) -> None:
        scrambled = [DiveFormField.NOTES, DiveFormField.TRIP_UUID, DiveFormField.ALTITUDE]

        assert canonical_hidden_fields(scrambled) == [
            DiveFormField.TRIP_UUID,
            DiveFormField.ALTITUDE,
            DiveFormField.NOTES,
        ]

    def test_duplicates_collapse(self) -> None:
        values = [DiveFormField.ALTITUDE, DiveFormField.ALTITUDE, DiveFormField.NOTES]

        assert canonical_hidden_fields(values) == [DiveFormField.ALTITUDE, DiveFormField.NOTES]

    def test_a_preset_stores_the_canonical_form(self) -> None:
        preset = DiveFormPresetCreate.model_validate(
            {"user_uuid": uuid7(), "name": "Odd order", "hidden_fields": ["notes", "altitude", "notes"]}
        )

        assert preset.hidden_fields == [DiveFormField.ALTITUDE, DiveFormField.NOTES]

    def test_the_user_column_stores_the_canonical_form(self) -> None:
        """The same rule on the other write path. If only one of the two canonicalized, the
        client's "which preset matches?" comparison would answer no for sets that are equal.
        """
        values = UserUpdate.model_validate({"dive_form_hidden_fields": ["notes", "altitude", "notes"]})

        assert values.dive_form_hidden_fields == [DiveFormField.ALTITUDE, DiveFormField.NOTES]

    def test_a_name_outside_the_vocabulary_is_rejected(self) -> None:
        """The whole point of typing the column: `cns_start` is a real dive column and not a
        form field, and a typo that stored it would silently hide nothing forever.
        """
        with pytest.raises(ValidationError) as exc_info:
            DiveFormPresetUpdate.model_validate({"hidden_fields": ["cns_start"]})

        assert "hidden_fields" in str(exc_info.value)

    def test_more_entries_than_there_are_fields_is_rejected(self) -> None:
        """The cap is on the input list, checked before duplicates collapse - so a body
        padded with repeats is refused rather than quietly canonicalized down to something
        small. Nothing legitimate sends more entries than the vocabulary has members.
        """
        too_many = [DiveFormField.ALTITUDE] * (len(DiveFormField) + 1)

        with pytest.raises(ValidationError):
            DiveFormPresetUpdate.model_validate({"hidden_fields": too_many})

    def test_exactly_as_many_entries_as_fields_is_accepted(self) -> None:
        """The boundary from the other side, so the cap cannot drift into rejecting a diver
        who has hidden everything hideable.
        """
        everything = DiveFormPresetUpdate.model_validate({"hidden_fields": list(DiveFormField)})

        assert everything.hidden_fields == list(DiveFormField)

    def test_an_explicit_null_is_rejected(self) -> None:
        """The column is `NOT NULL`. A preset that hides nothing is `[]`, which is exactly
        what Technical is - so `null` means nothing the database will take.
        """
        with pytest.raises(ValidationError) as exc_info:
            DiveFormPresetUpdate.model_validate({"hidden_fields": None})

        assert "cannot be null" in str(exc_info.value)


class TestAStoredFieldNameOutsideTheVocabulary:
    """The column is unconstrained `JSON` and the vocabulary is enforced in Pydantic alone,
    so a direct write can leave a name this build does not know - and the read shapes must
    not answer that with a 500 across the whole preset list. See *"A stored vocabulary is
    read back as a string"* in DECISIONS.md.

    The trap here is not the annotation but the *validator*: `DiveFormPresetBase` canonicalizes
    by rebuilding the list from `DiveFormField`'s members, so widening the type alone would
    have made an unrecognized name vanish from the response instead of failing it - the same
    wrong answer, told quietly.
    """

    ODD = "frobnicator"

    def test_a_read_carries_it_through_beside_the_names_it_knows(self) -> None:
        preset = DiveFormPresetReadInternal(
            id=1,
            uuid=uuid7(),
            user_id=1,
            name="Warm water",
            hidden_fields=[DiveFormField.ALTITUDE, self.ODD],
            created_at=datetime.now(UTC),
        )

        assert preset.hidden_fields == ["altitude", self.ODD]

    def test_the_public_shape_does_the_same(self) -> None:
        preset = DiveFormPresetRead(
            uuid=uuid7(),
            user_uuid=uuid7(),
            name="Warm water",
            hidden_fields=[self.ODD],
            created_at=datetime.now(UTC),
        )

        assert preset.hidden_fields == [self.ODD]

    def test_the_user_column_carries_it_too(self) -> None:
        """`UserRead` is the same vocabulary over the same kind of column, and
        `get_current_user` validates that row on every authenticated request - so a stray
        name there would have been a 500 on everything, not just on the presets list."""
        user = UserRead(
            uuid=uuid7(),
            name="User Userson",
            username="userson",
            email="user.userson@example.com",
            dive_form_hidden_fields=[self.ODD],
        )

        assert user.dive_form_hidden_fields == [self.ODD]

    def test_writing_one_is_still_a_422(self) -> None:
        """The vocabulary is unchanged: the enum is still what every write is typed with."""
        with pytest.raises(ValidationError):
            DiveFormPresetCreate(user_uuid=uuid7(), name="Warm water", hidden_fields=[self.ODD])
        with pytest.raises(ValidationError):
            DiveFormPresetUpdate(hidden_fields=[self.ODD])
        with pytest.raises(ValidationError):
            UserUpdate(dive_form_hidden_fields=[self.ODD])


class TestTheUserColumn:
    """`user.dive_form_hidden_fields` - the account's current state, read on `GET /user` and
    written on `PATCH /user`, the same two-endpoint surface `units` has.

    `test_update_explicit_nulls.py` covers the `NON_NULLABLE_FIELDS` half structurally, off
    the SQLAlchemy metadata; the null case here is that guard seen from the caller's side.
    """

    def test_an_untouched_account_hides_nothing(self) -> None:
        """A new diver sees the form exactly as it was before presets existed. Starting
        everyone on Basic was the alternative, and it would change the first form a new
        diver meets for the sake of divers who can pick it themselves.
        """
        values = UserRead.model_validate(
            {"uuid": uuid7(), "name": "Ada Lovelace", "username": "ada", "email": "ada@example.com"}
        )

        assert values.dive_form_hidden_fields == []

    @pytest.mark.asyncio
    async def test_patch_user_saves_the_hidden_fields(self, mock_db, current_user_dict) -> None:
        """The field has to be on `UserUpdate` as well as `UserRead`: the schema is
        `extra="forbid"`, so an omission here would 422 the Fields panel rather than save it.
        """
        from src.app.api.v1.users import patch_user

        user_update = UserUpdate(dive_form_hidden_fields=[DiveFormField.NOTES, DiveFormField.ALTITUDE])

        with patch("src.app.api.v1.users.crud_users") as mock_crud:
            mock_crud.exists = AsyncMock(return_value=False)
            mock_crud.update = AsyncMock(return_value=None)

            result = await patch_user(Mock(), user_update, current_user_dict, mock_db)

            assert result == {"message": "User updated"}
            written = mock_crud.update.call_args.kwargs["object"]
            assert written.model_dump(exclude_unset=True) == {
                "dive_form_hidden_fields": [DiveFormField.ALTITUDE, DiveFormField.NOTES]
            }

    def test_an_explicit_null_is_rejected(self) -> None:
        with pytest.raises(ValidationError) as exc_info:
            UserUpdate.model_validate({"dive_form_hidden_fields": None})

        assert "cannot be null" in str(exc_info.value)


class TestTheDefaults:
    """The three sets seeded per account, and the rule that adds back what is missing."""

    def test_there_are_three_named_basic_recreational_technical(self) -> None:
        assert [preset.name for preset in DEFAULT_PRESETS] == ["Basic", "Recreational", "Technical"]

    def test_each_default_is_already_in_canonical_form(self) -> None:
        """They are seed data written by hand, and they go into the column without passing
        through a schema. Writing one out of order would store a list no client comparison
        could match against the state that applying it produces.
        """
        for preset in DEFAULT_PRESETS:
            assert list(preset.hidden_fields) == canonical_hidden_fields(preset.hidden_fields), preset.name

    def test_technical_hides_nothing(self) -> None:
        """The empty set, not "every field listed". What is stored is the hidden set, so a
        field the form gains later is on screen under Technical without anybody editing it.
        """
        technical = next(preset for preset in DEFAULT_PRESETS if preset.name == "Technical")

        assert technical.hidden_fields == ()

    def test_basic_keeps_the_fields_a_holiday_diver_fills_in(self) -> None:
        """Pinned as the visible set, which is the shorter list under this preset and the
        one the intent above it is written in terms of - trip and bottom temperature were
        on it until they were hidden, and the assertion that catches a fourth field
        arriving on screen is this one rather than a hidden set nobody reads.
        """
        basic = next(preset for preset in DEFAULT_PRESETS if preset.name == "Basic")
        visible = [value for value in DiveFormField if value not in basic.hidden_fields]

        assert visible == [
            DiveFormField.DIVE_SITE_UUIDS,
            DiveFormField.MAX_DEPTH,
            DiveFormField.NOTES,
        ]

    def test_recreational_hides_water_type_alongside_the_planning_fields(self) -> None:
        """Pinned as the hidden set rather than as the visible one, which is the longer list
        here and would have to be edited every time the form gains a field. These sets are
        written by hand and go into the column without passing through a schema, so what a
        new account is seeded with is worth reading back somewhere it can be compared against
        the intent above it.
        """
        recreational = next(preset for preset in DEFAULT_PRESETS if preset.name == "Recreational")

        assert list(recreational.hidden_fields) == [
            DiveFormField.WATER_TYPE,
            DiveFormField.ALTITUDE,
            DiveFormField.MIXTURE_PO2_LIMIT,
            DiveFormField.MIXTURE_HELIUM,
            DiveFormField.MIXTURE_ROLE,
            DiveFormField.MIXTURE_USAGE,
        ]

    def test_with_all_three_present_nothing_is_missing(self) -> None:
        assert missing_default_presets(["Basic", "Recreational", "Technical"]) == []

    def test_the_comparison_is_case_insensitive(self) -> None:
        """A diver who renamed Basic to "basic" has it, whatever the shift key did. The
        unique index is on `lower(name)`, so a case-sensitive check here would try to create
        a row Postgres then refuses.
        """
        assert missing_default_presets(["BASIC", "recreational", "TeChNiCaL"]) == []

    def test_only_the_absent_one_comes_back(self) -> None:
        missing = missing_default_presets(["Recreational", "Technical", "Warm water"])

        assert [preset.name for preset in missing] == ["Basic"]

    def test_a_renamed_default_leaves_its_name_free(self) -> None:
        """Renaming Technical to "Tech" means the account no longer has anything called
        Technical - so restore creates a new one beside it rather than adopting "Tech".
        That is the cost of matching on name, and it is the behaviour that keeps restore
        from overwriting an edit.
        """
        missing = missing_default_presets(["Basic", "Recreational", "Tech"])

        assert [preset.name for preset in missing] == ["Technical"]


class TestTheRoutes:
    """Route-level behaviour that needs no database: the ownership refusal on create, and
    the duplicate-name refusals on create and rename.
    """

    def test_the_public_shape_drops_the_internal_ids(self) -> None:
        user_uuid = uuid7()
        internal = _internal()

        public = _to_public(internal, user_uuid=user_uuid)

        assert public.user_uuid == user_uuid
        assert public.uuid == internal.uuid
        assert public.hidden_fields == [DiveFormField.ALTITUDE]
        assert not hasattr(public, "user_id")

    @pytest.mark.asyncio
    async def test_creating_one_for_somebody_else_is_a_403(self, mock_db) -> None:
        """Naming an account that is not the caller's is a 403, not a 404: the caller got
        their *own* identity wrong, which is the distinction this API's status codes make.
        """
        caller = _caller()
        body = DiveFormPresetCreate(user_uuid=uuid7(), name="Warm water", hidden_fields=[])

        with pytest.raises(ForbiddenException):
            await write_dive_form_preset(Mock(), body, caller, mock_db)

    @pytest.mark.asyncio
    async def test_a_duplicate_name_is_refused_on_create(self, mock_db) -> None:
        caller = _caller()
        body = DiveFormPresetCreate(user_uuid=caller["uuid"], name="Warm water", hidden_fields=[])

        with patch(
            "src.app.api.v1.dive_form_presets.dive_form_preset_name_exists", new_callable=AsyncMock
        ) as name_exists:
            name_exists.return_value = True

            with pytest.raises(DuplicateValueException):
                await write_dive_form_preset(Mock(), body, caller, mock_db)

    @pytest.mark.asyncio
    async def test_a_rename_onto_an_existing_name_is_refused(self, mock_db) -> None:
        """And it excludes the row being renamed, so re-saving a preset under the name it
        already has is not a conflict with itself.
        """
        caller = _caller()
        stored = _internal(user_id=caller["id"])

        with (
            patch("src.app.api.v1.dive_form_presets._get_owned_dive_form_preset", new_callable=AsyncMock) as get_owned,
            patch(
                "src.app.api.v1.dive_form_presets.dive_form_preset_name_exists", new_callable=AsyncMock
            ) as name_exists,
        ):
            get_owned.return_value = stored
            name_exists.return_value = True

            with pytest.raises(DuplicateValueException):
                await patch_dive_form_preset(Mock(), stored.uuid, DiveFormPresetUpdate(name="Taken"), caller, mock_db)

            assert awaited_kwargs(name_exists)["exclude_id"] == stored.id

    @pytest.mark.asyncio
    async def test_replacing_only_the_hidden_set_asks_about_no_name(self, mock_db) -> None:
        """ "Update with current fields" sends `hidden_fields` alone. A name check on a body
        with no name in it would either compare against `None` or refuse the write.
        """
        caller = _caller()
        stored = _internal(user_id=caller["id"])

        with (
            patch("src.app.api.v1.dive_form_presets._get_owned_dive_form_preset", new_callable=AsyncMock) as get_owned,
            patch(
                "src.app.api.v1.dive_form_presets.dive_form_preset_name_exists", new_callable=AsyncMock
            ) as name_exists,
            patch("src.app.api.v1.dive_form_presets.crud_dive_form_presets") as crud,
        ):
            get_owned.return_value = stored
            crud.update = AsyncMock(return_value=None)

            result = await patch_dive_form_preset(
                Mock(),
                stored.uuid,
                DiveFormPresetUpdate(hidden_fields=[DiveFormField.NOTES]),
                caller,
                mock_db,
            )

            assert result == {"message": "Dive form preset updated"}
            name_exists.assert_not_awaited()
            assert awaited_kwargs(crud.update)["object"] == {"hidden_fields": [DiveFormField.NOTES]}


@pytest.mark.skipif(not db_available(), reason="Requires PostgreSQL")
class TestTheSeedAgainstPostgres:
    """The three rows really arriving, which no mock can tell you.

    `test_auth.py::TestCompleteProfile` covers the other half - that the seed runs inside the
    account's transaction with `commit=False`, so a registration either creates the account
    with its presets or creates nothing.
    """

    @pytest.mark.asyncio
    async def test_a_fresh_account_gets_the_three_defaults_with_their_sets(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = create_user(db)

        created = await seed_default_presets(async_db, user_id=diver.id)

        assert [preset.name for preset in created] == ["Basic", "Recreational", "Technical"]
        stored = await _stored_presets(async_db, diver.id)
        assert stored == {preset.name: list(preset.hidden_fields) for preset in DEFAULT_PRESETS}

    @pytest.mark.asyncio
    async def test_running_it_twice_creates_nothing_the_second_time(self, db: Session, async_db: AsyncSession) -> None:
        """Which is what makes the restore endpoint safe to offer whenever the diver asks,
        and what keeps the unique index from turning a double-click into a 500.
        """
        diver = create_user(db)
        await seed_default_presets(async_db, user_id=diver.id)

        again = await seed_default_presets(async_db, user_id=diver.id)

        assert again == []
        assert len(await _stored_presets(async_db, diver.id)) == 3

    @pytest.mark.asyncio
    async def test_a_deleted_default_is_the_only_one_that_comes_back(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        await seed_default_presets(async_db, user_id=diver.id)
        await async_db.execute(
            delete(DiveFormPreset).where(DiveFormPreset.user_id == diver.id, DiveFormPreset.name == "Basic")
        )
        await async_db.commit()

        created = await seed_default_presets(async_db, user_id=diver.id)

        assert [preset.name for preset in created] == ["Basic"]
        assert len(await _stored_presets(async_db, diver.id)) == 3

    @pytest.mark.asyncio
    async def test_an_edited_default_keeps_its_edit(self, db: Session, async_db: AsyncSession) -> None:
        """Add what is missing, never overwrite. A diver who took `notes` out of Basic has
        Basic, and restore has nothing to say about it.
        """
        diver = create_user(db)
        await seed_default_presets(async_db, user_id=diver.id)
        await async_db.execute(
            update(DiveFormPreset)
            .where(DiveFormPreset.user_id == diver.id, DiveFormPreset.name == "Basic")
            .values(hidden_fields=["notes"])
        )
        await async_db.commit()

        created = await seed_default_presets(async_db, user_id=diver.id)

        assert created == []
        assert (await _stored_presets(async_db, diver.id))["Basic"] == ["notes"]

    @pytest.mark.asyncio
    async def test_a_renamed_default_gets_a_new_one_beside_it(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        await seed_default_presets(async_db, user_id=diver.id)
        await async_db.execute(
            update(DiveFormPreset)
            .where(DiveFormPreset.user_id == diver.id, DiveFormPreset.name == "Technical")
            .values(name="Tech")
        )
        await async_db.commit()

        created = await seed_default_presets(async_db, user_id=diver.id)

        assert [preset.name for preset in created] == ["Technical"]
        stored = await _stored_presets(async_db, diver.id)
        assert sorted(stored) == ["Basic", "Recreational", "Tech", "Technical"]

    @pytest.mark.asyncio
    async def test_the_restore_route_answers_with_what_it_created(self, db: Session, async_db: AsyncSession) -> None:
        """The endpoint returns only the new rows, which is what lets a client say how many
        were added rather than repeating the whole list back at the diver.
        """
        diver = create_user(db)
        await seed_default_presets(async_db, user_id=diver.id)
        await async_db.execute(
            delete(DiveFormPreset).where(DiveFormPreset.user_id == diver.id, DiveFormPreset.name == "Recreational")
        )
        await async_db.commit()

        created = await restore_default_dive_form_presets(
            Mock(), _caller(user_id=diver.id, user_uuid=diver.uuid), async_db
        )

        assert [preset.name for preset in created] == ["Recreational"]
        assert created[0].user_uuid == diver.uuid

    @pytest.mark.asyncio
    async def test_a_seeded_account_can_be_deleted(self, db: Session, async_db: AsyncSession) -> None:
        """The FK carries `ondelete="CASCADE"`, which `test_user_cascade.py` asserts
        structurally for every FK into `user`. This is the behavioural half for these rows:
        an account with presets is still deletable, which it would not be if the cascade were
        missing.
        """
        diver = create_user(db)
        await seed_default_presets(async_db, user_id=diver.id)

        await async_db.execute(delete(User).where(User.id == diver.id))
        await async_db.commit()

        assert await _stored_presets(async_db, diver.id) == {}


@pytest.mark.skipif(not db_available(), reason="Requires PostgreSQL")
class TestTheBackfill:
    """The revision's data step, driven directly.

    Not through `alembic upgrade head`: `conftest.py`'s session-scoped autouse
    `upgrade_to_head()` has already run before any test body, so no test can create an
    account "before the revision". What can be tested is the callable itself, which is the
    part with the logic in it - and the module is reached through Alembic's script directory,
    so the test names the revision id rather than a filename.
    """

    @staticmethod
    def _revision_module() -> Any:
        script = ScriptDirectory.from_config(alembic_config())
        return script.get_revision(BACKFILL_REVISION).module

    @staticmethod
    def _backfill() -> Any:
        return TestTheBackfill._revision_module()._backfill_default_presets

    @pytest.mark.asyncio
    async def test_an_account_with_no_presets_gets_the_three_defaults(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """The whole point of the revision: an account that predates this feature never went
        through the registration seed, so without it the operator's own account is the one
        account on the instance whose panel has nothing to apply.
        """
        diver = create_user(db)

        written = self._backfill()(db.connection())
        db.commit()

        assert written >= 3
        stored = await _stored_presets(async_db, diver.id)
        # Against the revision's *own* frozen copy, not `DEFAULT_PRESETS`. The module
        # docstring is explicit that the live definition owes the copy nothing and that
        # what the copy owes the accounts it seeded is the defaults as they stood on the
        # day. Asserting today's constant asserted a coupling that is documented not to
        # exist, and held only until the two first diverged - which `mixture.helium`
        # becoming hideable is.
        frozen = self._revision_module()._DEFAULT_PRESETS
        assert stored == {name: list(fields) for name, fields in frozen}

    @pytest.mark.asyncio
    async def test_an_account_that_already_has_one_keeps_its_own_and_gains_the_rest(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """The "missing by name" guard, which is vacuous at upgrade time - the table is
        created three statements above it - and is what makes the step re-runnable by hand
        without tripping the unique index.
        """
        diver = create_user(db)
        db.add(DiveFormPreset(user_id=diver.id, name="Basic", hidden_fields=["notes"], uuid=uuid7()))
        db.commit()

        self._backfill()(db.connection())
        db.commit()

        stored = await _stored_presets(async_db, diver.id)
        assert sorted(stored) == ["Basic", "Recreational", "Technical"]
        assert stored["Basic"] == ["notes"]

    @pytest.mark.asyncio
    async def test_the_rows_it_writes_carry_a_uuid_and_a_timestamp(self, db: Session, async_db: AsyncSession) -> None:
        """Both columns are `NOT NULL` and neither has a server default: `PublicUUIDMixin`'s
        `uuid` comes from a dataclass `default_factory` the ORM applies on construction, and
        a migration constructs no models. A bare INSERT would hand Postgres a NULL.
        """
        diver = create_user(db)

        self._backfill()(db.connection())
        db.commit()

        rows = (
            (await async_db.execute(select(DiveFormPreset).where(DiveFormPreset.user_id == diver.id))).scalars().all()
        )
        assert len(rows) == 3
        assert all(row.uuid is not None and row.created_at is not None for row in rows)
        assert len({row.uuid for row in rows}) == 3


@pytest.mark.skipif(not db_available(), reason="Requires PostgreSQL")
class TestTheRoutesAgainstPostgres:
    """The wire contract `web-1` is written against, exercised end to end through a real
    session: what a create stores, what the list answers with, and that a delete really
    removes the row.

    Route functions called directly rather than over HTTP, the way the rest of this suite
    exercises handlers - `get_current_user` is the only dependency these have, and it is
    what `_caller` stands in for.
    """

    @pytest.mark.asyncio
    async def test_a_create_stores_the_canonical_set_and_answers_with_the_public_shape(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = create_user(db)
        caller = _caller(user_id=diver.id, user_uuid=diver.uuid)
        body = DiveFormPresetCreate.model_validate(
            {
                "user_uuid": diver.uuid,
                "name": "Warm water",
                # Out of order and with a repeat, which is what the canonical rule is for.
                "hidden_fields": ["notes", "altitude", "notes"],
            }
        )

        created = await write_dive_form_preset(Mock(), body, caller, async_db)

        assert created.name == "Warm water"
        assert created.hidden_fields == [DiveFormField.ALTITUDE, DiveFormField.NOTES]
        assert created.user_uuid == diver.uuid
        assert (await _stored_presets(async_db, diver.id))["Warm water"] == ["altitude", "notes"]

    @pytest.mark.asyncio
    async def test_the_list_is_alphabetical_and_scoped_to_the_caller(
        self, db: Session, async_db: AsyncSession, other_diver: User
    ) -> None:
        """`user_id` is the only thing keeping another diver's presets out of this page, and
        a suite with one diver in it cannot notice when that condition stops working.
        """
        diver = create_user(db)
        await seed_default_presets(async_db, user_id=diver.id)
        await seed_default_presets(async_db, user_id=other_diver.id)

        page = await read_dive_form_presets(
            Mock(), diver.uuid, _caller(user_id=diver.id, user_uuid=diver.uuid), async_db
        )

        assert [row["name"] for row in page["data"]] == ["Basic", "Recreational", "Technical"]
        assert page["total_count"] == 3
        assert {row["user_uuid"] for row in page["data"]} == {diver.uuid}

    @pytest.mark.asyncio
    async def test_out_of_range_pagination_is_clamped_rather_than_rejected(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        diver = create_user(db)
        await seed_default_presets(async_db, user_id=diver.id)

        page = await read_dive_form_presets(
            Mock(),
            diver.uuid,
            _caller(user_id=diver.id, user_uuid=diver.uuid),
            async_db,
            page=0,
            items_per_page=10_000,
        )

        assert page["page"] == 1
        assert page["items_per_page"] <= 100

    @pytest.mark.asyncio
    async def test_listing_somebody_elses_presets_is_a_403(
        self, db: Session, async_db: AsyncSession, other_diver: User
    ) -> None:
        """A `user_uuid` that is not the caller's own is the caller naming *themselves*
        wrongly, which is this API's 403 rather than its 404.
        """
        diver = create_user(db)

        with pytest.raises(ForbiddenException):
            await read_dive_form_presets(
                Mock(), other_diver.uuid, _caller(user_id=diver.id, user_uuid=diver.uuid), async_db
            )

    @pytest.mark.asyncio
    async def test_reading_one_back_answers_with_what_was_stored(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        caller = _caller(user_id=diver.id, user_uuid=diver.uuid)
        body = DiveFormPresetCreate(user_uuid=diver.uuid, name="Warm water", hidden_fields=[DiveFormField.ALTITUDE])
        created = await write_dive_form_preset(Mock(), body, caller, async_db)

        read_back = await read_dive_form_preset(Mock(), created.uuid, caller, async_db)

        assert read_back == created

    @pytest.mark.asyncio
    async def test_a_delete_removes_the_row_and_leaves_the_current_state_alone(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """Deleting the preset a diver is currently arranged like does not rearrange their
        form: a preset is a snapshot, and the account's own column is what the form reads.
        """
        diver = create_user(db)
        caller = _caller(user_id=diver.id, user_uuid=diver.uuid)
        await seed_default_presets(async_db, user_id=diver.id)
        basic = next(
            row
            for row in (
                await async_db.execute(select(DiveFormPreset).where(DiveFormPreset.user_id == diver.id))
            ).scalars()
            if row.name == "Basic"
        )

        result = await erase_dive_form_preset(Mock(), basic.uuid, caller, async_db)

        assert result == {"message": "Dive form preset deleted"}
        assert sorted(await _stored_presets(async_db, diver.id)) == ["Recreational", "Technical"]
        refreshed = (await async_db.execute(select(User).where(User.id == diver.id))).scalars().one()
        assert list(refreshed.dive_form_hidden_fields) == []

    @pytest.mark.asyncio
    async def test_a_duplicate_name_differing_only_by_case_is_refused(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """Against a real session rather than a mocked `name_exists`, so this is the
        `ux_dive_form_preset_user_id_name_lower` index and the helper in front of it agreeing
        - a 422 rather than the `IntegrityError` the index alone would raise.
        """
        diver = create_user(db)
        caller = _caller(user_id=diver.id, user_uuid=diver.uuid)
        await write_dive_form_preset(
            Mock(), DiveFormPresetCreate(user_uuid=diver.uuid, name="Warm water", hidden_fields=[]), caller, async_db
        )

        with pytest.raises(DuplicateValueException):
            await write_dive_form_preset(
                Mock(),
                DiveFormPresetCreate(user_uuid=diver.uuid, name="WARM WATER", hidden_fields=[]),
                caller,
                async_db,
            )

    @pytest.mark.asyncio
    async def test_renaming_a_preset_to_the_name_it_already_has_is_not_a_conflict(
        self, db: Session, async_db: AsyncSession
    ) -> None:
        """The `exclude_id` on the duplicate check, which is what keeps a preset from
        conflicting with itself. A panel that sends the whole form on save - name and hidden
        set together - hits this on every "Update with current fields" where the name was not
        touched, so without it the ordinary save is a 422.
        """
        diver = create_user(db)
        caller = _caller(user_id=diver.id, user_uuid=diver.uuid)
        created = await write_dive_form_preset(
            Mock(), DiveFormPresetCreate(user_uuid=diver.uuid, name="Warm water", hidden_fields=[]), caller, async_db
        )

        result = await patch_dive_form_preset(
            Mock(),
            created.uuid,
            DiveFormPresetUpdate(name="Warm water", hidden_fields=[DiveFormField.NOTES]),
            caller,
            async_db,
        )

        assert result == {"message": "Dive form preset updated"}
        assert (await _stored_presets(async_db, diver.id))["Warm water"] == ["notes"]

    @pytest.mark.asyncio
    async def test_renaming_onto_another_presets_name_is_refused(self, db: Session, async_db: AsyncSession) -> None:
        diver = create_user(db)
        caller = _caller(user_id=diver.id, user_uuid=diver.uuid)
        await write_dive_form_preset(
            Mock(), DiveFormPresetCreate(user_uuid=diver.uuid, name="Warm water", hidden_fields=[]), caller, async_db
        )
        second = await write_dive_form_preset(
            Mock(), DiveFormPresetCreate(user_uuid=diver.uuid, name="Cold water", hidden_fields=[]), caller, async_db
        )

        with pytest.raises(DuplicateValueException):
            await patch_dive_form_preset(Mock(), second.uuid, DiveFormPresetUpdate(name="warm water"), caller, async_db)

    @pytest.mark.asyncio
    async def test_two_accounts_may_each_hold_the_same_name(
        self, db: Session, async_db: AsyncSession, other_diver: User
    ) -> None:
        """The uniqueness is per account, not global - the index is on `(user_id,
        lower(name))`, and a rule that read it as global would make the second diver on an
        instance unable to seed."""
        diver = create_user(db)
        await write_dive_form_preset(
            Mock(),
            DiveFormPresetCreate(user_uuid=diver.uuid, name="Warm water", hidden_fields=[]),
            _caller(user_id=diver.id, user_uuid=diver.uuid),
            async_db,
        )

        created = await write_dive_form_preset(
            Mock(),
            DiveFormPresetCreate(user_uuid=other_diver.uuid, name="Warm water", hidden_fields=[]),
            _caller(user_id=other_diver.id, user_uuid=other_diver.uuid),
            async_db,
        )

        assert created.user_uuid == other_diver.uuid


async def _stored_presets(async_db: AsyncSession, user_id: int) -> dict[str, list[str]]:
    rows = (await async_db.execute(select(DiveFormPreset).where(DiveFormPreset.user_id == user_id))).scalars().all()
    return {row.name: list(row.hidden_fields) for row in rows}
