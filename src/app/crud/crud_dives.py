from fastcrud import FastCRUD

from ..models.dive import Dive
from ..schemas.dive import DiveCreateInternal, DiveDelete, DiveReadInternal, DiveUpdate, DiveUpdateInternal

CRUDDive = FastCRUD[Dive, DiveCreateInternal, DiveUpdate, DiveUpdateInternal, DiveDelete, DiveReadInternal]
crud_dives = CRUDDive(Dive)
