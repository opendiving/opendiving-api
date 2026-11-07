"""Restoring a logbook into the caller's own account, whatever wrote it.

The mirror of `/export/*`, and the half that makes "your data is never more than one curl
away" a round trip rather than an exit. What is true of the whole family is here rather than in
the service layer, because each is an HTTP concern:

- **The caller's own account, and nothing else.** No `username` or `user_uuid` parameter on the
  upload: the bearer token names the only logbook there is to import into. The document's own
  diver identity and settings are read, reported and never applied; its check-in details and its
  portrait are written only as the diver confirms them in the review, and its tag list is the
  diver's tags.
- **Any number of files, in any mix of the formats this build reads.** A DiveJSON document,
  the full-export archive, a UDDF file, a Subsurface `.ssrf`, a FIT, a Suunto app export, a
  Suunto DM5 XML export, a Shearwater Cloud database or XML export, or a zip of any of those,
  each sent as a `file` part - a watch writes one file per dive, and one file per import would
  cap a diver at ten dives an hour against the rate limit below. Which formats exactly is
  `divejson.read_formats()` and never a list written out here.
- **Uploaded once, reviewed anywhere, applied as reviewed.** `POST /imports` reads, converts and
  plans, and holds the readable files and the review until the import is applied or discarded,
  so one device can upload and another finish; `POST /import/{uuid}/apply` sends no bytes.
- **Rate limited, per user, on its own budget, and after the caller is known.** An import
  may carry half a gigabyte, parses whole logbooks and may make outbound WoRMS calls; the
  export endpoints' docstring records why a whole-logbook endpoint is throttled at all, and
  this one is dearer than any export. The upload reads its own body, after the session, the
  limit and the account's slot have been checked, so a request refused for any of them reads
  none of it. The apply has a budget of the same size beside it, and the reads are not limited.
- **The error taxonomy is `POST /dive/parse`'s.** 415 is "no reader here claims these
  bytes", 422 is "one did, and it failed", 413 is past a bound of the request or over the
  account's storage limit. A file that cannot be read is a row of the report, not a refusal
  of the import: the whole request answers with one of those only where no file of it
  reads, and then as its first refused file would on its own. Short of that, a file that is
  *readable* never fails: a record this app cannot store is skipped and reported, a value it
  cannot hold is dropped and reported, and what a file did not record comes back as a note
  on its dive rather than as a refusal.
- **Atomic in rows.** The apply is one transaction, committed once at the end, so an import
  that fails or is interrupted writes nothing and a retry cannot half-duplicate a logbook.
- **Served as stored.** An import's reads answer with the JSON the row holds, the schema on
  `response_model` being for the published docs alone: validating a stored review against the
  current schemas would turn the next required member into a 500 on every pending import.
"""

import logging
import uuid as uuid_pkg
from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastcrud import PaginatedListResponse, compute_offset, paginated_response
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.config import settings
from ...core.db.database import async_get_db, release_read_transaction
from ...core.exceptions.http_exceptions import BadRequestException, NotFoundException, UnprocessableEntityException
from ...core.utils.pagination import ItemsPerPageQuery, PageQuery, clamp_pagination
from ...core.utils.rate_limit import enforce_rate_limit
from ...models.logbook_import import LogbookImport
from ...schemas.logbook_import import (
    LogbookImportApply,
    LogbookImportNotes,
    LogbookImportRead,
    LogbookImportStatus,
    LogbookImportSummary,
)
from ...services.blob_store import BlobMissingError
from ...services.cache_invalidation import (
    invalidate_certification_caches,
    invalidate_contact_caches,
    invalidate_course_caches,
    invalidate_dive_caches,
    invalidate_dive_site_caches,
    invalidate_gear_caches,
    invalidate_trip_caches,
)
from ...services.logbook_import import (
    MAX_PARTS,
    ImportPart,
    ImportRequest,
    ImportTooLargeError,
    LoadedBatch,
    MalformedImportError,
    MalformedRequestError,
    UnsupportedImportError,
    batch_species,
    formats_this_build_reads,
    load_import,
    read_import_request,
    resolve_catalog_gaps,
    unresolved_aphia_ids,
)
from ...services.logbook_import.imports import (
    apply_held,
    delete_import,
    find_import,
    find_pending_import,
    held_files,
    held_parts,
    hold_upload,
    import_body,
    list_applied,
    lock_import,
    refuse_unless_pending,
    refuse_while_pending,
)
from ...services.logbook_import.parts import FILE_FIELD

logger = logging.getLogger(__name__)

router = APIRouter(tags=["import"])

# Built from the registry rather than written out, so the OpenAPI description cannot end up
# naming fewer formats than the build reads - the pin moves on its own.
_FILE_DESCRIPTION = (
    f"One file of the import, sent as a `file` part once per file - up to {MAX_PARTS} of them, in any mix: "
    "a DiveJSON document (`.divejson`), the full-export archive (`.zip`) containing one, a dive-computer file or "
    f"logbook in any of these formats: {formats_this_build_reads()}, or a `.zip` of any of them"
)


_UPLOAD_BODY = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {
                        FILE_FIELD: {
                            "type": "array",
                            "items": {"type": "string", "contentMediaType": "application/octet-stream"},
                            "description": _FILE_DESCRIPTION,
                        }
                    },
                    "required": [FILE_FIELD],
                }
            }
        },
    }
}


async def _enforce_import_limit(user_id: int) -> None:
    await enforce_rate_limit(
        f"import:user:{user_id}",
        settings.IMPORT_RATE_LIMIT_PER_USER,
        settings.IMPORT_RATE_LIMIT_WINDOW_SECONDS,
    )


async def _enforce_apply_limit(user_id: int) -> None:
    """A budget beside the upload's rather than a share of it, so one import is one call against
    each: a refused apply stays pending, and repeats the whole read and pass."""
    await enforce_rate_limit(
        f"import-apply:user:{user_id}",
        settings.IMPORT_RATE_LIMIT_PER_USER,
        settings.IMPORT_RATE_LIMIT_WINDOW_SECONDS,
    )


def _found(row: LogbookImport | None) -> LogbookImport:
    if row is None:
        raise NotFoundException("Import not found")
    return row


async def _read_held(db: AsyncSession, user_id: int, uuid: uuid_pkg.UUID, files: Sequence[Any]) -> ImportRequest:
    """The held files, or what another device left: finishing an import takes its files, so one
    gone from under this read is that device's apply or discard rather than lost bytes."""
    try:
        return await held_parts(files)
    except BlobMissingError:
        refuse_unless_pending(_found(await find_import(db, user_id=user_id, uuid=uuid)))
        raise


def _missing(field: str) -> RequestValidationError:
    """The 422 FastAPI answers a required form field with, for a body read here."""
    return RequestValidationError([{"type": "missing", "loc": ("body", field), "msg": "Field required", "input": None}])


async def _read(request: Request) -> ImportRequest:
    """Read the body, translating the part reader's refusals into their status codes.

    The caller releases the read transaction the session's authentication opened first: the
    body arrives at the client's pace, up to half a gigabyte, and a connection held idle
    across it is the pool exhaustion `release_read_transaction` exists to prevent.
    """
    try:
        body = await read_import_request(request)
    except ImportTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except MalformedRequestError as exc:
        raise BadRequestException(str(exc)) from exc
    if not body.parts:
        body.close()
        raise _missing(FILE_FIELD)
    return body


async def _load(parts: list[ImportPart]) -> LoadedBatch:
    """Read the files, translating the refusal of an import no file of which reads.

    Three, still: the reader translates every one of the converter's refusals into these
    same classes on its way out, so this stays the single place the taxonomy is written
    down.
    """
    try:
        return await load_import(parts)
    except ImportTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except UnsupportedImportError as exc:
        # 415 stays a raw `HTTPException`: unlike 400/403/404/422, `http_exceptions` has no
        # class for it - the same reason `POST /dive/parse` raises one by hand.
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except MalformedImportError as exc:
        raise UnprocessableEntityException(str(exc)) from exc


@router.post("/imports", status_code=201, response_model=LogbookImportRead, openapi_extra=_UPLOAD_BODY)
async def create_logbook_import(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> JSONResponse:
    """Upload any number of files, and answer with the review of what importing them would do.

    The readable files are held on the server until the import is applied or discarded, for at
    most the instance's limit, so the import can be finished from any device; `expires_at` says
    when it goes. Files cannot be added to it: a forgotten file means discarding it and uploading
    again. Nothing in your logbook changes until `POST /import/{uuid}/apply`.

    Each file is a `file` part, and the part's description lists what one may be. The files
    are classified by their bytes, never by their names; a zip that is not a full-export
    archive is opened, its readable files are held one by one, and the zip is not. At most one
    full-export archive is imported at a time.

    **An import of several files writes what its files would write imported one at a time, in
    the order it reads them**: the archive, then DiveJSON documents, then logbooks, then
    dive-computer files - the Suunto app's JSON exports ahead of the rest, each by name. So
    a dive computer's two exports of one dive are one dive with both files, whether they
    arrive together or on two days, and two computers' records of one dive are one dive with
    two recordings. A dive's first file decides its positions, which a later one only fills,
    and the Suunto app's JSON states each satellite fix's error where the same dive's FIT
    does not. A file that is one dive - one computer's record of it, whatever its format -
    is kept on the dive it becomes, as the dive form keeps a file it is handed, and a
    dive-computer file's dive takes its identifier from its bytes: the same whenever the
    same file comes again, under any name.

    `dives` has one row per dive the files hold. A `new` row carries the dive as it will be
    written. A `modified` row is a dive you have, carrying the dive as it will be and, in
    `changes`, each member that differs, what it was and what it becomes: adding a file or a
    recording is a change even where no value of the dive moves. An `unchanged` row is a dive you
    have that the import leaves as it is, and a `skipped` row one it will not write, with its
    `reason`. A dive you deleted here is skipped: no import brings one back, though a deleted
    dive's recordings can still reach a dive you have, as a new dive's would.

    Every other record lands in one of three buckets, per collection, summed over the files:
    **created**, **linked** (an existing record of yours already carries that identifier, or
    that name) or **skipped**. `files` says how many files and card images the documents name
    and how many of them the import actually carries - only the full-export archive carries
    any.

    `members` has one row per file - each part and each file a zip held - saying what it was
    read as, whether it is kept and why not, or why it was refused. A file that cannot be read
    stops nothing else and is not held. Treat a `code` or `kind` you do not recognise as
    information.

    `check_in_details` has one entry for each member of the check-in details the logbook
    carries - email, phone, date of birth, emergency contacts, insurance policies - keyed by
    the member's name, with your account's value beside the proposal. A list is proposed as a
    list: each of the logbook's rows is your own where every part it gives matches one of
    yours, otherwise the logbook's alone. An email that is the address you sign in with is not
    offered, unless it already is your check-in email.

    `portrait` is the archive's portrait beside your account's, when the archive carries one
    it can offer: yours as the digest `GET /user/portrait` answers to, the archive's as an
    inline image framed as it would be stored. It is absent when the archive's is your own,
    framed the same, and a portrait that cannot be offered is explained in the log.

    A person this app exported linked to an account here is proposed linked to it again,
    within the limit on linking people - an `account_linked` note names the account's
    current username - where the account still exists, is not yours and no other person of
    yours links it. Nothing of a link that cannot be made is kept.

    Species this instance's catalog did not hold are looked up in the World Register of Marine
    Species before the review is made, and stay in the catalog whatever becomes of the import.

    One import at a time: a 409 names the pending import while your account has one, on any
    device, and its body is not read. Rate limited per account, on a budget of its own; a
    request over it is a 429 and its body is not read. A 413 when the files together are past
    what one import may carry or plan - its `detail` says which - and one naming the storage
    used and the limit when the files the import would store do not fit in what your account
    has left, measured as they would be stored and with your own portrait kept. When no file can
    be read at all, the answer is what the first of them would get on its own: 415 when nothing
    here reads bytes of that kind, 422 when a reader does and the file is malformed.
    """
    user_id = current_user["id"]
    await _enforce_import_limit(user_id)
    await refuse_while_pending(db, user_id=user_id)
    await release_read_transaction(db)
    with await _read(request) as body, await _load(body.parts) as batch:
        # Before the row and outside its transaction: `resolve_species` commits its own rows and
        # rolls back before going outbound, and its worst case is on the order of a minute per
        # unknown species. See `services/logbook_import/species.py`.
        newly_resolved = await resolve_catalog_gaps(db, aphia_ids=unresolved_aphia_ids(batch_species(batch)))
        held = await hold_upload(
            db, user_id=user_id, parts=body.parts, batch=batch, newly_resolved_aphia_ids=newly_resolved
        )
        await db.commit()
    return JSONResponse(content=await import_body(db, held), status_code=201)


@router.get("/imports", response_model=PaginatedListResponse[LogbookImportSummary])
async def read_logbook_imports(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    page: PageQuery = 1,
    items_per_page: ItemsPerPageQuery = 20,
) -> dict[str, Any]:
    """List your applied imports, most recently applied first, each with its counts of dives by
    outcome. An import past the instance's retention is not listed, and a pending one never is:
    `GET /imports/pending` answers that."""
    page, items_per_page = clamp_pagination(page, items_per_page)
    rows, total = await list_applied(
        db,
        user_id=current_user["id"],
        offset=compute_offset(page, items_per_page),
        limit=items_per_page,
    )
    crud_data = {
        "data": [
            {"uuid": row.uuid, "created_at": row.created_at, "applied_at": row.applied_at, "counts": row.counts}
            for row in rows
        ],
        "total_count": total,
    }
    response: dict[str, Any] = paginated_response(crud_data=crud_data, page=page, items_per_page=items_per_page)
    return response


@router.get("/imports/pending", response_model=LogbookImportRead)
async def read_pending_logbook_import(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> JSONResponse:
    """Return your account's pending import, the review `POST /imports` answered with, from any
    device. 404 when there is none, or when it is past its limit."""
    row = _found(await find_pending_import(db, user_id=current_user["id"]))
    return JSONResponse(content=await import_body(db, row))


@router.get("/import/{uuid}", response_model=LogbookImportRead)
async def read_logbook_import(
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> JSONResponse:
    """Return one of your imports, pending or applied.

    An applied import is what the review showed beside what happened: a dive the apply skipped
    keeps the `uuid` and `dive` the review gave it, with its `reason`, and a `new` dive the apply
    added to a dive you already had carries that dive's `uuid`. 404 for an import that is not
    there, is past its limit, or is someone else's.
    """
    row = _found(await find_import(db, user_id=current_user["id"], uuid=uuid))
    return JSONResponse(content=await import_body(db, row))


@router.get("/import/{uuid}/notes", response_model=LogbookImportNotes)
async def read_logbook_import_notes(
    uuid: uuid_pkg.UUID,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> JSONResponse:
    """Return an import's log.

    `dives` has an entry for each dive row, in the review's order, with its `outcome`, the `reason`
    a skipped one was skipped, and its `notes`: each decision that is not a plain create, and what
    its files did not record, worked out, or read one way of several, one sentence at a time. A
    line about a value goes when another file of the dive supplied it. Everything about no dive is
    in `notes`, one group per code and sentence with a count and up to three places in your files.
    On an applied import these are what the apply found, with what it could not hold to the
    review. Treat a `code` or `kind` you do not recognise as information. 404 as
    `GET /import/{uuid}`.
    """
    return JSONResponse(content=_found(await find_import(db, user_id=current_user["id"], uuid=uuid)).notes)


@router.post("/import/{uuid}/apply", response_model=LogbookImportRead)
async def apply_logbook_import(
    uuid: uuid_pkg.UUID,
    choices: LogbookImportApply,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> JSONResponse:
    """Write a pending import as it was reviewed, and answer with the import, now `applied`.

    The review is what is written, from the files the upload left on the server, and no bytes
    are sent. A value of a recording or a cylinder that a `modified` dive's `changes` lists is
    written as its `after`, even where the stored value moved since, unless it has nowhere to
    land: the recording has gone, the dive's cylinder list was reshaped in between, or the row
    cannot hold it beside what it now records. Everything else `changes` lists - a file, a
    recording, a profile, a position read from the files, a cylinder's link to a gear item - comes
    of the files themselves. What did not arrive is named by a note on the dive. A dive the
    review showed that has since been deleted or merged away is skipped, and nothing is written
    for it. A dive the review showed as
    `new` that reached your logbook another way in between takes the outcome and `uuid` of the
    dive that holds it. A dive the review did not show that the files now reach, the logbook
    having moved, is written too, and follows the rows the review showed with a note saying so.
    `GET /import/{uuid}/notes` has what happened.

    **All or nothing in rows.** A failure at any point writes no records at all, so a retry
    after a timeout can never half-duplicate a logbook. Applied, the import's uploaded files are
    deleted, whatever it stored on your dives stays there, and the review, the result and the
    log stay readable until the instance's retention runs out or you delete the import.

    The document's diver identity and settings are never applied: this account keeps its own
    name, sign-in email, units and notification settings. Its check-in details are written as
    sent in `check_in_details` and only then - a member not sent, or one the logbook does not
    carry, stays as it is. What is sent meets the bounds `PATCH /user/checkin-details` does,
    so an emergency contact without a name or a policy without a provider is a 422.

    The archive's portrait replaces your account's only when `portrait` chooses the archive's, and
    only while your portrait is still the one the review showed; otherwise yours is kept,
    and a note says so when you had chosen the archive's.

    Each link to an account the import makes counts against the limit on linking people,
    and one past it is dropped with a note rather than failing the import. A link counts
    whether or not the import completes.

    A 409 when the import is already applied, or another device's apply got there first. Rate
    limited per account, on a budget of its own; a request over it is a 429 and writes nothing. Files
    that would take your account past its storage limit refuse the import whole with a 413, which
    here also counts the archive's portrait if `portrait` takes it. 404 as `GET /import/{uuid}`.
    """
    # The files an import stores are written before the rows that name them commit, so a failure
    # can strand an unreferenced file - harmless, and swept.
    user_id = current_user["id"]
    row = _found(await find_import(db, user_id=user_id, uuid=uuid))
    refuse_unless_pending(row)
    await _enforce_apply_limit(user_id)
    files = await held_files(db, row)
    await release_read_transaction(db)
    with await _read_held(db, user_id, uuid, files) as held, await _load(held.parts) as batch:
        # Only now: the files are read and converted with no transaction open, and whichever
        # device takes the row first is the one that applies. The other finds what it left.
        row = _found(await lock_import(db, user_id=user_id, uuid=uuid))
        refuse_unless_pending(row)
        await apply_held(
            db, row=row, user_id=user_id, batch=batch, check_in=choices.check_in_details, portrait=choices.portrait
        )
        await db.commit()

    # After the commit, never before: a cache dropped early can be refilled from the
    # pre-import state by any read that lands in between. An import fills the dive-site and
    # trip collections as well as everything the helpers below already cover, and those
    # two list caches live inside their own routers - see `services/cache_invalidation.py`.
    # Skipping them serves a restored diver empty pages for up to the 60-second list expiry,
    # at exactly the moment they go looking at what they just restored.
    await invalidate_dive_caches(user_id)
    await invalidate_certification_caches(user_id)
    await invalidate_contact_caches(user_id)
    await invalidate_course_caches(user_id)
    await invalidate_gear_caches(user_id)
    await invalidate_dive_site_caches(user_id)
    await invalidate_trip_caches(user_id)

    return JSONResponse(content=await import_body(db, row))


@router.delete("/import/{uuid}", status_code=204)
async def delete_logbook_import(
    uuid: uuid_pkg.UUID,
    status: Annotated[
        LogbookImportStatus,
        Query(description="The status of the import you are deleting: `pending` to discard, `applied` to forget"),
    ],
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> Response:
    """Discard a pending import, with the files it holds, or delete an applied one from your
    history. Nothing it wrote to your logbook is touched.

    A 409 when the import's status is not the one named, so a discard sent from a review another
    device has since applied deletes nothing. 404 as `GET /import/{uuid}`.
    """
    await delete_import(db, row=_found(await lock_import(db, user_id=current_user["id"], uuid=uuid)), status=status)
    return Response(status_code=204)
