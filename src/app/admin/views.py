from crudadmin import CRUDAdmin

from ..models.auth_audit_event import AuthAuditEvent
from ..models.certification import Certification
from ..models.course import Course
from ..models.dive import Dive
from ..models.dive_dive_site import DiveDiveSite
from ..models.dive_form_preset import DiveFormPreset
from ..models.dive_gear_item import DiveGearItem
from ..models.dive_mixture import DiveMixture
from ..models.dive_site import DiveSite
from ..models.dive_species import DiveSpecies
from ..models.gear_item import GearItem
from ..models.gear_service_record import GearServiceRecord
from ..models.gear_service_schedule import GearServiceSchedule
from ..models.gear_set import GearSet
from ..models.gear_set_item import GearSetItem
from ..models.species import Species
from ..models.species_name import SpeciesName
from ..models.trip import Trip
from ..models.trip_part import TripPart
from ..models.user import User
from ..models.user_dive_stats import UserDiveStats
from ..models.user_session import UserSession
from ..schemas.auth_audit_event import AuthAuditEventCreateInternal
from ..schemas.certification import CertificationCreateInternal, CertificationUpdate
from ..schemas.course import CourseCreateInternal, CourseUpdate
from ..schemas.dive import DiveCreateInternal, DiveUpdateInternal
from ..schemas.dive_dive_site import DiveDiveSiteCreate, DiveDiveSiteUpdate
from ..schemas.dive_form_preset import DiveFormPresetCreateInternal, DiveFormPresetUpdate
from ..schemas.dive_gear_item import DiveGearItemCreate, DiveGearItemUpdate
from ..schemas.dive_mixture import DiveMixtureCreateInternal, DiveMixtureUpdate
from ..schemas.dive_site import DiveSiteCreateInternal, DiveSiteUpdate
from ..schemas.gear_item import GearItemCreateInternal, GearItemUpdate
from ..schemas.gear_service import (
    GearServiceRecordCreateInternal,
    GearServiceRecordUpdate,
    GearServiceScheduleCreateInternal,
    GearServiceScheduleUpdate,
)
from ..schemas.gear_set import GearSetCreateInternal, GearSetUpdate
from ..schemas.gear_set_item import GearSetItemCreate, GearSetItemUpdate
from ..schemas.species import (
    DiveSpeciesCreate,
    DiveSpeciesUpdate,
    SpeciesCreate,
    SpeciesNameCreate,
    SpeciesNameUpdate,
    SpeciesUpdate,
)
from ..schemas.trip import TripCreateInternal, TripUpdate
from ..schemas.trip_part import TripPartCreate, TripPartUpdate
from ..schemas.user import UserAdminUpdate, UserCreateInternal
from ..schemas.user_dive_stats import UserDiveStatsUpdate
from ..schemas.user_session import UserSessionCreateInternal


def register_admin_views(admin: CRUDAdmin) -> None:
    """Register all models and their schemas with the admin interface.

    This function adds all available models to the admin interface with appropriate
    schemas and permissions.
    """

    # No password field anywhere - a `User` has no authentication method of its own
    # (see `AuthenticationProvider`), so admin-created users can sign in afterwards via
    # the normal email-magic-link flow using their `email`. Uses `UserAdminUpdate`
    # (not the public API's `UserUpdate`) so a superuser can still edit `email`
    # directly here without going through the verified email-change flow.
    admin.add_view(
        model=User,
        create_schema=UserCreateInternal,
        update_schema=UserAdminUpdate,
        allowed_actions={"view", "create", "update"},
    )

    admin.add_view(
        model=Dive,
        create_schema=DiveCreateInternal,
        update_schema=DiveUpdateInternal,
        allowed_actions={"view", "create", "update", "delete"},
    )

    admin.add_view(
        model=UserDiveStats,
        create_schema=UserDiveStatsUpdate,
        update_schema=UserDiveStatsUpdate,
        allowed_actions={"view"},
    )

    # Both auth tables are registered **view-only**, and for a reason of their own rather
    # than either of the two already written down below - not the hard-delete-cascade
    # argument the diver-owned resources take, and not the everybody's-row argument
    # `Species` takes.
    #
    # A `user_session` row is a live credential's backing state. A panel that could create
    # one would be a panel that mints a session for an account, and one that could edit one
    # could push `expires_at` out or clear `revoked_at`, which is un-revoking a device the
    # diver deliberately signed out. Deleting one is not dangerous so much as pointless: the
    # revoke path is `DELETE /user/session/{uuid}` and the cron sweep removes the row after.
    # `AuthAuditEvent` is the stronger case of the same argument - an audit trail whose rows
    # can be edited or removed by hand is worth nothing as evidence, which is the reasoning
    # the account purge already applies to `admin_audit_log` from the other direction.
    #
    # `UserSession` reaching this without a `"delete"` is legal only because it is exempt in
    # `NOT_A_DIVERS_OWN_RESOURCE`; `test_admin_config.py` derives its parametrize list from
    # that registry and would otherwise *require* `"create"` and `"update"` here.
    admin.add_view(
        model=UserSession,
        create_schema=UserSessionCreateInternal,
        update_schema=UserSessionCreateInternal,
        allowed_actions={"view"},
    )

    admin.add_view(
        model=AuthAuditEvent,
        create_schema=AuthAuditEventCreateInternal,
        update_schema=AuthAuditEventCreateInternal,
        allowed_actions={"view"},
    )

    # `DiveSite`, `Trip`, `Course`, `GearItem`, `GearSet`, `GearServiceSchedule` and
    # `DiveFormPreset` are registered without `"delete"`, and that is not squeamishness about
    # a superuser having the power. FastCRUD's `delete` branches on whether the model carries
    # `is_deleted`, and since they hard-delete it takes the `DELETE FROM` branch - so the button that
    # used to flag one row now destroys the row, its schedules, its service records and
    # every join row pointing at it, through the FK cascades. It would do that with **no
    # cache invalidation**: that lives on the API routes (`services/cache_invalidation.py`)
    # and nowhere else, so Redis would go on serving the deleted rows for the rest of the
    # TTL. Delete through the API. `view`/`create`/`update` are unaffected.
    admin.add_view(
        model=DiveSite,
        create_schema=DiveSiteCreateInternal,
        update_schema=DiveSiteUpdate,
        allowed_actions={"view", "create", "update"},
    )

    admin.add_view(
        model=Trip,
        create_schema=TripCreateInternal,
        update_schema=TripUpdate,
        allowed_actions={"view", "create", "update"},
    )

    # `CourseUpdate`, not a `*UpdateRequest`: unlike a dive or a certification, a course
    # carries no non-column reference for a request schema to add, so the API's PATCH body
    # and the admin form are the same shape. `ck_course_date_range` is what stops this form
    # storing an inverted date range - the merged-value check lives on the route, which the
    # panel does not go through.
    admin.add_view(
        model=Course,
        create_schema=CourseCreateInternal,
        update_schema=CourseUpdate,
        allowed_actions={"view", "create", "update"},
    )

    # `DiveFormPresetUpdate`, not a `*UpdateRequest`: like a course, a preset carries no
    # non-column reference for a request schema to add, so the API's PATCH body and the
    # admin form are the same shape.
    admin.add_view(
        model=DiveFormPreset,
        create_schema=DiveFormPresetCreateInternal,
        update_schema=DiveFormPresetUpdate,
        allowed_actions={"view", "create", "update"},
    )

    admin.add_view(
        model=DiveMixture,
        create_schema=DiveMixtureCreateInternal,
        update_schema=DiveMixtureUpdate,
        allowed_actions={"view", "create", "update", "delete"},
    )

    admin.add_view(
        model=DiveDiveSite,
        create_schema=DiveDiveSiteCreate,
        update_schema=DiveDiveSiteUpdate,
        allowed_actions={"view", "create", "update", "delete"},
    )

    # Rows here are replaced wholesale by every trip write, so an edit made in this panel
    # only sticks until the owner next saves the trip.
    admin.add_view(
        model=TripPart,
        create_schema=TripPartCreate,
        update_schema=TripPartUpdate,
        allowed_actions={"view", "create", "update", "delete"},
    )

    # The species catalog and its search index are registered **without** `"delete"`, and for
    # a different reason than the diver-owned ones above: these tables hard-delete too, but the row is
    # not one user's. Deleting a species would take every `dive_species` row pointing at it
    # through the FK cascade - silently removing a sighting from other people's dives, with
    # no cache invalidation, since that lives on the API routes and there is no route here
    # to put it on. Nothing in the app deletes a species by design (see `models/species.py`),
    # so the panel does not either.
    #
    # `update` stays, and an edit made here inherits the immutability decision's documented
    # cost: already-cached dive reads keep the old name until their TTL expires. There is no
    # way to express "invalidate every user's dives" in `services/cache_invalidation.py`, and
    # this is the one place that can provoke it.
    admin.add_view(
        model=Species,
        create_schema=SpeciesCreate,
        update_schema=SpeciesUpdate,
        allowed_actions={"view", "create", "update"},
    )

    admin.add_view(
        model=SpeciesName,
        create_schema=SpeciesNameCreate,
        update_schema=SpeciesNameUpdate,
        allowed_actions={"view", "create", "update"},
    )

    # The join table, unlike the two above: removing one of these unlinks a sighting from a
    # dive, which is exactly what it should do and affects only that dive.
    admin.add_view(
        model=DiveSpecies,
        create_schema=DiveSpeciesCreate,
        update_schema=DiveSpeciesUpdate,
        allowed_actions={"view", "create", "update", "delete"},
    )

    # `GearItem.dive_count` is derived (see `services.gear_stats`), so editing it here
    # only sticks until the owner's next dive create/update/delete recalculates it.
    admin.add_view(
        model=GearItem,
        create_schema=GearItemCreateInternal,
        update_schema=GearItemUpdate,
        allowed_actions={"view", "create", "update"},
    )

    # `last_service_on`/`next_due_on`/`next_due_at_dive_count` and the `notified_*`
    # fields are derived (see `services.gear_service.recalculate_service_schedule`), so
    # editing them here only sticks until the next schedule or record write.
    admin.add_view(
        model=GearServiceSchedule,
        create_schema=GearServiceScheduleCreateInternal,
        update_schema=GearServiceScheduleUpdate,
        allowed_actions={"view", "create", "update"},
    )

    admin.add_view(
        model=GearServiceRecord,
        create_schema=GearServiceRecordCreateInternal,
        update_schema=GearServiceRecordUpdate,
        allowed_actions={"view", "create", "update", "delete"},
    )

    admin.add_view(
        model=GearSet,
        create_schema=GearSetCreateInternal,
        update_schema=GearSetUpdate,
        allowed_actions={"view", "create", "update"},
    )

    admin.add_view(
        model=GearSetItem,
        create_schema=GearSetItemCreate,
        update_schema=GearSetItemUpdate,
        allowed_actions={"view", "create", "update", "delete"},
    )

    admin.add_view(
        model=DiveGearItem,
        create_schema=DiveGearItemCreate,
        update_schema=DiveGearItemUpdate,
        allowed_actions={"view", "create", "update", "delete"},
    )

    admin.add_view(
        model=Certification,
        create_schema=CertificationCreateInternal,
        update_schema=CertificationUpdate,
        allowed_actions={"view", "create", "update", "delete"},
    )

    # `CertificationFile` is deliberately *not* registered. There is no create/update form
    # that could meaningfully accept a file upload, and its rows are metadata plus a
    # `storage_key` naming a file on the volume - a panel that let you edit that key would
    # be a panel that lets you point a row at somebody else's card. Card files are managed
    # through `PUT`/`DELETE /certification/{uuid}/file/{side}` instead.
    #
    # Two gaps worth knowing when deleting a `Certification` from here. A *soft* delete
    # leaves its file rows behind entirely: only the API's `erase_certification` removes
    # them, because an application-level `is_deleted` never fires the FK cascade. A *hard*
    # delete does fire the cascade, which takes the rows and leaves their files on the
    # volume with nothing referencing them - reclaimed by
    # `src/scripts/sweep_orphaned_files.py`, and one of the reasons that script exists.
