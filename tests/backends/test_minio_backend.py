"""MinIOBackend 测试"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.backends.minio_backend import MinIOBackend
from app.core.config import Settings


class TestMinIOBackend:
    """MinIOBackend 测试"""

    @pytest.fixture
    def settings(self) -> Settings:
        """测试用 Settings"""
        return Settings(
            mysql_host="localhost",
            mysql_user="test",
            mysql_password="test",
            mysql_db="test",
            redis_host="localhost",
            redis_password="test",
            litellm_master_key="test",
            payload_s3_endpoint="http://localhost:9000",
            payload_s3_access_key="test",
            payload_s3_secret_key="test",
            payload_bucket_name="test-bucket",
        )

    @pytest.fixture
    def backend(self, settings: Settings) -> MinIOBackend:
        """测试用 MinIOBackend"""
        return MinIOBackend(settings)

    def test_init(self, backend: MinIOBackend):
        """初始化测试"""
        assert backend.settings is not None
        assert backend.session is not None
        assert backend.boto_config is not None

    @pytest.mark.asyncio
    async def test_write_payload_success(self, backend: MinIOBackend):
        """写入成功测试"""
        mock_client = AsyncMock()
        mock_client.put_object = AsyncMock(return_value={})

        with patch.object(backend.session, "client") as mock_session:
            mock_session.return_value.__aenter__.return_value = mock_client

            result = await backend.write_payload(
                request_id="test-123",
                prompt={"user_prompt": "test"},
                response={"reply": "test"},
                metadata={"date": "2026-09-06"},
            )

            assert result is True
            assert mock_client.put_object.call_count == 2

    @pytest.mark.asyncio
    async def test_write_payload_missing_date(self, backend: MinIOBackend):
        """缺少 date 字段测试"""
        result = await backend.write_payload(
            request_id="test-123",
            prompt={"user_prompt": "test"},
            response={"reply": "test"},
            metadata={},
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_write_payload_exception(self, backend: MinIOBackend):
        """写入异常测试"""
        mock_client = AsyncMock()
        mock_client.put_object = AsyncMock(side_effect=Exception("S3 error"))

        with patch.object(backend.session, "client") as mock_session:
            mock_session.return_value.__aenter__.return_value = mock_client

            result = await backend.write_payload(
                request_id="test-123",
                prompt={"user_prompt": "test"},
                response={"reply": "test"},
                metadata={"date": "2026-09-06"},
            )

            assert result is False

    @pytest.mark.asyncio
    async def test_read_payload_success(self, backend: MinIOBackend):
        """读取成功测试"""
        mock_prompt = MagicMock()
        mock_prompt.read = AsyncMock(return_value=b'{"user_prompt": "test"}')

        mock_response = MagicMock()
        mock_response.read = AsyncMock(return_value=b'{"reply": "test"}')

        mock_client = AsyncMock()
        mock_client.get_object = AsyncMock(
            side_effect=[
                {"Body": mock_prompt},
                {"Body": mock_response},
            ]
        )

        with patch.object(backend.session, "client") as mock_session:
            mock_session.return_value.__aenter__.return_value = mock_client

            prompt, response = await backend.read_payload(
                request_id="test-123",
                date="2026-09-06",
            )

            assert prompt == {"user_prompt": "test"}
            assert response == {"reply": "test"}

    @pytest.mark.asyncio
    async def test_read_payload_missing_date(self, backend: MinIOBackend):
        """缺少 date 参数测试"""
        prompt, response = await backend.read_payload(
            request_id="test-123",
            date=None,
        )

        assert prompt == {}
        assert response == {}

    @pytest.mark.asyncio
    async def test_read_payload_not_found(self, backend: MinIOBackend):
        """读取不存在测试"""
        mock_client = AsyncMock()
        mock_client.get_object = AsyncMock(side_effect=Exception("Not found"))

        with patch.object(backend.session, "client") as mock_session:
            mock_session.return_value.__aenter__.return_value = mock_client

            prompt, response = await backend.read_payload(
                request_id="test-123",
                date="2026-09-06",
            )

            assert "user_prompt" in prompt
            assert "reply" in response

    @pytest.mark.asyncio
    async def test_health_check_success(self, backend: MinIOBackend):
        """健康检查成功测试"""
        mock_client = AsyncMock()
        mock_client.list_buckets = AsyncMock(return_value={"Buckets": []})

        with patch.object(backend.session, "client") as mock_session:
            mock_session.return_value.__aenter__.return_value = mock_client

            result = await backend.health_check()

            assert result is True

    @pytest.mark.asyncio
    async def test_health_check_failure(self, backend: MinIOBackend):
        """健康检查失败测试"""
        mock_client = AsyncMock()
        mock_client.list_buckets = AsyncMock(side_effect=Exception("Connection error"))

        with patch.object(backend.session, "client") as mock_session:
            mock_session.return_value.__aenter__.return_value = mock_client

            result = await backend.health_check()

            assert result is False
