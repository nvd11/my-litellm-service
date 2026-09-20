"""LiteLLM Payload Direct Proxy & Inspection API Module."""

import base64
import gzip
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
from app.core.payload_uploader import _fold_base64_for_cache
from app.core.redis_client import get_redis_client
from app.db import get_async_engine, llm_request_logs

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Payloads"])

MAX_SINGLE_MESSAGE_CHARS: int = 5000
TRUNCATE_HEAD_CHARS: int = 2500
TRUNCATE_TAIL_CHARS: int = 1500


def _truncate_text_safely(
    text_val: str,
    max_chars: int = MAX_SINGLE_MESSAGE_CHARS,
    head_chars: int = TRUNCATE_HEAD_CHARS,
    tail_chars: int = TRUNCATE_TAIL_CHARS,
) -> tuple[str, bool]:
    """若单段文本超过字数上限，保留首尾关键上下文并注入明确的智能抽样折叠提示."""
    if len(text_val) <= max_chars:
        return text_val, False

    total_len = len(text_val)
    omitted_chars = total_len - head_chars - tail_chars
    truncated = (
        f"{text_val[:head_chars]}\n\n"
        f"（... 此处已自动智能抽样截断 {omitted_chars:,} 字符，"
        f"该消息单段总长 {total_len:,} 字符；"
        "点击下方「加载全量完整报文」可获取全部未截断内容 ...）\n\n"
        f"{text_val[-tail_chars:]}"
    )
    return truncated, True


def _truncate_content_recursively(content: Any) -> tuple[Any, bool]:
    """递归检查并安全截断超长字符串、Base64 图片或多模态结构中的大文本."""
    if isinstance(content, str):
        return _truncate_text_safely(content)

    if isinstance(content, dict):
        new_dict: dict[str, Any] = {}
        any_truncated = False
        for k, v in content.items():
            if k == "image_url":
                # 专门处理多模态图片 Base64 膨胀 (单张可达数兆字符)
                if isinstance(v, dict) and "url" in v and isinstance(v["url"], str):
                    url_val = v["url"]
                    if len(url_val) > 500 or url_val.startswith("data:image"):
                        any_truncated = True
                        new_dict[k] = {
                            "url": (
                                f"{url_val[:60]}... "
                                f"（此处已自动智能折叠 Base64 图片数据 {len(url_val):,} 字符；"
                                "点击下方「加载全量完整报文」可获取全部内容）"
                            )
                        }
                    else:
                        new_dict[k] = v
                elif isinstance(v, str) and (len(v) > 500 or v.startswith("data:image")):
                    any_truncated = True
                    new_dict[k] = (
                        f"{v[:60]}... "
                        f"（此处已自动智能折叠 Base64 图片数据 {len(v):,} 字符；"
                        "点击下方「加载全量完整报文」可获取全部内容）"
                    )
                else:
                    new_dict[k] = v
            elif isinstance(v, (dict, list, str)):
                new_v, tr = _truncate_content_recursively(v)
                if tr:
                    any_truncated = True
                new_dict[k] = new_v
            else:
                new_dict[k] = v
        return new_dict, any_truncated

    if isinstance(content, list):
        truncated_list: list[Any] = []
        any_truncated = False
        for item in content:
            if isinstance(item, (dict, list, str)):
                new_item, tr = _truncate_content_recursively(item)
                if tr:
                    any_truncated = True
                truncated_list.append(new_item)
            else:
                truncated_list.append(item)
        return truncated_list, any_truncated

    return content, False


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
    cache_key = f"litellm:payload:{request_id}"
    cached_prompt: dict[str, Any] | None = None
    cached_response: dict[str, Any] | None = None

    # 方案 1: 优先从 Redis L2 缓存中检索不可变 Payload (<1ms 极速直出，兼容 Gzip 压缩与历史明文)
    try:
        redis = get_redis_client(settings)
        cached_raw = await redis.get(cache_key)
        if cached_raw:
            if cached_raw.startswith("H4sI"):
                decompressed_str = gzip.decompress(base64.b64decode(cached_raw)).decode("utf-8")
                cached_obj = json.loads(decompressed_str)
            else:
                cached_obj = json.loads(cached_raw)
            if isinstance(cached_obj, dict):
                cached_prompt = cached_obj.get("prompt")
                cached_response = cached_obj.get("response")
                logger.debug("Redis payload cache hit for %s", request_id)
    except Exception as redis_err:
        logger.debug("Redis payload cache read failed for %s: %s", request_id, redis_err)

    if cached_prompt is not None and cached_response is not None:
        prompt_data = cached_prompt
        response_data = cached_response
        date_str = target_date.strftime("%Y-%m-%d") if target_date else datetime.now(UTC).strftime("%Y-%m-%d")
    else:
        date_str = None

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

        # 方案 1 & 4: 查得结果后，折叠超大图片并以 Gzip Level 1 压缩回填至 Redis L2 缓存 (TTL: 3天)
        if prompt_data or response_data:
            try:
                redis = get_redis_client(settings)
                cache_prompt = _fold_base64_for_cache(prompt_data)
                cache_response = _fold_base64_for_cache(response_data)
                raw_json = json.dumps(
                    {"prompt": cache_prompt, "response": cache_response},
                    ensure_ascii=False,
                )
                compressed_bytes = gzip.compress(raw_json.encode("utf-8"), compresslevel=1)
                cached_val = base64.b64encode(compressed_bytes).decode("ascii")
                await redis.set(
                    cache_key,
                    cached_val,
                    ex=86400 * 3,
                )
                logger.debug("Populated compressed Redis payload cache for %s", request_id)
            except Exception as cache_err:
                logger.debug("Could not populate Redis payload cache for %s: %s", request_id, cache_err)

    # 4. 构建公开访问 URL（仅 MinIO 后端适用）
    base_url = settings.payload_public_base_url.rstrip("/")
    prefix = f"{date_str}/{request_id}"
    prompt_url = f"{base_url}/{prefix}/prompt.json"
    response_url = f"{base_url}/{prefix}/response.json"

    # 5. 对超长多轮对话 (>30 条消息) 或单条巨型消息做智能轻量化抽样 (full=False 时触发)
    if not full and isinstance(prompt_data, dict):
        messages = prompt_data.get("messages")
        has_truncation = False

        if isinstance(messages, list):
            total_count = len(messages)

            # A. 多轮条数抽样：>30 条时保留前 5 条与后 20 条
            if total_count > 30:
                has_truncation = True
                prompt_data["total_messages_count"] = total_count
                notice_msg = {
                    "role": "system",
                    "content": (
                        f"（... 中间已自动智能折叠 {total_count - 25} 条历史问答，"
                        "点击下方「加载全量完整报文」可获取全量上下文 ...）"
                    ),
                }
                messages = messages[:5] + [notice_msg] + messages[-20:]

            # B. 单条消息字符保护：防止前 5 条或后 20 条中混有数兆字符的超大 Prompt / Tool 日志
            safe_messages: list[dict[str, Any]] = []
            for msg in messages:
                if isinstance(msg, dict) and "content" in msg:
                    new_msg = dict(msg)
                    truncated_c, tr = _truncate_content_recursively(msg["content"])
                    if tr:
                        has_truncation = True
                    new_msg["content"] = truncated_c
                    safe_messages.append(new_msg)
                else:
                    safe_messages.append(msg)

            prompt_data["messages"] = safe_messages

            if has_truncation:
                prompt_data["is_truncated"] = True
                if "total_messages_count" not in prompt_data:
                    prompt_data["total_messages_count"] = total_count

        # C. 对顶层 user_prompt 卡片同样执行字数保护
        if "user_prompt" in prompt_data and isinstance(prompt_data["user_prompt"], str):
            truncated_up, tr = _truncate_text_safely(prompt_data["user_prompt"])
            if tr:
                prompt_data["user_prompt"] = truncated_up
                prompt_data["is_truncated"] = True

    return PayloadInspectionResponse(
        request_id=request_id,
        date=date_str,
        prompt=prompt_data,
        response=response_data,
        prompt_url=prompt_url,
        response_url=response_url,
    )
