"""Shared async Redis client singleton for LiteLLM Observatory."""

from typing import Any
from redis import asyncio as redis_asyncio
from app.core.config import Settings

_redis_client: Any = None


def get_redis_client(settings: Settings) -> Any:
    """获取或懒加载全局单例 Redis 异步客户端.

    参数:
        settings: 系统配置对象 (包含 Redis 连接参数与连接超时配置)

    返回:
        redis_asyncio.Redis: 经过配置的 Redis 异步客户端单例
    """
    global _redis_client
    if _redis_client is None:
        _redis_client = redis_asyncio.Redis(
            host=settings.redis_host,
            port=settings.redis_port,
            password=settings.redis_password.get_secret_value(),
            socket_connect_timeout=settings.connect_timeout_seconds,
            socket_timeout=settings.connect_timeout_seconds,
            decode_responses=True,  # 自动将 bytes 解码为 str
        )
    return _redis_client


def reset_redis_client() -> None:
    """重置 Redis 单例客户端（主要用于单元测试状态隔离）."""
    global _redis_client
    _redis_client = None
