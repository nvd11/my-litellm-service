"""LiteLLM Observatory FastAPI Application Entrypoint."""

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import api_router

logger = logging.getLogger("app.main")

app = FastAPI(
    title="LiteLLM Observatory API & Dashboard",
    description="Enterprise observability metrics, audit logs, and payload inspection for LiteLLM.",
    version="1.0.0",
)

# 开启 Gzip 传输压缩，大幅减少网络传输耗时
app.add_middleware(GZipMiddleware, minimum_size=1000)

# 配置 CORS 允许跨域（本地开发时支持 Vite 本地调试端口）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health", tags=["Health"])
@app.get("/health/liveliness", tags=["Health"])
@app.get("/health/readiness", tags=["Health"])
@app.get("/api/v1/health", tags=["Health"])
async def health_check() -> dict[str, str]:
    """Health check endpoint for Kubernetes probes and monitoring."""
    return {"status": "ok", "service": "litellm-observability"}


# 注册业务 API 路由
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
