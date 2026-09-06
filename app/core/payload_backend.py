"""Payload 存储后端抽象基类"""

from abc import ABC, abstractmethod
from typing import Any


class PayloadBackend(ABC):
    """Payload 存储后端抽象基类，定义统一读写与检索接口"""

    @abstractmethod
    async def write_payload(
        self,
        request_id: str,
        prompt: dict[str, Any],
        response: dict[str, Any],
        metadata: dict[str, Any],
    ) -> bool:
        """写入 payload，返回是否成功

        Args:
            request_id: 请求唯一标识
            prompt: 用户输入报文
            response: 模型输出报文
            metadata: 调用元数据（model, key_alias, tokens, latency 等）

        Returns:
            bool: 写入是否成功
        """
        pass

    @abstractmethod
    async def read_payload(
        self,
        request_id: str,
        date: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """读取 payload，返回 (prompt, response)

        Args:
            request_id: 请求唯一标识
            date: 可选日期分区（YYYY-MM-DD），用于加速查询

        Returns:
            tuple: (prompt_data, response_data)，不存在时返回空 dict
        """
        pass

    @abstractmethod
    async def search_payloads(
        self,
        keyword: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 500,
    ) -> list[str]:
        """全文检索 Prompt 和 Response 内容，返回匹配的 request_id 列表

        Args:
            keyword: 搜索关键词
            start_date: 可选起始日期 (YYYY-MM-DD)
            end_date: 可选截止日期 (YYYY-MM-DD)
            limit: 最大返回请求数上限

        Returns:
            list[str]: 匹配的 request_id 列表
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """后端健康检查

        Returns:
            bool: 后端是否可用
        """
        pass
