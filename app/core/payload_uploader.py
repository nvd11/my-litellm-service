"""LiteLLM Asynchronous Payload (Prompt/Response) Offloading Module via PayloadBackend."""

import logging
from datetime import UTC, datetime
from typing import Any

from app.core.backends.factory import get_payload_backend
from app.core.config import Settings, get_settings
from app.core.payload_backend import PayloadBackend

logger = logging.getLogger(__name__)


def _to_serializable(obj: Any) -> Any:
    """Recursively convert objects to JSON-serializable structures."""
    if hasattr(obj, "model_dump"):
        try:
            return _to_serializable(obj.model_dump())
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return _to_serializable(obj.dict())
        except Exception:
            pass
    if isinstance(obj, dict):
        return {str(k): _to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_to_serializable(item) for item in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, Exception):
        return {"error_type": type(obj).__name__, "message": str(obj)}
    if hasattr(obj, "__str__") and not isinstance(obj, (int, float, bool, type(None))):
        return str(obj)
    return obj


def _json_default(obj: Any) -> Any:
    """Fallback serializer for any objects that evade recursive serialization."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if isinstance(obj, Exception):
        return {"error_type": type(obj).__name__, "message": str(obj)}
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    return str(obj)


def _extract_text_content(content: Any) -> str:
    """Extract human-readable plain text from strings, lists of multimodal blocks, or dicts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item["text"]))
                elif "content" in item:
                    parts.append(_extract_text_content(item["content"]))
                else:
                    parts.append(str(item))
            else:
                parts.append(str(item))
        return "\n".join(p for p in parts if p)
    if isinstance(content, dict):
        if "text" in content:
            return str(content["text"])
        return str(content)
    return str(content)


def extract_prompt_payload(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Extract and structure clear, readable request prompt details."""
    raw_messages = kwargs.get("messages") or []
    cleaned_messages = _to_serializable(raw_messages)

    system_prompts: list[str] = []
    user_prompts: list[str] = []

    if isinstance(cleaned_messages, list):
        for msg in cleaned_messages:
            if isinstance(msg, dict):
                role = str(msg.get("role", "")).lower()
                content = msg.get("content")
                if content is not None:
                    text_val = _extract_text_content(content)
                    if role == "system":
                        system_prompts.append(text_val)
                    elif role == "user":
                        user_prompts.append(text_val)

    system_prompt = "\n\n".join(system_prompts) if system_prompts else None
    latest_user_prompt = user_prompts[-1] if user_prompts else None

    # 提取调用参数 (temperature, max_tokens, stream 等)
    opt_params = kwargs.get("optional_params") or {}
    params = {
        "temperature": opt_params.get("temperature") or kwargs.get("temperature"),
        "max_tokens": opt_params.get("max_tokens") or kwargs.get("max_tokens"),
        "stream": opt_params.get("stream", False),
        "top_p": opt_params.get("top_p"),
    }
    # 过滤 None
    clean_params = {k: v for k, v in params.items() if v is not None}

    return {
        "model": kwargs.get("model") or "unknown",
        "system_prompt": system_prompt,
        "user_prompt": latest_user_prompt,
        "messages": cleaned_messages,
        "parameters": clean_params,
        "tools": _to_serializable(kwargs.get("tools")),
    }


def extract_response_payload(response_obj: Any) -> dict[str, Any]:
    """Extract and structure clean, readable response details from LiteLLM response."""
    if response_obj is None:
        return {"reply": None, "error": "Response object is None"}

    if isinstance(response_obj, Exception):
        return {
            "reply": None,
            "error": {
                "type": type(response_obj).__name__,
                "message": str(response_obj),
            },
        }

    raw = _to_serializable(response_obj)
    if not isinstance(raw, dict):
        return {"reply": str(raw)}

    # 提取 choices
    choices = raw.get("choices") or []
    first_choice = choices[0] if isinstance(choices, list) and choices else {}

    reply_content = None
    reasoning_content = None
    tool_calls = None
    finish_reason = None

    if isinstance(first_choice, dict):
        finish_reason = first_choice.get("finish_reason")
        msg = first_choice.get("message")
        if isinstance(msg, dict):
            reply_content = _extract_text_content(msg.get("content"))
            reasoning_content = _extract_text_content(msg.get("reasoning_content"))
            tool_calls = msg.get("tool_calls")
        elif first_choice.get("text"):
            reply_content = _extract_text_content(first_choice.get("text"))

    usage = raw.get("usage")

    return {
        "model": raw.get("model"),
        "reply": reply_content,
        "reasoning_content": reasoning_content,
        "tool_calls": tool_calls,
        "finish_reason": finish_reason,
        "usage": usage,
    }


async def async_upload_payload(
    request_id: str,
    kwargs: dict[str, Any],
    response_obj: Any,
    start_time: datetime | None = None,
    settings: Settings | None = None,
    backend: PayloadBackend | None = None,
) -> None:
    """Asynchronously upload Prompt and Response payloads via configured PayloadBackend.

    This function isolates all exceptions to ensure payload storage failures never impact
    primary proxy responses or database metric persistence.
    """
    if not request_id or not str(request_id).strip():
        return

    try:
        resolved_settings = settings or get_settings()
        if not resolved_settings.enable_payload_offload:
            return

        date_ref = start_time or datetime.now(UTC)
        if date_ref.tzinfo is None:
            date_ref = date_ref.replace(tzinfo=UTC)
        date_str = date_ref.strftime("%Y-%m-%d")

        prompt_dict = extract_prompt_payload(kwargs)
        response_dict = extract_response_payload(response_obj)

        # 构建元数据
        metadata = {
            "date": date_str,
            "timestamp": date_ref.isoformat(),
            "model": kwargs.get("model") or "unknown",
            "key_alias": kwargs.get("user_api_key_alias") or kwargs.get("key_alias") or "default",
            "status_code": 200 if not isinstance(response_obj, Exception) else 500,
            "latency_ms": 0,  # 由调用方填充
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "spend": 0.0,
        }

        # 获取后端实例
        payload_backend = backend or get_payload_backend(resolved_settings)

        # 写入 payload
        success = await payload_backend.write_payload(
            request_id=request_id,
            prompt=prompt_dict,
            response=response_dict,
            metadata=metadata,
        )

        if success:
            logger.debug("Successfully uploaded payload for request %s", request_id)
        else:
            logger.warning("Failed to upload payload for request %s", request_id)
    except Exception as exc:
        # Full exception boundary isolation
        logger.warning("Failed to async upload payload for %s: %s", request_id, exc)
