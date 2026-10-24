from fastcrud import FastCRUD

from ..models.dive_draft import DiveDraft
from ..schemas.dive_draft import DiveDraftReadInternal

# Only `get`, for `fetch_owned_or_raise`; every write is `services/dive_drafts.py`'s own.
CRUDDiveDraft = FastCRUD[
    DiveDraft,
    DiveDraftReadInternal,
    DiveDraftReadInternal,
    DiveDraftReadInternal,
    DiveDraftReadInternal,
    DiveDraftReadInternal,
]
crud_dive_drafts = CRUDDiveDraft(DiveDraft)
