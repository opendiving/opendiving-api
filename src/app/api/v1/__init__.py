from fastapi import APIRouter

from .dive_sites import router as dive_sites_router
from .dive_stats import router as dive_stats_router
from .dives import router as dives_router
from .health import router as health_router
from .login import router as login_router
from .logout import router as logout_router
from .trips import router as trips_router
from .users import router as users_router

router = APIRouter(prefix="/v1")
router.include_router(health_router)
router.include_router(login_router)
router.include_router(logout_router)
router.include_router(users_router)
router.include_router(trips_router)
router.include_router(dive_sites_router)
router.include_router(dives_router)
router.include_router(dive_stats_router)
