"""Unit tests for the health check endpoint."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app.api.v1.health import router as health_router


def _make_health_client() -> TestClient:
    """Build a minimal app exposing only the health router.

    This avoids exercising the full application lifespan (DB/Redis setup),
    keeping the test focused on the health endpoint's own behavior.
    """
    app = FastAPI()
    app.include_router(health_router)
    return TestClient(app)


class TestHealthCheck:
    def test_health_endpoint_returns_200(self):
        client = _make_health_client()

        response = client.get("/health")

        assert response.status_code == 200

    def test_health_endpoint_returns_expected_payload(self):
        client = _make_health_client()

        response = client.get("/health")
        body = response.json()

        assert body["status"] == "healthy"
        assert body["message"] == "API is running"
        assert "version" in body
