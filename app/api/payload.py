"""LiteLLM S3 Payload Direct Proxy & Inspection API Module."""

import json
import logging
from datetime import date, datetime
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
    settings: Settings = Depends(get_settings),
) -> Any:
    """Fetch structured Prompt and Response payloads directly from NUC MinIO S3."""
    date_str: str | None = target_date.strftime("%Y-%m-%d") if target_date else None

    # 1. 若未传入日期，尝试从 MySQL 查询该 request_id 的 created_at
    if not date_str:
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

    # 2. 默认退化为今日日期
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

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

    return PayloadInspectionResponse(
        request_id=request_id,
        date=date_str,
        prompt=prompt_data,
        response=response_data,
        prompt_url=prompt_url,
        response_url=response_url,
    )
