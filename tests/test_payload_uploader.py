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
    assert len(result["messages"]) == 2
    assert result["optional_params"]["temperature"] == 0.7
    assert result["tools"][0]["function"]["name"] == "test_tool"


def test_extract_response_payload() -> None:
    """Test extracting responses from dictionaries, models, and exceptions."""
    # 1. Dict
    res_dict = {"choices": [{"message": {"content": "Hi Boss"}}], "usage": {"total_tokens": 42}}
    assert extract_response_payload(res_dict)["usage"]["total_tokens"] == 42

    # 2. Mock model_dump object
    mock_model = MagicMock()
    mock_model.model_dump.return_value = {"content": "model output"}
    del mock_model.dict  # Ensure model_dump is picked
    assert extract_response_payload(mock_model) == {"content": "model output"}

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
