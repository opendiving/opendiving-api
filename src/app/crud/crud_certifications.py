from fastcrud import FastCRUD

from ..models.certification import Certification
from ..schemas.certification import (
    CertificationCreateInternal,
    CertificationDelete,
    CertificationReadInternal,
    CertificationUpdate,
    CertificationUpdateInternal,
)

CRUDCertification = FastCRUD[
    Certification,
    CertificationCreateInternal,
    CertificationUpdate,
    CertificationUpdateInternal,
    CertificationDelete,
    CertificationReadInternal,
]
crud_certifications = CRUDCertification(Certification)

# No `resolve_*_ids_for_user` / `get_*_uuids_by_id` helpers here, unlike
# `crud_gear_items`. Those exist because dives and gear sets reference gear by uuid and
# have to translate in both directions; nothing references a certification, so every
# lookup is the plain `crud_certifications.get(uuid=...)` the routes already do. The
# file rows that *do* hang off a certification are reached by its internal `id`, which
# the route already holds from that same lookup - see `services/certification_files.py`.
