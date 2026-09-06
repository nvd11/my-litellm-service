"""双写后端：主写 + 异步副写，用于灰度期数据对比"""

import asyncio
import logging
from typing import Any

from app.core.payload_backend import PayloadBackend

logger = logging.getLogger(__name__)


class DualWriteBackend(PayloadBackend):
    """双写后端：主写 MinIO，异步副写 VictoriaLogs"""

    def __init__(self, primary: PayloadBackend, secondary: PayloadBackend) -> None:
        self.primary = primary
        self.secondary = secondary

    async def write_payload(
        self,
        request_id: str,
        prompt: dict[str, Any],
        response: dict[str, Any],
        metadata: dict[str, Any],
    ) -> bool:
        """主写 + 异步副写

        Args:
            request_id: 请求唯一标识
            prompt: 用户输入报文
            response: 模型输出报文
            metadata: 调用元数据

        Returns:
            bool: 主写是否成功
        """
        # 主写（阻塞，确保成功）
        primary_ok = await self.primary.write_payload(request_id, prompt, response, metadata)

        # 异步副写（不阻塞主流程）
        asyncio.create_task(self._secondary_write(request_id, prompt, response, metadata))

        return primary_ok

    async def _secondary_write(
        self,
        request_id: str,
        prompt: dict[str, Any],
        response: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        """异步副写，失败只记日志"""
        try:
            ok = await self.secondary.write_payload(request_id, prompt, response, metadata)
            if not ok:
                logger.warning("Secondary write failed for %s", request_id)
        except Exception as e:
            logger.warning("Secondary write error for %s: %s", request_id, e)

    async def read_payload(
        self,
        request_id: str,
        date: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """优先从主后端读取

        Args:
            request_id: 请求唯一标识
            date: 可选日期分区（YYYY-MM-DD）

        Returns:
            tuple: (prompt_data, response_data)
        """
        return await self.primary.read_payload(request_id, date)

    async def search_payloads(
        self,
        keyword: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 500,
    ) -> list[str]:
        """优先使用支持检索的后端执行检索（若副后端支持则委派副后端）."""
        res = await self.primary.search_payloads(keyword, start_date, end_date, limit)
        if not res:
            res = await self.secondary.search_payloads(keyword, start_date, end_date, limit)
        return res

    async def health_check(self) -> bool:
        """双后端健康检查

        Returns:
            bool: 两个后端都可用才返回 True
        """
        primary_ok = await self.primary.health_check()
        secondary_ok = await self.secondary.health_check()
        return primary_ok and secondary_ok
