import uuid as uuid_pkg
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import ForbiddenException, NotFoundException, UnprocessableEntityException
from ...core.utils.cache import cache
from ...core.utils.pagination import clamp_pagination
from ...crud.crud_certifications import crud_certifications, get_expiring_overview_for_user
from ...schemas.certification import (
    CertificationAgency,
    CertificationCreate,
    CertificationCreateInternal,
    CertificationExpiringResponse,
    CertificationFileInfo,
    CertificationRead,
    CertificationReadInternal,
    CertificationSide,
    CertificationUpdate,
)
from ...services.cache_invalidation import invalidate_certification_caches
from ...services.certification_files import (
    UnsupportedCardFileError,
    delete_certification_file,
    delete_files_for_certification,
    get_certification_file_sha256,
    get_file_infos_for_certifications,
    load_certification_file,
    store_certification_file,
)

router = APIRouter(tags=["certifications"])

# `GET /certifications-expiring` returns every dated card rather than a date-filtered
# slice (see `CertificationExpiringResponse`), so it needs *some* bound. Mirrors
# `DUE_OVERVIEW_LIMIT` in `gear_service.py`; a diver holding 200 expiring certifications
# is not the case this card is sized for, and `truncated` tells them so.
EXPIRING_OVERVIEW_LIMIT = 200


def _to_public_certification(
    db_certification: CertificationReadInternal | dict[str, Any],
    *,
    user_uuid: uuid_pkg.UUID,
    files: list[CertificationFileInfo] | None = None,
) -> CertificationRead:
    """Convert an internal certification representation (integer FKs) into its public
    shape (owning user referenced by `uuid`, card file metadata embedded).

    `files` defaults to empty rather than being fetched here, so the read paths can
    resolve a whole page's files in one batched query - and so `write_certification` can
    skip the lookup entirely, a brand-new certification provably having none.
    """
    data = db_certification if isinstance(db_certification, dict) else db_certification.model_dump()
    return CertificationRead(
        **{k: v for k, v in data.items() if k not in ("id", "user_id")},
        user_uuid=user_uuid,
        files=files or [],
    )


def _validate_agency_pairing(agency: CertificationAgency, agency_other: str | None) -> None:
    """Enforce the `agency`/`agency_other` pairing on a PATCH's merged result.

    `CertificationBase` already does this for whole-object writes, but a PATCH may carry
    either field alone, so the check can only be made once the incoming values have been
    merged over the stored ones.
    """
    if agency == CertificationAgency.OTHER:
        if not (agency_other or "").strip():
            raise UnprocessableEntityException("agency_other is required when agency is 'other'")
    elif agency_other is not None:
        raise UnprocessableEntityException("agency_other may only be set when agency is 'other'")


async def _get_owned_certification(
    db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict, *, include_deleted: bool = False
) -> CertificationReadInternal:
    """Fetch a certification by public uuid and assert the caller owns it.

    Thin wrapper over `fetch_owned_or_raise` - see there for the 404/403 split and, in
    particular, why this must run before any `@cache`-wrapped read helper.
    """
    return await fetch_owned_or_raise(
        db=db,
        crud=crud_certifications,
        uuid=uuid,
        current_user=current_user,
        schema=CertificationReadInternal,
        not_found_message="Certification not found",
        include_deleted=include_deleted,
    )


@router.post("/certification", response_model=CertificationRead, status_code=201)
async def write_certification(
    request: Request,
    certification: CertificationCreate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> CertificationRead:
    """Create a certification. Card images are attached afterwards with
    `PUT /certification/{uuid}/file/{side}` - see that route for why.
    """
    if current_user["uuid"] != certification.user_uuid:
        raise ForbiddenException()

    certification_internal = CertificationCreateInternal(
        **certification.model_dump(exclude={"user_uuid"}), user_id=current_user["id"]
    )
    created = await crud_certifications.create(
        db=db, object=certification_internal, schema_to_select=CertificationReadInternal, return_as_model=True
    )
    await invalidate_certification_caches(current_user["id"])

    return _to_public_certification(cast(CertificationReadInternal, created), user_uuid=current_user["uuid"])


@cache(
    key_prefix="user_{user_id}_certifications:page_{page}:items_per_page:{items_per_page}",
    resource_id_name="user_id",
    expiration=60,
)
async def _cached_read_certifications(
    request: Request,
    user_id: int,
    user_uuid: uuid_pkg.UUID,
    db: AsyncSession,
    page: int,
    items_per_page: int,
) -> dict:
    """Fetches (and caches) a user's paginated certification list.

    Like the other cached read helpers, this must only ever be called after the caller's
    authorization has been checked by the route - `@cache` serves cached responses
    without re-running any authorization logic.

    Hand-written rather than built with `OwnedResourceCache` because the list embeds each
    row's card file metadata, which that factory's straight `get_multi`-plus-conversion
    shape has no room for (its own docstring says as much).
    """
    data = await crud_certifications.get_multi(
        db=db,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        user_id=user_id,
        is_deleted=False,
        # Newest certification first, matching `ix_certification_user_id_certified_on`.
        # `uuid` breaks ties: it's uuid7, so it orders by creation time, which keeps
        # pagination stable across pages when several cards share a date (or have none).
        sort_columns=["certified_on", "uuid"],
        sort_orders=["desc", "desc"],
    )
    # One batched query for the whole page's card files rather than one per row.
    # `get_multi` passes no `schema_to_select`, so each row still carries its internal `id`.
    files_by_certification = await get_file_infos_for_certifications(
        db=db, certification_ids=[item["id"] for item in data["data"]]
    )
    data["data"] = [
        _to_public_certification(item, user_uuid=user_uuid, files=files_by_certification[item["id"]]).model_dump()
        for item in data["data"]
    ]

    response: dict[str, Any] = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
    return response


@router.get("/certifications", response_model=PaginatedListResponse[CertificationRead])
async def read_certifications(
    request: Request,
    user_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
) -> dict:
    """List a user's certifications, newest first."""
    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    page, items_per_page = clamp_pagination(page, items_per_page)

    return await _cached_read_certifications(
        request,
        user_id=current_user["id"],
        user_uuid=user_uuid,
        db=db,
        page=page,
        items_per_page=items_per_page,
    )


@cache(key_prefix="user_{user_id}_certification", resource_id_name="uuid", resource_id_type=uuid_pkg.UUID)
async def _cached_read_certification(
    request: Request, user_id: int, uuid: uuid_pkg.UUID, owner_uuid: uuid_pkg.UUID, db: AsyncSession
) -> CertificationRead:
    """Fetches (and caches) a single certification by uuid. Authorization is checked by
    the route before this is ever reached - see `_cached_read_certifications`.
    """
    db_certification = await crud_certifications.get(
        db=db, uuid=uuid, is_deleted=False, schema_to_select=CertificationReadInternal, return_as_model=True
    )
    if db_certification is None:
        raise NotFoundException("Certification not found")

    db_certification = cast(CertificationReadInternal, db_certification)
    files = await get_file_infos_for_certifications(db=db, certification_ids=[db_certification.id])
    return _to_public_certification(db_certification, user_uuid=owner_uuid, files=files[db_certification.id])


@router.get("/certification/{uuid}", response_model=CertificationRead)
async def read_certification(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> CertificationRead:
    await _get_owned_certification(db, uuid, current_user)
    return await _cached_read_certification(
        request, user_id=current_user["id"], uuid=uuid, owner_uuid=current_user["uuid"], db=db
    )


@router.patch("/certification/{uuid}")
async def patch_certification(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: CertificationUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_certification = await _get_owned_certification(db, uuid, current_user)

    update_data = values.model_dump(exclude_unset=True)
    if "agency" in update_data or "agency_other" in update_data:
        _validate_agency_pairing(
            update_data.get("agency", CertificationAgency(db_certification.agency)),
            update_data.get("agency_other", db_certification.agency_other),
        )

    if update_data:
        await crud_certifications.update(db=db, object=update_data, uuid=uuid)
        await invalidate_certification_caches(db_certification.user_id)

    return {"message": "Certification updated"}


@router.delete("/certification/{uuid}")
async def erase_certification(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Soft-deletes a certification and hard-deletes its card files.

    The files go for good because they are the bulk of what this feature stores and
    nothing can read them once the certification is gone; the row itself is only
    soft-deleted, matching every other resource here. `is_deleted` is application-level,
    so the `ON DELETE CASCADE` on `certification_file.certification_id` never fires and
    the files have to be removed explicitly.
    """
    db_certification = await _get_owned_certification(db, uuid, current_user)

    await delete_files_for_certification(db=db, certification_id=db_certification.id, commit=False)
    await crud_certifications.delete(db=db, uuid=uuid)
    await invalidate_certification_caches(db_certification.user_id)

    return {"message": "Certification deleted"}


# -------------- card files --------------
#
# Uploads are a separate `PUT` rather than multipart on `POST /certification` for three
# reasons: it keeps the create endpoint a plain JSON one, identical in shape to every
# other resource here; replacing a card image becomes idempotent; and a diver can enter a
# certification now and photograph the card later, which is how it usually goes.


@router.put("/certification/{uuid}/file/{side}", response_model=CertificationFileInfo)
async def write_certification_file(
    request: Request,
    uuid: uuid_pkg.UUID,
    side: CertificationSide,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    file: Annotated[UploadFile, File(description="Photo or PDF of the card (JPEG, PNG, WEBP or PDF, max 10 MB)")],
) -> CertificationFileInfo:
    """Attach or replace one side's card image. Uploading a side that already has a file
    replaces it.
    """
    db_certification = await _get_owned_certification(db, uuid, current_user)

    try:
        info = await store_certification_file(db=db, certification_id=db_certification.id, side=side, upload=file)
    except UnsupportedCardFileError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc

    # Certification reads embed this file's metadata, so they're now stale.
    await invalidate_certification_caches(db_certification.user_id)
    return info


@router.get("/certification/{uuid}/file/{side}")
async def read_certification_file(
    request: Request,
    uuid: uuid_pkg.UUID,
    side: CertificationSide,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    v: Annotated[
        str | None,
        Query(description="Opaque cache-busting version token; ignored by the server"),
    ] = None,
) -> Response:
    """Serve one side's stored bytes to its owner.

    Deliberately *not* `@cache`d: Redis here holds serialized API responses, and parking
    multi-megabyte binaries in it would evict everything else the cache exists for. The
    `ETag`/`If-None-Match` pair below does the equivalent job in the browser, where the
    bytes are wanted anyway.

    `v` is read by nothing here. It is declared so the contract is visible rather than
    looking like a stray param: the response is cacheable for five minutes, and a card
    image can be *replaced* at this same URL, so the client varies `v` to give each
    version its own cache entry. Without it a diver who re-photographs a card keeps
    seeing the old one until the cache expires.
    """
    db_certification = await _get_owned_certification(db, uuid, current_user)

    # Check the hash before loading the bytes, so a conditional request costs one narrow
    # query rather than a full read that gets thrown away.
    sha256 = await get_certification_file_sha256(db=db, certification_id=db_certification.id, side=side)
    if sha256 is None:
        raise NotFoundException(f"No {side.value} image for this certification")

    etag = f'"{sha256}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, max-age=300"})

    file = await load_certification_file(db=db, certification_id=db_certification.id, side=side)
    if file is None:
        raise NotFoundException(f"No {side.value} image for this certification")

    return Response(
        content=file.data,
        media_type=file.content_type,
        headers={
            # `attachment`, not `inline`: the web app fetches this through its API client
            # and renders from a blob URL, so it never navigates here directly. Should
            # someone open the URL in a tab anyway, `attachment` stops a malicious PDF
            # from executing in the same-origin viewer.
            "Content-Disposition": f'attachment; filename="{file.original_filename}"',
            # The stored type is sniffed from the bytes, but say so explicitly: the
            # browser must not be free to re-interpret user-uploaded content as something
            # scriptable.
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            # `private` because this is one diver's card and no shared cache should keep
            # a copy; the `ETag` makes the re-validation after 5 minutes cheap.
            "Cache-Control": "private, max-age=300",
            "ETag": etag,
        },
    )


@router.delete("/certification/{uuid}/file/{side}")
async def erase_certification_file(
    request: Request,
    uuid: uuid_pkg.UUID,
    side: CertificationSide,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    db_certification = await _get_owned_certification(db, uuid, current_user)

    deleted = await delete_certification_file(db=db, certification_id=db_certification.id, side=side)
    if not deleted:
        raise NotFoundException(f"No {side.value} image for this certification")

    await invalidate_certification_caches(db_certification.user_id)
    return {"message": "Certification file deleted"}


# -------------------- dashboard --------------------
@cache(key_prefix="user_{user_id}_certifications_expiring", resource_id_name="user_id", expiration=60)
async def _cached_read_expiring(request: Request, user_id: int, db: AsyncSession) -> dict:
    """Fetches (and caches) the user's whole dated-certification list. Authorization
    happens in the route - see `_cached_read_certifications`.

    The key starts with `user_{id}_certification`, so the existing
    `invalidate_certification_caches` pattern already covers it; nothing extra to call.
    """
    data, truncated = await get_expiring_overview_for_user(db=db, user_id=user_id, limit=EXPIRING_OVERVIEW_LIMIT)
    return CertificationExpiringResponse(data=data, truncated=truncated).model_dump()


@router.get("/certifications-expiring", response_model=CertificationExpiringResponse)
async def read_certifications_expiring(
    request: Request,
    user_uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict:
    """Every certification the user owns that has an expiry date, soonest first.

    The certification twin of `GET /gear-service-due`, and it exists for the same
    reason: without it a dashboard card has to page through a diver's entire
    certification list client-side to find the few that are expiring, since an expiry is
    just as likely to sit on the oldest card as the newest and the list endpoint sorts by
    neither.

    Takes no date horizon on purpose: filtering by "expiring within N days" server-side
    would bake today's date into the cached response, which then quietly goes wrong at
    midnight. The client buckets into expiring-soon/expired itself.
    """
    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    return await _cached_read_expiring(request, user_id=current_user["id"], db=db)
