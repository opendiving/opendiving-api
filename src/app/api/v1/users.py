import uuid as uuid_pkg
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Annotated, Any, cast

from fastapi import APIRouter, Cookie, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import current_session_uuid, get_current_user, refuse_api_keys
from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import (
    BadRequestException,
    DuplicateValueException,
    NotFoundException,
    UnauthorizedException,
    UnprocessableEntityException,
)
from ...core.security import bearer_scheme, blacklist_token, blacklist_tokens, generate_secure_token, hash_token
from ...core.utils.binary_responses import not_modified, stored_bytes_response
from ...core.utils.cache import cache, delete_keys_by_pattern
from ...core.utils.client_ip import client_ip_bucket
from ...core.utils.pagination import DEFAULT_MAX_ITEMS_PER_PAGE, clamp_pagination
from ...core.utils.rate_limit import enforce_rate_limit
from ...core.utils.request_context import RequestContext
from ...core.utils.uploads import content_disposition_attachment
from ...crud.crud_auth_audit_events import record_auth_event
from ...crud.crud_authentication_requests import (
    claim_authentication_request,
    crud_authentication_requests,
    invalidate_live_requests,
)
from ...crud.crud_dive_form_presets import crud_dive_form_presets
from ...crud.crud_dive_sites import resolve_dive_site_ids_for_user
from ...crud.crud_invitations import account_exists_for
from ...crud.crud_trip_parts import get_places_for_user
from ...crud.crud_user_dive_stats import crud_user_dive_stats
from ...crud.crud_user_sessions import revoke_session
from ...crud.crud_users import crud_users
from ...models.dive_site import DiveSite
from ...models.user import User
from ...schemas.auth import LinkCheckResponse
from ...schemas.auth_audit_event import AuthEventType
from ...schemas.authentication_request import AuthenticationRequestCreate
from ...schemas.dive import DiveActivityPoint, DiveGasUsePoint
from ...schemas.email_change import (
    EmailChangeRequest,
    EmailChangeRequestResponse,
    EmailChangeVerifyRequest,
    EmailChangeVerifyResponse,
)
from ...schemas.location import LocationRead
from ...schemas.species import SpeciesLifeListEntry, SpeciesSuggestResponse
from ...schemas.storage import StorageUsageRead
from ...schemas.user import AccountDeletionResponse, UserRead, UserUpdate
from ...schemas.user_dive_stats import UserDiveStatsRead, UserDiveStatsReadInternal
from ...schemas.user_picture import PictureCrop, PictureCropRequest, PictureKind, PictureRead
from ...services.dive_activity import dive_activity
from ...services.dive_gas import gas_use_history
from ...services.dive_stats import sites_and_dive_days
from ...services.email_service import (
    send_account_deletion_email,
    send_best_effort,
    send_email_change_confirmation_email,
    send_email_changed_notification,
)
from ...services.species_life_list import species_life_list, species_life_list_detail, suggest_species
from ...services.storage_usage import get_storage_usage, storage_limit_bytes
from ...services.user_pictures import (
    AVATAR_FRAME,
    MAX_PICTURE_UPLOAD_SIZE,
    PORTRAIT_FRAME,
    InvalidCropError,
    PictureChangedError,
    StoredPictureFile,
    UnsupportedPictureError,
    copy_avatar_to_portrait,
    delete_picture,
    get_original,
    get_rendition,
    read_picture_bytes,
    recrop_picture,
    store_picture,
)
from .species import enforce_species_limit

router = APIRouter(tags=["user"])

# Note: there is no `POST /user` here - account creation only ever happens via
# `POST /auth/complete` (see `api.v1.auth`), after an identity (email or Google) has
# already been verified. There is no separate signup flow.

_EMAIL_CHANGE_REQUEST_RESPONSE = EmailChangeRequestResponse()


def _replay_result_or_reject(*, current_email: str, new_email: str) -> EmailChangeVerifyResponse:
    """Decide what a *second* verification of an email-change token gets back.

    Tolerated only while the change the token represents is the account's current email:
    re-applying that grants nothing, so a mail scanner detonating the link or a user
    clicking twice reports success rather than an error. A mismatch means the account has
    moved on since (a later change request), which makes this a genuine reuse.

    Shared by the two ways a caller can arrive second - reading an already-`used_at` row,
    and losing the conditional claim to a request still in flight - so the rule can't
    drift between them.
    """
    if current_email == new_email:
        return EmailChangeVerifyResponse(email=new_email)
    raise UnauthorizedException("This confirmation link has already been used.")


# Note: there is no `GET /users` here (yet) either - a public-facing listing of
# all users has the same "other users shouldn't see email" problem as a single
# lookup by uuid, and is being designed together with the eventual public-profile
# endpoint rather than left in its previous shape (which returned full `UserRead`,
# including `email`, for every user) in the meantime.


@router.get("/user", response_model=UserRead)
async def read_current_user(request: Request, current_user: Annotated[dict, Depends(get_current_user)]) -> dict:
    """The caller's own profile.

    No operation reads another account's profile.
    """
    # The token-resolved user as is, so it costs no query of its own.
    return current_user


# Note: there is no `GET /user/{uuid}` here (yet) - looking up *other* users will be
# added later as a separate, public-profile-shaped endpoint (limited fields, no
# email) rather than reusing this module's current-user-only routes. Until then, no route
# fetches another user's data but the named exceptions - see *"Current-user routes live at
# a bare `/user`"* in DECISIONS.md.


@router.patch("/user")
async def patch_user(
    request: Request,
    values: UserUpdate,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Partially update the caller's own profile.

    A field absent from the body is left as it is. The email is not among the fields:
    changing it takes the confirmation round-trip that starts at
    `POST /user/email-change/request`. The check-in details have
    `PATCH /user/checkin-details`. A `username` another account holds is a 422, and a
    username change is rate limited per account; the rest of the profile is not.
    `dive_form_preset_uuid` names one of the caller's own presets or is `null`; anything
    else is a 422.
    """
    # `email` is not in `UserUpdate` at all; the change goes through the verified round-trip.
    # The username change is rate limited for the reason `POST /auth/complete` is per-IP: the
    # availability check below answers a distinguishable "Username not available", so
    # unthrottled it is a wordlist oracle over who exists.
    claims_a_username = values.username is not None and values.username != current_user["username"]
    if claims_a_username:
        await enforce_rate_limit(
            f"username-change:user:{current_user['id']}",
            settings.USERNAME_CHANGE_RATE_LIMIT_PER_USER,
            settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
        )
        if await crud_users.exists(db=db, username=values.username):
            raise DuplicateValueException("Username not available")

    if values.dive_form_preset_uuid is not None and not await crud_dive_form_presets.exists(
        db=db, uuid=values.dive_form_preset_uuid, user_id=current_user["id"]
    ):
        raise UnprocessableEntityException("Dive form preset not found.")

    try:
        await crud_users.update(db=db, object=values, uuid=current_user["uuid"])
    except IntegrityError:
        await db.rollback()
        # The `exists` check above loses to a concurrent claim of the same name. Anything
        # else - the picked preset deleted in between - is not about the username.
        if claims_a_username:
            raise DuplicateValueException("Username not available") from None
        raise
    return {"message": "User updated"}


# -------------- pictures --------------
#
# The avatar and the check-in portrait, five routes each and the portrait's copy, rather
# than fields on `PATCH /user`, for the reason every stored payload here gets its own: the
# bytes live in the blob store and the row carries keys, so the write has an ordering rule
# (files first, row second) and the delete has the mirror of it. A JSON PATCH that could null
# a key would leave the file behind.
#
# Self-scoped like the rest of `/user` - no uuid anywhere, so there is no ownership check to
# get backwards. The one route serving a picture to anyone else is the check-in link's
# `GET /checkin/{token}/portrait`: the portrait's rendition, to whoever holds a live link.

_PICTURE_CACHE_CONTROL = "private, max-age=300"
_MAX_UPLOAD_MB = MAX_PICTURE_UPLOAD_SIZE // (1024 * 1024)
_CROP_DESCRIPTION = (
    'The crop, as JSON: `{"x": 0, "y": 72, "width": 3024, "height": 3888}` - a rectangle in the upright '
    "image's pixels, at the picture's ratio"
)


def _form_crop(raw: str | None) -> PictureCrop | None:
    """A multipart form carries the crop as a JSON string; a bad one is the same 422 a bad
    JSON body gets, pointed at the field."""
    if raw is None:
        return None
    try:
        return PictureCrop.model_validate_json(raw)
    except ValidationError as exc:
        raise RequestValidationError(
            [{**error, "loc": ("body", "crop", *error["loc"])} for error in exc.errors(include_url=False)]
        ) from exc


@contextmanager
def _picture_errors(crop: PictureCrop | None) -> Iterator[None]:
    """The service's refusals, as the statuses the routes promise."""
    try:
        yield
    except UnsupportedPictureError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except InvalidCropError as exc:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("body", "crop"),
                    "msg": str(exc),
                    "input": crop.model_dump() if crop else None,
                }
            ]
        ) from exc
    except PictureChangedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


async def _serve_picture(request: Request, stored: StoredPictureFile | None, *, kind: PictureKind) -> Response:
    """One of a picture's files, to its owner.

    Deliberately *not* `@cache`d, for the same reason as the card download: Redis here holds
    serialized API responses, and parking binaries in it evicts what the cache is for. The
    `ETag`/`If-None-Match` pair does that job in the browser instead.

    The key and the digest come from a narrow read in the route rather than from
    `current_user`, which is a snapshot taken when the request began. Serving from the
    snapshot would let a replacement commit and unlink the old blob mid-request, turning a
    routine race into a `BlobMissingError` - which is a loud 500 by design, because a row
    naming bytes that are gone is data loss and answering 404 is how nobody ever
    investigates it.
    """
    if stored is None:
        raise NotFoundException(f"No {kind.value} set")

    etag = f'"{stored.sha256}"'
    if request.headers.get("if-none-match") == etag:
        return not_modified(etag, cache_control=_PICTURE_CACHE_CONTROL)

    # `attachment`, not `inline`: the clients fetch this through their API client and render
    # from a blob URL, so nothing ever navigates here directly.
    return stored_bytes_response(
        await read_picture_bytes(stored),
        media_type=stored.content_type,
        etag=etag,
        cache_control=_PICTURE_CACHE_CONTROL,
        disposition=content_disposition_attachment(stored.filename, default=kind.value),
    )


_VERSION_DESCRIPTION = "Opaque cache-busting version token; ignored by the server"


@router.put("/user/avatar", response_model=PictureRead)
async def write_user_avatar(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    file: Annotated[
        UploadFile,
        File(
            description=f"Profile picture, max {_MAX_UPLOAD_MB} MB: a JPEG or PNG with a crop, "
            "or a JPEG, PNG, WEBP or GIF without one"
        ),
    ],
    crop: Annotated[str | None, Form(description=f"{_CROP_DESCRIPTION}, 1:1")] = None,
) -> PictureRead:
    """Set or replace the caller's profile picture.

    One avatar per account: uploading again replaces it, and the operation is idempotent.

    **With a crop, the file is kept as the original** - a JPEG or PNG, stripped of its
    metadata (location included) without touching a pixel - and the picture shown everywhere
    is rendered from it through the crop: oriented, cropped, bounded to 512 px and encoded as
    WebP. `PATCH /user/avatar` re-crops it later. **Without one**, nothing is kept but that
    rendition, drawn from the centred square of any of four formats. A crop outside the image
    or off 1:1 is a 422; a file that will not decode, or an original that is not a JPEG or
    PNG, a 415; anything over the size limit, or a picture that would take the account past
    its storage limit, a 413.

    The response carries the rendition's digest, which is its version: append it to
    `GET /user/avatar` as `?v=` so a replacement lands on a URL the browser has not cached.
    """
    picture_crop = _form_crop(crop)
    with _picture_errors(picture_crop):
        digest = await store_picture(
            db=db, user_id=current_user["id"], frame=AVATAR_FRAME, upload=file, crop=picture_crop
        )
    return PictureRead(sha256=digest)


@router.patch("/user/avatar", response_model=PictureRead)
async def adjust_user_avatar(
    request: Request,
    body: PictureCropRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> PictureRead:
    """Re-crop the caller's profile picture from the original it holds, which is untouched.

    404 when the avatar holds no original - one seeded from Google, or uploaded without a
    crop - 409 if it is replaced or removed while this renders, and 413 if the new rendition
    is larger than the old and the account has no room for the difference. A crop outside
    the image or off 1:1 is a 422. Answers the new rendition's digest, as `PUT` does.
    """
    with _picture_errors(body.crop):
        digest = await recrop_picture(db=db, user_id=current_user["id"], frame=AVATAR_FRAME, crop=body.crop)
    if digest is None:
        raise NotFoundException("The avatar has no original to adjust")
    return PictureRead(sha256=digest)


@router.get("/user/avatar")
async def read_user_avatar(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    v: Annotated[str | None, Query(description=_VERSION_DESCRIPTION)] = None,
) -> Response:
    """The caller's own avatar: its rendition, one WebP, always.

    Privately cacheable for five minutes, with an `ETag`; `If-None-Match` answers 304. The
    avatar is *replaced* at this same URL, so pass `UserRead.avatar_sha256` as `v`, which
    the server ignores, and each version gets its own cache entry. 404 when none is set.
    """
    # `v` is read by nothing here; it is declared so the contract is visible rather than
    # looking like a stray parameter.
    return await _serve_picture(
        request, await get_rendition(db=db, user_id=current_user["id"], frame=AVATAR_FRAME), kind=PictureKind.AVATAR
    )


@router.get("/user/avatar/original")
async def read_user_avatar_original(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    v: Annotated[str | None, Query(description=_VERSION_DESCRIPTION)] = None,
) -> Response:
    """The original the caller's avatar is rendered from, for adjusting its crop.

    The file as uploaded, less its metadata, under its own name and type; 404 when none is
    held. Privately cacheable for five minutes, with an `ETag`; `If-None-Match` answers 304.
    Pass `UserRead.avatar_original_sha256` as `v`, since a replaced original is served at
    this same URL.
    """
    return await _serve_picture(
        request, await get_original(db=db, user_id=current_user["id"], frame=AVATAR_FRAME), kind=PictureKind.AVATAR
    )


@router.delete("/user/avatar")
async def erase_user_avatar(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Remove the caller's profile picture, its original with it, leaving the account alone.

    404 when there was nothing to remove. There is no confirmation step: re-uploading undoes
    it.
    """
    # 404 on nothing to remove is the shape the card-file delete established.
    if not await delete_picture(db=db, user_id=current_user["id"], frame=AVATAR_FRAME):
        raise NotFoundException("No avatar set")

    return {"message": "Avatar removed"}


@router.put("/user/portrait", response_model=PictureRead)
async def write_user_portrait(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    file: Annotated[UploadFile, File(description=f"Portrait, a JPEG or PNG, max {_MAX_UPLOAD_MB} MB")],
    crop: Annotated[str, Form(description=f"{_CROP_DESCRIPTION}, 7:9")],
) -> PictureRead:
    """Set or replace the caller's portrait: the identification photo a dive shop's desk
    sees on the check-in page, and never where the profile picture appears.

    The file is kept as the original, stripped of its metadata (location included) without
    touching a pixel, and what is shown is rendered from it through the crop: oriented,
    cropped at 7:9 - 35x45 mm in proportion, the passport photo's shape - bounded to 900 px
    high, filled with white where it is transparent, and encoded as WebP. The crop is
    required, and a crop outside the image or off 7:9 is a 422; a file that is not a JPEG
    or PNG, or will not decode, a 415; anything over the size limit, or a portrait that
    would take the account past its storage limit, a 413.

    Answers the rendition's digest, the `?v=` for `GET /user/portrait`.
    """
    picture_crop = _form_crop(crop)
    with _picture_errors(picture_crop):
        digest = await store_picture(
            db=db, user_id=current_user["id"], frame=PORTRAIT_FRAME, upload=file, crop=picture_crop
        )
    return PictureRead(sha256=digest)


@router.post("/user/portrait/from-avatar", response_model=PictureRead)
async def copy_user_avatar_to_portrait(
    request: Request,
    body: PictureCropRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> PictureRead:
    """Make the profile picture's original the portrait's too, cropped at 7:9.

    A copy under keys of its own, carrying the original's name and type, so removing or
    replacing either picture later leaves the other whole. Replaces any portrait there is.
    404 while the avatar holds no original, which is what an avatar seeded from Google or
    uploaded without a crop looks like - upload the portrait instead. A crop outside the
    image or off 7:9 is a 422, and a copy that would take the account past its storage limit
    a 413.
    """
    with _picture_errors(body.crop):
        digest = await copy_avatar_to_portrait(db=db, user_id=current_user["id"], crop=body.crop)
    if digest is None:
        raise NotFoundException("The avatar has no original to copy")
    return PictureRead(sha256=digest)


@router.patch("/user/portrait", response_model=PictureRead)
async def adjust_user_portrait(
    request: Request,
    body: PictureCropRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> PictureRead:
    """Re-crop the caller's portrait from its original, which is untouched.

    404 without a portrait, 409 if it is replaced or removed while this renders, 422 for a
    crop outside the image or off 7:9, and 413 if the new rendition is larger than the old
    and the account has no room for the difference. Answers the new rendition's digest.
    """
    with _picture_errors(body.crop):
        digest = await recrop_picture(db=db, user_id=current_user["id"], frame=PORTRAIT_FRAME, crop=body.crop)
    if digest is None:
        raise NotFoundException("No portrait set")
    return PictureRead(sha256=digest)


@router.get("/user/portrait")
async def read_user_portrait(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    v: Annotated[str | None, Query(description=_VERSION_DESCRIPTION)] = None,
) -> Response:
    """The caller's own portrait: its 7:9 rendition, one WebP.

    Privately cacheable for five minutes, with an `ETag`; `If-None-Match` answers 304. Pass
    `UserRead.portrait_sha256` as `v`, as for the avatar. 404 when none is set.
    """
    return await _serve_picture(
        request,
        await get_rendition(db=db, user_id=current_user["id"], frame=PORTRAIT_FRAME),
        kind=PictureKind.PORTRAIT,
    )


@router.get("/user/portrait/original")
async def read_user_portrait_original(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    v: Annotated[str | None, Query(description=_VERSION_DESCRIPTION)] = None,
) -> Response:
    """The original the caller's portrait is rendered from, for adjusting its crop.

    The file as uploaded, less its metadata, under its own name and type; 404 when none is
    held. Privately cacheable for five minutes, with an `ETag`; `If-None-Match` answers 304.
    Pass `UserRead.portrait_original_sha256` as `v`.
    """
    return await _serve_picture(
        request,
        await get_original(db=db, user_id=current_user["id"], frame=PORTRAIT_FRAME),
        kind=PictureKind.PORTRAIT,
    )


@router.delete("/user/portrait")
async def erase_user_portrait(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> dict[str, str]:
    """Remove the caller's portrait and its original. 404 when there was none."""
    if not await delete_picture(db=db, user_id=current_user["id"], frame=PORTRAIT_FRAME):
        raise NotFoundException("No portrait set")

    return {"message": "Portrait removed"}


@router.post(
    "/user/email-change/request", response_model=EmailChangeRequestResponse, dependencies=[Depends(refuse_api_keys)]
)
async def request_email_change(
    request: Request,
    body: EmailChangeRequest,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> EmailChangeRequestResponse:
    """Start changing the caller's email: a confirmation link goes to the *new* address, and
    the change takes effect only when that link is confirmed at
    `POST /user/email-change/verify`, which proves the caller controls the address.

    From a signed-in session, never an API key. Starting again supersedes any earlier link
    still live, so one is live at a time. 400 when `new_email` is already the caller's
    address; otherwise the response is the same generic message whether or not the address
    belongs to another account. Rate limited per account and per IP.
    """
    new_email = body.new_email.lower()
    if new_email == current_user["email"].lower():
        raise BadRequestException("That's already your email address.")

    await enforce_rate_limit(
        f"email-change:user:{current_user['id']}",
        settings.EMAIL_CHANGE_REQUEST_RATE_LIMIT_PER_USER,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )
    await enforce_rate_limit(
        f"email-change:ip:{client_ip_bucket(request)}",
        settings.EMAIL_CHANGE_REQUEST_RATE_LIMIT_PER_USER * 5,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    # Only one *live* change request per user at a time, regardless of which target
    # address a previous one was for.
    await invalidate_live_requests(db, purpose="email_change", user_id=current_user["id"])

    raw_token = generate_secure_token()
    expires_at = datetime.now(UTC) + timedelta(minutes=settings.EMAIL_CHANGE_TOKEN_EXPIRE_MINUTES)
    await crud_authentication_requests.create(
        db=db,
        object=AuthenticationRequestCreate(
            email=new_email,
            token_hash=hash_token(raw_token),
            expires_at=expires_at,
            purpose="email_change",
            user_id=current_user["id"],
        ),
    )

    # The creation is the event; invalidating whatever was live before it (above) is
    # housekeeping of this same request and gets none of its own. The row carries the
    # account, so it sits on the 90-day tier - and no `email`, because `user_id` already
    # names the subject and the *target* address is not what this event is about.
    await record_auth_event(
        db,
        event_type=AuthEventType.EMAIL_CHANGE_REQUESTED,
        context=RequestContext.from_request(request),
        user_id=current_user["id"],
    )

    confirm_url = f"{settings.FRONTEND_URL}/settings/confirm-email?token={raw_token}"
    await send_email_change_confirmation_email(new_email=new_email, confirm_url=confirm_url)

    return _EMAIL_CHANGE_REQUEST_RESPONSE


@router.get("/user/email-change/verify/check", response_model=LinkCheckResponse)
async def check_email_change_link(
    request: Request, response: Response, token: str, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> LinkCheckResponse:
    """Check an email-change link without using it.

    Anonymous. `valid` is false for a link that is unknown, already used, superseded by a
    newer request or expired, and true otherwise, with `email` carrying the address the
    change would apply. Nothing is marked used and no state changes, so a confirmation page
    can show an error for a dead link before offering its button, and the target address up
    front. Never cached (`Cache-Control: private, no-store`). Rate limited per IP.
    """
    # Opts out of the default public caching for the reason `auth.check_email_link` does: an
    # anonymous side-effect-free GET, but the token is in the query string and the body is an
    # email address.
    response.headers["Cache-Control"] = "private, no-store"

    await enforce_rate_limit(
        f"email-change-verify-check:ip:{client_ip_bucket(request)}",
        settings.MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    auth_request = await crud_authentication_requests.get(db=db, token_hash=hash_token(token), purpose="email_change")
    if auth_request is None or auth_request["invalidated_at"] is not None or auth_request["used_at"] is not None:
        return LinkCheckResponse(valid=False)

    expires_at = auth_request["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
        return LinkCheckResponse(valid=False)

    return LinkCheckResponse(valid=True, email=auth_request["email"])


@router.post("/user/email-change/verify", response_model=EmailChangeVerifyResponse)
async def verify_email_change(
    request: Request,
    body: EmailChangeVerifyRequest,
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> EmailChangeVerifyResponse:
    """Confirm an email change: apply the new address the link was sent to.

    Anonymous - the link may be opened on another device than the one that asked - and the
    token in the body is the credential. Answers the account's new `email`, and the old
    address is told of the change. Confirming the same link again is not an error while the
    address it carries is still the account's: it answers the same success and changes
    nothing. 401 for a link that is unknown, expired, superseded by a newer request, or
    already used for an address the account has since moved away from; 422 when the address
    is registered to another account; 404 when the account has been deleted. Rate limited
    per IP.
    """
    # The replay tolerance is a safety net for races - a double click, or a mail client's
    # link scanner detonating the link before the human does - and a no-op that reports the
    # change back rather than re-touching the row or re-notifying anyone.
    # `check_email_change_link` is what keeps a human from re-triggering this after the first
    # use. Two submissions genuinely racing meet the same rule from the other side: the
    # `used_at` read below cannot decide it alone, so only the winner of the conditional claim
    # (`claim_authentication_request`) applies the change, and the loser re-reads the
    # account's email and takes the identical tolerated-replay-or-reject decision.
    await enforce_rate_limit(
        f"email-change-verify:ip:{client_ip_bucket(request)}",
        settings.MAGIC_LINK_VERIFY_RATE_LIMIT_PER_IP,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    auth_request = await crud_authentication_requests.get(
        db=db, token_hash=hash_token(body.token), purpose="email_change"
    )
    if auth_request is None:
        raise UnauthorizedException("This confirmation link is invalid.")

    if auth_request["invalidated_at"] is not None:
        raise UnauthorizedException("This confirmation link is no longer valid - a newer request was made.")

    new_email = auth_request["email"]
    user_id = auth_request["user_id"]

    db_user = await crud_users.get(db=db, id=user_id, is_deleted=False)
    if db_user is None:
        raise NotFoundException("User not found")

    current_email = db_user["email"] if isinstance(db_user, dict) else db_user.email

    if auth_request["used_at"] is not None:
        return _replay_result_or_reject(current_email=current_email, new_email=new_email)

    expires_at = auth_request["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < datetime.now(UTC):
        raise UnauthorizedException("This confirmation link has expired.")

    if await account_exists_for(db, email=new_email.lower()):
        raise DuplicateValueException("Email is already registered to another account")

    try:
        # Claimed first, and `commit=False` throughout so the claim and the email land in
        # one transaction. Claiming first means losing the race costs nothing to undo;
        # committing separately (FastCRUD's `update` does by default) would leave a window
        # where the account's email has changed but its confirmation token is still live.
        claimed = await claim_authentication_request(db, request_id=auth_request["id"], commit=False)
        if claimed:
            await crud_users.update(db=db, object={"email": new_email}, id=user_id, commit=False)
            # In the same transaction as the claim and the address change: an event saying
            # the change was applied must not survive a rollback that undid it. Only on the
            # winning branch - a tolerated replay below re-reports a change this request did
            # not make, and recording it again would count one change twice.
            #
            # The account id comes off the claimed row rather than from a session: this
            # endpoint is anonymous by design, because the link may be opened on a
            # different device than the one that asked for the change.
            await record_auth_event(
                db,
                event_type=AuthEventType.EMAIL_CHANGE_COMPLETED,
                context=RequestContext.from_request(request),
                user_id=user_id,
                commit=False,
            )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise DuplicateValueException("Email is already registered to another account") from None

    if not claimed:
        # Another verification of this same token got there first. Unlike sign-in that
        # isn't automatically a rejection here - re-reading the account's email applies
        # the same rule as the `used_at` fast path above, so a double click that lost the
        # race still reports the change it asked for rather than erroring.
        db_user = await crud_users.get(db=db, id=user_id, is_deleted=False)
        if db_user is None:
            raise NotFoundException("User not found")
        current_email = db_user["email"] if isinstance(db_user, dict) else db_user.email
        return _replay_result_or_reject(current_email=current_email, new_email=new_email)

    await send_best_effort(
        send_email_changed_notification(old_email=current_email, new_email=new_email),
        what="the email-changed notice",
        user_id=user_id,
    )

    return EmailChangeVerifyResponse(email=new_email)


@router.get("/user/dive-stats", response_model=UserDiveStatsRead)
async def read_dive_stats(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> UserDiveStatsRead:
    """The caller's aggregate dive statistics.

    An account with no dives logged yet gets zeroed figures, never a 404.
    """
    derived = await sites_and_dive_days(db, user_id=current_user["id"])
    stats = await crud_user_dive_stats.get(
        db=db, user_id=current_user["id"], schema_to_select=UserDiveStatsReadInternal, return_as_model=True
    )
    if stats is None:
        # No dives logged yet - return zeroed-out stats rather than 404, since
        # every user conceptually has stats, they just haven't been created yet.
        # total_dives/max_depth/total_submerged_time/species_seen have Pydantic defaults, but mypy's
        # pydantic plugin doesn't recognize defaults declared via `Annotated[..., Field(default=...)]`.
        return UserDiveStatsRead(  # type: ignore[call-arg]
            user_uuid=current_user["uuid"], created_at=datetime.now(UTC), **derived._asdict()
        )

    stats = cast(UserDiveStatsReadInternal, stats)
    return UserDiveStatsRead(
        **{k: v for k, v in stats.model_dump().items() if k != "user_id"},
        user_uuid=current_user["uuid"],
        **derived._asdict(),
    )


@router.get("/user/storage", response_model=StorageUsageRead)
async def read_storage_usage(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> StorageUsageRead:
    """What the caller's uploads occupy, by kind, against this instance's storage limit.

    `used_bytes` is what an upload is checked against: one that would take it past
    `limit_bytes` is a 413. Dive-computer files count at their stored size, which for a
    compressed file is less than the sizes its recordings list; a file stored uncompressed
    counts at its full size. Never cached: every upload and delete shows at once.
    """
    # Not cached, like `GET /user`: every upload and delete would otherwise owe it an
    # invalidation, and the sums are a handful of indexed rows.
    usage = await get_storage_usage(db, user_id=current_user["id"])
    return StorageUsageRead(
        used_bytes=usage.used_bytes,
        limit_bytes=storage_limit_bytes(),
        dive_files_bytes=usage.dive_files_bytes,
        certification_files_bytes=usage.certification_files_bytes,
        pictures_bytes=usage.pictures_bytes,
        dive_files_count=usage.dive_files_count,
        dives_with_files_count=usage.dives_with_files_count,
        certification_files_count=usage.certification_files_count,
        certifications_with_files_count=usage.certifications_with_files_count,
    )


# Keyed under the `user_{id}_dives:` prefix on purpose, even though this hangs off
# `/user/...` rather than `/dives`: `invalidate_dive_caches()` already sweeps
# `user_{id}_dives:*` after every dive create, update and delete, so this series drops
# with them and needed no invalidation change at all. A key of its own (say
# `user_{id}_gas_use:`) would have been a third pattern to remember to add there, and the
# bug from forgetting is a graph that silently keeps showing yesterday's dives.
#
# Same authorization caveat as `_cached_read_dives` in `dives.py`: `@cache` serves a hit
# without re-running the route's body, so this must only ever be called with the *calling*
# user's own id, which is all the route below passes.
@cache(key_prefix="user_{user_id}_dives:gas_use_history", resource_id_name="user_id", expiration=60)
async def _cached_gas_use_history(request: Request, user_id: int, db: AsyncSession) -> list[DiveGasUsePoint]:
    """Fetches (and caches) a user's whole gas-use series. Authorization happens in the
    route before this is reached - `@cache` serves a hit without re-checking it.
    """
    return await gas_use_history(db=db, user_id=user_id)


@router.get("/user/gas-use-history", response_model=list[DiveGasUsePoint])
async def read_gas_use_history(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> list[DiveGasUsePoint]:
    """Every dive of the caller's that records enough to derive its gas use, oldest first.

    The whole series, unpaginated. A dive that cannot produce a figure is absent, and
    `GET /dive/{uuid}` is where a diver finds out why one of theirs is missing.
    """
    # The whole career because this exists to be plotted as a trend; a chart series, not a
    # checklist, which is why a dive without a figure is absent rather than carried with nulls.
    return await _cached_gas_use_history(request, user_id=current_user["id"], db=db)


# Keyed under `user_{id}_dives:` for the same reason as `_cached_gas_use_history` above:
# `invalidate_dive_caches()` already sweeps that prefix after every dive write, so this
# series drops with them and needs no invalidation change of its own. The same
# authorization caveat applies - `@cache` serves a hit without re-running the route body,
# so this must only ever be called with the calling user's own id.
@cache(key_prefix="user_{user_id}_dives:dive_activity", resource_id_name="user_id", expiration=60)
async def _cached_dive_activity(request: Request, user_id: int, db: AsyncSession) -> list[DiveActivityPoint]:
    """Fetches (and caches) a user's dives-per-day series. Authorization happens in the
    route below, before this is reached.
    """
    return await dive_activity(db=db, user_id=user_id)


@router.get("/user/dive-activity", response_model=list[DiveActivityPoint])
async def read_dive_activity(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> list[DiveActivityPoint]:
    """How many dives the caller logged on each calendar day, oldest first.

    The whole series, unpaginated: one small object per day with diving in it. Days without
    diving are absent, not zeroed, so a client drawing a grid of days, months or years fills
    the gaps itself. Counts are bucketed by each dive's *own* local day, so a dive keeps the
    day it was logged on wherever it is read from.
    """
    # Days rather than months because the client windows this one series three ways and sums
    # the finer buckets into the coarser ones itself (see `DiveActivityPoint`). Whole-career
    # for the reason the gas-use series is: it exists to be plotted.
    return await _cached_dive_activity(request, user_id=current_user["id"], db=db)


# Keyed under `user_{id}_trips:`, the trip list's prefix, for the reason the dive series
# above sit under the dives': `invalidate_trip_caches()` sweeps it after every write that
# can change a trip, so this drops with the list and needs no pattern of its own. Same
# authorization caveat - only ever called with the calling user's own id.
@cache(key_prefix="user_{user_id}_trips:places", resource_id_name="user_id", expiration=60)
async def _cached_trip_places(request: Request, user_id: int, db: AsyncSession) -> list[LocationRead]:
    """Fetches (and caches) a user's trip places. Authorization happens in the route
    below, before this is reached.
    """
    return await get_places_for_user(db=db, user_id=user_id)


@router.get("/user/trip-places", response_model=list[LocationRead])
async def read_trip_places(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> list[LocationRead]:
    """Every place the caller's trips went, each once, for a map to frame.

    The whole set, unpaginated, since a map frames all of it or none. A part with no place,
    or a place the geocoder gave no position, is left out, and a place on several trips
    appears once. A caller with no placed trips gets an empty list.
    """
    return await _cached_trip_places(request, user_id=current_user["id"], db=db)


# Keyed under `user_{id}_dives:` for the same reason as the two series above, and with a
# second obligation they do not have: **every query parameter has to be in the key**.
# `@cache` builds its key from this template's placeholders and the resource id and nothing
# else, so a prefix missing `page` and `search` would collapse every page *and* every search of
# one diver onto a single entry - page 2 serving page 1, and a search serving the unfiltered
# list, for sixty seconds at a time. Nothing downstream catches that: `TestListCacheKeys`
# asserts `page_{page}` only for `OwnedResourceCache` instances, and this is deliberately a
# hand-written aggregate. The shape is `_cached_read_dives`', not `_cached_gas_use_history`',
# which is an unparameterised whole-series fetch with no page to vary by.
#
# The prefix is what makes this drop with the rest: `invalidate_dive_caches()` sweeps
# `user_{id}_dives:*` after every dive create, update and delete, and deliberately not a wider
# pattern. A namespace of its own would have been a third pattern to remember to add there, and
# the bug from forgetting it is specific here - `species_seen` is uncached and drops instantly
# on a dive delete, so the dashboard tile and this list would disagree, breaking the very
# equality that makes the two checkable against each other.
#
# Same authorization caveat as everywhere in this file: `@cache` serves a hit without re-running
# the body, so this must only ever be called with the calling user's own id.
# Named rather than written inline so a test can assert on it directly. That is worth the one
# extra line here and nowhere else in this file: this is the key whose omissions produce a
# wrong answer rather than a slow one, and every other way of checking it either reads this
# file's source text or passes whenever it happens to run outside the 60 s TTL.
SPECIES_LIFE_LIST_CACHE_KEY_PREFIX = (
    "user_{user_id}_dives:species:page_{page}:items_per_page:{items_per_page}:search:{search}:site_{dive_site_id}"
)


@cache(key_prefix=SPECIES_LIFE_LIST_CACHE_KEY_PREFIX, resource_id_name="user_id", expiration=60)
async def _cached_species_life_list(
    request: Request,
    user_id: int,
    db: AsyncSession,
    page: int,
    items_per_page: int,
    search: str | None,
    dive_site_id: int | None,
) -> dict:
    """Fetches (and caches) a page of the caller's life list. Authorization happens in the
    route before this is reached.
    """
    data = await species_life_list(
        db=db,
        user_id=user_id,
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
        search=search,
        dive_site_id=dive_site_id,
    )
    response: dict[str, Any] = paginated_response(crud_data=data, page=page, items_per_page=items_per_page)
    return response


@router.get("/user/species", response_model=PaginatedListResponse[SpeciesLifeListEntry])
async def read_species_life_list(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: int = 1,
    items_per_page: int = 10,
    search: Annotated[
        str | None,
        Query(max_length=255, description="Match by any name the species goes by, as the catalog search does"),
    ] = None,
    dive_site_uuid: Annotated[
        uuid_pkg.UUID | None, Query(description="Only the species sighted on dives naming this site")
    ] = None,
) -> dict:
    """Every species the caller has ever logged, most recently seen first.

    One row per taxon rather than per sighting: `dive_count`, `dive_site_count`, `first_seen` and
    `last_seen` are that diver's whole history with it, and `photo_sha256` is non-null when there is a photo to
    render (build `/species/{uuid}/photo?v=<digest>` from it - that route needs no token).

    Counts **live** dives only, so soft-deleting a dive drops its species from this list when it
    was the only sighting - which is also why `total_count` equals the `species_seen` on
    `GET /user/dive-stats` for the same account.

    `dive_site_uuid` keeps the species sighted on dives naming that site at any position, as
    the site's summary counts them, with each one's figures over those dives alone. A uuid that
    is not one of the caller's sites returns an empty page rather than an error.

    Out-of-range pagination is clamped, not rejected.
    """
    page, items_per_page = clamp_pagination(page, items_per_page)
    # Normalized here rather than in the service, so the shape the cache key is built from is
    # the shape this route accepted - the reasoning `read_species_search` spells out for its
    # own search parameter. An all-whitespace term becomes `None` rather than a `%%` pattern
    # that matches the unfiltered list under a different key.
    normalized = " ".join(search.split()) if search is not None else None
    dive_site_id: int | None = None
    if dive_site_uuid is not None:
        # The dives list's -1: it matches no site, so a foreign uuid reads as an empty page.
        site_map = await resolve_dive_site_ids_for_user(
            db=db, dive_site_uuids=[dive_site_uuid], user_id=current_user["id"]
        )
        dive_site_id = (site_map or {}).get(dive_site_uuid, -1)
    return await _cached_species_life_list(
        request,
        user_id=current_user["id"],
        db=db,
        page=page,
        items_per_page=items_per_page,
        search=normalized or None,
        dive_site_id=dive_site_id,
    )


# Declared before `/user/species/{uuid}`, which would otherwise read "suggest" as a uuid and 422.
@router.get("/user/species/suggest", response_model=SpeciesSuggestResponse)
async def read_species_suggestions(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    q: Annotated[str, Query(max_length=255, description="Common or scientific name; empty for no filter")] = "",
    dive_site_uuid: Annotated[
        list[uuid_pkg.UUID] | None,
        Query(
            max_length=DEFAULT_MAX_ITEMS_PER_PAGE,
            description="The sites the dive names, repeated once per site: the species logged on the caller's "
            "dives there come first. One that is not the caller's names no dives",
        ),
    ] = None,
    exclude_species_uuid: Annotated[
        list[uuid_pkg.UUID] | None,
        Query(
            max_length=DEFAULT_MAX_ITEMS_PER_PAGE,
            description="The species the dive already holds, repeated once per species: none of them is suggested",
        ),
    ] = None,
) -> SpeciesSuggestResponse:
    """The dive form's species menu, in three tiers on one page.

    First the species the caller logged on their dives at the given sites, by how many such
    dives (`dive_count_at_sites`); then the rest of the species they have logged, by
    `last_seen`; then the catalog and register matches, ranked as `GET /species/search` ranks
    them, without the species already shown. A typed `q` filters the first two tiers by the
    scientific and common name - one letter is enough - and keeps their order.

    The registers are asked only when `q` has two or more characters and the caller's own
    matches leave room on the page, and only then does the request count against the
    per-account species budget, so it is the only case that can 429. `has_more` is true when
    the page was cut, or when a typed `q` never reached the catalog search.
    """
    # Uncached, like the search it wraps.
    user_id = current_user["id"]
    dive_site_ids: list[int] = []
    if dive_site_uuid:
        # Not `resolve_dive_site_ids_for_user`, which answers nothing at all when any one uuid is
        # foreign: a form holding a deleted site beside a live one keeps the live one's tier.
        dive_site_ids = list(
            (
                await db.scalars(
                    select(DiveSite.id).where(DiveSite.uuid.in_(dive_site_uuid), DiveSite.user_id == user_id)
                )
            ).all()
        )

    return await suggest_species(
        db,
        user_id=user_id,
        query=q,
        dive_site_ids=dive_site_ids,
        excluded_uuids=exclude_species_uuid or [],
        before_search=partial(enforce_species_limit, user_id),
    )


# Under the life list's prefix, so the same dive-write sweep drops it and the two never
# disagree - as must the site count, which a site delete changes through its own call to
# `invalidate_dive_caches`. A species row is immutable, so its uuid is a stable key segment.
SPECIES_LIFE_LIST_DETAIL_CACHE_KEY_PREFIX = "user_{user_id}_dives:species:{species_uuid}"


@cache(key_prefix=SPECIES_LIFE_LIST_DETAIL_CACHE_KEY_PREFIX, resource_id_name="user_id", expiration=60)
async def _cached_species_life_list_detail(
    request: Request, user_id: int, db: AsyncSession, species_uuid: uuid_pkg.UUID
) -> SpeciesLifeListEntry:
    """Fetches (and caches) one species' entry for the caller. The 404 raises before anything
    is stored, so a first sighting shows at once rather than after the TTL."""
    detail = await species_life_list_detail(db=db, user_id=user_id, species_uuid=species_uuid)
    if detail is None:
        raise NotFoundException("Species not in your log")
    return detail


@router.get("/user/species/{uuid}", response_model=SpeciesLifeListEntry)
async def read_species_life_list_detail(
    request: Request,
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> SpeciesLifeListEntry:
    """The caller's history with one species: its `GET /user/species` row, with the same
    figures.

    404 when no live dive of the caller's records the species, whether or not the catalog
    holds it: exactly the species the life list leaves out.
    """
    # The uuid is the catalog's, which belongs to nobody, so the scoping is the query's and
    # there is no ownership check to make.
    return await _cached_species_life_list_detail(request, user_id=current_user["id"], db=db, species_uuid=uuid)


@router.delete("/user", response_model=AccountDeletionResponse, dependencies=[Depends(refuse_api_keys)])
async def erase_user(
    request: Request,
    response: Response,
    current_user: Annotated[dict, Depends(get_current_user)],
    session_uuid: Annotated[uuid_pkg.UUID | None, Depends(current_session_uuid)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    access_token: Annotated[str, Depends(bearer_scheme)],
    refresh_token: Annotated[str | None, Cookie(alias="refresh_token")] = None,
) -> AccountDeletionResponse:
    """Delete the caller's own account, reversibly for the instance's grace period.

    From a signed-in session, never an API key. The account goes **dark immediately**: every
    request on every device is a 401 from this instant, the presented tokens are revoked and
    the refresh cookie is cleared. It stays recoverable through `POST /auth/restore` until
    `purge_after`, which the response carries and a confirmation email repeats; after that,
    every row and stored file the account owns is gone for good. The caller's other sessions
    are not revoked, so a restore brings them back signed in; `DELETE /user/sessions` ends
    them. A repeated request answers the date the first one set. Rate limited per account.
    """
    # Nothing between dark and deleted: `get_current_user` filters `is_deleted=False`, so every
    # read 401s at once, and `/auth/refresh` re-resolves the row so the other devices' cookies
    # stop rotating too. Someone deleting their account because they want to be *gone from it*
    # gets that, not a fortnight's countdown banner. `purge_deleted_accounts` issues the real
    # `DELETE FROM "user"` when the grace period runs out.
    #
    # No other session row is touched, and that is load-bearing rather than an oversight. The
    # other devices are already inert, and leaving them *unrevoked* is what lets
    # `POST /auth/restore` bring the account back with those sessions intact - "a restore that
    # silently signed every other device out would be a worse answer", which is exactly the
    # property the refresh check was written to preserve.
    #
    # The response is composed before the confirmation email is attempted, so a dead relay
    # costs the copy rather than the date.
    await enforce_rate_limit(
        f"account-deletion:user:{current_user['id']}",
        settings.ACCOUNT_DELETION_RATE_LIMIT_PER_USER,
        settings.MAGIC_LINK_RATE_LIMIT_WINDOW_SECONDS,
    )

    # Conditional on the row actually transitioning, not on what `get_current_user`
    # resolved. Two concurrent calls from one live session both pass that dependency
    # before either commits, and an unconditional write would rewrite `deleted_at` - which
    # is the deletion clock - and send a second email. `RETURNING` is what makes "did this
    # call do it?" one statement rather than a check-then-act with the same race in it.
    now = datetime.now(UTC)
    deleted_at = cast(
        datetime | None,
        (
            await db.execute(
                update(User)
                .where(User.id == current_user["id"], User.is_deleted.is_(False))
                .values(is_deleted=True, deleted_at=now)
                .returning(User.deleted_at)
            )
        ).scalar_one_or_none(),
    )
    newly_deleted = deleted_at is not None
    if deleted_at is None:
        # Already pending: report the deadline the *first* request set, so the second of a
        # double submit answers with the same date. A later retry never gets here -
        # `get_current_user` refuses a deleted account before the route runs.
        deleted_at = cast(
            datetime | None,
            (await db.execute(select(User.deleted_at).where(User.id == current_user["id"]))).scalar_one_or_none(),
        )
    await db.commit()

    if refresh_token:
        await blacklist_tokens(access_token=access_token, refresh_token=refresh_token, db=db)
        response.delete_cookie(key="refresh_token")
    else:
        await blacklist_token(token=access_token, db=db)

    if session_uuid is not None:
        await revoke_session(db, session_uuid=session_uuid)

    # Commits the revoke above with it. `newly_deleted` is not a condition on the event:
    # the caller asked to delete their account either way, and a repeated request is a
    # thing the trail should show rather than hide.
    await record_auth_event(
        db,
        event_type=AuthEventType.ACCOUNT_DELETION_REQUESTED,
        context=RequestContext.from_request(request),
        user_id=current_user["id"],
    )

    # Null only if the row is not there to read - it was purged between `get_current_user`
    # resolving it and this statement - or if something flagged it without setting the
    # clock, the never-purge state `purge_deleted_accounts` warns about. Neither is worth
    # 500ing at somebody who is trying to leave, so answer with a date derived from now.
    purge_after = (deleted_at or now) + timedelta(days=settings.ACCOUNT_DELETION_GRACE_DAYS)

    # Hygiene rather than correctness: every read 401s from this instant regardless, and
    # user ids are sequential and never reused, so nothing can be served out of these.
    await delete_keys_by_pattern(f"user_{current_user['id']}_*")

    if newly_deleted:
        # Last, and never fatal. The deletion has already committed; a relay failure that
        # propagated would leave the user locked out *and* never told the purge date,
        # which is strictly worse than no email. `send_gear_service_digests` makes the
        # same trade the other way round, for a job that can safely repeat itself.
        await send_best_effort(
            send_account_deletion_email(current_user["email"], purge_after),
            what="the account-deletion confirmation",
            user_id=current_user["id"],
        )

    return AccountDeletionResponse(message="User deleted", purge_after=purge_after)
