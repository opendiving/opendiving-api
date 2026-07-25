from fastcrud import FastCRUD

from ..models.trip import Trip
from ..schemas.trip import TripCreateInternal, TripDelete, TripRead, TripUpdate, TripUpdateInternal

CRUDTrip = FastCRUD[Trip, TripCreateInternal, TripUpdate, TripUpdateInternal, TripDelete, TripRead]
crud_trips = CRUDTrip(Trip)
