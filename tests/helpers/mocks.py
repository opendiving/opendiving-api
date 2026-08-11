from typing import Any

from fastapi.encoders import jsonable_encoder

from src.app import models
from tests.conftest import fake


def get_current_user(user: models.User) -> dict[str, Any]:
    # `jsonable_encoder` is typed as returning `Any`; a model always encodes to a dict,
    # and the callers here index into it as one.
    encoded: dict[str, Any] = jsonable_encoder(user)
    return encoded


def oauth2_scheme() -> str:
    token = fake.sha256()
    if isinstance(token, bytes):
        token = token.decode("utf-8")
    return token  # type: ignore
