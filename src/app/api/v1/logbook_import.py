"""Restoring a logbook into the caller's own account, whatever wrote it.

The mirror of `/export/*`, and the half that makes "your data is never more than one curl
away" a round trip rather than an exit. Six things are true of both endpoints, and each is
here rather than in the service layer because each is an HTTP concern:

- **The caller's own account, and nothing else.** No `username` or `user_uuid` parameter:
  the bearer token names the only logbook there is to import into, so there is no
  authorization decision to get wrong. The document's own diver identity and settings are
  read, reported and never applied; its check-in details and its portrait are written only
  as the diver confirms them in the preview, and its tag list is the diver's tags.
- **Any number of files, in any mix of the formats this build reads.** A DiveJSON document,
  the full-export archive, a UDDF file, a Subsurface `.ssrf`, a FIT, a Suunto app export, a
  Suunto DM5 XML export, or a zip of any of those, each sent as a `file` part - a watch
  writes one file per dive, and one file per import would cap a diver at ten dives an hour
  against the rate limit below. Which formats exactly is `divejson.read_formats()` and never
  a list written out here.
- **Two phases, mirroring the parse-then-attach flow.** `POST /import/logbook/preview`
  reads, converts, plans and reports, storing nothing; `POST /import/logbook` re-uploads the
  same files with the preview's token and writes. The token attests which bytes the report
  was about - the shape `create_dive_file_token` already uses - and it is minted over every
  uploaded file's name and digest, not over what they convert to, so it names the files a
  diver picked. They travel twice, which is the trade that flow already made. A server-side
  spool keyed by the token is the recorded escape hatch, not the design.
- **Rate limited, per user, on its own budget, and after the caller is known.** An import
  may carry half a gigabyte, parses whole logbooks and may make outbound WoRMS calls; the
  export endpoints' docstring records why a whole-logbook endpoint is throttled at all, and
  this one is dearer than any export. Both routes read their own body, after the session
  and the limit have been checked, so a request refused for either reads none of it.
- **The error taxonomy is `POST /dive/parse`'s.** 415 is "no reader here claims these
  bytes", 422 is "one did, and it failed", 413 is past a bound of the request or over the
  account's storage limit. A file that cannot be read is a row of the report, not a refusal
  of the import: the whole request answers with one of those only where no file of it
  reads, and then as its first refused file would on its own. Short of that, a file that is
  *readable* never fails: a record this app cannot store is skipped and reported, a value it
  cannot hold is dropped and reported, and what a conversion could not carry comes back on
  `ImportReport.conversion` rather than as a refusal.
- **Atomic in rows.** Apply is one transaction, committed once at the end, so an import
  that fails or is interrupted writes nothing and a retry cannot half-duplicate a logbook.
"""

import copy
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from ...api.dependencies import get_current_user
from ...core.config import settings
from ...core.db.database import async_get_db
from ...core.exceptions.http_exceptions import BadRequestException, UnprocessableEntityException
from ...core.security import create_logbook_import_token, verify_logbook_import_token
from ...core.utils.rate_limit import enforce_rate_limit
from ...schemas.logbook_import import ImportCheckInSubmission, ImportPortraitChoice, ImportPreview, ImportResult
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
    BatchReport,
    ImportPart,
    ImportRequest,
    ImportTooLargeError,
    LoadedBatch,
    MalformedImportError,
    MalformedRequestError,
    UnsupportedImportError,
    batch_digest,
    batch_species,
    formats_this_build_reads,
    import_batch,
    load_import,
    read_import_request,
    resolve_catalog_gaps,
    unresolved_aphia_ids,
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


def _inlined(model: type[BaseModel]) -> dict[str, Any]:
    """A model's JSON schema with its `$defs` written in place, for a document that holds it
    nowhere else: a request body read by hand is not one FastAPI publishes components for."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return resolve(copy.deepcopy(defs[ref.removeprefix("#/$defs/")]))
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(value) for value in node]
        return node

    resolved: dict[str, Any] = resolve(schema)
    return resolved


def _json_field(model: type[BaseModel], description: str) -> dict[str, Any]:
    return {
        "anyOf": [
            {"type": "string", "contentMediaType": "application/json", "contentSchema": _inlined(model)},
            {"type": "null"},
        ],
        "description": description,
    }


_FILES = {
    "type": "array",
    "items": {"type": "string", "contentMediaType": "application/octet-stream"},
    "description": _FILE_DESCRIPTION,
}

_CHECK_IN_DESCRIPTION = (
    "The check-in details to write, as JSON: the preview's proposals as kept or edited. A detail left out is not "
    "written, and `null` clears it."
)
_PORTRAIT_DESCRIPTION = (
    "The choice made for the preview's `portrait`, as JSON: `take` or `keep`, with the `account_sha256` the preview "
    "showed. Left out, the account keeps its portrait."
)


def _body_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {"schema": {"type": "object", "properties": properties, "required": required}}
            },
        }
    }


_PREVIEW_BODY = _body_schema({FILE_FIELD: _FILES}, [FILE_FIELD])
_APPLY_BODY = _body_schema(
    {
        FILE_FIELD: _FILES,
        "token": {"type": "string", "description": "The `token` from these files' preview"},
        "check_in_details": _json_field(ImportCheckInSubmission, _CHECK_IN_DESCRIPTION),
        "portrait": _json_field(ImportPortraitChoice, _PORTRAIT_DESCRIPTION),
    },
    [FILE_FIELD, "token"],
)

_APPLY_FIELDS = ("token", "check_in_details", "portrait")


async def _enforce_import_limit(user_id: int) -> None:
    await enforce_rate_limit(
        f"import:user:{user_id}",
        settings.IMPORT_RATE_LIMIT_PER_USER,
        settings.IMPORT_RATE_LIMIT_WINDOW_SECONDS,
    )


def _missing(field: str) -> RequestValidationError:
    """The 422 FastAPI answers a required form field with, for a body read here."""
    return RequestValidationError([{"type": "missing", "loc": ("body", field), "msg": "Field required", "input": None}])


async def _read(request: Request, fields: tuple[str, ...] = ()) -> ImportRequest:
    """Read the body, translating the part reader's refusals into their status codes."""
    try:
        body = await read_import_request(request, fields=fields)
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


def _submitted[T: BaseModel](body: ImportRequest, field: str, model: type[T]) -> T | None:
    """A JSON form field as `model`, validated as FastAPI validates a `Json[...] | None` form
    field, or `None` where the request leaves it out or sends `null`."""
    raw = body.fields.get(field)
    if raw is None or raw.strip() == "null":
        return None
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise RequestValidationError(
            [{**error, "loc": ("body", field, *error["loc"])} for error in exc.errors(include_url=False)]
        ) from exc


def _report(report: BatchReport) -> dict[str, object]:
    return {
        "collections": report.collections,
        "files": report.files,
        "notes": report.notes,
        "notes_truncated": report.notes_truncated,
        "conversion": report.conversion,
        "members": report.members,
        "dives": report.dives,
    }


@router.post("/import/logbook/preview", response_model=ImportPreview, openapi_extra=_PREVIEW_BODY)
async def preview_logbook_import(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> ImportPreview:
    """Read any number of files and report what importing them would do. Stores nothing.

    Each file is a `file` part, and the part's description lists what one may be. The files
    are classified by their bytes, never by their names; a zip that is not a full-export
    archive is opened, and its files join the rest. At most one full-export archive is
    imported at a time.

    **An import of several files writes what its files would write imported one at a time,
    in the order it reads them**: the archive, then DiveJSON documents, then logbooks, then
    dive-computer files, each by name. So a dive computer's two exports of one dive are one
    dive with both files, whether they arrive together or on two days, and two computers'
    records of one dive are one dive with two recordings. A file that is one dive - one
    computer's record of it, whatever its format - is kept on the dive it becomes, as the dive
    form keeps a file it is handed, and a dive-computer file's dive takes its identifier from
    its bytes: the same whenever the same file comes again, under any name.

    Every record lands in one of four buckets, per collection, summed over the files: **created**,
    **linked** (an existing record of yours already carries that identifier, or that name),
    **restored** (a record you deleted here, coming back under its original identifier) or
    **skipped**. `notes` explains every decision that is not a plain create, one sentence at a
    time, and `files` says how many files and card images the documents name and how many of
    them the import actually carries - only the full-export archive carries any.

    `members` has one row per file - each part and each file a zip held - saying what it was
    read as, whether it is kept and why not, or why it was refused. A file that cannot be read
    stops nothing else. `dives` has one row per dive the import creates or touches, with the
    most that happens to it and the files it came from.

    `conversion` is present when any file was not DiveJSON already, and says what the
    conversion could not carry: findings grouped by kind and message, each with up to three
    paths into your original files. Treat a `kind` you do not recognise as a plain finding.

    `check_in_details` has one entry for each check-in detail the logbook carries - date of
    birth, phone, emergency contact, dive insurance - with your account's value beside the
    proposal. An emergency contact or an insurance is proposed whole: your own where every
    part the logbook gives matches it, otherwise the logbook's alone.

    `portrait` is the archive's portrait beside your account's, when the archive carries one
    it can offer: yours as the digest `GET /user/portrait` answers to, the archive's as an
    inline image framed as it would be stored. It is absent when the archive's is your own,
    framed the same, and a portrait that cannot be offered is explained in `notes`.

    A person this app exported linked to an account here is proposed linked to it again,
    within the limit on linking people - an `account_linked` note names the account's
    current username - where the account still exists, is not yours and no other person of
    yours links it. Nothing of a link that cannot be made is kept.

    The `token` in the response goes to `POST /import/logbook` with the same files under the
    same names. It says which bytes this report describes and nothing more: the import
    re-reads, re-converts and re-plans, because your logbook may have moved between the two
    calls.

    A 413 when the files together are past what one import may carry or plan - its `detail`
    says which - and one naming the storage used and the limit when the files the import
    would store do not fit in what your account has left, measured as they would be stored and
    with your own portrait kept. When no file can be read at all, the answer is what the first
    of them would get on its own.
    """
    await _enforce_import_limit(current_user["id"])
    with await _read(request) as body, await _load(body.parts) as batch:
        report = await import_batch(db, user_id=current_user["id"], batch=batch, apply=False)
        return ImportPreview(
            format=report.first.format,
            version=report.first.version,
            generator=report.first.generator,
            archive=report.archive,
            token=create_logbook_import_token(user_uuid=current_user["uuid"], sha256=batch_digest(body.parts)),
            check_in_details=report.check_in_details,
            portrait=report.portrait,
            **_report(report),
        )


@router.post("/import/logbook", response_model=ImportResult, openapi_extra=_APPLY_BODY)
async def apply_logbook_import(
    request: Request,
    current_user: Annotated[dict, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(async_get_db)],
) -> ImportResult:
    """Import files into your account, after previewing them.

    Answers with the same report the preview did, describing what was actually written. The
    counts can differ from the preview's where your logbook moved in between - a dive
    deleted since is restored rather than linked - which is why the plan is made afresh here
    rather than replayed. A converted file is converted again here and reaches the same
    document: nothing in the conversion depends on when it runs. The rows of `dives` carry
    the identifiers of the dives written.

    **All or nothing in rows.** A failure at any point writes no records at all, so a retry
    after a timeout can never half-duplicate a logbook. Two things sit outside that on
    purpose: the files an import stores are written before the transaction that names them
    commits, so a failure can strand an unreferenced file (harmless, and swept), and species
    this instance's catalog did not hold are looked up in the World Register of Marine
    Species before the transaction opens and stay whether the import completes or not.

    The document's diver identity and settings are never applied: this account keeps its own
    name, email, units and notification settings. Its check-in details are written as sent in
    `check_in_details` and only then - a detail not sent, or one the logbook does not carry,
    stays as it is. What is sent meets the bounds `PATCH /user` does, and an emergency
    contact without a name or an insurance without a provider is a 422.

    The archive's portrait replaces your account's only when `portrait` says `take`, and
    only while your portrait is still the one the preview showed; otherwise yours is kept,
    and a note says so when you had chosen the archive's.

    Each link to an account the import makes counts against the limit on linking people,
    and one past it is dropped with a note rather than failing the import. A link counts
    whether or not the import completes.

    Files that would take your account past its storage limit refuse the import whole, with
    the preview's 413 - which here also counts the archive's portrait if `portrait` takes it.
    A set of files that differs from the preview's, by bytes or by name, is a 422.
    """
    await _enforce_import_limit(current_user["id"])
    with await _read(request, _APPLY_FIELDS) as body:
        token = body.fields.get("token")
        if token is None:
            raise _missing("token")
        check_in = _submitted(body, "check_in_details", ImportCheckInSubmission)
        portrait = _submitted(body, "portrait", ImportPortraitChoice)
        if check_in is not None and (anchor_errors := check_in.anchor_errors()):
            raise RequestValidationError(
                [{**error, "loc": ("body", "check_in_details", *error["loc"])} for error in anchor_errors]
            )

        claims = verify_logbook_import_token(token)
        if claims is None or claims.user_uuid != str(current_user["uuid"]):
            raise UnprocessableEntityException("This preview has expired. Preview the files again to import them.")
        if claims.sha256 != batch_digest(body.parts):
            raise UnprocessableEntityException(
                "These files are not the ones that were previewed. Preview them again to import them."
            )

        with await _load(body.parts) as batch:
            # Before the write, and outside its transaction: `resolve_species` commits its own
            # rows and rolls back before going outbound, and its worst case is on the order of
            # a minute per unknown species. See `services/logbook_import/species.py`.
            newly_resolved = await resolve_catalog_gaps(db, aphia_ids=unresolved_aphia_ids(batch_species(batch)))
            report = await import_batch(
                db,
                user_id=current_user["id"],
                batch=batch,
                apply=True,
                newly_resolved_aphia_ids=newly_resolved,
                check_in=check_in,
                portrait=portrait,
            )
            await db.commit()

    # After the commit, never before: a cache dropped early can be refilled from the
    # pre-import state by any read that lands in between. An import fills the dive-site and
    # trip collections as well as everything the helpers below already cover, and those
    # two list caches live inside their own routers - see `services/cache_invalidation.py`.
    # Skipping them serves a restored diver empty pages for up to the 60-second list expiry,
    # at exactly the moment they go looking at what they just restored.
    user_id = current_user["id"]
    await invalidate_dive_caches(user_id)
    await invalidate_certification_caches(user_id)
    await invalidate_contact_caches(user_id)
    await invalidate_course_caches(user_id)
    await invalidate_gear_caches(user_id)
    await invalidate_dive_site_caches(user_id)
    await invalidate_trip_caches(user_id)

    return ImportResult(**_report(report))
