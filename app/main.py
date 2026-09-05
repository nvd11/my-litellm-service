"""LiteLLM Observatory FastAPI Application Entrypoint."""

import logging
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import api_router
from app.api.health import router as health_router
from app.core.config import parse_csv_origins

logger = logging.getLogger("app.main")

app = FastAPI(
    title="LiteLLM Observatory API & Dashboard",
    description="Enterprise observability metrics, audit logs, and payload inspection for LiteLLM.",
    version="1.0.0",
)

# 开启 Gzip 传输压缩，大幅减少网络传输耗时
app.add_middleware(GZipMiddleware, minimum_size=1000)

# 配置 CORS：默认仅放行本地 Vite 调试端口，生产可通过 DASHBOARD_ALLOWED_ORIGINS 覆盖。
DEFAULT_DASHBOARD_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
]
dashboard_allowed_origins = parse_csv_origins(
    os.getenv("DASHBOARD_ALLOWED_ORIGINS"),
    DEFAULT_DASHBOARD_ORIGINS,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=dashboard_allowed_origins,
    allow_credentials="*" not in dashboard_allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 注册健康检查与业务 API 路由
app.include_router(health_router)
app.include_router(api_router)

# 静态资源挂载与 SPA 路由兜底
static_dir = Path(__file__).resolve().parent / "static"
if not static_dir.exists():
    static_dir.mkdir(parents=True, exist_ok=True)

# 挂载静态静态文件目录
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/dashboard/{full_path:path}", include_in_schema=False)
@app.get("/dashboard", include_in_schema=False)
@app.get("/", include_in_schema=False)
async def serve_dashboard(request: Request, full_path: str = "") -> FileResponse:
    """Serve the React SPA dashboard index.html for all dashboard sub-routes."""
    index_file = static_dir / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return JSONResponse(
        status_code=404,
        content={"error": "Dashboard UI static bundle not found."},
    )
