"""PayloadBackend 抽象基类测试"""

import pytest

from app.core.payload_backend import PayloadBackend


class ConcreteBackend(PayloadBackend):
    """测试用具体实现类"""

    async def write_payload(
        self,
        request_id: str,
        prompt: dict,
        response: dict,
        metadata: dict,
    ) -> bool:
        return True

    async def read_payload(
        self,
        request_id: str,
        date: str | None = None,
    ) -> tuple[dict, dict]:
        return {"prompt": "test"}, {"response": "test"}

    async def health_check(self) -> bool:
        return True


class TestPayloadBackend:
    """PayloadBackend 抽象基类测试"""

    def test_cannot_instantiate_abstract_class(self):
        """抽象基类不能直接实例化"""
        with pytest.raises(TypeError):
            PayloadBackend()

    def test_concrete_implementation(self):
        """具体实现类可以实例化"""
        backend = ConcreteBackend()
        assert isinstance(backend, PayloadBackend)

    @pytest.mark.asyncio
    async def test_write_payload_interface(self):
        """write_payload 接口签名正确"""
        backend = ConcreteBackend()
        result = await backend.write_payload(
            request_id="test-123",
            prompt={"test": "prompt"},
            response={"test": "response"},
            metadata={"model": "test-model"},
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_read_payload_interface(self):
        """read_payload 接口签名正确"""
        backend = ConcreteBackend()
        prompt, response = await backend.read_payload(
            request_id="test-123",
            date="2026-09-06",
        )
        assert prompt == {"prompt": "test"}
        assert response == {"response": "test"}

    @pytest.mark.asyncio
    async def test_health_check_interface(self):
        """health_check 接口签名正确"""
        backend = ConcreteBackend()
        result = await backend.health_check()
        assert result is True
