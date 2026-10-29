"""The operator's contract: the invite queue, inviting from it in a batch, the daily totals of
accounts and sign-ins, and curating the species catalog's photos.

**The first routes in `/api/v1` to be superuser-gated.** `is_superuser` has existed on
`User` since the beginning and until now gated exactly one thing - the docs router on
`staging` (`core/setup.py`). The routes here are the second, and they are gated by a
**router-level** dependency rather than per handler, so the route-walking auth guard
(`tests/helpers/routes.py`, which reads include-level dependencies through the merged
dependant) sees the marker on every route here without each one naming it, and a route
added to this module later cannot arrive unprotected.

**Not the CRUDAdmin panel, and not a page.** These are ordinary JSON routes; the UI that
drives them is a superuser-gated section of the web app. The deciding argument is auth:
this app has no passwords, the access token is a bearer header held in browser memory and
the refresh cookie is single-use and `SameSite=lax`, so a browser *navigating* to a
server-rendered admin page on this origin carries no credential this app recognises. That
leaves an API-side admin either keeping a second identity with its own password, which is
what CRUDAdmin is, or building a cookie-to-page auth bridge nobody else ships.

The web gate is a convenience over this one and never a substitute: a non-superuser is
refused here whatever page they came from.
"""

import uuid as uuid_pkg
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_superuser
from ...core.config import settings
from ...core.db.database import async_get_db, release_read_transaction
from ...core.exceptions.http_exceptions import NotFoundException, UnprocessableEntityException
from ...core.utils.pagination import ItemsPerPageQuery, PageQuery, clamp_pagination
from ...core.utils.request_context import RequestContext
from ...crud.crud_auth_audit_events import record_auth_event
from ...crud.crud_daily_totals import daily_totals_between
from ...crud.crud_invitations import account_exists_for, crud_invitations, live_invitation_from
from ...crud.crud_invite_requests import delete_invite_requests
from ...crud.crud_user_sessions import accounts_with_a_live_session
from ...models.invite_request import InviteRequest
from ...models.species import Species
from ...models.user import User
from ...schemas.auth_audit_event import AuthEventType
from ...schemas.daily_total import DailyMetric, DailyStatsDay, DailyStatsRead, DailyStatsTotals
from ...schemas.invitation import (
    AdminInvitationBatchRequest,
    AdminInvitationBatchResponse,
    AdminInvitationOutcome,
    InvitationCreateInternal,
    InvitationReadInternal,
)
from ...schemas.invite_request import (
    AdminInviteRequestDeleteRequest,
    AdminInviteRequestDeleteResponse,
    AdminInviteRequestRead,
)
from ...schemas.join_channel import JoinChannelRead
from ...schemas.species import (
    AdminSpeciesFilter,
    AdminSpeciesPhotoCandidate,
    AdminSpeciesPhotoCandidates,
    AdminSpeciesPhotoPin,
    AdminSpeciesRead,
    PhotoCuration,
)
from ...services import species_photos, species_service
from ...services.email_service import send_best_effort, send_invitation_email
from ...services.species_life_list import species_search_clause

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(get_current_superuser)])


@router.get("/invite-requests", response_model=PaginatedListResponse[AdminInviteRequestRead])
async def read_invite_requests(
    db: Annotated[AsyncSession, Depends(async_get_db)], page: PageQuery = 1, items_per_page: ItemsPerPageQuery = 10
) -> dict:
    """The queue: every pending invite request, newest first. Superuser only.

    Every row is pending - a request that was invited, declined or swept is gone, not
    flagged - so there is no filter and no status column. `has_account` says whether the
    address already has an account here, compared case-insensitively. Out-of-range
    pagination is clamped.
    """
    # `has_account` is a `LEFT JOIN` on `lower(User.email)`, and both halves of that matter.
    # *This* route may consult the `user` table where `POST /invite-requests` structurally
    # may not, because its caller is the operator rather than an anonymous stranger; and the
    # comparison is on the lowered column because `POST /auth/complete` stores the onboarding
    # token's address verbatim, so an account created through Google may hold capitals while
    # every row in this table is lowercase.
    page, items_per_page = clamp_pagination(page, items_per_page)

    account_exists = (
        select(func.count()).select_from(User).where(func.lower(User.email) == InviteRequest.email).scalar_subquery()
    )
    rows = (
        await db.execute(
            select(InviteRequest.email, InviteRequest.created_at, (account_exists > 0).label("has_account"))
            .order_by(InviteRequest.created_at.desc())
            .offset(compute_offset(page, items_per_page))
            .limit(items_per_page)
        )
    ).all()
    total = await db.scalar(select(func.count()).select_from(InviteRequest))

    data: dict[str, Any] = {
        "data": [
            AdminInviteRequestRead(email=row.email, created_at=row.created_at, has_account=row.has_account).model_dump()
            for row in rows
        ],
        "total_count": int(total or 0),
    }
    return paginated_response(crud_data=data, page=page, items_per_page=items_per_page)


@router.post("/invitations", response_model=AdminInvitationBatchResponse)
async def invite_batch(
    request: Request,
    body: AdminInvitationBatchRequest,
    current_user: Annotated[dict, Depends(get_current_superuser)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> AdminInvitationBatchResponse:
    """Invite a batch of addresses, and report what happened to each of them. Superuser only.

    Addresses that never requested an invitation are accepted too; a pending request is
    optional context, not a precondition. Each address gets its own `outcome` - `invited`,
    `already_registered`, `already_invited` or `mail_failed` - so a partial mail failure
    shows *which* addresses got through. A `mail_failed` row is a real invitation whose
    invitee has not been told: the address is admitted, and the operator can tell them
    another way.

    The mails are sent one after another before the response, so a full batch takes a
    while. No quota applies. Up to 100 addresses per request; more is a 422.
    """
    # A per-address outcome rather than a 5xx on the first problem: a partial SMTP failure
    # then shows which addresses got through instead of an error that hides them. Sends are
    # sequential and inline: the worker runs crons only (DECISIONS.md, *The Arq worker runs
    # crons only, and nothing in the API enqueues*), and a one-off operator action of at most
    # `MAX_ADDRESSES_PER_BATCH` addresses does not justify this app's first queued job. No
    # quota check because superusers are exempt from `INVITATIONS_PER_USER`, and nobody else
    # reaches this route.
    context = RequestContext.from_request(request)
    results: list[AdminInvitationOutcome] = []

    for raw in body.emails:
        email = raw.lower()

        if await account_exists_for(db, email=email):
            results.append(AdminInvitationOutcome(email=email, outcome="already_registered"))
            continue
        if await live_invitation_from(db, email=email, user_id=current_user["id"]):
            results.append(AdminInvitationOutcome(email=email, outcome="already_invited"))
            continue

        # Same transaction as the insert, for the same reason the member's route does it:
        # an address with a live invitation must not also be sitting in this queue. First,
        # because whether it was there is what the invitation records: the account it lets
        # in is counted under `waitlist` rather than `invitation`.
        from_queue = await delete_invite_requests(db, emails=[email], commit=False) > 0
        await crud_invitations.create(
            db=db,
            object=InvitationCreateInternal(email=email, user_id=current_user["id"], from_invite_request=from_queue),
            commit=False,
            schema_to_select=InvitationReadInternal,
            return_as_model=True,
        )
        await record_auth_event(
            db,
            event_type=AuthEventType.INVITATION_CREATED,
            context=context,
            user_id=current_user["id"],
            email=email,
            commit=False,
        )
        await db.commit()

        # After the commit, so the invitation stands and the address is admitted. One
        # unreachable relay or one rejected recipient must not take the rest of the batch
        # with it, and the per-address outcome is how the operator sees which ones to follow
        # up by hand.
        sent = await send_best_effort(
            send_invitation_email(email=email, inviter_name=current_user["name"]),
            what="an invitation email",
            user_id=current_user["id"],
        )
        if not sent:
            results.append(AdminInvitationOutcome(email=email, outcome="mail_failed"))
            continue

        results.append(AdminInvitationOutcome(email=email, outcome="invited"))

    return AdminInvitationBatchResponse(results=results)


@router.delete("/invite-requests", response_model=AdminInviteRequestDeleteResponse)
async def remove_invite_requests(
    body: AdminInviteRequestDeleteRequest, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> AdminInviteRequestDeleteResponse:
    """Drop addresses from the queue - spam, or a request the operator declines. Superuser
    only.

    A request is keyed by its address, which is all anybody knows about it. Nothing is
    written anywhere else: the address is not told, and nothing stops it asking again beyond
    the rate limits on `POST /invite-requests`. `removed` counts what actually went, which
    may be fewer than were asked for: an invitation, or the sweep, may have taken a row
    between the operator reading the queue and acting on it.
    """
    # Body-keyed rather than a `{uuid}` route because an `invite_request` row has no public
    # identifier - the address is the only thing anybody knows about a request, which is also
    # why the model carries no `uuid` at all.
    removed = await delete_invite_requests(db, emails=[email.lower() for email in body.emails])
    return AdminInviteRequestDeleteResponse(removed=removed)


# The longest range `GET /admin/stats` answers: the longest run of three calendar months,
# so one request stays a bounded read however the dates are typed.
MAX_STATS_DAYS = 92


@router.get("/stats", response_model=DailyStatsRead)
async def read_daily_stats(
    db: Annotated[AsyncSession, Depends(async_get_db)],
    first: Annotated[date, Query(alias="from", description="First UTC day of the range, as YYYY-MM-DD")],
    last: Annotated[date, Query(alias="to", description="Last UTC day of the range, inclusive")],
) -> DailyStatsRead:
    """The daily totals for every UTC day from `from` to `to` inclusive, zero-filled, with
    the instance's join channels and two figures as of now. Superuser only.

    Every number is a count that names no account: accounts created per day and per door,
    distinct accounts that signed in, distinct accounts with a session used that day.
    `totals.accounts` counts every account, one inside its deletion grace period included,
    and `totals.active_now` the accounts holding a session that can still authenticate.

    422 for a range running backwards or longer than 92 days.
    """
    # No "active in the last 30 days" figure, because nothing records one: the sessions that
    # would say so are deleted once dead, and a per-account last-seen date is a record this
    # app does not keep.
    if last < first:
        raise UnprocessableEntityException("`to` is before `from`.")
    span = (last - first).days + 1
    if span > MAX_STATS_DAYS:
        raise UnprocessableEntityException(f"A range is at most {MAX_STATS_DAYS} days; this one is {span}.")

    created: dict[date, dict[str, int]] = defaultdict(dict)
    counted: dict[tuple[date, str], int] = {}
    for row in await daily_totals_between(db, first=first, last=last):
        if row.metric == DailyMetric.ACCOUNTS_CREATED:
            created[row.day][row.key] = row.count
        else:
            counted[(row.day, row.metric)] = row.count

    days = [first + timedelta(days=offset) for offset in range(span)]
    return DailyStatsRead(
        from_=first,
        to=last,
        channels=[JoinChannelRead(slug=slug, label=label) for slug, label in settings.join_channels.items()],
        days=[
            DailyStatsDay(
                day=day,
                accounts_created=created.get(day, {}),
                sign_ins=counted.get((day, DailyMetric.SIGN_INS), 0),
                active_accounts=counted.get((day, DailyMetric.ACTIVE_ACCOUNTS), 0),
            )
            for day in days
        ],
        totals=DailyStatsTotals(
            accounts=int(await db.scalar(select(func.count()).select_from(User)) or 0),
            active_now=await accounts_with_a_live_session(db),
        ),
    )


# -------------- the species catalog's photos --------------

# Each chip as the predicate it is. `narrow` reads the stored bytes' width, which a photo kept
# before the floor existed lacks until the backfill's `--recheck-size` measures it - and that
# pass drops every unpinned narrow photo as it goes, so the chip lists pins.
_SPECIES_FILTERS = {
    AdminSpeciesFilter.WITH_PHOTO: Species.photo_storage_key.is_not(None),
    AdminSpeciesFilter.WITHOUT_PHOTO: Species.photo_storage_key.is_(None),
    AdminSpeciesFilter.HIDDEN: Species.photo_curation == PhotoCuration.HIDDEN,
    AdminSpeciesFilter.PINNED: Species.photo_curation == PhotoCuration.PINNED,
    AdminSpeciesFilter.NARROW: Species.photo_width < species_photos.COMMONS_THUMBNAIL_WIDTH,
}

# One message for every way the photo did not arrive, because `unavailable` covers a register
# that did not answer and bytes that would not decode alike, and the row is unchanged either way.
_PHOTO_UNAVAILABLE = "The photo could not be fetched; nothing changed."


@router.get("/species", response_model=PaginatedListResponse[AdminSpeciesRead])
async def read_admin_species(
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: PageQuery = 1,
    items_per_page: ItemsPerPageQuery = 10,
    search: Annotated[str | None, Query(max_length=255, description="Any name the species goes by")] = None,
    chip: Annotated[
        AdminSpeciesFilter | None, Query(alias="filter", description="Only the species of this kind")
    ] = None,
) -> dict:
    """Every species in the catalog, newest first, with what the operator needs to curate its
    photo. Superuser only.

    `search` matches the names the catalog search matches on. `filter` narrows to one kind:
    `with_photo`, `without_photo`, `hidden`, `pinned`, or `narrow` - a stored photo under the
    500 px floor, which only a pinned one may keep. Out-of-range pagination is clamped.
    """
    # Unindexed and uncached: the catalog is thousands of rows and this is one operator's page.
    page, items_per_page = clamp_pagination(page, items_per_page)

    clauses = []
    if search and (term := search.strip()):
        clauses.append(species_search_clause(term))
    if chip is not None:
        clauses.append(_SPECIES_FILTERS[chip])

    rows = (
        await db.execute(
            select(Species)
            .where(*clauses)
            .order_by(Species.created_at.desc(), Species.id.desc())
            .offset(compute_offset(page, items_per_page))
            .limit(items_per_page)
        )
    ).scalars()
    total = await db.scalar(select(func.count()).select_from(Species).where(*clauses))

    data: dict[str, Any] = {
        "data": [AdminSpeciesRead.model_validate(row, from_attributes=True).model_dump() for row in rows],
        "total_count": int(total or 0),
    }
    return paginated_response(crud_data=data, page=page, items_per_page=items_per_page)


@dataclass(frozen=True, slots=True)
class _PhotoInputs:
    id: int
    aphia_id: int
    scientific_name: str
    genus: str | None
    wikidata_qid: str | None
    photo_file: str | None


async def _photo_inputs(db: AsyncSession, uuid: uuid_pkg.UUID) -> _PhotoInputs:
    """The columns the photo routes work from, detached, with the read transaction released -
    every route below goes outbound or writes next, and none may hold a connection idle across
    a Commons call. 404 for an unknown uuid."""
    row = (
        await db.execute(
            select(
                Species.id,
                Species.aphia_id,
                Species.scientific_name,
                Species.genus,
                Species.wikidata_qid,
                Species.photo_file,
            ).where(Species.uuid == uuid)
        )
    ).one_or_none()
    if row is None:
        raise NotFoundException("Species not found")
    await release_read_transaction(db)
    return _PhotoInputs(*row)


async def _admin_species(db: AsyncSession, species_id: int) -> AdminSpeciesRead:
    species = (await db.execute(select(Species).where(Species.id == species_id))).scalar_one()
    return AdminSpeciesRead.model_validate(species, from_attributes=True)


@router.get("/species/{uuid}/photo-candidates", response_model=AdminSpeciesPhotoCandidates)
async def read_species_photo_candidates(
    uuid: uuid_pkg.UUID, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> AdminSpeciesPhotoCandidates:
    """The Wikimedia Commons files the operator may pin for this species, each with a preview.
    Superuser only.

    The species' Wikidata item's own images (P18) first, then the file stored now, then its
    Commons category's files - the item's category (P373), or one of the stored file's
    categories when the item names none - capped at two dozen. `preview` is a `data:` URI of
    the 250 px rendition, so a page shows it without contacting Wikimedia; it is `null` where
    the bytes did not arrive in time.

    A species with no Wikidata item and no stored file answers an empty list; pinning a pasted
    title still works. 404 for an unknown species.
    """
    row = await _photo_inputs(db, uuid)
    found = await species_service.photo_candidates(
        wikidata_qid=row.wikidata_qid, photo_file=row.photo_file, genus=row.genus
    )
    return AdminSpeciesPhotoCandidates(
        category=found.category,
        candidates=[
            AdminSpeciesPhotoCandidate(
                file=candidate.file,
                width=candidate.width,
                height=candidate.height,
                license=candidate.credit.license_name,
                author=candidate.credit.author,
                source_url=candidate.credit.source_url,
                preview=candidate.preview,
                is_current=candidate.is_current,
            )
            for candidate in found.candidates
        ],
    )


@router.put("/species/{uuid}/photo", response_model=AdminSpeciesRead)
async def pin_species_photo(
    uuid: uuid_pkg.UUID, body: AdminSpeciesPhotoPin, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> AdminSpeciesRead:
    """Pin a Wikimedia Commons file as this species' photo, replacing whatever was there.
    Superuser only.

    `file` is a file title, with or without `File:`, or its `commons.wikimedia.org/wiki/File:`
    page URL. The size floor does not apply: the operator saw the file and chose it. A pinned
    photo stays, untouched by the automatic selection, until a re-fetch or a hide replaces
    it. Answers the row, `photo_curation` reading `pinned`.

    404 for an unknown species. 422 for input that names no raster file, a file Commons does
    not have, or bytes that will not decode; 503 when Commons could not be asked, with the
    row unchanged.
    """
    # The backfill skips a pinned row, `--force` included; only a re-fetch hands it back to
    # the rule.
    title = species_photos.normalize_file_title(body.file)
    if title is None:
        raise UnprocessableEntityException("That is not the title or page URL of a photograph on Wikimedia Commons.")
    row = await _photo_inputs(db, uuid)

    try:
        attempt = await species_service.fetch_pinned_photo(title)
    except species_service.CommonsFileMissingError as exc:
        raise UnprocessableEntityException(f"Wikimedia Commons has no file named {title!r}.") from exc
    except species_photos.UnsupportedPhotoImageError as exc:
        raise UnprocessableEntityException(f"{title!r} could not be used: {exc}") from exc
    if attempt.photo is None:
        raise HTTPException(status_code=503, detail=_PHOTO_UNAVAILABLE)

    await species_photos.write_curated_photo(db, species_id=row.id, photo=attempt.photo, curation=PhotoCuration.PINNED)
    return await _admin_species(db, row.id)


@router.delete("/species/{uuid}/photo", response_model=AdminSpeciesRead)
async def hide_species_photo(
    uuid: uuid_pkg.UUID, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> AdminSpeciesRead:
    """Hide this species' photo: clear it, delete its bytes, and keep the automatic selection
    from putting one back. Superuser only.

    Undoing a hide is a re-fetch or a pin. Answers the row, `photo_curation` reading
    `hidden`. 404 for an unknown species.
    """
    # The stored bytes go rather than being kept behind a flag, because nothing would serve
    # them and a kept blob is one the sweeper cannot reclaim.
    row = await _photo_inputs(db, uuid)
    await species_photos.write_curated_photo(db, species_id=row.id, photo=None, curation=PhotoCuration.HIDDEN)
    return await _admin_species(db, row.id)


@router.post("/species/{uuid}/photo/refetch", response_model=AdminSpeciesRead)
async def refetch_species_photo(
    uuid: uuid_pkg.UUID, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> AdminSpeciesRead:
    """Run the automatic photo selection for this species again, now, and make the row say
    what it answers. Superuser only.

    What lands an upstream fix to Wikidata on this instance at once, and what hands a hidden
    or pinned row back to the automatic selection: either way `photo_curation` ends up
    `null`. A photo the selection chooses replaces what was there; a selection that declines -
    no candidate, an ambiguous choice, a file under the floor - clears the stored photo.

    404 for an unknown species. 503, with the row and its bytes untouched, when the
    selection could not be run: a species register or Commons that did not answer, a spent
    provider counter, bytes that would not decode, or the time budget running out. Slow by
    nature: it waits on Wikidata and Commons.
    """
    row = await _photo_inputs(db, uuid)
    attempt = await species_service.fetch_photo_for_species(scientific_name=row.scientific_name, aphia_id=row.aphia_id)
    if not attempt.completed or attempt.outcome is species_service.PhotoOutcome.UNAVAILABLE:
        raise HTTPException(status_code=503, detail=_PHOTO_UNAVAILABLE)

    await species_photos.write_curated_photo(db, species_id=row.id, photo=attempt.photo, curation=None)
    return await _admin_species(db, row.id)
