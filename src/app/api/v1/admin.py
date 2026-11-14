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

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request
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
from ...crud.crud_daily_totals import daily_totals_between, month_start, monthly_active_accounts_between
from ...crud.crud_invitations import account_exists_for, crud_invitations, live_invitation_from
from ...crud.crud_invite_requests import delete_invite_requests
from ...crud.crud_species_photo_proposals import has_reviewable_proposal, reviewable_counts, reviewable_proposals
from ...crud.crud_user_sessions import accounts_with_a_live_session
from ...models.invite_request import InviteRequest
from ...models.species import Species
from ...models.user import User
from ...schemas.auth_audit_event import AuthEventType
from ...schemas.daily_total import DailyMetric, DailyStatsDay, DailyStatsRead, DailyStatsTotals, MonthlyStats
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
    AdminSpeciesColumns,
    AdminSpeciesFilter,
    AdminSpeciesPhotoCandidate,
    AdminSpeciesPhotoCandidates,
    AdminSpeciesPhotoPin,
    AdminSpeciesRead,
    PhotoCuration,
)
from ...schemas.species_photo_proposal import AdminSpeciesPhotoProposalRead, SpeciesPhotoProposalDecline
from ...services import species_photo_proposals, species_photos, species_service
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
    """The daily totals for every UTC day from `from` to `to` inclusive, zero-filled, the
    distinct active accounts of every calendar month the range touches, with the instance's
    join channels and two figures as of now. Superuser only.

    Every number is a count that names no account: accounts created per day and per door,
    distinct accounts that signed in, distinct accounts with a session used that day.
    `months` holds one entry per UTC calendar month from the one holding `from` to the one
    holding `to`, each counting the distinct accounts with a session used at any time in that
    whole month - so far, for the current one, and up to an hour behind. A month's
    `active_accounts` is `null` when the instance holds no count of it, which is every month
    before it began counting. `totals.accounts` counts every account, one inside its deletion
    grace period included, and `totals.active_now` the accounts holding a session that can
    still authenticate.

    422 for a range running backwards or longer than 92 days.
    """
    # Calendar months rather than a rolling thirty days: a month's id set is discarded once
    # the month has closed, and a rolling window would need the ids kept past that.
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
    monthly = await monthly_active_accounts_between(db, first=first, last=last)
    months = sorted({month_start(day) for day in days})
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
        months=[MonthlyStats(month=month, active_accounts=monthly.get(month)) for month in months],
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
    AdminSpeciesFilter.PROPOSED: has_reviewable_proposal(),
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
    `with_photo`, `without_photo`, `hidden`, `pinned`, `narrow` - a stored photo under the
    500 px floor, which only a pinned one may keep - or `proposed`, a species with a photo
    proposal waiting. Each row counts its waiting proposals in `photo_proposals_waiting`; a
    proposal whose account is being deleted is left out of the count and the filter, and comes
    back if the account is restored. Out-of-range pagination is clamped.

    Answers only a signed-in session: an API key is refused with 403.
    """
    # Unindexed and uncached: the catalog is thousands of rows and this is one operator's page.
    page, items_per_page = clamp_pagination(page, items_per_page)

    clauses = []
    if search and (term := search.strip()):
        clauses.append(species_search_clause(term))
    if chip is not None:
        clauses.append(_SPECIES_FILTERS[chip])

    rows = list(
        (
            await db.execute(
                select(Species)
                .where(*clauses)
                .order_by(Species.created_at.desc(), Species.id.desc())
                .offset(compute_offset(page, items_per_page))
                .limit(items_per_page)
            )
        ).scalars()
    )
    total = await db.scalar(select(func.count()).select_from(Species).where(*clauses))
    waiting = await reviewable_counts(db, [row.id for row in rows])

    data: dict[str, Any] = {
        "data": [_admin_row(row, waiting.get(row.id, 0)).model_dump() for row in rows],
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


def _admin_row(species: Species, waiting: int) -> AdminSpeciesRead:
    columns = AdminSpeciesColumns.model_validate(species, from_attributes=True)
    return AdminSpeciesRead(**columns.model_dump(), photo_proposals_waiting=waiting)


async def _admin_species(db: AsyncSession, species_id: int) -> AdminSpeciesRead:
    species = (await db.execute(select(Species).where(Species.id == species_id))).scalar_one()
    waiting = await reviewable_counts(db, [species_id])
    return _admin_row(species, waiting.get(species_id, 0))


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
    it. Every photo proposal waiting on the species that names this file is accepted, and every
    other one is closed. Answers the row, `photo_curation` reading `pinned`.

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
    return await _pin(db, species_id=row.id, title=title)


async def _pin(db: AsyncSession, *, species_id: int, title: str) -> AdminSpeciesRead:
    """Fetch `title` and pin it, refusing as the pin route documents. The caller has released
    its read transaction: this goes outbound first."""
    try:
        attempt = await species_service.fetch_pinned_photo(title)
    except species_service.CommonsFileMissingError as exc:
        raise UnprocessableEntityException(f"Wikimedia Commons has no file named {title!r}.") from exc
    except species_photos.UnsupportedPhotoImageError as exc:
        raise UnprocessableEntityException(f"{title!r} could not be used: {exc}") from exc
    if attempt.photo is None:
        raise HTTPException(status_code=503, detail=_PHOTO_UNAVAILABLE)

    await species_photos.write_curated_photo(
        db, species_id=species_id, photo=attempt.photo, curation=PhotoCuration.PINNED
    )
    return await _admin_species(db, species_id)


@router.delete("/species/{uuid}/photo", response_model=AdminSpeciesRead)
async def hide_species_photo(
    uuid: uuid_pkg.UUID, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> AdminSpeciesRead:
    """Hide this species' photo: clear it, delete its bytes, and keep the automatic selection
    from putting one back. Superuser only.

    Every photo proposal waiting on the species is closed. Undoing a hide is a re-fetch or a
    pin. Answers the row, `photo_curation` reading `hidden`. 404 for an unknown species.
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
    `null`, and the species accepts photo proposals again. A photo the selection chooses
    replaces what was there; a selection that declines - no candidate, an ambiguous choice, a
    file under the floor - clears the stored photo.

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


# -------------- photo proposals --------------


@router.get("/species/{uuid}/photo-proposals", response_model=list[AdminSpeciesPhotoProposalRead])
async def review_species_photo_proposals(
    uuid: uuid_pkg.UUID, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> list[AdminSpeciesPhotoProposalRead]:
    """The photo proposals waiting on this species, oldest first and at most two dozen, each with
    its file, note, proposer's name and date. Superuser only.

    Each also carries what Wikimedia Commons says of the file - the original's `width` and
    `height`, `license`, `author`, `source_url`, and `preview`, a `data:` URI of its 250 px
    rendition - and every one of those is `null` where Commons has no such file or gave no
    answer in time; the proposal is listed either way. A proposal whose account is being deleted
    is left out. Slow by nature: it waits on Commons.

    404 for an unknown species. Answers only a signed-in session: an API key is refused with 403.
    """
    # The cap is the picker's: these previews are charged to no Commons counter either, on the
    # same premise of one capped read at one operator's pace.
    species_id = await db.scalar(select(Species.id).where(Species.uuid == uuid))
    if species_id is None:
        raise NotFoundException("Species not found")
    proposals = await reviewable_proposals(db, species_id=species_id, limit=species_service.CANDIDATE_LIMIT)
    await release_read_transaction(db)

    files = await species_service.proposed_files([proposal["file"] for proposal in proposals])
    answer = []
    for proposal in proposals:
        found = files.get(proposal["file"])
        answer.append(
            AdminSpeciesPhotoProposalRead(
                uuid=proposal["uuid"],
                file=proposal["file"],
                note=proposal["note"],
                proposer_name=proposal["proposer_name"],
                created_at=proposal["created_at"],
                width=found.width if found else None,
                height=found.height if found else None,
                license=found.credit.license_name if found else None,
                author=found.credit.author if found else None,
                source_url=found.credit.source_url if found else None,
                preview=found.preview if found else None,
            )
        )
    return answer


@router.post("/species-photo-proposal/{uuid}/accept", response_model=AdminSpeciesRead)
async def accept_species_photo_proposal(
    uuid: uuid_pkg.UUID, db: Annotated[AsyncSession, Depends(async_get_db)]
) -> AdminSpeciesRead:
    """Accept a waiting photo proposal: pin its file as the species' photo, exactly as pinning
    it by hand does, and answer the species' row. Superuser only.

    The proposal reads `accepted`, as does any other waiting on the species that names the same
    file, and every other one waiting there is closed. Whatever a pin refuses, this refuses the
    same way, leaving the proposal waiting: 422 for a file Commons does not have or bytes that
    will not decode, 503 when Commons could not be asked.

    409 on a proposal already decided, withdrawn or closed; 404 on no such proposal and on one
    whose account is being deleted. Answers only a signed-in session: an API key is refused with
    403. Slow by nature: it waits on Commons.
    """
    proposal = await species_photo_proposals.waiting_for_review(db, uuid=uuid)
    species_id, title = proposal.species_id, proposal.file
    await release_read_transaction(db)
    return await _pin(db, species_id=species_id, title=title)


@router.post("/species-photo-proposal/{uuid}/decline", response_model=AdminSpeciesRead)
async def decline_species_photo_proposal(
    uuid: uuid_pkg.UUID,
    db: Annotated[AsyncSession, Depends(async_get_db)],
    values: Annotated[SpeciesPhotoProposalDecline | None, Body()] = None,
) -> AdminSpeciesRead:
    """Decline a waiting photo proposal, with an optional reason the proposer reads, and answer
    the species' row. Superuser only.

    The species' photo is unchanged. The declined file never waits for this species again,
    whoever proposes it. 409 on a proposal already decided, withdrawn or closed; 404 on no such
    proposal and on one whose account is being deleted. Answers only a signed-in session: an API
    key is refused with 403.
    """
    species_id = await species_photo_proposals.decline(db, uuid=uuid, reason=values.reason if values else "")
    return await _admin_species(db, species_id)
