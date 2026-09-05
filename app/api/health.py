"""Health and readiness probe API module."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.core.connectivity import check_all

router = APIRouter(tags=["Health"])

SERVICE_NAME = "litellm-observability"


class DependencyHealth(BaseModel):
    """Stable health status for one infrastructure dependency."""

    name: str
    ok: bool
    latency_ms: float | None
    detail: str


class ReadinessResponse(BaseModel):
    """Readiness payload returned when all dependencies are healthy."""

    status: str
    service: str
    dependencies: list[DependencyHealth]


@router.get("/health", include_in_schema=False)
@router.get("/health/liveliness", include_in_schema=False)
async def liveness_check() -> dict[str, str]:
    """Lightweight liveness probe that never touches external dependencies."""

    return {"status": "ok", "service": SERVICE_NAME}


@router.get("/health/readiness", include_in_schema=False, response_model=None)
async def readiness_check(
    settings: Settings = Depends(get_settings),
) -> ReadinessResponse | JSONResponse:
    """Readiness probe that verifies MySQL and Redis connectivity."""

    try:
        results = await check_all(settings)
        dependencies = [
            DependencyHealth(
                name=result.name,
                ok=result.ok,
                latency_ms=result.latency_ms,
                detail=result.detail,
            )
            for result in results
        ]
    except Exception as exc:  # Health checks must remain stable and non-sensitive.
        dependencies = [
            DependencyHealth(
                name="readiness",
                ok=False,
                latency_ms=None,
                detail=type(exc).__name__,
            )
        ]

    is_ready = all(item.ok for item in dependencies)
    payload = ReadinessResponse(
        status="ok" if is_ready else "degraded",
        service=SERVICE_NAME,
        dependencies=dependencies,
    )
    if not is_ready:
        return JSONResponse(status_code=503, content=payload.model_dump())
    return payload
