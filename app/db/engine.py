"""SQLAlchemy 异步引擎与数据库连接池生命周期管理 (Async Engine & Connection Pool).

业务背景与设计说明:
1. 采用 `mysql+aiomysql://` 异步驱动协议，实现非阻塞数据库连接与操作。
2. 连接池自愈与保活设计:
   - `pool_recycle=300`: 5分钟自动回收并重连连接，彻底杜绝 OCI 堡垒机/NAT 空闲超时
     导致的 MySQL 2006 (Server has gone away) 报错。
   - `pool_pre_ping=True`: 每次从连接池借出连接前执行轻量 ping 探活，确保借出的连接 100% 可用。
   - `pool_size=10, max_overflow=20`: 支持高并发大模型审计日志的弹性入库。
3. 密码安全与 URL 编码:
   使用 `quote_plus` 对复杂数据库密码进行转义编码，防止包含特殊字符时 DSN 解析异常。
"""

import logging
from urllib.parse import quote_plus

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings, get_settings

# 日志记录器
logger = logging.getLogger(__name__)

# 全局单例 AsyncEngine 实例
_async_engine: AsyncEngine | None = None


def get_mysql_async_url(settings: Settings) -> str:
    """构建 MySQL 异步连接 DSN 字符串 (使用 aiomysql 驱动并对密码进行安全 URL 转义).

    参数:
        settings: 全局系统配置对象 (含 MySQL 主机、端口、用户名、密码与库名)

    返回:
        str: 格式如 `mysql+aiomysql://user:pass@host:port/db` 的连接串
    """
    safe_password = quote_plus(settings.mysql_password.get_secret_value())
    return (
        f"mysql+aiomysql://{settings.mysql_user}:{safe_password}"
        f"@{settings.mysql_host}:{settings.mysql_port}/{settings.mysql_db}"
    )


def get_async_engine(settings: Settings | None = None) -> AsyncEngine:
    """获取或异步懒加载全局 SQLAlchemy AsyncEngine 单例.

    参数:
        settings: 可选的配置实例；未传入时自动通过 `get_settings()` 获取。

    返回:
        AsyncEngine: 配置了心跳探活与连接回收的异步引擎实例。
    """
    global _async_engine
    if _async_engine is None:
        if settings is None:
            settings = get_settings()

        url = get_mysql_async_url(settings)
        _async_engine = create_async_engine(
            url,
            pool_recycle=300,  # 5分钟保活回收
            pool_pre_ping=True,  # 借出前探活
            pool_size=10,  # 默认常驻连接数
            max_overflow=20,  # 突发允许扩展的最大连接数
            connect_args={"connect_timeout": settings.connect_timeout_seconds},
        )
        logger.debug(
            "Initialized AsyncEngine for %s:%s",
            settings.mysql_host,
            settings.mysql_port,
        )
    return _async_engine


async def close_async_engine() -> None:
    """优雅关闭与销毁 AsyncEngine 连接池 (主要用于测试清理与服务停止)."""
    global _async_engine
    if _async_engine is not None:
        await _async_engine.dispose()
        _async_engine = None
        logger.debug("Disposed SQLAlchemy AsyncEngine.")
