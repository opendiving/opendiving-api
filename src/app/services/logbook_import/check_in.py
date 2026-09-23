"""The check-in details a document carries: offered in the preview, written as confirmed.

The importer cannot tell a restore of this account's own export from a buddy's file or a
stranger's UDDF, so no detail is written on the document's say-so. The preview shows, for
each detail the document carries, the account's value beside a proposal; the apply writes
exactly the details submitted beside the token and changed by them, so a client that never
showed the section writes nothing, and a detail the document does not carry is neither
shown nor written.

**An object is proposed whole.** The proposal is the account's contact or insurance when
every member the document's carries equals the account's - so this app's own UDDF, which
has no slot for a policy number, proposes the account's insurance with its number - and
otherwise the document's object alone. Filling a member the document lacks from the
account's object would pair one insurer's name with another's policy number.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from ...schemas.logbook_import import (
    ImportBornOnDetail,
    ImportCheckInDetail,
    ImportCheckInEmergencyContact,
    ImportCheckInInsurance,
    ImportCheckInSubmission,
    ImportDiver,
    ImportEmergencyContact,
    ImportEmergencyContactDetail,
    ImportInsurance,
    ImportInsuranceDetail,
    ImportNoteCode,
    ImportPhoneDetail,
)
from ...schemas.user import EMERGENCY_CONTACT_FIELDS, INSURANCE_FIELDS, is_blank

Note = Callable[[ImportNoteCode, str], None]

# Each object's members, and the account column each is stored in.
_CONTACT_COLUMNS = dict(zip(("name", "phone", "relationship"), EMERGENCY_CONTACT_FIELDS, strict=True))
_INSURANCE_COLUMNS = dict(zip(("provider", "number", "expires_on"), INSURANCE_FIELDS, strict=True))

# What a diver reads each detail as, in a note.
_LABELS = {
    "born_on": "date of birth",
    "phone": "phone number",
    "emergency_contact": "emergency contact",
    "insurance": "dive insurance",
}


def _text(value: Any) -> Any:
    return None if is_blank(value) else value


def _account_object[O: (ImportCheckInEmergencyContact, ImportCheckInInsurance)](
    model: type[O], account: Mapping[str, Any], columns: Mapping[str, str]
) -> O | None:
    """The account's object, member by member from its columns, or `None` where all are unset."""
    members = {member: _text(account.get(column)) for member, column in columns.items()}
    return model(**members) if any(value is not None for value in members.values()) else None


def _whole[O: (ImportCheckInEmergencyContact, ImportCheckInInsurance)](document: O, account: O | None) -> O:
    carried = document.model_dump(exclude_none=True)
    if account is not None and all(getattr(account, member) == value for member, value in carried.items()):
        return account
    return document


def _first_anchored[C: (ImportEmergencyContact, ImportInsurance)](
    objects: list[C], anchor: str, detail: str, note: Note, *, unanchored: str
) -> C | None:
    """The first object that names its anchor, with a note for every other one.

    One of each is what the account stores, so a second is dropped rather than chosen
    between; one with no anchor is not an object the format admits, and is dropped before
    the counting starts rather than taking the first place from a real one.
    """
    anchored = []
    for candidate in objects:
        if is_blank(getattr(candidate, anchor)):
            note(ImportNoteCode.CHECK_IN_DETAIL_DROPPED, unanchored)
        else:
            anchored.append(candidate)
    for extra in anchored[1:]:
        note(
            ImportNoteCode.CHECK_IN_DETAIL_DROPPED,
            f"The {_LABELS[detail]} “{getattr(extra, anchor)}” is not offered: this account holds one, "
            "and the document lists another first.",
        )
    return anchored[0] if anchored else None


def propose(diver: ImportDiver, account: Mapping[str, Any], note: Note) -> list[ImportCheckInDetail]:
    """The preview's section: one entry per detail `diver` carries, in the form's order."""
    details: list[ImportCheckInDetail] = []
    if diver.born_on is not None:
        details.append(ImportBornOnDetail(account=account.get("date_of_birth"), proposed=diver.born_on))
    if diver.phone is not None and not is_blank(diver.phone):
        details.append(ImportPhoneDetail(account=_text(account.get("phone")), proposed=diver.phone))

    contact = _first_anchored(
        diver.emergency_contacts,
        "name",
        "emergency_contact",
        note,
        unanchored="An emergency contact in the document names nobody, so it is not offered.",
    )
    if contact is not None:
        ours = _account_object(ImportCheckInEmergencyContact, account, _CONTACT_COLUMNS)
        theirs = ImportCheckInEmergencyContact(
            name=contact.name, phone=_text(contact.phone), relationship=_text(contact.relationship)
        )
        details.append(ImportEmergencyContactDetail(account=ours, proposed=_whole(theirs, ours)))

    insurance = _first_anchored(
        diver.insurances,
        "provider",
        "insurance",
        note,
        unanchored="A dive insurance in the document names no provider, so it is not offered.",
    )
    if insurance is not None:
        ours_insurance = _account_object(ImportCheckInInsurance, account, _INSURANCE_COLUMNS)
        theirs_insurance = ImportCheckInInsurance(
            provider=insurance.provider, number=_text(insurance.number), expires_on=insurance.expires_on
        )
        details.append(ImportInsuranceDetail(account=ours_insurance, proposed=_whole(theirs_insurance, ours_insurance)))
    return details


def to_write(
    details: Sequence[ImportCheckInDetail],
    submission: ImportCheckInSubmission | None,
    account: Mapping[str, Any],
    note: Note,
) -> dict[str, Any]:
    """The account columns the apply writes, noting each detail it writes.

    A submitted detail the document does not carry is ignored, and so is one that would
    change nothing, so applying the same document twice with the same submission writes
    nothing the second time. An object's columns are written together.
    """
    if submission is None:
        return {}
    carried = {detail.detail for detail in details}
    values: dict[str, Any] = {}
    for detail, columns in submission.columns().items():
        if detail not in carried:
            continue
        if all(_text(account.get(column)) == value for column, value in columns.items()):
            continue
        values.update(columns)
        cleared = all(value is None for value in columns.values())
        note(
            ImportNoteCode.CHECK_IN_DETAIL_WRITTEN,
            f"The {_LABELS[detail]} was cleared from this account, as confirmed in the preview."
            if cleared
            else f"The {_LABELS[detail]} confirmed in the preview was saved to this account.",
        )
    return values
