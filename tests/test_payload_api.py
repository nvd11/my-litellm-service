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

    async def search_payloads(self, keyword, start_date=None, end_date=None, limit=500):
        return ["test-123"]

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
        from unittest.mock import AsyncMock, patch

        from app.main import app

        # 覆盖依赖注入
        app.dependency_overrides[get_payload_backend] = lambda: mock_backend

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.set = AsyncMock(return_value=True)

        with patch("app.api.payload.get_redis_client", return_value=mock_redis):
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

    def test_get_request_payload_redis_cache_hit(self, client, mock_backend):
        """验证命中 Redis L2 缓存时直接直出，不调用后端 read_payload."""
        from unittest.mock import AsyncMock, patch

        cached_data = json.dumps({
            "prompt": {"user_prompt": "cached from redis"},
            "response": {"reply": "instant reply from redis"},
        })

        mock_redis = AsyncMock()
        mock_redis.get = AsyncMock(return_value=cached_data)

        with patch("app.api.payload.get_redis_client", return_value=mock_redis):
            response = client.get("/api/v1/logs/cached-req-777/payload?date=2026-09-06")

        assert response.status_code == 200
        data = response.json()
        assert data["prompt"]["user_prompt"] == "cached from redis"
        assert data["response"]["reply"] == "instant reply from redis"
        mock_redis.get.assert_called_once_with("litellm:payload:cached-req-777")

    def test_get_request_payload_single_oversized_message_truncation(self, client, mock_backend):
        """单条超长消息 (>5000 字符) 智能抽样截断测试."""
        huge_content = "A" * 20000
        messages = [
            {"role": "system", "content": "system instruction"},
            {"role": "user", "content": huge_content},
            {"role": "assistant", "content": "normal response"},
        ]
        mock_backend.read_result = (
            {"user_prompt": huge_content, "messages": messages},
            {"reply": "test"},
        )

        response = client.get("/api/v1/logs/huge-msg-123/payload?date=2026-09-06")
        assert response.status_code == 200
        data = response.json()

        prompt = data["prompt"]
        assert prompt["is_truncated"] is True
        assert prompt["total_messages_count"] == 3
        assert len(prompt["messages"]) == 3

        user_msg = prompt["messages"][1]
        assert len(user_msg["content"]) < len(huge_content)
        assert "此处已自动智能抽样截断" in user_msg["content"]
        assert "20,000" in user_msg["content"]

        # 检查顶层 user_prompt 也被安全保护
        assert len(prompt["user_prompt"]) < len(huge_content)
        assert "此处已自动智能抽样截断" in prompt["user_prompt"]

    def test_get_request_payload_single_oversized_message_full(self, client, mock_backend):
        """带 full=true 时单条超长消息保持全量未截断."""
        huge_content = "A" * 20000
        messages = [
            {"role": "system", "content": "system instruction"},
            {"role": "user", "content": huge_content},
        ]
        mock_backend.read_result = (
            {"user_prompt": huge_content, "messages": messages},
            {"reply": "test"},
        )

        response = client.get("/api/v1/logs/huge-msg-123/payload?date=2026-09-06&full=true")
        assert response.status_code == 200
        data = response.json()

        prompt = data["prompt"]
        assert "is_truncated" not in prompt
        assert len(prompt["messages"]) == 2
        assert prompt["messages"][1]["content"] == huge_content
        assert prompt["user_prompt"] == huge_content

    def test_get_request_payload_multimodal_base64_image_truncation(self, client, mock_backend):
        """验证多模态超大 Base64 图片 (image_url) 在秒开模式下被智能安全折叠."""
        huge_base64 = "data:image/png;base64," + ("iVBORw0KGgoAAAANSUhEUgAA" * 1000)
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "请看这张截图："},
                    {"type": "image_url", "image_url": {"url": huge_base64}},
                ],
            }
        ]
        mock_backend.read_result = (
            {"user_prompt": "请看这张截图", "messages": messages},
            {"reply": "收到图片"},
        )

        response = client.get("/api/v1/logs/img-msg-001/payload?date=2026-09-06")
        assert response.status_code == 200
        data = response.json()

        prompt = data["prompt"]
        assert prompt["is_truncated"] is True
        user_msg = prompt["messages"][0]
        assert isinstance(user_msg["content"], list)
        img_item = user_msg["content"][1]
        img_url = img_item["image_url"]["url"]
        assert len(img_url) < len(huge_base64)
        assert "Base64 图片数据" in img_url
