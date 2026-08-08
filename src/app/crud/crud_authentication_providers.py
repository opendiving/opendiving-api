from fastcrud import FastCRUD

from ..models.authentication_provider import AuthenticationProvider
from ..schemas.authentication_provider import (
    AuthenticationProviderCreate,
    AuthenticationProviderRead,
    AuthenticationProviderUpdate,
)

CRUDAuthenticationProvider = FastCRUD[
    AuthenticationProvider,
    AuthenticationProviderCreate,
    AuthenticationProviderUpdate,
    AuthenticationProviderUpdate,
    AuthenticationProviderUpdate,
    AuthenticationProviderRead,
]
crud_authentication_providers = CRUDAuthenticationProvider(AuthenticationProvider)
