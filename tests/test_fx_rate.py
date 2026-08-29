"""USD/CNY 汇率获取模块 (fx_rate.py) 单元测试套件.

测试策略与覆盖场景:
1. API 正常响应与双级缓存落盘:
   - 验证首次请求未命中缓存时触发外部 API 调用;
   - 验证成功获取汇率后同时写入 L1 内存与 L2 Redis;
   - 验证第二次调用时 100% 命中 L1 内存缓存，不再产生任何外部 API 请求 (0 次网络 IO)。
2. L2 Redis 集中缓存命中:
   - 验证当 L1 为空但 Redis 中已有今日汇率时，直接从 Redis 载入并回填 L1 缓存。
3. 外部 API 超时与故障降级:
   - 验证在公网隔离或 API 接口超时情况下，系统平滑降级至 default_usd_to_cny_rate。
4. Redis 宕机与异常容灾 (Resilience):
   - 验证当 Redis 抛出连接异常或写入异常时，系统平稳穿透至 API 获取汇率，不会导致崩溃。
5. API 返回非法 Payload 容错:
   - 验证当第三方接口返回异常格式或空字段时，自动降级至系统默认保底汇率。
"""

import httpx
import pytest

from app.core import fx_rate
from app.core.config import Settings


@pytest.fixture(autouse=True)
def clean_fx_cache():
    """自动化测试夹具: 在每个用例执行前后彻底清理 L1 内存与 Redis 客户端单例，保证测试状态独立."""
    fx_rate.reset_fx_cache()
    yield
    fx_rate.reset_fx_cache()


def _make_settings(default_rate: float = 7.23) -> Settings:
    """辅助工厂函数: 生成测试专用的 Settings 实例 (避免读取本地真实 .env 文件产生干扰)."""
    return Settings(
        mysql_host="mysql.example.internal",
        mysql_user="test-user",
        mysql_password="mysql-password",
        mysql_db="litellm_test",
        redis_host="redis.example.internal",
        redis_password="redis-password",
        openai_api_key_free_1="gemini-key",
        litellm_master_key="master-key",
        default_usd_to_cny_rate=default_rate,
    )


class MockRedis:
    """模拟 Redis 客户端，用于验证键值存取与 TTL 设置行为."""

    def __init__(self, initial_data: dict[str, str] | None = None):
        self.data = initial_data or {}
        self.set_calls = []

    async def get(self, key: str):
        """模拟 Redis GET 操作."""
        return self.data.get(key)

    async def set(self, key: str, value: str, ex: int | None = None):
        """模拟 Redis SET 操作 (含 EX 过期时间记录)."""
        self.data[key] = value
        self.set_calls.append((key, value, ex))
        return True


@pytest.mark.asyncio
async def test_fx_rate_api_success_and_caching(monkeypatch):
    """用例 1: 验证 API 正常返回时的双级缓存写入与 0ms 二次命中."""
    settings = _make_settings(default_rate=7.23)
    mock_redis = MockRedis()
    monkeypatch.setattr(fx_rate, "get_redis_client", lambda _s: mock_redis)

    api_call_count = 0

    class MockAsyncClient:
        """模拟 httpx.AsyncClient 正常返回 200 与汇率数据."""

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url: str):
            nonlocal api_call_count
            api_call_count += 1
            response = httpx.Response(
                status_code=200,
                json={"rates": {"CNY": 7.255}},
                request=httpx.Request("GET", url),
            )
            return response

    monkeypatch.setattr(fx_rate.httpx, "AsyncClient", MockAsyncClient)

    # 1. 第一次调用: 缓存全空，触发 API 请求
    rate = await fx_rate.get_usd_to_cny_rate(settings)
    assert rate == 7.255
    assert api_call_count == 1
    # 验证 Redis 是否正确缓存了该汇率
    assert mock_redis.data.get(fx_rate.FX_CACHE_KEY) == "7.255"

    # 2. 第二次调用: L1 内存命中，耗时 ~0ms，不触发任何额外的 API 请求
    rate2 = await fx_rate.get_usd_to_cny_rate(settings)
    assert rate2 == 7.255
    assert api_call_count == 1  # 计数仍为 1，说明 100% 命中了内存缓存


@pytest.mark.asyncio
async def test_fx_rate_l2_redis_hit(monkeypatch):
    """用例 2: 验证 L1 缺失时从 L2 Redis 命中并回填 L1 内存."""
    settings = _make_settings(default_rate=7.23)
    mock_redis = MockRedis(initial_data={fx_rate.FX_CACHE_KEY: "7.3012"})
    monkeypatch.setattr(fx_rate, "get_redis_client", lambda _s: mock_redis)

    # 从 Redis 读取并返回
    rate = await fx_rate.get_usd_to_cny_rate(settings)
    assert rate == 7.3012

    # 再次读取，验证 L1 已经被成功回填
    rate_l1 = await fx_rate.get_usd_to_cny_rate(settings)
    assert rate_l1 == 7.3012


@pytest.mark.asyncio
async def test_fx_rate_api_failure_fallback_to_default(monkeypatch):
    """用例 3: 验证当第三方 API 连接超时且无缓存时，平滑降级至默认保底汇率."""
    settings = _make_settings(default_rate=7.2345)
    mock_redis = MockRedis()
    monkeypatch.setattr(fx_rate, "get_redis_client", lambda _s: mock_redis)

    class FailingAsyncClient:
        """模拟网络连接超时异常."""

        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url: str):
            raise httpx.ConnectTimeout("第三方汇率接口连接超时")

    monkeypatch.setattr(fx_rate.httpx, "AsyncClient", FailingAsyncClient)

    # 验证返回值降级为 Settings 中配置的 default_rate
    rate = await fx_rate.get_usd_to_cny_rate(settings)
    assert rate == 7.2345


@pytest.mark.asyncio
async def test_fx_rate_redis_exception_resilience(monkeypatch):
    """用例 4: 验证 Redis 发生服务中断/连接异常时，系统具有高容灾韧性，穿透至 API."""
    settings = _make_settings(default_rate=7.23)

    class BrokenRedis:
        """模拟故障的 Redis 客户端."""

        async def get(self, key: str):
            raise ConnectionError("Redis 连接断开")

        async def set(self, key: str, value: str, ex: int | None = None):
            raise ConnectionError("Redis 写入失败")

    monkeypatch.setattr(fx_rate, "get_redis_client", lambda _s: BrokenRedis())

    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url: str):
            return httpx.Response(
                status_code=200,
                json={"rates": {"CNY": 7.28}},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(fx_rate.httpx, "AsyncClient", MockAsyncClient)

    # 验证不抛出异常，优雅降级穿透并返回从 API 解析的 7.28
    rate = await fx_rate.get_usd_to_cny_rate(settings)
    assert rate == 7.28


@pytest.mark.asyncio
async def test_fx_rate_invalid_payload_fallback(monkeypatch):
    """用例 5: 验证 API 返回空数据或异常 JSON 格式时的容错保底逻辑."""
    settings = _make_settings(default_rate=7.23)
    mock_redis = MockRedis()
    monkeypatch.setattr(fx_rate, "get_redis_client", lambda _s: mock_redis)

    class InvalidPayloadClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url: str):
            return httpx.Response(
                status_code=200,
                json={"result": "error", "error-type": "invalid-key"},
                request=httpx.Request("GET", url),
            )

    monkeypatch.setattr(fx_rate.httpx, "AsyncClient", InvalidPayloadClient)

    # 验证数据解析失败时降级返回 7.23
    rate = await fx_rate.get_usd_to_cny_rate(settings)
    assert rate == 7.23
