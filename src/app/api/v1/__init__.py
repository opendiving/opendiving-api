from fastapi import APIRouter

from .auth import router as auth_router
from .dive_sites import router as dive_sites_router
from .dives import router as dives_router
from .gear_items import router as gear_items_router
from .gear_sets import router as gear_sets_router
from .health import router as health_router
from .trips import router as trips_router
from .users import router as users_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
router.include_router(auth_router)
router.include_router(users_router)
router.include_router(trips_router)
router.include_router(dive_sites_router)
router.include_router(gear_items_router)
router.include_router(gear_sets_router)
router.include_router(dives_router)
