"""The API document the structural guards walk, built with every experimental feature on.

An application leaves an off feature's operations out of its document, and CI runs with every
feature off, so a guard reading the document as served there would never check a gated route.
Every walk over the document's operations reads it through here instead;
`tests/test_experimental_features.py` fails on a walk that does not.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app.core.config import EVERY_EXPERIMENTAL_FEATURE, settings
from src.app.core.setup import OPENAPI_URL


@contextmanager
def every_experimental_feature_on(app: FastAPI) -> Iterator[None]:
    """`app` with every experimental feature on, its document rebuilt on the way in and on the way
    out, so neither side is handed the other's."""
    with patch.object(settings, "EXPERIMENTAL_FEATURES", EVERY_EXPERIMENTAL_FEATURE):
        app.openapi_schema = None
        try:
            yield
        finally:
            app.openapi_schema = None


def document_with_every_feature_on(app: FastAPI) -> dict[str, Any]:
    """`app.openapi()`, as an instance with every experimental feature on builds it."""
    with every_experimental_feature_on(app):
        return app.openapi()


def served_document_with_every_feature_on(app: FastAPI) -> dict[str, Any]:
    """The document `app` serves, credentials annotated, with every experimental feature on."""
    with every_experimental_feature_on(app):
        response = TestClient(app).get(OPENAPI_URL)
    assert response.status_code == 200
    document: dict[str, Any] = response.json()
    return document
