from fastapi import APIRouter

from .admin import router as admin_router
from .auth import router as auth_router
from .certifications import router as certifications_router
from .config import router as config_router
from .contact import router as contact_router
from .courses import router as courses_router
from .dive_sites import router as dive_sites_router
from .dives import router as dives_router
from .export import router as export_router
from .gear_items import router as gear_items_router
from .gear_service import router as gear_service_router
from .gear_sets import router as gear_sets_router
from .geocoding import router as geocoding_router
from .health import router as health_router
from .invitations import router as invitations_router
from .passkeys import router as passkeys_router
from .sessions import router as sessions_router
from .species import router as species_router
from .trips import router as trips_router
from .users import router as users_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
router.include_router(config_router)
router.include_router(auth_router)
router.include_router(users_router)
router.include_router(passkeys_router)
router.include_router(sessions_router)
router.include_router(invitations_router)
router.include_router(admin_router)
router.include_router(trips_router)
router.include_router(courses_router)
router.include_router(dive_sites_router)
router.include_router(geocoding_router)
router.include_router(gear_items_router)
router.include_router(gear_sets_router)
router.include_router(gear_service_router)
router.include_router(certifications_router)
router.include_router(species_router)
router.include_router(dives_router)
router.include_router(contact_router)
router.include_router(export_router)
