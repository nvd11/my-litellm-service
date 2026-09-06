"""PayloadBackend 抽象基类测试"""

import pytest

from app.core.payload_backend import PayloadBackend


class ConcreteBackend(PayloadBackend):
    """测试用具体实现类"""

    async def write_payload(self, request_id, prompt, response, metadata):
        return True

    async def read_payload(self, request_id, date=None):
        return {"user_prompt": "test"}, {"reply": "test"}

    async def search_payloads(self, keyword, start_date=None, end_date=None, limit=500):
        return ["test-req-id"]

    async def health_check(self):
        return True


class TestPayloadBackend:
    """PayloadBackend 接口测试"""

    def test_abstract_class_cannot_instantiate(self):
        """抽象基类不能直接实例化"""
        with pytest.raises(TypeError):
            PayloadBackend()  # type: ignore

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
            prompt={"user_prompt": "test"},
            response={"reply": "test"},
            metadata={"model": "test"},
        )
        assert result is True

    @pytest.mark.asyncio
    async def test_read_payload_interface(self):
        """read_payload 接口签名正确"""
        backend = ConcreteBackend()
        prompt, response = await backend.read_payload("test-123")
        assert prompt == {"user_prompt": "test"}
        assert response == {"reply": "test"}

    @pytest.mark.asyncio
    async def test_search_payloads_interface(self):
        """search_payloads 接口签名正确"""
        backend = ConcreteBackend()
        rids = await backend.search_payloads("test-kw")
        assert rids == ["test-req-id"]

    @pytest.mark.asyncio
    async def test_health_check_interface(self):
        """health_check 接口签名正确"""
        backend = ConcreteBackend()
        result = await backend.health_check()
        assert result is True
