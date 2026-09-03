"""LiteLLM Observatory API Router Package."""

from fastapi import APIRouter

from app.api.logs import router as logs_router
from app.api.metrics import router as metrics_router
from app.api.payload import router as payload_router

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(logs_router)
api_router.include_router(metrics_router)
api_router.include_router(payload_router)
