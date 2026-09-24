"""Restoring a logbook into the caller's own account, whatever wrote it.

The mirror of `/export/*`, and the half that makes "your data is never more than one curl
away" a round trip rather than an exit. Six things are true of both endpoints, and each is
here rather than in the service layer because each is an HTTP concern:

- **The caller's own account, and nothing else.** No `username` or `user_uuid` parameter:
  the bearer token names the only logbook there is to import into, so there is no
  authorization decision to get wrong. The document's own diver identity and settings are
  read, reported and never applied; its check-in details and its portrait are written only
  as the diver confirms them in the preview.
- **Any format the converter reads, and the app's own two.** A DiveJSON document, the
  full-export archive, a UDDF file, a Subsurface `.ssrf`, a FIT, a Suunto app export, a
  Suunto DM5 XML export, or a zip whose members are all one of those - a watch writes one file per dive, and one file
  per import would cap a diver at ten dives an hour against the rate limit below. Which
  formats exactly is `divejson.read_formats()` and never a list written out here.
- **Two phases, mirroring the parse-then-attach flow.** `POST /import/logbook/preview`
  reads, converts, plans and reports, storing nothing; `POST /import/logbook` re-uploads the
  same file with the preview's token and writes. The token attests which bytes the report
  was about - the shape `create_dive_file_token` already uses, and it is minted over the
  *uploaded* bytes, not the converted document, so it names the file a diver picked. The
  file travels twice, which is the trade that flow already made. A server-side spool keyed
  by the token is the recorded escape hatch, not the design.
- **Rate limited, per user, on its own budget.** An import spools up to half a gigabyte,
  parses a whole logbook and may make outbound WoRMS calls; the export endpoints' docstring
  records why a whole-logbook endpoint is throttled at all, and this one is dearer than any
  export.
- **The error taxonomy is `POST /dive/parse`'s.** 415 is "no reader here claims these
  bytes", 422 is "one did, and it failed", 413 is over the cap. A file that is *readable*
  never fails: a record this app cannot store is skipped and reported, a value it cannot
  hold is dropped and reported, and what a conversion could not carry comes back on
  `ImportReport.conversion` rather than as a refusal.
- **Atomic in rows.** Apply is one transaction, committed once at the end, so an import
  that fails or is interrupted writes nothing and a retry cannot half-duplicate a logbook.
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.exceptions import RequestValidationError
from pydantic import Json
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import UnprocessableEntityException
from ...core.security import create_logbook_import_token, verify_logbook_import_token
from ...core.utils.rate_limit import enforce_rate_limit
from ...schemas.logbook_import import ImportCheckInSubmission, ImportPortraitChoice, ImportPreview, ImportResult
from ...services.cache_invalidation import (
    invalidate_certification_caches,
    invalidate_course_caches,
    invalidate_dive_caches,
    invalidate_dive_site_caches,
    invalidate_gear_caches,
    invalidate_trip_caches,
)
from ...services.logbook_import import (
    ImportPlan,
    ImportTooLargeError,
    LoadedImport,
    MalformedImportError,
    UnsupportedImportError,
    conversion_report,
    formats_this_build_reads,
    load_import,
    plan_import,
    resolve_catalog_gaps,
    unresolved_aphia_ids,
    write_import,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["import"])

# Built from the registry rather than written out, so the OpenAPI description cannot end up
# naming fewer formats than the build reads - the pin moves on its own.
_FILE_DESCRIPTION = (
    "A DiveJSON document (`.divejson`), the full-export archive (`.zip`) containing one, a dive-computer "
    f"logbook in any of these formats: {formats_this_build_reads()}, or a `.zip` whose files are all one of them"
)


async def _enforce_import_limit(user_id: int) -> None:
    await enforce_rate_limit(
        f"import:user:{user_id}",
        settings.IMPORT_RATE_LIMIT_PER_USER,
        settings.IMPORT_RATE_LIMIT_WINDOW_SECONDS,
    )


async def _load(file: UploadFile) -> LoadedImport:
    """Read the upload, translating the reader's three refusals into their status codes.

    Three, still, now that a conversion can fail here too: the reader translates every one
    of the converter's refusals into these same classes on its way out, including the
    library's own cap refusal, so this stays the single place the taxonomy is written down.
    """
    try:
        return await load_import(file)
    except ImportTooLargeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except UnsupportedImportError as exc:
        # 415 stays a raw `HTTPException`: unlike 400/403/404/422, `http_exceptions` has no
        # class for it - the same reason `POST /dive/parse` raises one by hand.
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except MalformedImportError as exc:
        raise UnprocessableEntityException(str(exc)) from exc


def _body(plan: ImportPlan, loaded: LoadedImport) -> dict[str, object]:
    return {
        "collections": plan.collection_reports(),
        "files": plan.file_report(),
        "notes": plan.notes,
        "notes_truncated": plan.notes_dropped,
        "conversion": conversion_report(loaded),
    }


@router.post("/import/logbook/preview", response_model=ImportPreview)
async def preview_logbook_import(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    file: Annotated[UploadFile, File(description=_FILE_DESCRIPTION)],
) -> ImportPreview:
    """Read a logbook and report what importing it would do. Stores nothing.

    The file may be a DiveJSON document, the full-export archive, or a dive-computer logbook
    in any format this build converts - the upload field's description lists them. A `.zip`
    whose files are all one of those formats is read as one logbook, which is how a watch's
    account export arrives.

    Every record lands in one of four buckets, per collection: **created**, **linked** (an
    existing record of yours already carries that identifier, or that name), **restored** (a
    record you deleted here, coming back under its original identifier) or **skipped**.
    `notes` explains every decision that is not a plain create, one sentence at a time, and
    `files` says how many dive-computer files and card images the logbook references and how
    many of them it actually contains - only the full-export archive carries any.

    `conversion` is present when the file was not DiveJSON already, and says what the
    conversion could not carry: findings grouped by kind and message, each with up to three
    paths into your original file. Treat a `kind` you do not recognise as a plain finding.

    `check_in_details` has one entry for each check-in detail the logbook carries - date of
    birth, phone, emergency contact, dive insurance - with your account's value beside the
    proposal. An emergency contact or an insurance is proposed whole: your own where every
    part the logbook gives matches it, otherwise the logbook's alone.

    `portrait` is the archive's portrait beside your account's, when the archive carries one
    it can offer: yours as the digest `GET /user/portrait` answers to, the archive's as an
    inline image framed as it would be stored. It is absent when the archive's is your own,
    framed the same, and a portrait that cannot be offered is explained in `notes`.

    The `token` in the response goes to `POST /import/logbook` with the same file. It says
    which bytes this report describes and nothing more: the import re-reads, re-converts and
    re-plans, because your logbook may have moved between the two calls.
    """
    await _enforce_import_limit(current_user["id"])
    with await _load(file) as loaded:
        plan = await plan_import(db, user_id=current_user["id"], loaded=loaded)
        return ImportPreview(
            format=loaded.document.format,
            version=loaded.document.version,
            generator=loaded.document.generator,
            archive=loaded.is_archive,
            token=create_logbook_import_token(user_uuid=current_user["uuid"], sha256=loaded.digest),
            check_in_details=plan.check_in_details,
            portrait=await plan.portrait_offer(),
            **_body(plan, loaded),
        )


@router.post("/import/logbook", response_model=ImportResult)
async def apply_logbook_import(
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
    file: Annotated[UploadFile, File(description=_FILE_DESCRIPTION)],
    token: Annotated[str, Form(description="The `token` from this file's preview")],
    check_in_details: Annotated[
        Json[ImportCheckInSubmission] | None,
        Form(
            description="The check-in details to write, as JSON: the preview's proposals as kept or edited. A "
            "detail left out is not written, and `null` clears it."
        ),
    ] = None,
    portrait: Annotated[
        Json[ImportPortraitChoice] | None,
        Form(
            description="The choice made for the preview's `portrait`, as JSON: `take` or `keep`, with the "
            "`account_sha256` the preview showed. Left out, the account keeps its portrait."
        ),
    ] = None,
) -> ImportResult:
    """Import a logbook into your account, after previewing it.

    Answers with the same report the preview did, describing what was actually written. The
    counts can differ from the preview's where your logbook moved in between - a dive
    deleted since is restored rather than linked - which is why the plan is made afresh here
    rather than replayed. A converted file is converted again here and reaches the same
    document: nothing in the conversion depends on when it runs.

    **All or nothing in rows.** A failure at any point writes no records at all, so a retry
    after a timeout can never half-duplicate a logbook. Two things sit outside that on
    purpose: a restored file's bytes are written before the transaction that names them, so
    a failure can strand an unreferenced file (harmless, and swept), and species this
    instance's catalog did not hold are looked up in the World Register of Marine Species
    before the transaction opens and stay whether the import completes or not.

    The document's diver identity and settings are never applied: this account keeps its own
    name, email, units and notification settings. Its check-in details are written as sent in
    `check_in_details` and only then - a detail not sent, or one the logbook does not carry,
    stays as it is. What is sent meets the bounds `PATCH /user` does, and an emergency
    contact without a name or an insurance without a provider is a 422.

    The archive's portrait replaces your account's only when `portrait` says `take`, and
    only while your portrait is still the one the preview showed; otherwise yours is kept,
    and a note says so when you had chosen the archive's.
    """
    if check_in_details is not None and (anchor_errors := check_in_details.anchor_errors()):
        raise RequestValidationError(
            [{**error, "loc": ("body", "check_in_details", *error["loc"])} for error in anchor_errors]
        )
    await _enforce_import_limit(current_user["id"])
    with await _load(file) as loaded:
        claims = verify_logbook_import_token(token)
        if claims is None or claims.user_uuid != str(current_user["uuid"]):
            raise UnprocessableEntityException("This preview has expired. Preview the file again to import it.")
        if claims.sha256 != loaded.digest:
            raise UnprocessableEntityException(
                "This file is not the one that was previewed. Preview it again to import it."
            )

        # Before the write, and outside its transaction: `resolve_species` commits its own
        # rows and rolls back before going outbound, and its worst case is on the order of a
        # minute per unknown species. See `services/logbook_import/species.py`.
        newly_resolved = await resolve_catalog_gaps(db, aphia_ids=unresolved_aphia_ids(loaded.document.species))

        plan = await plan_import(
            db,
            user_id=current_user["id"],
            loaded=loaded,
            resolution_ran=True,
            newly_resolved_aphia_ids=newly_resolved,
            check_in=check_in_details,
            portrait=portrait,
        )
        await write_import(db, user_id=current_user["id"], loaded=loaded, plan=plan)
        await db.commit()

    # After the commit, never before: a cache dropped early can be refilled from the
    # pre-import state by any read that lands in between. An import fills the dive-site and
    # trip collections as well as everything the four helpers below already cover, and those
    # two list caches live inside their own routers - see `services/cache_invalidation.py`.
    # Skipping them serves a restored diver empty pages for up to the 60-second list expiry,
    # at exactly the moment they go looking at what they just restored.
    user_id = current_user["id"]
    await invalidate_dive_caches(user_id)
    await invalidate_certification_caches(user_id)
    await invalidate_course_caches(user_id)
    await invalidate_gear_caches(user_id)
    await invalidate_dive_site_caches(user_id)
    await invalidate_trip_caches(user_id)

    return ImportResult(**_body(plan, loaded))
