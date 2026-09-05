"""LiteLLM S3 Payload Direct Proxy & Inspection API Module."""

import json
import logging
from datetime import UTC, date, datetime
from typing import Any

import aioboto3
from botocore.config import Config
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select

from app.core.config import Settings, get_settings
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
) -> Any:
    """Fetch structured Prompt and Response payloads directly from NUC MinIO S3."""
    date_str: str | None = None

    # 1. 优先从 MySQL 查询该 request_id 实际落库时的 UTC 日期分区 (对齐 S3 物理路径)
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

    prefix = f"{date_str}/{request_id}"
    base_url = settings.payload_public_base_url.rstrip("/")
    prompt_url = f"{base_url}/{prefix}/prompt.json"
    response_url = f"{base_url}/{prefix}/response.json"

    session = aioboto3.Session()
    boto_config = Config(
        connect_timeout=5.0,
        read_timeout=5.0,
        retries={"max_attempts": 2},
    )

    prompt_data: dict[str, Any] = {}
    response_data: dict[str, Any] = {}

    try:
        async with session.client(
            "s3",
            endpoint_url=settings.payload_s3_endpoint,
            aws_access_key_id=settings.payload_s3_access_key,
            aws_secret_access_key=settings.payload_s3_secret_key.get_secret_value(),
            config=boto_config,
        ) as s3_client:
            # 尝试拉取 prompt.json
            try:
                prompt_obj = await s3_client.get_object(
                    Bucket=settings.payload_bucket_name,
                    Key=f"{prefix}/prompt.json",
                )
                prompt_bytes = await prompt_obj["Body"].read()
                prompt_data = json.loads(prompt_bytes.decode("utf-8"))
            except Exception as e:
                logger.debug("Prompt payload not found in S3 for %s: %s", request_id, e)
                prompt_data = {"user_prompt": "（此历史调用的原始输入报文未在 MinIO 归档）"}

            # 尝试拉取 response.json
            try:
                resp_obj = await s3_client.get_object(
                    Bucket=settings.payload_bucket_name,
                    Key=f"{prefix}/response.json",
                )
                resp_bytes = await resp_obj["Body"].read()
                response_data = json.loads(resp_bytes.decode("utf-8"))
            except Exception as e:
                logger.debug("Response payload not found in S3 for %s: %s", request_id, e)
                response_data = {"reply": "（此历史调用的原始模型回复未在 MinIO 归档）"}
    except Exception as exc:
        logger.warning("Failed to connect to S3 to read payload for %s: %s", request_id, exc)
        prompt_data = {"user_prompt": f"（S3 存储节点响应超时或暂时离线: {exc}）"}
        response_data = {"reply": "（无法从 NUC MinIO 读取回复报文）"}

    # 对超长多轮对话 (>30 条消息) 做智能轻量化抽样，缩短 99% 的网络传输耗时实现毫秒级秒开
    messages = prompt_data.get("messages")
    if isinstance(messages, list) and len(messages) > 30 and not full:
        total_count = len(messages)
        prompt_data["total_messages_count"] = total_count
        prompt_data["is_truncated"] = True
        notice_msg = {
            "role": "system",
            "content": (
                f"（... 中间已自动智能折叠 {total_count - 25} 条历史问答，"
                "点击下方“加载全部消息”可获取全量上下文 ...）"
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
