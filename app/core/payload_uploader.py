"""LiteLLM Asynchronous Payload (Prompt/Response) Offloading Module via S3 API."""

import json
import logging
from datetime import UTC, datetime
from typing import Any

import aioboto3
from botocore.config import Config

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


def _to_serializable(obj: Any) -> Any:
    """Recursively convert objects to JSON-serializable structures."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if isinstance(obj, dict):
        return {k: _to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [_to_serializable(item) for item in obj]
    if isinstance(obj, (datetime,)):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, Exception):
        return {"error_type": type(obj).__name__, "message": str(obj)}
    if hasattr(obj, "__str__") and not isinstance(obj, (int, float, bool, type(None))):
        return str(obj)
    return obj


def extract_prompt_payload(kwargs: dict[str, Any]) -> dict[str, Any]:
    """Extract and structure request prompt details from LiteLLM hook kwargs."""
    return {
        "model": kwargs.get("model"),
        "messages": _to_serializable(kwargs.get("messages", [])),
        "optional_params": _to_serializable(kwargs.get("optional_params", {})),
        "litellm_params": _to_serializable(kwargs.get("litellm_params", {})),
        "tools": _to_serializable(kwargs.get("tools")),
        "functions": _to_serializable(kwargs.get("functions")),
    }


def extract_response_payload(response_obj: Any) -> dict[str, Any]:
    """Extract and structure response details from LiteLLM response object or error."""
    if response_obj is None:
        return {}
    if isinstance(response_obj, Exception):
        return {
            "error": {
                "type": type(response_obj).__name__,
                "message": str(response_obj),
            }
        }
    serialized = _to_serializable(response_obj)
    if isinstance(serialized, dict):
        return serialized
    return {"raw_response": str(serialized)}


async def async_upload_payload(
    request_id: str,
    kwargs: dict[str, Any],
    response_obj: Any,
    start_time: datetime | None = None,
    settings: Settings | None = None,
) -> None:
    """Asynchronously upload Prompt and Response payloads to the S3 / MinIO storage endpoint.

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
        key_prefix = f"{date_str}/{request_id}"

        prompt_dict = extract_prompt_payload(kwargs)
        response_dict = extract_response_payload(response_obj)

        prompt_bytes = json.dumps(prompt_dict, ensure_ascii=False, indent=2).encode("utf-8")
        response_bytes = json.dumps(response_dict, ensure_ascii=False, indent=2).encode("utf-8")

        session = aioboto3.Session()
        boto_config = Config(
            connect_timeout=resolved_settings.payload_upload_timeout_seconds,
            read_timeout=resolved_settings.payload_upload_timeout_seconds,
            retries={"max_attempts": 2},
        )

        secret_key_str = resolved_settings.payload_s3_secret_key.get_secret_value()

        async with session.client(
            "s3",
            endpoint_url=resolved_settings.payload_s3_endpoint,
            aws_access_key_id=resolved_settings.payload_s3_access_key,
            aws_secret_access_key=secret_key_str,
            config=boto_config,
        ) as s3_client:
            await s3_client.put_object(
                Bucket=resolved_settings.payload_bucket_name,
                Key=f"{key_prefix}/prompt.json",
                Body=prompt_bytes,
                ContentType="application/json; charset=utf-8",
            )
            await s3_client.put_object(
                Bucket=resolved_settings.payload_bucket_name,
                Key=f"{key_prefix}/response.json",
                Body=response_bytes,
                ContentType="application/json; charset=utf-8",
            )

        logger.debug("Successfully uploaded payload for request %s to S3", request_id)
    except Exception as exc:
        # Full exception boundary isolation
        logger.warning("Failed to async upload payload for %s to S3: %s", request_id, exc)
