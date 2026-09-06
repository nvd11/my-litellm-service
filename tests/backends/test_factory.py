"""Payload 后端工厂测试"""

import pytest

from app.core.backends.dual_write_backend import DualWriteBackend
from app.core.backends.factory import get_dual_write_backend, get_payload_backend
from app.core.backends.minio_backend import MinIOBackend
from app.core.backends.victorialogs_backend import VictoriaLogsBackend
from app.core.config import Settings


class TestFactory:
    """工厂测试"""

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
            victorialogs_url="http://localhost:9428",
        )

    def test_get_minio_backend(self, settings: Settings):
        """获取 MinIO 后端测试"""
        settings.payload_backend = "minio"
        backend = get_payload_backend(settings)
        assert isinstance(backend, MinIOBackend)

    def test_get_victorialogs_backend(self, settings: Settings):
        """获取 VictoriaLogs 后端测试"""
        settings.payload_backend = "victorialogs"
        backend = get_payload_backend(settings)
        assert isinstance(backend, VictoriaLogsBackend)

    def test_get_dual_backend(self, settings: Settings):
        """获取双写后端测试"""
        settings.payload_backend = "dual"
        backend = get_payload_backend(settings)
        assert isinstance(backend, DualWriteBackend)
        assert isinstance(backend.primary, MinIOBackend)
        assert isinstance(backend.secondary, VictoriaLogsBackend)

    def test_get_backend_default(self, settings: Settings):
        """默认后端测试（minio）"""
        # 不设置 payload_backend，使用默认值
        backend = get_payload_backend(settings)
        assert isinstance(backend, MinIOBackend)

    def test_get_backend_case_insensitive(self, settings: Settings):
        """后端类型大小写不敏感测试"""
        settings.payload_backend = "MINIO"
        backend = get_payload_backend(settings)
        assert isinstance(backend, MinIOBackend)

        settings.payload_backend = "VictoriaLogs"
        backend = get_payload_backend(settings)
        assert isinstance(backend, VictoriaLogsBackend)

    def test_get_backend_unknown(self, settings: Settings):
        """未知后端类型测试"""
        settings.payload_backend = "unknown"
        with pytest.raises(ValueError, match="Unknown payload backend"):
            get_payload_backend(settings)

    def test_get_dual_write_backend(self, settings: Settings):
        """获取双写后端函数测试"""
        backend = get_dual_write_backend(settings)
        assert isinstance(backend, DualWriteBackend)
        assert isinstance(backend.primary, MinIOBackend)
        assert isinstance(backend.secondary, VictoriaLogsBackend)
