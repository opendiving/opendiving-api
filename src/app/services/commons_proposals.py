"""A diver proposing to OpenDiving Commons, and a reviewer deciding.

A diver's own site joins the Commons by a change of owner on the same row, so its uuid, the
dives naming it and the proposer's saved row are untouched. A change to a record leaves the
record alone until a reviewer accepts it.

**Acceptance writes the body it was sent, never the row.** The diver may go on editing their
site while the proposal waits, and the reviewer approved what was on their screen.

The repository only copies what this instance holds, so an accepted record is held to the
repository's record rules here, and a body breaking one is refused by naming it. Normalising on
the way to the repository instead would leave the copy different from its source.
"""

import uuid as uuid_pkg
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast

from divejson import SPEC_VERSION
from divejson.validate import validate_document
from fastapi import HTTPException
from fastcrud import compute_offset
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..core.config import settings
from ..core.exceptions.http_exceptions import NotFoundException, UnprocessableEntityException
from ..core.utils.rate_limit import enforce_rate_limit
from ..crud.crud_dive_site_proposals import get_waiting_proposals_page, write_waiting_proposal
from ..crud.crud_dive_sites import commons_records_carrying, in_the_commons, replace_site_with_record
from ..models.dive_site import DiveSite
from ..models.dive_site_proposal import DiveSiteProposal
from ..models.saved_site import SavedSite
from ..models.user import User
from ..schemas.dive_site import (
    DEPTH_RANGE_MESSAGE,
    CommonsMatchRule,
    DiveSiteMembers,
    DiveSiteMembersRead,
    DiveSiteProposalKind,
    DiveSiteProposalStatus,
)
from ..schemas.dive_site_proposal import (
    ATTESTATIONS,
    AdminDiveSiteProposalRead,
    DiveSiteProposalAccept,
    DiveSiteProposalCreate,
    DiveSiteProposalRead,
    DiveSiteProposalSite,
    DiveSiteProposer,
)
from ..schemas.location import DIVE_SITE_LOCATION_PREFIX, LOCATION_FIELDS
from .cache_invalidation import (
    invalidate_commons_record_caches,
    invalidate_dive_caches,
    invalidate_dive_site_caches,
    invalidate_trip_caches,
)
from .commons_matches import find_commons_matches
from .commons_records import COMMONS_REGISTRY, SHARED_MEMBER_COLUMNS, feature_of

CONTRIBUTION_SOURCE = "contribution"
CONTRIBUTION_LICENSE = "CC0-1.0"

_NOT_FOUND = "Proposal not found"
_PIN_DECIMALS = 6
_PIN_REQUIRED = "a Commons record keeps its pin"

# Every locality column but the name: a record's published form carries the name alone, so a
# site that had a geocoded centre or box gives them up on becoming one.
_NO_GEOCODED_LOCALITY = {f"{DIVE_SITE_LOCATION_PREFIX}{field}": None for field in LOCATION_FIELDS if field != "name"}

_SITE_COLUMNS = (
    DiveSite.id,
    DiveSite.uuid,
    DiveSite.user_id,
    DiveSite.commons_source,
    DiveSite.commons_license,
    DiveSite.commons_seed_registry,
    DiveSite.commons_seed_identifier,
    DiveSite.commons_retrieved,
    *(getattr(DiveSite, column) for column in SHARED_MEMBER_COLUMNS),
)


def _shared_members(row: Mapping[str, Any], *, prefix: str = "") -> dict[str, Any]:
    return {column: row[f"{prefix}{column}"] for column in SHARED_MEMBER_COLUMNS}


def _seed_entry(site: Mapping[str, Any]) -> dict[str, str] | None:
    """The registry entry a seed's file in the repository is placed by."""
    if site["commons_seed_registry"] is None:
        return None
    return {"registry": site["commons_seed_registry"], "identifier": site["commons_seed_identifier"]}


def _seed_entry_refusal(site: Mapping[str, Any], external_ids: list[dict[str, str]]) -> str | None:
    seed_entry = _seed_entry(site)
    if seed_entry is None or seed_entry in external_ids:
        return None
    return f"a seed keeps the entry it was seeded from, {seed_entry['registry']} {seed_entry['identifier']}"


async def _site(db: AsyncSession, site_id: int, *, for_update: bool = False) -> dict[str, Any]:
    statement = select(*_SITE_COLUMNS).where(DiveSite.id == site_id)
    if for_update:
        statement = statement.with_for_update()
    return dict((await db.execute(statement)).mappings().one())


def _proposal_read(row: Mapping[str, Any], site: Mapping[str, Any]) -> DiveSiteProposalRead:
    return DiveSiteProposalRead(
        uuid=row["uuid"],
        kind=row["kind"],
        status=row["status"],
        reason=row["reason"],
        site=DiveSiteProposalSite(uuid=site["uuid"], name=site["name"]),
        created_at=row["created_at"],
        decided_at=row["decided_at"],
    )


async def _members_of_a_new_site(db: AsyncSession, site: Mapping[str, Any]) -> dict[str, Any]:
    members = _shared_members(site)
    if members["latitude"] is None or members["longitude"] is None:
        raise UnprocessableEntityException("A site needs a pin to be proposed for the Commons")
    # A site sharing an entry with a record is a copy of it. Replacing it, or proposing a change
    # to the record, is what the diver means.
    matches = await find_commons_matches(db, members)
    if any(match.rule is CommonsMatchRule.IDENTITY for match in matches):
        raise HTTPException(
            status_code=409,
            detail="This site shares a registry entry with a Commons site: replace it, or propose a change to that one",
        )
    return members


def _members_of_a_change(site: Mapping[str, Any], values: DiveSiteProposalCreate) -> dict[str, Any]:
    if values.members is None:
        raise UnprocessableEntityException("A change to a Commons site names the members it wants different")
    current = _shared_members(site)
    sent = values.members.model_dump(mode="json", exclude_unset=True)
    changed = {member: value for member, value in sent.items() if value != current[member]}
    if not changed:
        raise UnprocessableEntityException("This changes nothing on the Commons site")

    after = {**current, **changed}
    if after["latitude"] is None or after["longitude"] is None:
        raise UnprocessableEntityException(_PIN_REQUIRED)
    if refusal := _seed_entry_refusal(site, after["external_ids"]):
        raise UnprocessableEntityException(refusal)
    if after["depth_from"] is not None and after["depth_to"] is not None and after["depth_from"] > after["depth_to"]:
        raise UnprocessableEntityException(DEPTH_RANGE_MESSAGE)
    return changed


async def propose(
    db: AsyncSession, *, site_id: int, user_id: int, values: DiveSiteProposalCreate
) -> DiveSiteProposalRead:
    """Propose the diver's own site, or a change to a Commons record, which the caller has
    established they may read. Proposing again on the site replaces what waits."""
    site = await _site(db, site_id)
    kind = DiveSiteProposalKind.CHANGE if site["user_id"] is None else DiveSiteProposalKind.NEW_SITE
    if values.attestation != ATTESTATIONS[kind]:
        raise UnprocessableEntityException(f"The attestation for this proposal reads: {ATTESTATIONS[kind]}")

    if kind is DiveSiteProposalKind.CHANGE:
        members = _members_of_a_change(site, values)
    else:
        if values.members is not None:
            raise UnprocessableEntityException("Your own site is proposed as it stands: edit it, then propose it")
        members = await _members_of_a_new_site(db, site)

    await enforce_rate_limit(
        f"commons-proposal:user:{user_id}",
        settings.COMMONS_PROPOSAL_RATE_LIMIT_PER_USER,
        settings.COMMONS_PROPOSAL_RATE_LIMIT_WINDOW_SECONDS,
    )
    uuid, created_at = await write_waiting_proposal(
        db,
        user_id=user_id,
        dive_site_id=site_id,
        kind=kind,
        members=members,
        attestation=ATTESTATIONS[kind],
        message=values.message,
        now=datetime.now(UTC),
    )
    await db.commit()
    await invalidate_dive_site_caches(user_id)
    return _proposal_read(
        {
            "uuid": uuid,
            "kind": kind.value,
            "status": DiveSiteProposalStatus.WAITING.value,
            "reason": None,
            "created_at": created_at,
            "decided_at": None,
        },
        site,
    )


async def locked_proposal(
    db: AsyncSession, *, uuid: uuid_pkg.UUID, user_id: int | None = None
) -> DiveSiteProposal | None:
    """A proposal to decide, locked so two decisions cannot both find it waiting: one of this
    diver's, or for a reviewer any account's.

    Not one whose account is being deleted: that diver has gone dark and cannot withdraw, and
    accepting would put their site beyond the reach of the purge. Restoring the account brings
    it back.
    """
    statement = (
        select(DiveSiteProposal)
        .join(User, User.id == DiveSiteProposal.user_id)
        .where(DiveSiteProposal.uuid == uuid, User.is_deleted.is_(False))
        .with_for_update(of=DiveSiteProposal)
    )
    if user_id is not None:
        statement = statement.where(DiveSiteProposal.user_id == user_id)
    proposal: DiveSiteProposal | None = await db.scalar(statement)
    return proposal


async def _waiting_proposal(db: AsyncSession, *, uuid: uuid_pkg.UUID, user_id: int | None = None) -> DiveSiteProposal:
    proposal = await locked_proposal(db, uuid=uuid, user_id=user_id)
    if proposal is None:
        raise NotFoundException(_NOT_FOUND)
    if proposal.status != DiveSiteProposalStatus.WAITING.value:
        raise HTTPException(status_code=409, detail="This proposal has already been decided or withdrawn")
    return proposal


async def withdraw(db: AsyncSession, *, uuid: uuid_pkg.UUID, user_id: int) -> None:
    proposal = await _waiting_proposal(db, uuid=uuid, user_id=user_id)
    await _decide(db, proposal, DiveSiteProposalStatus.WITHDRAWN)
    await db.commit()
    await invalidate_dive_site_caches(user_id)


async def decline(db: AsyncSession, *, uuid: uuid_pkg.UUID, reason: str) -> DiveSiteProposalRead:
    proposal = await _waiting_proposal(db, uuid=uuid)
    proposer_id = proposal.user_id
    await _decide(db, proposal, DiveSiteProposalStatus.DECLINED, reason=reason)
    read = await _read_decided(db, proposal)
    await db.commit()
    # The proposer's, not the reviewer's: their read of the site carries the reason, for an hour.
    await invalidate_dive_site_caches(proposer_id)
    return read


async def _decide(
    db: AsyncSession,
    proposal: DiveSiteProposal,
    status: DiveSiteProposalStatus,
    *,
    reason: str | None = None,
    dive_site_id: int | None = None,
) -> None:
    now = datetime.now(UTC)
    proposal.status = status.value
    proposal.reason = reason
    proposal.decided_at = now
    proposal.updated_at = now
    if dive_site_id is not None:
        proposal.dive_site_id = dive_site_id
    await db.flush()


async def _read_decided(db: AsyncSession, proposal: DiveSiteProposal) -> DiveSiteProposalRead:
    site = await _site(db, proposal.dive_site_id)
    return _proposal_read(
        {
            "uuid": proposal.uuid,
            "kind": proposal.kind,
            "status": proposal.status,
            "reason": proposal.reason,
            "created_at": proposal.created_at,
            "decided_at": proposal.decided_at,
        },
        site,
    )


def _as_columns(members: DiveSiteMembers) -> dict[str, Any]:
    """The body as what is written, the pin rounded to the decimals the record rules allow."""
    columns = members.model_dump(mode="json")
    if columns["latitude"] is not None and columns["longitude"] is not None:
        columns["latitude"] = round(columns["latitude"], _PIN_DECIMALS)
        columns["longitude"] = round(columns["longitude"], _PIN_DECIMALS)
    return columns


def _names_a_record(entry: Mapping[str, str]) -> uuid_pkg.UUID | None:
    if entry["registry"] != COMMONS_REGISTRY:
        return None
    try:
        return uuid_pkg.UUID(entry["identifier"])
    except ValueError:
        return None


async def _record_rule_refusals(
    db: AsyncSession, columns: Mapping[str, Any], *, site: Mapping[str, Any], provenance: Mapping[str, Any]
) -> list[str]:
    """The repository's record rules, as they bear on what a review writes. Its rules on a
    record's shape, members and provenance hold by construction: `feature_of` is what publishes
    it, and the provenance is either the seed's own or the one a contribution always has."""
    refusals = []
    latitude, longitude = columns["latitude"], columns["longitude"]
    if latitude is None or longitude is None:
        return [_PIN_REQUIRED]
    if latitude == 0 and longitude == 0:
        refusals.append("the pin is 0, 0, which is a missing position and not a dive site")
    if refusal := _seed_entry_refusal(site, columns["external_ids"]):
        refusals.append(refusal)

    entries = {(entry["registry"], entry["identifier"]) for entry in columns["external_ids"]}
    if entries:
        carriers = await db.execute(
            select(DiveSite.name, DiveSite.external_ids).where(
                DiveSite.id.in_(commons_records_carrying(entries)), DiveSite.id != site["id"]
            )
        )
        for carrier in carriers:
            shared = entries & {(entry["registry"], entry["identifier"]) for entry in carrier.external_ids}
            for registry, identifier in sorted(shared):
                refusals.append(f"the entry {registry} {identifier} is also carried by {carrier.name}")

    named = {uuid for uuid in map(_names_a_record, columns["external_ids"]) if uuid is not None}
    if site["uuid"] in named:
        refusals.append(f"an {COMMONS_REGISTRY} entry names this record's own uuid")
    named.discard(site["uuid"])
    if named:
        existing = await db.scalars(select(DiveSite.name).where(DiveSite.uuid.in_(named), in_the_commons()))
        refusals.extend(f"an {COMMONS_REGISTRY} entry names {name}, which still exists" for name in existing)

    if not refusals:
        refusals.extend(_divejson_refusals({**columns, "uuid": site["uuid"], **provenance}))
    return refusals


def _divejson_refusals(row: Mapping[str, Any]) -> list[str]:
    """The DiveJSON half of the record rules: a record is a valid dive site once its geometry is
    folded back into `position`, and the format's own validator is what says so."""
    feature = feature_of(row)
    longitude, latitude = feature["geometry"]["coordinates"]
    site = {**feature["properties"], "position": {"latitude": latitude, "longitude": longitude}}
    document = {"format": "divejson", "version": SPEC_VERSION, "exported_at": "2026-01-01T00:00:00Z", "sites": [site]}
    return [f"DiveJSON: {issue}" for issue in validate_document(document)]


def _provenance_of(site: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "commons_source": site["commons_source"],
        "commons_license": site["commons_license"],
        "commons_seed_registry": site["commons_seed_registry"],
        "commons_seed_identifier": site["commons_seed_identifier"],
        "commons_retrieved": site["commons_retrieved"],
    }


_CONTRIBUTION_PROVENANCE = {
    "commons_source": CONTRIBUTION_SOURCE,
    "commons_license": CONTRIBUTION_LICENSE,
    "commons_seed_registry": None,
    "commons_seed_identifier": None,
    "commons_retrieved": None,
}


async def _write_reviewed(
    db: AsyncSession, members: DiveSiteMembers, *, site: Mapping[str, Any], provenance: Mapping[str, Any], **also: Any
) -> None:
    """Write what the reviewer sent onto the row, or refuse it by naming the rule it breaks."""
    columns = _as_columns(members)
    refusals = await _record_rule_refusals(db, columns, site=site, provenance=provenance)
    if refusals:
        raise UnprocessableEntityException("; ".join(refusals))
    now = datetime.now(UTC)
    await db.execute(
        update(DiveSite)
        .where(DiveSite.id == site["id"])
        .values(**columns, **_NO_GEOCODED_LOCALITY, commons_reviewed_at=now, updated_at=now, **also)
    )


async def _accept_into(
    db: AsyncSession, proposal: DiveSiteProposal, values: DiveSiteProposalAccept
) -> tuple[uuid_pkg.UUID, int]:
    """Fold the proposer's site into a record it duplicates: the row goes, the proposal does not."""
    if proposal.kind != DiveSiteProposalKind.NEW_SITE.value:
        raise UnprocessableEntityException("Only a new site can be accepted into a Commons site")
    record_id = await db.scalar(
        select(DiveSite.id).where(DiveSite.uuid == values.into, in_the_commons()).with_for_update()
    )
    if record_id is None:
        raise UnprocessableEntityException("That Commons site does not exist")
    record = await _site(db, record_id)
    if values.members is not None:
        await _write_reviewed(db, values.members, site=record, provenance=_provenance_of(record))

    site_id = proposal.dive_site_id
    # First: the proposal cascades with the row it points at.
    await _decide(db, proposal, DiveSiteProposalStatus.ACCEPTED, dive_site_id=record_id)
    await replace_site_with_record(db, user_id=proposal.user_id, site_id=site_id, record_id=record_id)
    return record["uuid"], record_id


async def _accept_as_written(
    db: AsyncSession, proposal: DiveSiteProposal, members: DiveSiteMembers
) -> tuple[uuid_pkg.UUID, int]:
    site = await _site(db, proposal.dive_site_id, for_update=True)
    if proposal.kind == DiveSiteProposalKind.CHANGE.value:
        await _write_reviewed(db, members, site=site, provenance=_provenance_of(site))
    else:
        await _write_reviewed(
            db,
            members,
            site=site,
            provenance=_CONTRIBUTION_PROVENANCE,
            user_id=None,
            commons_source=CONTRIBUTION_SOURCE,
            commons_license=CONTRIBUTION_LICENSE,
            created_at=datetime.now(UTC),
        )
    await _decide(db, proposal, DiveSiteProposalStatus.ACCEPTED)
    return site["uuid"], site["id"]


async def accept(db: AsyncSession, *, uuid: uuid_pkg.UUID, values: DiveSiteProposalAccept) -> DiveSiteProposalRead:
    proposal = await _waiting_proposal(db, uuid=uuid)
    proposer_id = proposal.user_id
    folding = values.into is not None
    if folding:
        record_uuid, record_id = await _accept_into(db, proposal, values)
    else:
        # The schema requires one or the other.
        record_uuid, record_id = await _accept_as_written(db, proposal, cast(DiveSiteMembers, values.members))
    read = await _read_decided(db, proposal)
    saver_ids = set(await db.scalars(select(SavedSite.user_id).where(SavedSite.dive_site_id == record_id)))
    await db.commit()

    await invalidate_commons_record_caches(record_uuid, saver_ids=saver_ids)
    if folding:
        await invalidate_dive_site_caches(proposer_id)
        await invalidate_dive_caches(proposer_id)
        await invalidate_trip_caches(proposer_id)
    return read


async def read_queue(db: AsyncSession, *, page: int, items_per_page: int) -> dict[str, Any]:
    """One page of the queue, each row with the records the proposed site may be."""
    queue = await get_waiting_proposals_page(db, offset=compute_offset(page, items_per_page), limit=items_per_page)
    rows = []
    for row in queue["data"]:
        current = _shared_members(row, prefix="site_")
        proposed = {**current, **row["members"]}
        rows.append(
            AdminDiveSiteProposalRead(
                uuid=row["uuid"],
                kind=row["kind"],
                message=row["message"],
                created_at=row["created_at"],
                proposer=DiveSiteProposer(
                    name=row["proposer_name"],
                    accepted_count=row["accepted_count"],
                    declined_count=row["declined_count"],
                ),
                site_uuid=row["site_uuid"],
                current=DiveSiteMembersRead(**current),
                proposed=DiveSiteMembersRead(**proposed),
                changed=list(row["members"]),
                matches=await find_commons_matches(db, proposed, excluding_id=row["dive_site_id"]),
            ).model_dump()
        )
    return {"data": rows, "total_count": queue["total_count"]}
