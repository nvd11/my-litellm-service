"""VictoriaLogsBackend 单元与集成测试（涵盖超限切块分片与动态合并还原）."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.backends.victorialogs_backend import VictoriaLogsBackend
from app.core.config import Settings


class TestVictoriaLogsBackend:
    """VictoriaLogsBackend 基础与分片切块测试."""

    @pytest.fixture
    def settings(self) -> Settings:
        """测试用 Settings."""
        return Settings(
            mysql_host="localhost",
            mysql_user="test",
            mysql_password="test",
            mysql_db="test",
            redis_host="localhost",
            redis_password="test",
            litellm_master_key="test",
            victorialogs_url="http://localhost:9428",
        )

    @pytest.fixture
    def backend(self, settings: Settings) -> VictoriaLogsBackend:
        """测试用 VictoriaLogsBackend."""
        return VictoriaLogsBackend(settings)

    def test_init(self, backend: VictoriaLogsBackend):
        """初始化测试."""
        assert backend.settings is not None
        assert backend.endpoint == "http://localhost:9428"
        assert backend.timeout is not None
        assert backend.chunk_size == 1_500_000

    @pytest.mark.asyncio
    async def test_write_payload_small_single_shard(self, backend: VictoriaLogsBackend):
        """小报文单片入库测试."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await backend.write_payload(
                request_id="test-123",
                prompt={"user_prompt": "test prompt"},
                response={"reply": "test response"},
                metadata={
                    "timestamp": "2026-09-06T05:32:46.000Z",
                    "model": "gemini-3.8-flash",
                    "key_alias": "test-key",
                    "status_code": 200,
                    "latency_ms": 100,
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                    "spend": 0.001,
                },
            )

            assert result is True
            mock_client.post.assert_called_once()

            # 验证请求体
            call_args = mock_client.post.call_args
            assert call_args[0][0] == "http://localhost:9428/insert/jsonline"
            assert call_args[1]["headers"]["Content-Type"] == "application/stream+json"

            body = call_args[1]["content"].decode("utf-8").strip()
            lines = body.split("\n")
            assert len(lines) == 1

            data = json.loads(lines[0])
            assert data["_stream"] == '{env="prod",service="litellm",type="payload"}'
            assert data["request_id"] == "test-123"
            assert data["shard_index"] == 1
            assert data["total_shards"] == 1
            assert "prompt_chunk" in data
            assert json.loads(data["prompt_chunk"]) == {"user_prompt": "test prompt"}
            assert json.loads(data["response"]) == {"reply": "test response"}

    @pytest.mark.asyncio
    async def test_write_payload_large_chunking(self, settings: Settings):
        """超大报文切片分块写入测试（自定义小 chunk_size 模拟）."""
        # 设置单块 100 字符上限进行精确切分测试
        custom_backend = VictoriaLogsBackend(settings, chunk_size=100)

        # 构造一个 250 字符的大 prompt
        large_prompt = {
            "messages": [
                {"role": "user", "content": "A" * 200},
            ]
        }
        small_response = {"choices": [{"message": {"content": "ok"}}]}

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await custom_backend.write_payload(
                request_id="large-req-999",
                prompt=large_prompt,
                response=small_response,
                metadata={
                    "timestamp": "2026-09-06T12:00:00Z",
                    "model": "gemini-3.7-flash",
                },
            )

            assert result is True
            mock_client.post.assert_called_once()

            # 验证批量请求体包含了多个分片
            content_bytes = mock_client.post.call_args[1]["content"]
            lines = [
                line.strip() for line in content_bytes.decode("utf-8").split("\n") if line.strip()
            ]
            assert len(lines) >= 3  # 预期切成 3 个分片以上

            shards = [json.loads(line) for line in lines]
            for i, s in enumerate(shards, 1):
                assert s["request_id"] == "large-req-999"
                assert s["shard_index"] == i
                assert s["total_shards"] == len(shards)
                assert f"[shard {i}/{len(shards)}]" in s["_msg"]
                assert len(s["prompt_chunk"]) <= 100
                if i == 1:
                    assert s["response"] != ""
                else:
                    assert s["response"] == ""

    @pytest.mark.asyncio
    async def test_read_payload_dynamic_shards_reassembly(self, backend: VictoriaLogsBackend):
        """动态读取乱序返回的多个分片并按序号正确合并还原."""
        # 模拟超大 Prompt 拆分为 3 块，并故意乱序返回
        original_prompt = {
            "model": "gemini-3.8-flash",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Long context " + ("hello " * 100)},
            ],
        }
        full_json_str = json.dumps(original_prompt, ensure_ascii=False)
        chunk1 = full_json_str[:200]
        chunk2 = full_json_str[200:400]
        chunk3 = full_json_str[400:]

        # 模拟 VictoriaLogs 返回乱序的 3 个分片记录
        log_shard_3 = json.dumps(
            {
                "request_id": "req-shards-1",
                "shard_index": 3,
                "total_shards": 3,
                "prompt_chunk": chunk3,
                "response": "",
            }
        )
        log_shard_1 = json.dumps(
            {
                "request_id": "req-shards-1",
                "shard_index": 1,
                "total_shards": 3,
                "prompt_chunk": chunk1,
                "response": json.dumps({"reply": "reconstructed answer"}),
            }
        )
        log_shard_2 = json.dumps(
            {
                "request_id": "req-shards-1",
                "shard_index": 2,
                "total_shards": 3,
                "prompt_chunk": chunk2,
                "response": "",
            }
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        # 故意乱序: 3 -> 1 -> 2
        mock_response.text = f"{log_shard_3}\n{log_shard_1}\n{log_shard_2}\n"

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            prompt, response = await backend.read_payload(
                request_id="req-shards-1",
                date="2026-09-06",
            )

            # 验证还原后的数据完全对齐无损
            assert prompt == original_prompt
            assert response == {"reply": "reconstructed answer"}

    @pytest.mark.asyncio
    async def test_read_payload_legacy_single_field_compatibility(
        self, backend: VictoriaLogsBackend
    ):
        """向后兼容旧格式（单字段 prompt 无 prompt_chunk）的读取还原."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = json.dumps(
            {
                "request_id": "legacy-req",
                "prompt": json.dumps({"user_prompt": "legacy format"}),
                "response": json.dumps({"reply": "legacy reply"}),
            }
        )

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            prompt, response = await backend.read_payload(request_id="legacy-req")

            assert prompt == {"user_prompt": "legacy format"}
            assert response == {"reply": "legacy reply"}

    @pytest.mark.asyncio
    async def test_write_payload_failure(self, backend: VictoriaLogsBackend):
        """写入失败测试."""
        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await backend.write_payload(
                request_id="test-123",
                prompt={"user_prompt": "test"},
                response={"reply": "test"},
                metadata={"timestamp": "2026-09-06T05:32:46.000Z"},
            )

            assert result is False

    @pytest.mark.asyncio
    async def test_write_payload_exception(self, backend: VictoriaLogsBackend):
        """写入网络异常隔离测试."""
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(side_effect=Exception("Network error"))
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await backend.write_payload(
                request_id="test-123",
                prompt={"user_prompt": "test"},
                response={"reply": "test"},
                metadata={"timestamp": "2026-09-06T05:32:46.000Z"},
            )

            assert result is False

    @pytest.mark.asyncio
    async def test_read_payload_not_found(self, backend: VictoriaLogsBackend):
        """读取不存在时返回空字典."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = ""

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            prompt, response = await backend.read_payload(request_id="test-not-exist")

            assert prompt == {}
            assert response == {}

    @pytest.mark.asyncio
    async def test_health_check_success(self, backend: VictoriaLogsBackend):
        """健康检查成功测试."""
        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await backend.health_check()
            assert result is True

    @pytest.mark.asyncio
    async def test_health_check_failure(self, backend: VictoriaLogsBackend):
        """健康检查失败测试."""
        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_class.return_value.__aenter__.return_value = mock_client

            result = await backend.health_check()
            assert result is False
