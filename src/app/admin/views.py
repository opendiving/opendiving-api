from crudadmin import CRUDAdmin

from ..models.dive import Dive
from ..models.dive_dive_site import DiveDiveSite
from ..models.dive_gear_item import DiveGearItem
from ..models.dive_mixture import DiveMixture
from ..models.dive_site import DiveSite
from ..models.gear_item import GearItem
from ..models.gear_set import GearSet
from ..models.gear_set_item import GearSetItem
from ..models.trip import Trip
from ..models.user import User
from ..models.user_dive_stats import UserDiveStats
from ..schemas.dive import DiveCreateInternal, DiveUpdateInternal
from ..schemas.dive_dive_site import DiveDiveSiteCreate, DiveDiveSiteUpdate
from ..schemas.dive_gear_item import DiveGearItemCreate, DiveGearItemUpdate
from ..schemas.dive_mixture import DiveMixtureCreateInternal, DiveMixtureUpdate
from ..schemas.dive_site import DiveSiteCreateInternal, DiveSiteUpdate
from ..schemas.gear_item import GearItemCreateInternal, GearItemUpdate
from ..schemas.gear_set import GearSetCreateInternal, GearSetUpdate
from ..schemas.gear_set_item import GearSetItemCreate, GearSetItemUpdate
from ..schemas.trip import TripCreateInternal, TripUpdate
from ..schemas.user import UserAdminUpdate, UserCreateInternal
from ..schemas.user_dive_stats import UserDiveStatsUpdate


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

    admin.add_view(
        model=DiveSite,
        create_schema=DiveSiteCreateInternal,
        update_schema=DiveSiteUpdate,
        allowed_actions={"view", "create", "update", "delete"},
    )

    admin.add_view(
        model=Trip,
        create_schema=TripCreateInternal,
        update_schema=TripUpdate,
        allowed_actions={"view", "create", "update", "delete"},
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

    # `GearItem.dive_count` is derived (see `services.gear_stats`), so editing it here
    # only sticks until the owner's next dive create/update/delete recalculates it.
    admin.add_view(
        model=GearItem,
        create_schema=GearItemCreateInternal,
        update_schema=GearItemUpdate,
        allowed_actions={"view", "create", "update", "delete"},
    )

    admin.add_view(
        model=GearSet,
        create_schema=GearSetCreateInternal,
        update_schema=GearSetUpdate,
        allowed_actions={"view", "create", "update", "delete"},
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

