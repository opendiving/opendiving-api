from crudadmin import CRUDAdmin

from ..models.dive import Dive
from ..models.dive_dive_site import DiveDiveSite
from ..models.dive_mixture import DiveMixture
from ..models.dive_site import DiveSite
from ..models.trip import Trip
from ..models.user import User
from ..models.user_dive_stats import UserDiveStats
from ..schemas.dive import DiveCreateInternal, DiveUpdateInternal
from ..schemas.dive_dive_site import DiveDiveSiteCreate, DiveDiveSiteUpdate
from ..schemas.dive_mixture import DiveMixtureCreateInternal, DiveMixtureUpdate
from ..schemas.dive_site import DiveSiteCreateInternal, DiveSiteUpdate
from ..schemas.trip import TripCreateInternal, TripUpdate
from ..schemas.user import UserCreateInternal, UserUpdate
from ..schemas.user_dive_stats import UserDiveStatsUpdate


def register_admin_views(admin: CRUDAdmin) -> None:
    """Register all models and their schemas with the admin interface.

    This function adds all available models to the admin interface with appropriate
    schemas and permissions.
    """

    # No password field anywhere - a `User` has no authentication method of its own
    # (see `AuthenticationProvider`), so admin-created users can sign in afterwards via
    # the normal email-magic-link flow using their `email`.
    admin.add_view(
        model=User,
        create_schema=UserCreateInternal,
        update_schema=UserUpdate,
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

