"""Payload API 测试（集成测试覆盖普通读取与多分片动态合并场景）."""

import json

import pytest
from fastapi.testclient import TestClient

from app.core.backends.factory import get_payload_backend
from app.core.payload_backend import PayloadBackend


class MockBackend(PayloadBackend):
    """测试用 Mock 后端."""

    def __init__(self):
        self.read_result = ({"user_prompt": "test"}, {"reply": "test"})

    async def write_payload(self, request_id, prompt, response, metadata):
        return True

    async def read_payload(self, request_id, date=None):
        return self.read_result

    async def health_check(self):
        return True


class TestPayloadAPI:
    """Payload API 测试."""

    @pytest.fixture
    def mock_backend(self):
        """Mock 后端."""
        return MockBackend()

    @pytest.fixture
    def client(self, mock_backend):
        """测试客户端."""
        from app.main import app

        # 覆盖依赖注入
        app.dependency_overrides[get_payload_backend] = lambda: mock_backend

        with TestClient(app) as client:
            yield client

        app.dependency_overrides.clear()

    def test_get_request_payload_success(self, client, mock_backend):
        """获取 payload 成功测试."""
        mock_backend.read_result = (
            {"user_prompt": "你好", "messages": [{"role": "user", "content": "你好"}]},
            {"reply": "你好！有什么可以帮你的？"},
        )

        response = client.get("/api/v1/logs/test-123/payload?date=2026-09-06")

        assert response.status_code == 200
        data = response.json()
        assert data["request_id"] == "test-123"
        assert data["date"] == "2026-09-06"
        assert data["prompt"]["user_prompt"] == "你好"
        assert data["response"]["reply"] == "你好！有什么可以帮你的？"
        assert "prompt_url" in data
        assert "response_url" in data

    def test_get_request_payload_not_found(self, client, mock_backend):
        """获取不存在 payload 测试."""
        mock_backend.read_result = (
            {"user_prompt": "（此历史调用的原始输入报文未在 MinIO 归档）"},
            {"reply": "（此历史调用的原始模型回复未在 MinIO 归档）"},
        )

        response = client.get("/api/v1/logs/nonexistent/payload?date=2026-09-06")

        assert response.status_code == 200
        data = response.json()
        assert "未在 MinIO 归档" in data["prompt"]["user_prompt"]

    def test_get_request_payload_with_truncation(self, client, mock_backend):
        """长消息截断测试."""
        # 创建 35 条消息
        messages = [{"role": "user", "content": f"消息 {i}"} for i in range(35)]
        mock_backend.read_result = (
            {"user_prompt": "test", "messages": messages},
            {"reply": "test"},
        )

        response = client.get("/api/v1/logs/test-123/payload?date=2026-09-06")

        assert response.status_code == 200
        data = response.json()
        assert data["prompt"]["is_truncated"] is True
        assert data["prompt"]["total_messages_count"] == 35
        assert len(data["prompt"]["messages"]) == 26  # 5 + 1 + 20

    def test_get_request_payload_full(self, client, mock_backend):
        """完整消息加载测试."""
        messages = [{"role": "user", "content": f"消息 {i}"} for i in range(35)]
        mock_backend.read_result = (
            {"user_prompt": "test", "messages": messages},
            {"reply": "test"},
        )

        response = client.get("/api/v1/logs/test-123/payload?date=2026-09-06&full=true")

        assert response.status_code == 200
        data = response.json()
        assert "is_truncated" not in data["prompt"]
        assert len(data["prompt"]["messages"]) == 35

    def test_get_request_payload_no_date(self, client, mock_backend):
        """无日期参数测试."""
        mock_backend.read_result = (
            {"user_prompt": "test"},
            {"reply": "test"},
        )

        response = client.get("/api/v1/logs/test-123/payload")

        assert response.status_code == 200
        data = response.json()
        assert data["request_id"] == "test-123"
        from datetime import UTC, datetime

        today = datetime.now(UTC).strftime("%Y-%m-%d")
        assert data["date"] == today

    def test_get_request_payload_reassembled_shards_integration(self, client, mock_backend):
        """验证多分片动态聚合还原后通过 API 正常呈现."""
        reassembled_prompt = {
            "model": "gemini-3.8-flash",
            "messages": [
                {"role": "user", "content": "Chunked message 1"},
                {"role": "assistant", "content": "Chunked message 2"},
            ],
            "parameters": {"temperature": 0.7},
        }
        reassembled_response = {
            "choices": [{"message": {"content": "Final chunked response"}}],
            "usage": {"total_tokens": 500},
        }

        # 模拟后端返回合并后的结构
        mock_backend.read_result = (reassembled_prompt, reassembled_response)

        response = client.get("/api/v1/logs/chunked-req-001/payload?date=2026-09-06")
        assert response.status_code == 200
        data = response.json()
        assert data["request_id"] == "chunked-req-001"
        assert len(data["prompt"]["messages"]) == 2
        assert data["response"]["choices"][0]["message"]["content"] == "Final chunked response"

    def test_get_request_payload_string_fallback_safe_parse(self, client, mock_backend):
        """验证后端返回原始 json 字符串时 API 层能兜底平稳解析."""
        mock_backend.read_result = (
            json.dumps({"user_prompt": "string payload"}),
            json.dumps({"reply": "string reply"}),
        )

        response = client.get("/api/v1/logs/str-req-002/payload?date=2026-09-06")
        assert response.status_code == 200
        data = response.json()
        assert data["prompt"]["user_prompt"] == "string payload"
        assert data["response"]["reply"] == "string reply"
