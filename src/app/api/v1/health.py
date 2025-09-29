from fastapi import APIRouter
from pydantic import BaseModel

from src.app.core.config import settings

router = APIRouter(prefix="/health", tags=["health"])


class HealthResponse(BaseModel):
    status: str
    version: str
    message: str


@router.get("/", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Health check endpoint to verify API is running."""
    return HealthResponse(status="healthy", version=settings.APP_VERSION or "unknown", message="API is running")
