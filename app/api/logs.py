"""LiteLLM Audit Logs Query API Module."""

from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, or_, select

from app.core.config import Settings, get_settings
from app.db import get_async_engine, llm_request_logs

router = APIRouter(tags=["Audit Logs"])


class LogItem(BaseModel):
    """Structured audit log entry with dynamic S3 payload hyperlinks."""

    id: str
    request_id: str
    api_key_alias: str
    model_requested: str
    model_used: str
    provider: str
    provider_key_alias: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    cost_cny: float
    fx_rate: float
    latency_ms: int
    status_code: int
    created_at: datetime
    prompt_url: str
    response_url: str


class PaginatedLogsResponse(BaseModel):
    """Paginated response containing list of logs and pagination metadata."""

    items: list[LogItem]
    total: int
    page: int
    page_size: int
    total_pages: int


@router.get("/logs", response_model=PaginatedLogsResponse)
async def list_audit_logs(
    page: int = Query(1, ge=1, description="Page number starting from 1"),
    page_size: int = Query(20, ge=1, le=100, description="Number of items per page"),
    start_date: date | None = Query(None, description="Start date filter (YYYY-MM-DD)"),
    end_date: date | None = Query(None, description="End date filter (YYYY-MM-DD)"),
    api_key_alias: str | None = Query(None, description="Filter by client API key alias"),
    model_used: str | None = Query(None, description="Filter by actual model used"),
    status_code: int | None = Query(None, description="Filter by HTTP status code"),
    search: str | None = Query(None, description="Keyword search in request_id or key alias"),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Retrieve paginated audit logs with multi-condition filters and S3 hyperlinks."""
    engine = get_async_engine(settings)

    # 1. 构造过滤条件列表
    conditions: list[Any] = []

    if start_date:
        start_min = datetime.combine(start_date, datetime.min.time())
        conditions.append(llm_request_logs.c.created_at >= start_min)
    if end_date:
        end_max = datetime.combine(end_date, datetime.max.time())
        conditions.append(llm_request_logs.c.created_at <= end_max)
    if api_key_alias and api_key_alias.strip():
        conditions.append(llm_request_logs.c.api_key_alias == api_key_alias.strip())
    if model_used and model_used.strip():
        conditions.append(llm_request_logs.c.model_used == model_used.strip())
    if status_code is not None:
        conditions.append(llm_request_logs.c.status_code == status_code)
    if search and search.strip():
        keyword = f"%{search.strip()}%"
        conditions.append(
            or_(
                llm_request_logs.c.request_id.like(keyword),
                llm_request_logs.c.api_key_alias.like(keyword),
                llm_request_logs.c.model_used.like(keyword),
            )
        )

    # 2. 查询总记录数
    count_stmt = select(func.count()).select_from(llm_request_logs)
    if conditions:
        count_stmt = count_stmt.where(*conditions)

    # 3. 构造分页数据查询
    offset = (page - 1) * page_size
    data_stmt = (
        select(llm_request_logs)
        .where(*conditions)
        .order_by(desc(llm_request_logs.c.created_at))
        .offset(offset)
        .limit(page_size)
    )

    async with engine.connect() as conn:
        total_result = await conn.execute(count_stmt)
        total_count = total_result.scalar_one_or_none() or 0

        data_result = await conn.execute(data_stmt)
        rows = data_result.fetchall()

    # 4. 组装结果与动态超链接
    base_url = settings.payload_public_base_url.rstrip("/")
    items: list[LogItem] = []

    for row in rows:
        created_dt: datetime = row.created_at
        date_str = created_dt.strftime("%Y-%m-%d")
        req_id = row.request_id

        prompt_url = f"{base_url}/{date_str}/{req_id}/prompt.json"
        response_url = f"{base_url}/{date_str}/{req_id}/response.json"

        items.append(
            LogItem(
                id=str(row.id),
                request_id=str(row.request_id),
                api_key_alias=str(row.api_key_alias),
                model_requested=str(row.model_requested),
                model_used=str(row.model_used),
                provider=str(row.provider),
                provider_key_alias=str(row.provider_key_alias),
                prompt_tokens=int(row.prompt_tokens),
                completion_tokens=int(row.completion_tokens),
                total_tokens=int(row.total_tokens),
                cost_usd=float(row.cost_usd),
                cost_cny=float(row.cost_cny),
                fx_rate=float(row.fx_rate),
                latency_ms=int(row.latency_ms),
                status_code=int(row.status_code),
                created_at=created_dt,
                prompt_url=prompt_url,
                response_url=response_url,
            )
        )

    total_pages = (total_count + page_size - 1) // page_size if total_count > 0 else 1

    return PaginatedLogsResponse(
        items=items,
        total=total_count,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )
