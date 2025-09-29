from fastcrud import FastCRUD

from ..models.dive import Dive
from ..schemas.dive import DiveCreateInternal, DiveDelete, DiveRead, DiveUpdate, DiveUpdateInternal

CRUDDive = FastCRUD[Dive, DiveCreateInternal, DiveUpdate, DiveUpdateInternal, DiveDelete, DiveRead]
crud_dives = CRUDDive(Dive)
