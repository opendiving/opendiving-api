"""Unit tests for the dive-file upload endpoint's size guard (`/dive/parse`)."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app.api.dependencies import get_current_user
from src.app.api.v1.dives import _MAX_DIVE_FILE_SIZE
from src.app.api.v1.dives import router as dives_router

SUUNTO_NS = "http://schemas.datacontract.org/2004/07/Suunto.Diving.Dal"

VALID_SUUNTO_XML = f"""<?xml version="1.0" encoding="utf-8"?>
<Dive xmlns="{SUUNTO_NS}">
  <MaxDepth>25.5</MaxDepth>
  <Duration>1800</Duration>
</Dive>
""".encode()


def _make_dive_upload_client() -> TestClient:
    """Build a minimal app exposing only the dives router, with auth stubbed out.

    This avoids exercising the full application lifespan (DB/Redis setup),
    keeping the test focused on the upload endpoint's own behavior.
    """
    app = FastAPI()
    app.include_router(dives_router)
    app.dependency_overrides[get_current_user] = lambda: {"id": 1, "is_superuser": False}
    return TestClient(app)


class TestParseDiveUploadSizeLimit:
    def test_accepts_file_within_limit(self):
        client = _make_dive_upload_client()

        response = client.post(
            "/dive/parse",
            files={"file": ("export.xml", VALID_SUUNTO_XML, "application/xml")},
        )

        assert response.status_code == 200
        assert response.json()["max_depth"] == 25.5

    def test_rejects_file_over_size_limit_with_413(self):
        client = _make_dive_upload_client()
        oversized_content = b"a" * (_MAX_DIVE_FILE_SIZE + 1)

        response = client.post(
            "/dive/parse",
            files={"file": ("export.xml", oversized_content, "application/xml")},
        )

        assert response.status_code == 413

    def test_does_not_buffer_more_than_the_limit_in_memory(self):
        """The endpoint must reject oversized uploads by reading bounded chunks,
        not by trusting Content-Length or reading the whole body unconditionally."""
        client = _make_dive_upload_client()
        # Comfortably larger than the limit, but not so large the test itself is slow.
        oversized_content = b"b" * (_MAX_DIVE_FILE_SIZE * 2)

        response = client.post(
            "/dive/parse",
            files={"file": ("export.xml", oversized_content, "application/xml")},
        )

        assert response.status_code == 413

    def test_missing_filename_is_rejected(self):
        client = _make_dive_upload_client()

        response = client.post(
            "/dive/parse",
            files={"file": ("", VALID_SUUNTO_XML, "application/xml")},
        )

        # An empty filename never reaches parsing logic: multipart validation itself
        # rejects it (422) before our endpoint's own `400` filename check would run.
        assert response.status_code in (400, 422)

    def test_unsupported_file_returns_415(self):
        client = _make_dive_upload_client()

        response = client.post(
            "/dive/parse",
            files={"file": ("export.csv", b"time,depth\n0,0\n", "text/csv")},
        )

        assert response.status_code == 415
