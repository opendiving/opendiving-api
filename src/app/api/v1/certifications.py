import uuid as uuid_pkg
from typing import Annotated, Any, NoReturn, cast

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import fetch_owned_or_raise, get_current_user
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import ForbiddenException, NotFoundException, UnprocessableEntityException
from ...core.utils.cache import cache
from ...core.utils.pagination import clamp_pagination
from ...core.utils.uploads import content_disposition_attachment
from ...crud.crud_certifications import (
    crud_certifications,
    get_certifications_page,
    get_expiring_overview_for_user,
)
from ...crud.crud_courses import get_course_uuids_by_ids, resolve_course_id_for_user
from ...schemas.certification import (
    CertificationCreate,
    CertificationCreateInternal,
    CertificationExpiringResponse,
    CertificationFileInfo,
    CertificationRead,
    CertificationReadInternal,
    CertificationSide,
    CertificationUpdateRequest,
    validate_agency_pairing,
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

# The one integrity failure a certification write can hit. `course_id` is the only FK on
# this table that a request can set, and it is resolved by a separate query - so a course
# hard-deleted in the window between `resolve_course_id_for_user` and the write reaches
# Postgres as a violation of this constraint.
#
# 422 rather than a raw 500, and the same sentence a foreign or missing uuid already gets:
# from the caller's side the two are the same thing, and the dive routes answer that way
# for the identical FK (`_fk_error_detail` in `dives.py`). This is the narrow shape of
# that helper rather than a copy of it - the other constraints on this table are `NOT
# NULL`s the schemas already refuse, so there is nothing else here to translate.
_COURSE_FK_CONSTRAINT = "certification_course_id_fkey"


async def _refuse_a_vanished_course(db: AsyncSession, exc: IntegrityError) -> NoReturn:
    """Turn a lost `course_id` reference into the 422 the rest of the API gives for one.

    Rolls back first: the failed statement leaves the session in an aborted transaction,
    and anything the route did afterwards (a cache invalidation, say) would run against a
    connection that refuses every command. Matches `write_dive`'s handling.

    Re-raises anything else untouched. A constraint this does not recognize is a bug
    somewhere else, and dressing it as "Course not found." would send the caller after the
    wrong thing - the failure `_fk_error_detail`'s own comment records.
    """
    await db.rollback()
    if _COURSE_FK_CONSTRAINT in str(exc.orig):
        raise UnprocessableEntityException("Course not found.") from exc
    raise exc


def _to_public_certification(
    db_certification: CertificationReadInternal | dict[str, Any],
    *,
    user_uuid: uuid_pkg.UUID,
    course_uuid: uuid_pkg.UUID | None = None,
    files: list[CertificationFileInfo] | None = None,
) -> CertificationRead:
    """Convert an internal certification representation (integer FKs) into its public
    shape (owning user and training course referenced by `uuid`, card file metadata
    embedded).

    `files` defaults to empty rather than being fetched here, so the read paths can
    resolve a whole page's files in one batched query - and so `write_certification` can
    skip the lookup entirely, a brand-new certification provably having none.
    `course_uuid` is passed in for the same reason: this stays a synchronous pure
    function, and each caller resolves the value the cheapest way it can - batched across
    a page for the readers, straight off the request body for the create path.
    """
    data = db_certification if isinstance(db_certification, dict) else db_certification.model_dump()
    return CertificationRead(
        **{k: v for k, v in data.items() if k not in ("id", "user_id", "course_id")},
        user_uuid=user_uuid,
        course_uuid=course_uuid,
        files=files or [],
    )


def _validate_agency_pairing(agency: str, agency_other: str | None) -> None:
    """Enforce the `agency`/`agency_other` pairing on a PATCH's merged result.

    `CertificationBase` already does this for whole-object writes, but a PATCH may carry
    either field alone, so the check can only be made once the incoming values have been
    merged over the stored ones. The comparison itself stays in the schema layer so this
    path and the whole-object one cannot drift apart; only the way it is reported differs,
    a `ValueError` there being a per-field 422 and this one the flat `{"detail": ...}`
    every other route-level refusal returns.
    """
    try:
        validate_agency_pairing(agency, agency_other)
    except ValueError as e:
        raise UnprocessableEntityException(str(e)) from e


async def _get_owned_certification(
    db: AsyncSession, uuid: uuid_pkg.UUID, current_user: dict, *, include_deleted: bool = False
) -> CertificationReadInternal:
    """Fetch a certification by public uuid and assert the caller owns it.

    Thin wrapper over `fetch_owned_or_raise` - see there for why someone else's row reads
    as a 404 and, in particular, why this must run before any `@cache`-wrapped read
    helper.
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

    `course_uuid` optionally links the card to the training course that issued it; one
    that isn't the caller's own - or doesn't exist - is a 422, the same answer
    `POST /dive` gives for a trip it cannot resolve.
    """
    if current_user["uuid"] != certification.user_uuid:
        raise ForbiddenException()

    course_id: int | None = None
    if certification.course_uuid is not None:
        course_id = await resolve_course_id_for_user(
            db=db, course_uuid=certification.course_uuid, user_id=current_user["id"]
        )
        if course_id is None:
            raise UnprocessableEntityException("Course not found.")

    certification_internal = CertificationCreateInternal(
        **certification.model_dump(exclude={"user_uuid", "course_uuid"}),
        user_id=current_user["id"],
        course_id=course_id,
    )
    try:
        created = await crud_certifications.create(
            db=db, object=certification_internal, schema_to_select=CertificationReadInternal, return_as_model=True
        )
    except IntegrityError as e:
        await _refuse_a_vanished_course(db, e)
    await invalidate_certification_caches(current_user["id"])

    return _to_public_certification(
        cast(CertificationReadInternal, created),
        user_uuid=current_user["uuid"],
        course_uuid=certification.course_uuid,
    )


@cache(
    key_prefix="user_{user_id}_certifications:page_{page}:items_per_page:{items_per_page}:course_{course_id}",
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
    course_id: int | None,
) -> dict:
    """Fetches (and caches) a user's paginated certification list.

    Like the other cached read helpers, this must only ever be called after the caller's
    authorization has been checked by the route - `@cache` serves cached responses
    without re-running any authorization logic.

    Hand-written rather than built with `OwnedResourceCache` because the list embeds each
    row's card file metadata, which that factory's straight `get_multi`-plus-conversion
    shape has no room for (its own docstring says as much).

    Keyed and filtered by the internal `course_id` (rather than the caller-supplied uuid)
    since the route has already resolved it - the same shape `_cached_read_dives` uses,
    and for the same reason: the filter is a dimension the answer varies on, so it has to
    appear in the key.
    """
    # Not `get_multi`: the list wants dateless cards *last*, and `sort_orders` cannot say
    # `NULLS LAST` - see `_LIST_ORDER` in `crud_certifications`.
    data = await get_certifications_page(
        db=db,
        user_id=user_id,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        course_id=course_id,
    )
    # One batched query for the whole page's card files rather than one per row. The rows
    # are whole-table dicts, so each still carries its internal `id`.
    files_by_certification = await get_file_infos_for_certifications(
        db=db, certification_ids=[item["id"] for item in data["data"]]
    )
    referenced_course_ids = [item["course_id"] for item in data["data"] if item["course_id"] is not None]
    course_uuid_by_id = await get_course_uuids_by_ids(db=db, course_ids=referenced_course_ids, user_id=user_id)
    data["data"] = [
        _to_public_certification(
            item,
            user_uuid=user_uuid,
            course_uuid=course_uuid_by_id.get(item["course_id"]) if item["course_id"] is not None else None,
            files=files_by_certification[item["id"]],
        ).model_dump()
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
    course_uuid: uuid_pkg.UUID | None = None,
) -> dict:
    """List a user's certifications, newest first.

    `course_uuid` narrows the list to the cards one training course issued - which is what
    a course's own page reads. One naming a course that doesn't exist or isn't the
    caller's returns an empty page rather than an error, exactly as `GET /dives`' filters
    do, so it reveals nothing about whether that course exists.
    """
    if current_user["uuid"] != user_uuid:
        raise ForbiddenException()

    page, items_per_page = clamp_pagination(page, items_per_page)

    course_id: int | None = None
    if course_uuid is not None:
        # -1 is a sentinel that can never match a real course, so filtering safely yields
        # an empty result set for a nonexistent/foreign course uuid.
        course_id = await resolve_course_id_for_user(db=db, course_uuid=course_uuid, user_id=current_user["id"]) or -1

    return await _cached_read_certifications(
        request,
        user_id=current_user["id"],
        user_uuid=user_uuid,
        db=db,
        page=page,
        items_per_page=items_per_page,
        course_id=course_id,
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

    course_uuid: uuid_pkg.UUID | None = None
    if db_certification.course_id is not None:
        course_uuid_by_id = await get_course_uuids_by_ids(
            db=db, course_ids=[db_certification.course_id], user_id=user_id
        )
        course_uuid = course_uuid_by_id.get(db_certification.course_id)

    return _to_public_certification(
        db_certification, user_uuid=owner_uuid, course_uuid=course_uuid, files=files[db_certification.id]
    )


@router.get("/certification/{uuid}", response_model=CertificationRead)
async def read_certification(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> CertificationRead:
    """Return a single certification, with metadata for any stored card images.

    404 when no such certification exists - and the same 404 when it belongs to another
    user, so someone else's uuid stays unprobeable. The image bytes themselves are served
    by `GET /certification/{uuid}/file/{side}`.
    """
    await _get_owned_certification(db, uuid, current_user)
    return await _cached_read_certification(
        request, user_id=current_user["id"], uuid=uuid, owner_uuid=current_user["uuid"], db=db
    )


@router.patch("/certification/{uuid}")
async def patch_certification(
    request: Request,
    uuid: uuid_pkg.UUID,
    values: CertificationUpdateRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Partially update a certification; omitted fields are left untouched.

    404 unless the caller owns it, exactly as for a certification that doesn't exist.
    `agency` and `agency_other` are validated as a pair against the resulting values, so
    clearing one while the other still requires it is a 422 rather than a half-updated
    row. Passing `null` for `course_uuid` detaches the card from its training course,
    which is distinct from omitting the key; a course that isn't the caller's own is a
    422.
    """
    db_certification = await _get_owned_certification(db, uuid, current_user)

    update_data = values.model_dump(exclude={"course_uuid"}, exclude_unset=True)
    if "agency" in update_data or "agency_other" in update_data:
        _validate_agency_pairing(
            update_data.get("agency", db_certification.agency),
            update_data.get("agency_other", db_certification.agency_other),
        )

    # Keyed off `model_fields_set` rather than the value, so an explicit null (detach) is
    # distinguishable from an omitted key (leave alone) - the same branch `patch_dive` has
    # for `trip_uuid`.
    if "course_uuid" in values.model_fields_set:
        if values.course_uuid is None:
            update_data["course_id"] = None
        else:
            course_id = await resolve_course_id_for_user(
                db=db, course_uuid=values.course_uuid, user_id=db_certification.user_id
            )
            if course_id is None:
                raise UnprocessableEntityException("Course not found.")
            update_data["course_id"] = course_id

    if update_data:
        try:
            await crud_certifications.update(db=db, object=update_data, uuid=uuid)
        except IntegrityError as e:
            await _refuse_a_vanished_course(db, e)
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
            "Content-Disposition": content_disposition_attachment(file.original_filename, default="card"),
            # The stored type is sniffed from the bytes, but say so explicitly: the
            # browser must not be free to re-interpret user-uploaded content as something
            # scriptable.
            "X-Content-Type-Options": "nosniff",
            # `frame-ancestors` is spelled out because it does not fall back to
            # `default-src`: a response with its own policy opts out of
            # `SecurityHeadersMiddleware`'s default and would otherwise be framable
            # however strict the rest of this is.
            "Content-Security-Policy": "default-src 'none'; sandbox; frame-ancestors 'none'",
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
    """Delete one side's card image from a certification, leaving the certification itself.

    404 unless the caller owns it, exactly as for a certification that doesn't exist, and
    404 again when that side has no image stored - so this is not idempotent: a repeat
    delete reports the absence rather than succeeding quietly.
    """
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
