from fastcrud import FastCRUD
from sqlalchemy import select

from ..models.dive import Dive
from ..models.dive_dive_site import DiveDiveSite
from ..models.dive_gear_item import DiveGearItem
from ..schemas.dive import DiveCreateInternal, DiveDelete, DiveReadInternal, DiveUpdate, DiveUpdateInternal

CRUDDive = FastCRUD[Dive, DiveCreateInternal, DiveUpdate, DiveUpdateInternal, DiveDelete, DiveReadInternal]

# Lets callers filter dives by dive site or gear item (e.g. `id__at_dive_site=some_id`,
# `id__with_gear_item=some_id`) with a single `IN (subquery)` condition instead of
# resolving matching dive ids in a separate round trip.
crud_dives = CRUDDive(
    Dive,
    custom_filters={
        "at_dive_site": lambda column: lambda dive_site_id: column.in_(
            select(DiveDiveSite.dive_id).where(DiveDiveSite.dive_site_id == dive_site_id)
        ),
        "with_gear_item": lambda column: lambda gear_item_id: column.in_(
            select(DiveGearItem.dive_id).where(DiveGearItem.gear_item_id == gear_item_id)
        ),
    },
)
