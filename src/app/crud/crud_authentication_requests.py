from fastcrud import FastCRUD

from ..models.authentication_request import AuthenticationRequest
from ..schemas.authentication_request import (
    AuthenticationRequestCreate,
    AuthenticationRequestRead,
    AuthenticationRequestUpdate,
)

CRUDAuthenticationRequest = FastCRUD[
    AuthenticationRequest,
    AuthenticationRequestCreate,
    AuthenticationRequestUpdate,
    AuthenticationRequestUpdate,
    AuthenticationRequestUpdate,
    AuthenticationRequestRead,
]
crud_authentication_requests = CRUDAuthenticationRequest(AuthenticationRequest)
