"""Unit tests for asynchronous S3 payload uploader module."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.core.payload_uploader import (
    async_upload_payload,
    extract_prompt_payload,
    extract_response_payload,
)


@pytest.fixture
def test_settings() -> Settings:
    """Provide a valid Settings instance for testing payload offload."""
    return Settings(
        mysql_host="127.0.0.1",
        mysql_user="root",
        mysql_password=SecretStr("pass"),
        mysql_db="test_db",
        redis_host="127.0.0.1",
        redis_password=SecretStr("redis_pass"),
        openai_api_key_free_1=SecretStr("key1"),
        litellm_master_key=SecretStr("sk-master"),
        enable_payload_offload=True,
        payload_s3_endpoint="http://127.0.0.1:9000",
        payload_s3_access_key="admin",
        payload_s3_secret_key=SecretStr("secret123"),
        payload_bucket_name="litellm-payloads",
        payload_upload_timeout_seconds=1.0,
    )


def test_extract_prompt_payload() -> None:
    """Test serializing prompt parameters and messages."""
    kwargs = {
        "model": "gemini-3.7-flash",
        "messages": [
            {"role": "system", "content": "You are Cindy."},
            {"role": "user", "content": "Hello!"},
        ],
        "optional_params": {"temperature": 0.7},
        "tools": [{"type": "function", "function": {"name": "test_tool"}}],
    }

    result = extract_prompt_payload(kwargs)
    assert result["model"] == "gemini-3.7-flash"
    assert result["system_prompt"] == "You are Cindy."
    assert result["user_prompt"] == "Hello!"
    assert len(result["messages"]) == 2
    assert result["parameters"]["temperature"] == 0.7
    assert result["tools"][0]["function"]["name"] == "test_tool"


def test_extract_response_payload() -> None:
    """Test extracting responses from dictionaries, models, and exceptions."""
    # 1. Dict
    res_dict = {"choices": [{"message": {"content": "Hi Boss"}}], "usage": {"total_tokens": 42}}
    assert extract_response_payload(res_dict)["reply"] == "Hi Boss"
    assert extract_response_payload(res_dict)["usage"]["total_tokens"] == 42

    # 2. Mock model_dump object
    mock_model = MagicMock()
    mock_model.model_dump.return_value = {"choices": [{"message": {"content": "model output"}}]}
    del mock_model.dict  # Ensure model_dump is picked
    assert extract_response_payload(mock_model)["reply"] == "model output"

    # 3. Exception
    exc = ValueError("Simulated upstream error")
    err_res = extract_response_payload(exc)
    assert err_res["error"]["type"] == "ValueError"
    assert "Simulated upstream error" in err_res["error"]["message"]


@pytest.mark.asyncio
async def test_async_upload_payload_disabled(test_settings: Settings) -> None:
    """When enable_payload_offload is False, it should exit early without calling S3."""
    test_settings.enable_payload_offload = False
    with patch("aioboto3.Session") as mock_session_cls:
        await async_upload_payload(
            request_id="req-123",
            kwargs={"messages": []},
            response_obj={},
            settings=test_settings,
        )
        mock_session_cls.assert_not_called()


@pytest.mark.asyncio
async def test_async_upload_payload_empty_id(test_settings: Settings) -> None:
    """When request_id is empty or None, it should do nothing."""
    with patch("aioboto3.Session") as mock_session_cls:
        await async_upload_payload(
            request_id="",
            kwargs={"messages": []},
            response_obj={},
            settings=test_settings,
        )
        mock_session_cls.assert_not_called()


@pytest.mark.asyncio
async def test_async_upload_payload_success(test_settings: Settings) -> None:
    """Verify S3 put_object is called twice for prompt and response."""
    mock_s3_client = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_s3_client
    mock_context.__aexit__.return_value = None

    mock_session = MagicMock()
    mock_session.client.return_value = mock_context

    with patch("aioboto3.Session", return_value=mock_session):
        start_time = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)
        await async_upload_payload(
            request_id="req-test-uuid",
            kwargs={"model": "gemini-3.7-flash", "messages": [{"role": "user", "content": "ping"}]},
            response_obj={"choices": [{"message": {"content": "pong"}}]},
            start_time=start_time,
            settings=test_settings,
        )

        assert mock_s3_client.put_object.call_count == 2
        calls = mock_s3_client.put_object.call_args_list

        # Call 1: prompt.json
        prompt_call = calls[0].kwargs
        assert prompt_call["Bucket"] == "litellm-payloads"
        assert prompt_call["Key"] == "2026-09-02/req-test-uuid/prompt.json"
        assert b"ping" in prompt_call["Body"]

        # Call 2: response.json
        resp_call = calls[1].kwargs
        assert resp_call["Bucket"] == "litellm-payloads"
        assert resp_call["Key"] == "2026-09-02/req-test-uuid/response.json"
        assert b"pong" in resp_call["Body"]


@pytest.mark.asyncio
async def test_async_upload_payload_with_nested_datetime(test_settings: Settings) -> None:
    """Ensure complex LiteLLM kwargs with nested datetimes serialize cleanly without error."""
    mock_s3_client = AsyncMock()
    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_s3_client
    mock_context.__aexit__.return_value = None

    mock_session = MagicMock()
    mock_session.client.return_value = mock_context

    with patch("aioboto3.Session", return_value=mock_session):
        await async_upload_payload(
            request_id="req-datetime-test",
            kwargs={
                "model": "gemini-3.7-flash",
                "litellm_params": {
                    "arrival_time": datetime.now(UTC),
                    "nested_dates": [datetime(2026, 9, 3, 15, 0, 0, tzinfo=UTC)],
                },
                "messages": [{"role": "user", "content": "hello"}],
            },
            response_obj={"created_at": datetime.now(UTC)},
            settings=test_settings,
        )
        assert mock_s3_client.put_object.call_count == 2


@pytest.mark.asyncio
async def test_async_upload_payload_exception_isolated(test_settings: Settings) -> None:
    """Ensure upload exceptions (e.g. timeout / network down) are caught and isolated."""
    mock_session = MagicMock()
    mock_session.client.side_effect = RuntimeError("S3 endpoint down")

    with patch("aioboto3.Session", return_value=mock_session):
        # Should not raise exception
        await async_upload_payload(
            request_id="req-fail-test",
            kwargs={"messages": []},
            response_obj={},
            settings=test_settings,
        )


@pytest.mark.asyncio
async def test_async_upload_payload_redis_write_through(test_settings: Settings) -> None:
    """Ensure newly generated requests immediately write-through to Redis with 3 days TTL."""
    import json
    mock_redis = AsyncMock()
    mock_backend = AsyncMock()
    mock_backend.write_payload = AsyncMock(return_value=True)

    with patch("app.core.payload_uploader.get_redis_client", return_value=mock_redis):
        await async_upload_payload(
            request_id="req-new-3days-ttl",
            kwargs={"model": "gemini-3.8-flash", "messages": [{"role": "user", "content": "hello"}]},
            response_obj={"choices": [{"message": {"content": "world"}}]},
            settings=test_settings,
            backend=mock_backend,
        )

    # 验证 redis.set 在落盘前已被即时调用，且经过 Gzip Level 1 极速压缩与 Base64 编码，TTL 为 3 天
    mock_redis.set.assert_called_once()
    args, kwargs = mock_redis.set.call_args
    assert args[0] == "litellm:payload:req-new-3days-ttl"
    # Gzip 压缩后的 Base64 字符串以 H4sI 开头
    assert isinstance(args[1], str)
    assert args[1].startswith("H4sI")

    import base64
    import gzip
    decompressed = gzip.decompress(base64.b64decode(args[1])).decode("utf-8")
    data = json.loads(decompressed)
    assert data["prompt"]["user_prompt"] == "hello"
    assert data["response"]["reply"] == "world"
    assert kwargs["ex"] == 86400 * 3


@pytest.mark.asyncio
async def test_async_upload_payload_folds_base64_in_redis_cache(test_settings: Settings) -> None:
    """验证写入 Redis 缓存时，超大 Base64 图片被折叠以防污染缓存，而冷存储保留全量."""
    import base64
    import gzip
    import json
    mock_redis = AsyncMock()
    mock_backend = AsyncMock()
    mock_backend.write_payload = AsyncMock(return_value=True)

    huge_img = "data:image/png;base64," + ("ABCD" * 1000)
    messages = [
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": huge_img}}]}
    ]

    with patch("app.core.payload_uploader.get_redis_client", return_value=mock_redis):
        await async_upload_payload(
            request_id="req-fold-img-test",
            kwargs={"model": "gemini-3.8-flash", "messages": messages},
            response_obj={"choices": [{"message": {"content": "see image"}}]},
            settings=test_settings,
            backend=mock_backend,
        )

    # 1. 验证写入 Redis 的内容中图片已被折叠
    mock_redis.set.assert_called_once()
    args, _ = mock_redis.set.call_args
    decompressed = gzip.decompress(base64.b64decode(args[1])).decode("utf-8")
    cached_data = json.loads(decompressed)
    cached_img_url = cached_data["prompt"]["messages"][0]["content"][0]["image_url"]["url"]
    assert len(cached_img_url) < len(huge_img)
    assert "Base64 image folded for L2 cache" in cached_img_url

    # 2. 验证冷存储 write_payload 接收到的仍是完整未折叠数据
    mock_backend.write_payload.assert_called_once()
    call_kwargs = mock_backend.write_payload.call_args[1]
    backend_img_url = call_kwargs["prompt"]["messages"][0]["content"][0]["image_url"]["url"]
    assert backend_img_url == huge_img
