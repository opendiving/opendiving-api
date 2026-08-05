from fastcrud import FastCRUD

from ..models.user_dive_stats import UserDiveStats
from ..schemas.user_dive_stats import (
    UserDiveStatsCreateInternal,
    UserDiveStatsDelete,
    UserDiveStatsRead,
    UserDiveStatsUpdate,
    UserDiveStatsUpdateInternal,
)

CRUDUserDiveStats = FastCRUD[
    UserDiveStats,
    UserDiveStatsCreateInternal,
    UserDiveStatsUpdate,
    UserDiveStatsUpdateInternal,
    UserDiveStatsDelete,
    UserDiveStatsRead,
]
crud_user_dive_stats = CRUDUserDiveStats(UserDiveStats)
