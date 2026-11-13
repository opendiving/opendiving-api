from urllib.parse import urlsplit

from mcp.server.auth.routes import validate_issuer_url
from pydantic import AnyHttpUrl

from ..core.config import settings


def issuer_url() -> str:
    return settings.frontend_origin + "/api/v1/mcp/oauth"


def resource_url() -> str:
    return settings.frontend_origin + "/api/v1/mcp"


def validate_configuration() -> None:
    validate_issuer_url(AnyHttpUrl(issuer_url()))


def valid_redirect(uri: str) -> bool:
    url = urlsplit(uri)
    return bool(
        url.hostname
        and not url.username
        and not url.password
        and not url.fragment
        and (url.scheme == "https" or (url.scheme == "http" and url.hostname in {"127.0.0.1", "::1", "localhost"}))
    )
