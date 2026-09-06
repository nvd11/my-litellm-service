"""LiteLLM Payload Direct Proxy & Inspection API Module."""

import json
import logging
from datetime import UTC, date, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.core.backends.factory import get_payload_backend
from app.core.config import Settings, get_settings
from app.core.payload_backend import PayloadBackend
from app.db import get_async_engine, llm_request_logs

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Payloads"])


class PayloadInspectionResponse(BaseModel):
    """Structured inspection data for a single LLM request."""

    request_id: str
    date: str
    prompt: dict[str, Any]
    response: dict[str, Any]
    prompt_url: str
    response_url: str


@router.get("/logs/{request_id}/payload", response_model=PayloadInspectionResponse)
async def get_request_payload(
    request_id: str,
    target_date: date | None = Query(
        None, alias="date", description="Date of the request (YYYY-MM-DD)"
    ),
    full: bool = Query(False, description="Whether to load full messages without truncation"),
    settings: Settings = Depends(get_settings),
    backend: PayloadBackend = Depends(get_payload_backend),
) -> Any:
    """Fetch structured Prompt and Response payloads via configured PayloadBackend.

    When multiple shards exist in VictoriaLogs for an oversized payload, the backend
    dynamically reassembles them in ascending shard_index order back into the full document.
    """
    date_str: str | None = None

    # 1. 优先从 MySQL 查询该 request_id 实际落库时的 UTC 日期分区
    try:
        engine = get_async_engine(settings)
        stmt = (
            select(llm_request_logs.c.created_at)
            .where(llm_request_logs.c.request_id == request_id)
            .limit(1)
        )
        async with engine.connect() as conn:
            result = await conn.execute(stmt)
            created_dt = result.scalar_one_or_none()
            if created_dt and isinstance(created_dt, datetime):
                date_str = created_dt.strftime("%Y-%m-%d")
    except Exception as db_err:
        logger.debug("Could not query created_at from MySQL for payload %s: %s", request_id, db_err)

    # 2. 兜底回退至传入日期或当前日期
    if not date_str:
        if target_date:
            date_str = target_date.strftime("%Y-%m-%d")
        else:
            date_str = datetime.now(UTC).strftime("%Y-%m-%d")

    # 3. 通过抽象基类读取 payload (VictoriaLogs 内部自动完成多分片重组还原)
    prompt_data, response_data = await backend.read_payload(request_id, date_str)

    # 容错：如果后端返回的是字符串则安全解析为字典
    if isinstance(prompt_data, str):
        try:
            prompt_data = json.loads(prompt_data)
        except Exception:
            prompt_data = {"user_prompt": prompt_data}
    if isinstance(response_data, str):
        try:
            response_data = json.loads(response_data)
        except Exception:
            response_data = {"reply": response_data}

    # 4. 构建公开访问 URL（仅 MinIO 后端适用）
    base_url = settings.payload_public_base_url.rstrip("/")
    prefix = f"{date_str}/{request_id}"
    prompt_url = f"{base_url}/{prefix}/prompt.json"
    response_url = f"{base_url}/{prefix}/response.json"

    # 5. 对超长多轮对话 (>30 条消息) 做智能轻量化抽样
    messages = prompt_data.get("messages")
    if isinstance(messages, list) and len(messages) > 30 and not full:
        total_count = len(messages)
        prompt_data["total_messages_count"] = total_count
        prompt_data["is_truncated"] = True
        notice_msg = {
            "role": "system",
            "content": (
                f"（... 中间已自动智能折叠 {total_count - 25} 条历史问答，"
                "点击下方「加载全部消息」可获取全量上下文 ...）"
            ),
        }
        prompt_data["messages"] = messages[:5] + [notice_msg] + messages[-20:]

    return PayloadInspectionResponse(
        request_id=request_id,
        date=date_str,
        prompt=prompt_data,
        response=response_data,
        prompt_url=prompt_url,
        response_url=response_url,
    )
