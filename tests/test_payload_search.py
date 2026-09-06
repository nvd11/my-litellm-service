"""测试 Payload 全文搜索检索功能以及与各类筛选条件的联动."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from app.core.backends.factory import get_payload_backend
from app.core.backends.victorialogs_backend import VictoriaLogsBackend
from app.core.config import Settings, get_settings
from app.core.payload_backend import PayloadBackend
from app.main import app


@pytest.fixture
def test_settings() -> Settings:
    """测试用统一配置 fixture."""
    return Settings(
        mysql_host="127.0.0.1",
        mysql_user="root",
        mysql_password=SecretStr("pass"),
        mysql_db="test_db",
        redis_host="127.0.0.1",
        redis_password=SecretStr("redis_pass"),
        openai_api_key_free_1=SecretStr("key1"),
        litellm_master_key=SecretStr("sk-master"),
        victorialogs_url="http://localhost:9428",
    )


class SearchMockBackend(PayloadBackend):
    """测试用支持 search_payloads 的 Mock 后端."""

    def __init__(self, matched_rids: list[str] | None = None):
        self.matched_rids = (
            matched_rids if matched_rids is not None else ["req-match-1", "req-match-2"]
        )

    async def write_payload(self, request_id, prompt, response, metadata):
        return True

    async def read_payload(self, request_id, date=None):
        return {"user_prompt": f"prompt for {request_id}"}, {"reply": "reply content"}

    async def search_payloads(
        self,
        keyword: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 500,
    ) -> list[str]:
        if keyword == "notfound":
            return []
        return self.matched_rids

    async def health_check(self):
        return True


@pytest.mark.asyncio
async def test_logs_payload_search_empty_matches(test_settings: Settings) -> None:
    """当 payload_search 未匹配到任何 request_id 时，直接返回空列表而无需查库."""
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_payload_backend] = lambda: SearchMockBackend(matched_rids=[])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/v1/logs?payload_search=nonexistent_token")
        assert res.status_code == 200
        data = res.json()
        assert data["total"] == 0
        assert data["items"] == []

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_metrics_payload_search_empty_matches(test_settings: Settings) -> None:
    """当 payload_search 未匹配到任何 request_id 时，汇总卡片直接清零返回默认空结构."""
    app.dependency_overrides[get_settings] = lambda: test_settings
    app.dependency_overrides[get_payload_backend] = lambda: SearchMockBackend(matched_rids=[])

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        res = await client.get("/api/v1/metrics/summary?payload_search=nonexistent_token")
        assert res.status_code == 200
        data = res.json()
        assert data["today_requests"] == 0
        assert data["today_tokens"] == 0
        assert data["today_cost_cny"] == 0.0

    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_victorialogs_backend_search_payloads_parsing(test_settings: Settings) -> None:
    """测试 VictoriaLogsBackend.search_payloads 方法正确构建 LogsQL 并在返回时提取 request_id."""
    backend = VictoriaLogsBackend(test_settings)

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = (
        '{"request_id": "rid-1", "_time": "2026-09-06T00:00:00Z"}\n'
        '{"request_id": "rid-2", "_time": "2026-09-06T00:01:00Z"}\n'
        '{"request_id": "rid-1", "_time": "2026-09-06T00:02:00Z"}\n'  # 模拟重复
    )

    with patch("httpx.AsyncClient") as mock_client_class:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_class.return_value.__aenter__.return_value = mock_client

        rids = await backend.search_payloads(
            keyword="Python error",
            start_date="2026-09-06",
            end_date="2026-09-06",
            limit=50,
        )

        assert rids == ["rid-1", "rid-2"]  # 自动去重
        call_args = mock_client.post.call_args
        query_sent = call_args[1]["data"]["query"]
        assert 'prompt_chunk:"Python error"' in query_sent
        assert "_time: 2026-09-06" in query_sent
        assert "limit 50" in query_sent
