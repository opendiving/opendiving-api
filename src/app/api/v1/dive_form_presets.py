import uuid as uuid_pkg
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import DuplicateValueException, ForbiddenException
from ...core.utils.pagination import clamp_pagination
from ...crud.crud_dive_form_presets import crud_dive_form_presets, dive_form_preset_name_exists
from ...schemas.dive_form_preset import (
    DiveFormPresetCreate,
    DiveFormPresetCreateInternal,
    DiveFormPresetRead,
    DiveFormPresetReadInternal,
    DiveFormPresetUpdate,
)
from ...services.dive_form_presets import seed_default_presets

router = APIRouter(tags=["dive-form-presets"])

_DUPLICATE_NAME = "A dive form preset with this name already exists"


async def _get_owned_dive_form_preset(
    db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict
) -> DiveFormPresetReadInternal:
    """Fetch a preset by public uuid and assert the caller owns it.

    Thin wrapper over `fetch_owned_or_raise` - see there for why someone else's row reads as
    a 404.
    """
    return await fetch_owned_or_raise(
        db=db,
        crud=crud_dive_form_presets,
        uuid=uuid,
        current_user=current_user,
        schema=DiveFormPresetReadInternal,
        not_found_message="Dive form preset not found",
    )


def _to_public(preset: DiveFormPresetReadInternal | dict[str, Any], *, user_uuid: uuid_pkg.UUID) -> DiveFormPresetRead:
    """Convert an internal preset representation (integer FKs) into its public shape, with
    the owning user referenced by `uuid`."""
    data = preset if isinstance(preset, dict) else preset.model_dump()
    return DiveFormPresetRead(**{k: v for k, v in data.items() if k not in ("id", "user_id")}, user_uuid=user_uuid)


@router.post("/dive-form-preset", response_model=DiveFormPresetRead, status_code=201)
async def write_dive_form_preset(
    request: Request,
    preset: DiveFormPresetCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveFormPresetRead:
    """Save the current set of hidden dive-form fields under a name.

    `user_uuid` must be the caller's own (403 otherwise). Preset names are unique per
    account, case-insensitively, so reusing one is a 422 - the same rule trips, dive sites
    and gear sets keep.

    `hidden_fields` may arrive in any order and with repeats; what is stored is the
    canonical form - form order, duplicates collapsed - so two equal sets are two equal
    lists and a client can decide which preset matches the account's current state by
    comparing them element by element. A name the vocabulary does not contain is a 422.
    """
    if current_user["uuid"] != preset.user_uuid:
        raise ForbiddenException()

    if await dive_form_preset_name_exists(db=db, user_id=current_user["id"], name=preset.name):
        raise DuplicateValueException(_DUPLICATE_NAME)

    created = await crud_dive_form_presets.create(
        db=db,
        object=DiveFormPresetCreateInternal(
            user_id=current_user["id"], name=preset.name, hidden_fields=preset.hidden_fields
        ),
        schema_to_select=DiveFormPresetReadInternal,
        return_as_model=True,
    )
    return _to_public(created, user_uuid=current_user["uuid"])


@router.get("/dive-form-presets", response_model=PaginatedListResponse[DiveFormPresetRead])
async def read_dive_form_presets(
    request: Request,
    user_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
) -> dict:
    """List the caller's dive form presets alphabetically.

    `user_uuid` must be the caller's own (403 otherwise). Out-of-range pagination is
    clamped, not rejected.

    Deliberately not Redis-cached, unlike the other per-user owned resources: nothing
    embeds a preset, so there is nothing to go stale anywhere else, and the panel that reads
    this list reads it once when it opens (see `OwnedResourceCache`'s docstring, which names
    every resource that opts out and why).
    """
    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    page, items_per_page = clamp_pagination(page, items_per_page)

    data = await crud_dive_form_presets.get_multi(
        db=db,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        user_id=current_user["id"],
        sort_columns="name",
        sort_orders="asc",
    )
    data["data"] = [_to_public(row, user_uuid=user_uuid).model_dump() for row in data["data"]]

    response: dict[str, Any] = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
    return response


@router.get("/dive-form-preset/{uuid}", response_model=DiveFormPresetRead)
async def read_dive_form_preset(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> DiveFormPresetRead:
    """Return a single dive form preset.

    404 when no such preset exists - and the same 404 when it belongs to another account, so
    someone else's uuid stays unprobeable.
    """
    preset = await _get_owned_dive_form_preset(db, uuid, current_user)
    return _to_public(preset, user_uuid=current_user["uuid"])


@router.patch("/dive-form-preset/{uuid}")
async def patch_dive_form_preset(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: DiveFormPresetUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Rename a preset, replace its hidden set, or both.

    `hidden_fields` replaces the preset's set wholesale - this is what "Update with current
    fields" sends - and is stored canonically, exactly as on create. Renaming onto a name
    the account already holds is a 422, compared case-insensitively.

    A preset is a snapshot: nothing here touches `user.dive_form_hidden_fields`, so editing
    the preset a diver is currently arranged like does not rearrange their form. Applying it
    is a separate `PATCH /user`.
    """
    preset = await _get_owned_dive_form_preset(db, uuid, current_user)

    if values.name is not None and await dive_form_preset_name_exists(
        db=db, user_id=preset.user_id, name=values.name, exclude_id=preset.id
    ):
        raise DuplicateValueException(_DUPLICATE_NAME)

    update_data = values.model_dump(exclude_unset=True)
    if update_data:
        await crud_dive_form_presets.update(db=db, object=update_data, uuid=uuid)

    return {"message": "Dive form preset updated"}


@router.delete("/dive-form-preset/{uuid}")
async def erase_dive_form_preset(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Delete a preset. The row really goes, and nothing points at it: a preset is a
    shortcut for filling in one column, so deleting the one the diver is currently arranged
    like leaves their form exactly as it is.

    404 unless the caller owns it, and a second `DELETE` on the same uuid is a 404 too. One
    of the three seeded defaults deleted this way comes back from
    `POST /dive-form-presets/defaults`.
    """
    await _get_owned_dive_form_preset(db, uuid, current_user)

    await crud_dive_form_presets.delete(db=db, uuid=uuid)

    return {"message": "Dive form preset deleted"}


@router.post("/dive-form-presets/defaults", response_model=list[DiveFormPresetRead])
async def restore_default_dive_form_presets(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> list[DiveFormPresetRead]:
    """Add back whichever of the three default presets - Basic, Recreational, Technical -
    this account no longer has, matched by name case-insensitively.

    **Adds what is missing; never overwrites.** A default the diver edited keeps their edit,
    and one they renamed stays under its new name with the original created beside it. So
    this is idempotent and a client may offer it whenever the diver asks: with all three
    present it creates nothing and answers with an empty list.

    Returns only what it created, in the order the defaults are declared, which is what lets
    a client say how many were added.
    """
    created = await seed_default_presets(db, user_id=current_user["id"])
    return [_to_public(preset, user_uuid=current_user["uuid"]) for preset in created]
