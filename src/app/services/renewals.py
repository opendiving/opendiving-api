"""When a certification or the dive insurance is worth a reminder email, and what it says.

Pure, like the top half of `services.gear_service`: no session and no clock of its own, so
the rules are tested without a database (`tests/test_renewals.py`) and
`core.worker.functions.send_renewal_reminders` only supplies rows and today's date.

`expiry_stage` is the twin of `certificationExpiryStatus` in the web app's
`src/lib/certification.ts`, which badges the dashboard's Renewals card, and the constant
below is named identically on both sides so one `grep CERTIFICATION_EXPIRING_SOON` finds the
pair - the convention `SERVICE_DUE_SOON_DAYS` set. Change one and change the other: a card
and an email that disagree about which rows are running out are worse than either alone.
"""

from datetime import date
from enum import StrEnum

from ..schemas.certification import CertificationAgency

# How far ahead a certification or a policy counts as "expiring soon". Longer than gear's 30
# days because renewing a rescue or first-aid card means booking a course with an
# instructor, not dropping a regulator off at a shop.
CERTIFICATION_EXPIRING_SOON_DAYS = 90


class ExpiryStage(StrEnum):
    """The web's `CertificationExpiryStatus`, value for value, and what the notify columns
    store."""

    EXPIRING_SOON = "expiring_soon"
    EXPIRED = "expired"


def expiry_stage(expires_on: date | None, today: date) -> ExpiryStage | None:
    """`None` for no date and for a date still outside the window.

    A card expiring today is still `expiring_soon`: it is valid through that day, which is
    also where the web draws the line.
    """
    if expires_on is None:
        return None
    days_left = (expires_on - today).days
    if days_left < 0:
        return ExpiryStage.EXPIRED
    if days_left <= CERTIFICATION_EXPIRING_SOON_DAYS:
        return ExpiryStage.EXPIRING_SOON
    return None


def should_remind(
    *, stage: ExpiryStage | None, expires_on: date | None, notified_stage: str | None, notified_for: date | None
) -> bool:
    """Whether this run's email includes the subject: once per (stage, date), never daily.

    `services.gear_service.should_notify` minus its two gear-only arms. There is no dive
    count to trip, and no re-nag: an expired card is either renewed, which moves the date
    and re-arms this, or let lapse, and the Renewals card keeps showing it either way.
    """
    return stage is not None and (notified_stage, notified_for) != (stage.value, expires_on)


def expiry_text(stage: ExpiryStage, expires_on: date) -> str:
    """The phrase after a subject's name - the Renewals card's own wording."""
    verb = "expired" if stage is ExpiryStage.EXPIRED else "expires"
    return f"{verb} {expires_on:%-d %b %Y}"


# The agencies as they spell themselves, for the email's card names. The web keeps its own
# copy (`CERTIFICATION_AGENCY_LABELS` in `lib/api/certifications.ts`); this one exists for
# the reason `services.gear_service._SERVICE_KIND_LABELS` does - the email has no browser to
# render it. Several cannot be derived from the value (ScotSAC, ProTec, NSS-CDS).
_AGENCY_LABELS = {
    CertificationAgency.PADI: "PADI",
    CertificationAgency.SSI: "SSI",
    CertificationAgency.NAUI: "NAUI",
    CertificationAgency.SDI: "SDI",
    CertificationAgency.TDI: "TDI",
    CertificationAgency.CMAS: "CMAS",
    CertificationAgency.RAID: "RAID",
    CertificationAgency.BSAC: "BSAC",
    CertificationAgency.GUE: "GUE",
    CertificationAgency.IANTD: "IANTD",
    CertificationAgency.PSAI: "PSAI",
    CertificationAgency.DAN: "DAN",
    CertificationAgency.EFR: "EFR",
    CertificationAgency.ANDI: "ANDI",
    CertificationAgency.SNSI: "SNSI",
    CertificationAgency.ACUC: "ACUC",
    CertificationAgency.PSS: "PSS",
    CertificationAgency.IDA: "IDA",
    CertificationAgency.NDL: "NDL",
    CertificationAgency.UTD: "UTD",
    CertificationAgency.SAA: "SAA",
    CertificationAgency.SCOTSAC: "ScotSAC",
    CertificationAgency.IAC: "IAC",
    CertificationAgency.PROTEC: "ProTec",
    CertificationAgency.PDIC: "PDIC",
    CertificationAgency.NASE: "NASE",
    CertificationAgency.SEI: "SEI",
    CertificationAgency.YMCA: "YMCA",
    CertificationAgency.ERDI: "ERDI",
    CertificationAgency.AIDA: "AIDA",
    CertificationAgency.MOLCHANOVS: "Molchanovs",
    CertificationAgency.PFI: "PFI",
    CertificationAgency.APNEA_ACADEMY: "Apnea Academy",
    CertificationAgency.FII: "FII",
    CertificationAgency.NSS_CDS: "NSS-CDS",
    CertificationAgency.NACD: "NACD",
    CertificationAgency.IDEA: "IDEA",
    CertificationAgency.DIWA: "DIWA",
    CertificationAgency.OTHER: "Other",
}


def certification_label(*, agency: str, agency_other: str | None, name: str) -> str:
    """Agency, then level - "PADI Rescue Diver" - as the web's `certificationLabel` names a
    card: a diver can hold the same level from two agencies, so the name alone would not say
    which card is running out.

    `agency` is the stored string, which may be outside the vocabulary (*"A stored
    vocabulary is read back as a string"* in DECISIONS.md), and then prints as it is.
    """
    if agency == CertificationAgency.OTHER:
        label = (agency_other or "").strip() or _AGENCY_LABELS[CertificationAgency.OTHER]
    else:
        try:
            label = _AGENCY_LABELS[CertificationAgency(agency)]
        except ValueError:
            label = agency
    return f"{label} {name}"


def insurance_label(provider: str | None) -> str:
    """The Renewals card's insurance row, as one phrase: a policy with a date and no insurer
    named is still a policy running out."""
    named = (provider or "").strip()
    return f"{named} dive insurance" if named else "Dive insurance"
