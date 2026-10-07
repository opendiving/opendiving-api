"""The check-in details a document carries: offered in the preview, written as confirmed.

The importer cannot tell a restore of this account's own export from a buddy's file or a
stranger's UDDF, so no detail is written on the document's say-so. The preview shows, for
each member of the check-in details the document carries, the account's value beside a
proposal; the apply writes exactly the members submitted beside the token and changed by
them, so a client that never showed the section writes nothing, and a member the document
does not carry is neither shown nor written.

**A list is proposed as a list, row by row against the account's rows.** For each document
row, in document order, the first account row not already matched whose members equal every
member the document row carries stands in for it - so this app's own UDDF, which has no slot
for a policy number, proposes the account's policies with their numbers - and otherwise the
document row stands as it is. A row is never filled member by member from an account row,
which would pair one insurer's name with another's policy number.

**The email is never the sign-in address.** Every export made before the check-in email
existed carries the sign-in address as the diver's `email`, so a document address equal to
this account's, in any case, is dropped with a note - unless it already is the account's
check-in email, in which case it is proposed as the account's and changes nothing.
"""

from collections.abc import Callable, Sequence
from typing import Any

from pydantic import EmailStr, TypeAdapter, ValidationError

from ...schemas.checkin_details import (
    MAX_EMERGENCY_CONTACTS,
    MAX_INSURANCE_POLICIES,
    CheckinDetailsRead,
    EmergencyContact,
    InsurancePolicy,
    is_blank,
)
from ...schemas.logbook_import import (
    ImportCheckInDetail,
    ImportCheckInSubmission,
    ImportDateOfBirthDetail,
    ImportDiver,
    ImportEmailDetail,
    ImportEmergencyContact,
    ImportEmergencyContactsDetail,
    ImportInsurance,
    ImportInsurancePoliciesDetail,
    ImportNoteCode,
    ImportPhoneDetail,
)

Note = Callable[[ImportNoteCode, str], None]

_EMAIL = TypeAdapter(EmailStr)

# What a diver reads each member as, in a note.
_LABELS = {
    "email": "email address",
    "phone": "phone number",
    "date_of_birth": "date of birth",
    "emergency_contacts": "emergency contacts",
    "insurance_policies": "insurance policies",
}


def _text(value: str | None) -> str | None:
    return None if value is None or is_blank(value) else value.strip()


def _email(document: str | None, account: str | None, sign_in: str, note: Note) -> ImportEmailDetail | None:
    if document is None or is_blank(document):
        return None
    try:
        address = _EMAIL.validate_python(document.strip())
    except ValidationError:
        note(
            ImportNoteCode.CHECK_IN_DETAIL_DROPPED,
            "The email address in the document is not one this app can store, so it is not offered.",
        )
        return None
    if account is not None and account.lower() == address.lower():
        return ImportEmailDetail(account=account, proposed=account)
    if address.lower() == sign_in.lower():
        note(
            ImportNoteCode.CHECK_IN_DETAIL_DROPPED,
            "The email address in the document is the one this account signs in with, so it is not offered: "
            "the check-in email is the address you give out.",
        )
        return None
    return ImportEmailDetail(account=account, proposed=address)


def _anchored[R: (ImportEmergencyContact, ImportInsurance)](
    rows: Sequence[R], anchor: str, cap: int, label: str, note: Note, *, unanchored: str
) -> list[R]:
    """The rows that name their anchor, up to the list's cap, with a note for every other.

    One with no anchor is not a row the format admits, and is dropped before the counting
    starts rather than taking a place from a real one.
    """
    kept: list[R] = []
    for row in rows:
        if is_blank(getattr(row, anchor)):
            note(ImportNoteCode.CHECK_IN_DETAIL_DROPPED, unanchored)
        elif len(kept) == cap:
            note(
                ImportNoteCode.CHECK_IN_DETAIL_DROPPED,
                f"“{getattr(row, anchor).strip()}” is not offered: this app keeps at most {cap} {label}.",
            )
        else:
            kept.append(row)
    return kept


def _matched[R: (EmergencyContact, InsurancePolicy)](document: list[R], account: list[R]) -> list[R]:
    """Each document row, or the first unmatched account row whose members equal every member
    the document row carries."""
    unmatched = list(account)
    proposed = []
    for row in document:
        carried = row.model_dump(exclude_none=True)
        match = next(
            (ours for ours in unmatched if all(getattr(ours, member) == value for member, value in carried.items())),
            None,
        )
        if match is not None:
            unmatched.remove(match)
        proposed.append(row if match is None else match)
    return proposed


def propose(diver: ImportDiver, account: CheckinDetailsRead, sign_in: str, note: Note) -> list[ImportCheckInDetail]:
    """The preview's section: one entry per member `diver` carries, in the object's order.

    `sign_in` is the account's sign-in address, which is never proposed as its check-in email.
    """
    details: list[ImportCheckInDetail] = []
    email = _email(diver.email, account.email, sign_in, note)
    if email is not None:
        details.append(email)
    if diver.phone is not None and not is_blank(diver.phone):
        details.append(ImportPhoneDetail(account=account.phone, proposed=diver.phone.strip()))
    if diver.born_on is not None:
        details.append(ImportDateOfBirthDetail(account=account.date_of_birth, proposed=diver.born_on))

    contacts = _anchored(
        diver.emergency_contacts,
        "name",
        MAX_EMERGENCY_CONTACTS,
        _LABELS["emergency_contacts"],
        note,
        unanchored="An emergency contact in the document names nobody, so it is not offered.",
    )
    if contacts:
        theirs = [
            EmergencyContact(name=row.name.strip(), phone=_text(row.phone), relationship=_text(row.relationship))
            for row in contacts
            if row.name is not None
        ]
        details.append(
            ImportEmergencyContactsDetail(
                account=account.emergency_contacts, proposed=_matched(theirs, account.emergency_contacts)
            )
        )

    policies = _anchored(
        diver.insurances,
        "provider",
        MAX_INSURANCE_POLICIES,
        _LABELS["insurance_policies"],
        note,
        unanchored="An insurance policy in the document names no provider, so it is not offered.",
    )
    if policies:
        theirs_policies = [
            InsurancePolicy(provider=row.provider.strip(), number=_text(row.number), expires_on=row.expires_on)
            for row in policies
            if row.provider is not None
        ]
        details.append(
            ImportInsurancePoliciesDetail(
                account=account.insurance_policies, proposed=_matched(theirs_policies, account.insurance_policies)
            )
        )
    return details


def to_write(
    details: Sequence[ImportCheckInDetail],
    submission: ImportCheckInSubmission | None,
    account: CheckinDetailsRead,
    note: Note,
) -> dict[str, Any]:
    """The members the apply writes, as `PATCH /user/checkin-details` takes them, noting each.

    A submitted member the document does not carry is ignored, and so is one that would
    change nothing, so applying the same document twice with the same submission writes
    nothing the second time.
    """
    if submission is None:
        return {}
    carried = {detail.detail for detail in details}
    current = account.model_dump()
    values: dict[str, Any] = {}
    # Whole rows: `exclude_unset` would drop a row's unsent members, and the account's carry them as null.
    for member, value in submission.model_dump(include=submission.model_fields_set).items():
        if member not in carried or value == current[member]:
            continue
        values[member] = value
        label, verb = _LABELS[member], "were" if isinstance(value, list) else "was"
        note(
            ImportNoteCode.CHECK_IN_DETAIL_WRITTEN,
            f"The {label} {verb} cleared from this account, as confirmed in the preview."
            if value is None or value == []
            else f"The {label} confirmed in the preview {verb} saved to this account.",
        )
    return values
