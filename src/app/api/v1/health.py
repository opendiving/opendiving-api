from fastapi import APIRouter

from ...core.config import settings

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check() -> dict[str, str]:
    """Health check endpoint to verify API is running."""
    return {"status": "healthy", "version": settings.APP_VERSION or "unknown", "message": "API is running"}
