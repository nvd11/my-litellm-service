"""Payload 后端工厂"""

from app.core.backends.dual_write_backend import DualWriteBackend
from app.core.backends.minio_backend import MinIOBackend
from app.core.backends.victorialogs_backend import VictoriaLogsBackend
from app.core.config import Settings
from app.core.payload_backend import PayloadBackend


def get_payload_backend(settings: Settings) -> PayloadBackend:
    """根据配置获取 Payload 后端实例

    Args:
        settings: 应用配置

    Returns:
        PayloadBackend: 后端实例

    Raises:
        ValueError: 未知的后端类型
    """
    backend_type = getattr(settings, "payload_backend", "minio").lower()

    if backend_type == "victorialogs":
        return VictoriaLogsBackend(settings)
    elif backend_type == "minio":
        return MinIOBackend(settings)
    elif backend_type == "dual":
        return get_dual_write_backend(settings)
    else:
        raise ValueError(f"Unknown payload backend: {backend_type}")


def get_dual_write_backend(settings: Settings) -> PayloadBackend:
    """获取双写后端（灰度期使用）

    Args:
        settings: 应用配置

    Returns:
        PayloadBackend: 双写后端实例
    """
    primary = MinIOBackend(settings)
    secondary = VictoriaLogsBackend(settings)
    return DualWriteBackend(primary, secondary)
