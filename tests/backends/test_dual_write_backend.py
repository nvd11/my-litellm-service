"""DualWriteBackend 测试"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.core.backends.dual_write_backend import DualWriteBackend
from app.core.payload_backend import PayloadBackend


class MockBackend(PayloadBackend):
    """测试用 Mock 后端"""

    def __init__(self, name: str, write_result: bool = True, read_result: tuple = ({}, {})):
        self.name = name
        self.write_result = write_result
        self.read_result = read_result
        self.write_calls = []
        self.read_calls = []

    async def write_payload(
        self,
        request_id: str,
        prompt: dict,
        response: dict,
        metadata: dict,
    ) -> bool:
        self.write_calls.append(
            {
                "request_id": request_id,
                "prompt": prompt,
                "response": response,
                "metadata": metadata,
            }
        )
        return self.write_result

    async def read_payload(
        self,
        request_id: str,
        date: str | None = None,
    ) -> tuple[dict, dict]:
        self.read_calls.append(
            {
                "request_id": request_id,
                "date": date,
            }
        )
        return self.read_result

    async def search_payloads(
        self,
        keyword: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 500,
    ) -> list[str]:
        return ["req-search-found"]

    async def health_check(self) -> bool:
        return True


class TestDualWriteBackend:
    """DualWriteBackend 测试"""

    @pytest.fixture
    def primary_backend(self) -> MockBackend:
        """主后端"""
        return MockBackend("primary", write_result=True)

    @pytest.fixture
    def secondary_backend(self) -> MockBackend:
        """副后端"""
        return MockBackend("secondary", write_result=True)

    @pytest.fixture
    def dual_backend(
        self,
        primary_backend: MockBackend,
        secondary_backend: MockBackend,
    ) -> DualWriteBackend:
        """双写后端"""
        return DualWriteBackend(primary_backend, secondary_backend)

    def test_init(self, dual_backend: DualWriteBackend):
        """初始化测试"""
        assert dual_backend.primary is not None
        assert dual_backend.secondary is not None

    @pytest.mark.asyncio
    async def test_write_payload_success(
        self,
        dual_backend: DualWriteBackend,
        primary_backend: MockBackend,
        secondary_backend: MockBackend,
    ):
        """双写成功测试"""
        result = await dual_backend.write_payload(
            request_id="test-123",
            prompt={"user_prompt": "test"},
            response={"reply": "test"},
            metadata={"date": "2026-09-06"},
        )

        assert result is True
        assert len(primary_backend.write_calls) == 1
        assert primary_backend.write_calls[0]["request_id"] == "test-123"

        # 等待异步副写完成
        await asyncio.sleep(0.1)
        assert len(secondary_backend.write_calls) == 1
        assert secondary_backend.write_calls[0]["request_id"] == "test-123"

    @pytest.mark.asyncio
    async def test_write_payload_primary_failure(
        self,
        primary_backend: MockBackend,
        secondary_backend: MockBackend,
    ):
        """主写失败测试"""
        primary_backend.write_result = False
        dual_backend = DualWriteBackend(primary_backend, secondary_backend)

        result = await dual_backend.write_payload(
            request_id="test-123",
            prompt={"user_prompt": "test"},
            response={"reply": "test"},
            metadata={"date": "2026-09-06"},
        )

        assert result is False
        assert len(primary_backend.write_calls) == 1

        # 副写仍然会执行
        await asyncio.sleep(0.1)
        assert len(secondary_backend.write_calls) == 1

    @pytest.mark.asyncio
    async def test_write_payload_secondary_failure(
        self,
        primary_backend: MockBackend,
        secondary_backend: MockBackend,
    ):
        """副写失败测试（不影响主写）"""
        secondary_backend.write_result = False
        dual_backend = DualWriteBackend(primary_backend, secondary_backend)

        result = await dual_backend.write_payload(
            request_id="test-123",
            prompt={"user_prompt": "test"},
            response={"reply": "test"},
            metadata={"date": "2026-09-06"},
        )

        assert result is True  # 主写成功
        assert len(primary_backend.write_calls) == 1

        await asyncio.sleep(0.1)
        assert len(secondary_backend.write_calls) == 1

    @pytest.mark.asyncio
    async def test_read_payload(
        self,
        dual_backend: DualWriteBackend,
        primary_backend: MockBackend,
        secondary_backend: MockBackend,
    ):
        """读取测试（只从主后端读取）"""
        primary_backend.read_result = ({"user_prompt": "test"}, {"reply": "test"})

        prompt, response = await dual_backend.read_payload(
            request_id="test-123",
            date="2026-09-06",
        )

        assert prompt == {"user_prompt": "test"}
        assert response == {"reply": "test"}
        assert len(primary_backend.read_calls) == 1
        assert len(secondary_backend.read_calls) == 0  # 副后端不读

    @pytest.mark.asyncio
    async def test_health_check_both_ok(
        self,
        dual_backend: DualWriteBackend,
    ):
        """双后端健康检查成功"""
        result = await dual_backend.health_check()
        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_primary_failure(
        self,
        primary_backend: MockBackend,
        secondary_backend: MockBackend,
    ):
        """主后端健康检查失败"""
        primary_backend.health_check = AsyncMock(return_value=False)
        dual_backend = DualWriteBackend(primary_backend, secondary_backend)

        result = await dual_backend.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_secondary_failure(
        self,
        primary_backend: MockBackend,
        secondary_backend: MockBackend,
    ):
        """副后端健康检查失败"""
        secondary_backend.health_check = AsyncMock(return_value=False)
        dual_backend = DualWriteBackend(primary_backend, secondary_backend)

        result = await dual_backend.health_check()
        assert result is False
