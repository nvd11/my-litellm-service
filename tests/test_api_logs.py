"""Unit tests for LiteLLM Observatory FastAPI endpoints."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.config import Settings, get_settings
from app.main import app


@pytest.fixture
def mock_test_settings() -> Settings:
    """Fixture providing isolated test settings."""
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
        payload_public_base_url="https://payloads.jppwl.asia/litellm-payloads",
        payload_upload_timeout_seconds=1.0,
    )


@pytest.mark.asyncio
async def test_health_endpoints() -> None:
    """Test health check probe endpoints."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/health")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"

        res_v1 = await client.get("/api/v1/health")
        assert res_v1.status_code == 200


@pytest.mark.asyncio
async def test_list_audit_logs(mock_test_settings: Settings) -> None:
    """Test paginated logs endpoint with mocked database rows."""
    app.dependency_overrides[get_settings] = lambda: mock_test_settings

    mock_row = MagicMock()
    mock_row.id = "uuid-1"
    mock_row.request_id = "req-1"
    mock_row.api_key_alias = "cindy"
    mock_row.model_requested = "gemini-3.7-flash"
    mock_row.model_used = "gemini-3.7-flash"
    mock_row.provider = "google-gemini"
    mock_row.provider_key_alias = "OPENAI_API_KEY_FREE_3"
    mock_row.prompt_tokens = 20
    mock_row.completion_tokens = 30
    mock_row.total_tokens = 50
    mock_row.cost_usd = 0.0001
    mock_row.cost_cny = 0.00072
    mock_row.fx_rate = 7.23
    mock_row.latency_ms = 850
    mock_row.status_code = 200
    mock_row.created_at = datetime(2026, 9, 3, 15, 0, 0, tzinfo=UTC)

    mock_conn = AsyncMock()
    mock_conn.execute.side_effect = [
        # Count query
        MagicMock(scalar_one_or_none=MagicMock(return_value=1)),
        # Data query
        MagicMock(fetchall=MagicMock(return_value=[mock_row])),
    ]

    mock_engine = MagicMock()
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn

    with patch("app.api.logs.get_async_engine", return_value=mock_engine):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get("/api/v1/logs?page=1&page_size=10&api_key_alias=cindy")
            assert res.status_code == 200
            data = res.json()
            assert data["total"] == 1
            assert data["page"] == 1
            assert len(data["items"]) == 1
            item = data["items"][0]
            assert item["request_id"] == "req-1"
            assert item["api_key_alias"] == "cindy"
            assert "prompt_url" in item
            expected_url = (
                "https://payloads.jppwl.asia/litellm-payloads/2026-09-03/req-1/prompt.json"
            )
            assert item["prompt_url"] == expected_url

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_summary_metrics(mock_test_settings: Settings) -> None:
    """Test daily summary metrics calculation."""
    app.dependency_overrides[get_settings] = lambda: mock_test_settings

    mock_sum_row = MagicMock(
        total_requests=10,
        sum_tokens=5000,
        sum_cost_cny=0.35,
        sum_cost_usd=0.05,
        avg_latency=1200.4,
        success_count=9,
    )
    mock_key_row = MagicMock(
        api_key_alias="cindy",
        req_count=8,
        key_tokens=4000,
        key_cost_cny=0.28,
    )
    mock_model_row = MagicMock(
        model_used="gemini-3.7-flash",
        model_count=10,
        model_tokens=5000,
        model_cost_cny=0.35,
    )

    mock_conn = AsyncMock()
    mock_conn.execute.side_effect = [
        MagicMock(fetchone=MagicMock(return_value=mock_sum_row)),
        MagicMock(fetchall=MagicMock(return_value=[mock_key_row])),
        MagicMock(fetchall=MagicMock(return_value=[mock_model_row])),
    ]

    mock_engine = MagicMock()
    mock_engine.connect.return_value.__aenter__.return_value = mock_conn

    with patch("app.api.metrics.get_async_engine", return_value=mock_engine):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get("/api/v1/metrics/summary?date=2026-09-03")
            assert res.status_code == 200
            data = res.json()
            assert data["today_requests"] == 10
            assert data["today_tokens"] == 5000
            assert data["success_rate"] == 90.0
            assert len(data["active_keys"]) == 1
            assert data["active_keys"][0]["alias"] == "cindy"

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_get_request_payload_success(mock_test_settings: Settings) -> None:
    """Test retrieving structured S3 payload."""
    app.dependency_overrides[get_settings] = lambda: mock_test_settings

    mock_s3_client = AsyncMock()
    prompt_sample = b'{"model":"gemini","system_prompt":"sys","user_prompt":"hi"}'
    mock_s3_client.get_object.side_effect = [
        {"Body": AsyncMock(read=AsyncMock(return_value=prompt_sample))},
        {"Body": AsyncMock(read=AsyncMock(return_value=b'{"reply":"hello there"}'))},
    ]

    mock_context = AsyncMock()
    mock_context.__aenter__.return_value = mock_s3_client
    mock_context.__aexit__.return_value = None

    mock_session = MagicMock()
    mock_session.client.return_value = mock_context

    with patch("aioboto3.Session", return_value=mock_session):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            res = await client.get("/api/v1/logs/req-test-99/payload?date=2026-09-03")
            assert res.status_code == 200
            data = res.json()
            assert data["request_id"] == "req-test-99"
            assert data["prompt"]["user_prompt"] == "hi"
            assert data["response"]["reply"] == "hello there"

    app.dependency_overrides.clear()
