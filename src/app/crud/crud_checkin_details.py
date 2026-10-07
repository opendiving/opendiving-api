from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.checkin_details import CheckinDetails, CheckinEmergencyContact, CheckinInsurancePolicy
from ..schemas.checkin_details import (
    CheckinDetailsRead,
    CheckinDetailsUpdate,
    EmergencyContact,
    EmergencyContactInput,
    InsurancePolicy,
    InsurancePolicyInput,
)

_SCALARS = ("email", "phone", "date_of_birth")


async def read_checkin_details(db: AsyncSession, *, user_id: int) -> CheckinDetailsRead:
    """The account's check-in details, the empty object where it has saved none."""
    row = (
        await db.execute(
            select(CheckinDetails.email, CheckinDetails.phone, CheckinDetails.date_of_birth).where(
                CheckinDetails.user_id == user_id
            )
        )
    ).one_or_none()
    contacts = await db.execute(
        select(CheckinEmergencyContact.name, CheckinEmergencyContact.phone, CheckinEmergencyContact.relationship)
        .where(CheckinEmergencyContact.user_id == user_id)
        .order_by(CheckinEmergencyContact.position)
    )
    policies = await db.execute(
        select(CheckinInsurancePolicy.provider, CheckinInsurancePolicy.number, CheckinInsurancePolicy.expires_on)
        .where(CheckinInsurancePolicy.user_id == user_id)
        .order_by(CheckinInsurancePolicy.position)
    )
    return CheckinDetailsRead(
        **({} if row is None else row._asdict()),
        emergency_contacts=[EmergencyContact(**contact._asdict()) for contact in contacts],
        insurance_policies=[InsurancePolicy(**policy._asdict()) for policy in policies],
    )


async def write_checkin_details(
    db: AsyncSession, *, user_id: int, values: CheckinDetailsUpdate, commit: bool = True
) -> None:
    """Write the members `values` carries, each replacing the account's whole, and leave the
    rest alone.

    The parent row is upserted first whatever is sent, so its row lock serializes two writes
    of one diver's object - a double-submit, or an import's apply beside a card save - before
    either replaces child rows, which have no unique constraint to refuse a double insert.
    """
    present = values.model_fields_set
    scalars = {name: getattr(values, name) for name in _SCALARS if name in present}
    statement = insert(CheckinDetails).values(user_id=user_id, **scalars)
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[CheckinDetails.user_id],
            # A write carrying no scalar still takes the lock, by setting the key to itself.
            set_=scalars or {"user_id": statement.excluded.user_id},
        )
    )
    if values.emergency_contacts is not None:
        await _replace_contacts(db, user_id, values.emergency_contacts)
    if values.insurance_policies is not None:
        await _replace_policies(db, user_id, values.insurance_policies)
    if commit:
        await db.commit()


async def _replace_contacts(db: AsyncSession, user_id: int, contacts: Sequence[EmergencyContactInput]) -> None:
    await db.execute(delete(CheckinEmergencyContact).where(CheckinEmergencyContact.user_id == user_id))
    db.add_all(
        CheckinEmergencyContact(user_id=user_id, position=position, **contact.model_dump())
        for position, contact in enumerate(contacts)
    )


async def _replace_policies(db: AsyncSession, user_id: int, policies: Sequence[InsurancePolicyInput]) -> None:
    """Delete-then-insert, carrying each reminder pair across to the new row whose provider
    and expiry equal its old row's - what a reminder line names. A corrected number is the
    same policy, so it keeps a reminder already sent; a new expiry re-arms it. Each old row
    hands its pair on once, in order."""
    old = (
        await db.execute(
            select(
                CheckinInsurancePolicy.provider,
                CheckinInsurancePolicy.expires_on,
                CheckinInsurancePolicy.notified_stage,
                CheckinInsurancePolicy.notified_for,
            )
            .where(CheckinInsurancePolicy.user_id == user_id)
            .order_by(CheckinInsurancePolicy.position)
        )
    ).all()
    unclaimed: list[Any] = [row for row in old if row.notified_stage is not None or row.notified_for is not None]
    await db.execute(delete(CheckinInsurancePolicy).where(CheckinInsurancePolicy.user_id == user_id))
    for position, policy in enumerate(policies):
        carried = next(
            (row for row in unclaimed if (row.provider, row.expires_on) == (policy.provider, policy.expires_on)), None
        )
        if carried is not None:
            unclaimed.remove(carried)
        db.add(
            CheckinInsurancePolicy(
                user_id=user_id,
                position=position,
                **policy.model_dump(),
                notified_stage=None if carried is None else carried.notified_stage,
                notified_for=None if carried is None else carried.notified_for,
            )
        )
