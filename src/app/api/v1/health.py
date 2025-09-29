from fastapi import APIRouter
from pydantic import BaseModel

from src.app.core.config import settings

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/")
async def health_check() -> dict[str, str]:
    """Health check endpoint to verify API is running."""
    return {"status": "healthy", "version": settings.APP_VERSION or "unknown", "message": "API is running"}
