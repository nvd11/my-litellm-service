"""LiteLLM Summary Metrics & Analytics API Module."""

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import desc, func, select

from app.core.config import Settings, get_settings
from app.db import get_async_engine, llm_request_logs

router = APIRouter(tags=["Metrics"])

# 业务基准时区：香港时间 (HKT, UTC+8)
HKT = timezone(timedelta(hours=8))


class ActiveKeyMetric(BaseModel):
    """Aggregate statistics for an individual API key alias."""

    alias: str
    count: int
    tokens: int
    cost_cny: float


class ModelBreakdownMetric(BaseModel):
    """Aggregate statistics per model."""

    model: str
    count: int
    tokens: int
    cost_cny: float


class SummaryMetricsResponse(BaseModel):
    """Daily overall metrics summary."""

    date: str
    today_requests: int
    today_tokens: int
    today_cost_cny: float
    today_cost_usd: float
    avg_latency_ms: int
    success_rate: float
    active_keys: list[ActiveKeyMetric]
    models_breakdown: list[ModelBreakdownMetric]


@router.get("/metrics/summary", response_model=SummaryMetricsResponse)
async def get_summary_metrics(
    target_date: date | None = Query(
        None, alias="date", description="Target date in HKT (default: today HKT)"
    ),
    settings: Settings = Depends(get_settings),
) -> Any:
    """Calculate daily summary statistics in HKT (requests, tokens, cost, latency, success rate)."""
    engine = get_async_engine(settings)
    eval_date = target_date or datetime.now(HKT).date()
    # 将 HKT 自然日 [00:00:00, 23:59:59.999999] 转换为底层 MySQL 存储的 UTC 时间区间 (-8小时)
    start_dt = datetime.combine(eval_date, datetime.min.time()) - timedelta(hours=8)
    end_dt = datetime.combine(eval_date, datetime.max.time()) - timedelta(hours=8)

    # 1. 基础日聚合指标查询
    summary_stmt = (
        select(
            func.count().label("total_requests"),
            func.sum(llm_request_logs.c.total_tokens).label("sum_tokens"),
            func.sum(llm_request_logs.c.cost_cny).label("sum_cost_cny"),
            func.sum(llm_request_logs.c.cost_usd).label("sum_cost_usd"),
            func.avg(llm_request_logs.c.latency_ms).label("avg_latency"),
            func.sum(func.if_(llm_request_logs.c.status_code == 200, 1, 0)).label("success_count"),
        )
        .where(llm_request_logs.c.created_at >= start_dt)
        .where(llm_request_logs.c.created_at <= end_dt)
    )

    # 2. Key 别名消耗排行查询
    keys_stmt = (
        select(
            llm_request_logs.c.api_key_alias,
            func.count().label("req_count"),
            func.sum(llm_request_logs.c.total_tokens).label("key_tokens"),
            func.sum(llm_request_logs.c.cost_cny).label("key_cost_cny"),
        )
        .where(llm_request_logs.c.created_at >= start_dt)
        .where(llm_request_logs.c.created_at <= end_dt)
        .group_by(llm_request_logs.c.api_key_alias)
        .order_by(desc("req_count"))
        .limit(10)
    )

    # 3. 模型调用占比查询
    models_stmt = (
        select(
            llm_request_logs.c.model_used,
            func.count().label("model_count"),
            func.sum(llm_request_logs.c.total_tokens).label("model_tokens"),
            func.sum(llm_request_logs.c.cost_cny).label("model_cost_cny"),
        )
        .where(llm_request_logs.c.created_at >= start_dt)
        .where(llm_request_logs.c.created_at <= end_dt)
        .group_by(llm_request_logs.c.model_used)
        .order_by(desc("model_count"))
        .limit(10)
    )

    async with engine.connect() as conn:
        sum_res = await conn.execute(summary_stmt)
        sum_row = sum_res.fetchone()

        keys_res = await conn.execute(keys_stmt)
        key_rows = keys_res.fetchall()

        models_res = await conn.execute(models_stmt)
        model_rows = models_res.fetchall()

    total_requests = int(sum_row.total_requests or 0) if sum_row else 0
    total_tokens = int(sum_row.sum_tokens or 0) if sum_row else 0
    total_cost_cny = round(float(sum_row.sum_cost_cny or 0.0), 4) if sum_row else 0.0
    total_cost_usd = round(float(sum_row.sum_cost_usd or 0.0), 4) if sum_row else 0.0
    avg_latency = int(round(float(sum_row.avg_latency or 0.0))) if sum_row else 0
    success_count = int(sum_row.success_count or 0) if sum_row else 0

    success_rate = round((success_count / total_requests) * 100, 2) if total_requests > 0 else 100.0

    active_keys = [
        ActiveKeyMetric(
            alias=str(r.api_key_alias),
            count=int(r.req_count),
            tokens=int(r.key_tokens or 0),
            cost_cny=round(float(r.key_cost_cny or 0.0), 4),
        )
        for r in key_rows
    ]

    models_breakdown = [
        ModelBreakdownMetric(
            model=str(r.model_used),
            count=int(r.model_count),
            tokens=int(r.model_tokens or 0),
            cost_cny=round(float(r.model_cost_cny or 0.0), 4),
        )
        for r in model_rows
    ]

    return SummaryMetricsResponse(
        date=eval_date.strftime("%Y-%m-%d"),
        today_requests=total_requests,
        today_tokens=total_tokens,
        today_cost_cny=total_cost_cny,
        today_cost_usd=total_cost_usd,
        avg_latency_ms=avg_latency,
        success_rate=success_rate,
        active_keys=active_keys,
        models_breakdown=models_breakdown,
    )
