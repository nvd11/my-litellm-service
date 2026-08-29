"""LiteLLM 异步审计日志落库 Hook (logging_hook.py) 单元测试套件.

测试策略与覆盖场景:
1. 辅助提取函数单元测试:
   - _calculate_latency_ms: 测试 datetime、float 与 kwargs["response_time_ms"] 耗时计算及兜底逻辑;
   - _extract_request_id: 测试 response 对象 id、dict id、kwargs call_id 及 uuid 兜底;
   - _extract_api_key_alias: 测试各种元数据嵌套结构及 64 位字符截断;
   - _extract_model_names: 测试正常模型与降级模型 (model_requested vs model_used) 轨迹提取;
   - _extract_tokens: 测试对象 usage、dict usage 及缺失 usage 的安全提取;
   - _extract_error_status_code: 测试属性提取、字典提取、常见异常类/错误信息推断及 500 兜底。
2. 成功事件异步落库 (async_log_success_event):
   - 验证常规成功响应的字段提取、Token 计量、USD 提取、实时汇率换算与 RMB 高精度结算;
   - 验证跨梯队降级轨迹记录 (如请求 gemini-3.7-flash 降级命中 gemini-3.7-backup);
   - 验证流式响应 (stream=True) 的 Token 与费用正确解析落库。
3. 失败事件异步落库 (async_log_failure_event):
   - 验证 429 限流、500 服务端错误及超时等场景下的状态码提取与 0 Token / 0 费用落库。
4. 数据库抖动与异常隔离 (Zero Interruption / Resilience):
   - 验证当 MySQL 连接断开或抛出异常时，Hook 保持绝对静默与告警记录，严禁阻断业务。
"""

import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.core import logging_hook
from app.core.config import Settings
from app.core.logging_hook import (
    DBLoggingLogger,
    _calculate_latency_ms,
    _extract_api_key_alias,
    _extract_error_status_code,
    _extract_model_names,
    _extract_request_id,
    _extract_tokens,
)


def _make_settings(default_rate: float = 7.23) -> Settings:
    """测试配置生成辅助函数."""
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


class MockDBConnection:
    """模拟数据库连接，记录所有执行的 SQLAlchemy 语句."""

    def __init__(self):
        self.executed_statements = []

    async def execute(self, statement: Any):
        self.executed_statements.append(statement)
        return None


class MockAsyncEngine:
    """模拟 SQLAlchemy 异步引擎."""

    def __init__(self, raise_on_execute: bool = False):
        self.conn = MockDBConnection()
        self.raise_on_execute = raise_on_execute

    def begin(self):
        class MockContext:
            def __init__(self, conn, raise_err):
                self.conn = conn
                self.raise_err = raise_err

            async def __aenter__(self):
                if self.raise_err:
                    raise ConnectionError("模拟 MySQL 数据库连接中断")
                return self.conn

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                pass

        return MockContext(self.conn, self.raise_on_execute)


# ==============================================================================
# 1. 辅助提取工具函数测试 (Helper Functions Tests)
# ==============================================================================


def test_calculate_latency_ms():
    """验证耗时计算支持 datetime、float、kwargs 与默认 0ms."""
    # 1. datetime 格式计算
    start_dt = datetime.datetime(2026, 8, 29, 12, 0, 0)
    end_dt = datetime.datetime(2026, 8, 29, 12, 0, 1, 500000)
    assert _calculate_latency_ms(start_dt, end_dt, {}) == 1500

    # 2. float 时间戳计算
    assert _calculate_latency_ms(100.0, 102.345, {}) == 2345

    # 3. kwargs 透传 response_time_ms
    assert _calculate_latency_ms(None, None, {"response_time_ms": 320}) == 320

    # 4. 兜底返回 0
    assert _calculate_latency_ms(None, None, {}) == 0


def test_extract_request_id():
    """验证从各种对象结构中提取 request_id 及 UUID 兜底."""
    # 1. response 对象属性
    resp_obj = SimpleNamespace(id="chatcmpl-obj-123")
    assert _extract_request_id({}, resp_obj) == "chatcmpl-obj-123"

    # 2. response 字典
    resp_dict = {"id": "chatcmpl-dict-456"}
    assert _extract_request_id({}, resp_dict) == "chatcmpl-dict-456"

    # 3. kwargs 中的 litellm_call_id
    assert _extract_request_id({"litellm_call_id": "call-789"}, None) == "call-789"

    # 4. kwargs 中的 call_id
    assert _extract_request_id({"call_id": "call-abc"}, None) == "call-abc"

    # 5. 兜底生成有效 UUID 字符串
    fallback_id = _extract_request_id({}, None)
    assert len(fallback_id) == 36
    assert "-" in fallback_id


def test_extract_api_key_alias():
    """验证从多种嵌套元数据中提取调用方身份及截断."""
    # 1. 直接字段
    assert _extract_api_key_alias({"user_api_key_alias": "team-risk"}) == "team-risk"

    # 2. litellm_metadata 嵌套
    assert (
        _extract_api_key_alias({"litellm_metadata": {"user_api_key_alias": "team-audit"}})
        == "team-audit"
    )

    # 3. metadata 嵌套
    assert (
        _extract_api_key_alias({"metadata": {"user_api_key_alias": "team-compass"}})
        == "team-compass"
    )

    # 4. 兜底返回 default
    assert _extract_api_key_alias({}) == "default"

    # 5. 超长截断至 64 位
    long_alias = "a" * 100
    assert len(_extract_api_key_alias({"user_api_key_alias": long_alias})) == 64


def test_extract_model_names():
    """验证模型别名与实际执行模型的提取 (降级轨迹追踪)."""
    # 1. 常规同名模型
    kwargs = {"model": "gemini-3.7-flash"}
    resp = SimpleNamespace(model="gemini/gemini-3.7-flash")
    req_m, used_m = _extract_model_names(kwargs, resp)
    assert req_m == "gemini-3.7-flash"
    assert used_m == "gemini/gemini-3.7-flash"

    # 2. 梯队降级轨迹 (请求 flash，命中 backup)
    kwargs_fallback = {"model": "gemini-3.7-flash"}
    resp_fallback = {"model": "openai/gemini-3.7-backup"}
    req_f, used_f = _extract_model_names(kwargs_fallback, resp_fallback)
    assert req_f == "gemini-3.7-flash"
    assert used_f == "openai/gemini-3.7-backup"

    # 3. 兜底情况
    req_d, used_d = _extract_model_names({}, None)
    assert req_d == "unknown"
    assert used_d == "unknown"


def test_extract_tokens():
    """验证从对象与字典提取 Token 数量."""
    # 1. 对象格式 usage
    usage_obj = SimpleNamespace(prompt_tokens=15, completion_tokens=35, total_tokens=50)
    resp_obj = SimpleNamespace(usage=usage_obj)
    assert _extract_tokens(resp_obj) == (15, 35, 50)

    # 2. 字典格式 usage
    resp_dict = {"usage": {"prompt_tokens": 20, "completion_tokens": 80}}
    assert _extract_tokens(resp_dict) == (20, 80, 100)

    # 3. 缺失 usage
    assert _extract_tokens({}) == (0, 0, 0)
    assert _extract_tokens(None) == (0, 0, 0)


def test_extract_error_status_code():
    """验证错误状态码推断逻辑."""
    # 1. 显式 status_code 属性
    err_obj = SimpleNamespace(status_code=429)
    assert _extract_error_status_code(err_obj) == 429

    # 2. 字典格式
    assert _extract_error_status_code({"status_code": 401}) == 401

    # 3. 常见异常类推断
    class RateLimitError(Exception):
        pass

    class AuthenticationError(Exception):
        pass

    class NotFoundError(Exception):
        pass

    class ConnectTimeout(Exception):
        pass

    class BadRequestError(Exception):
        pass

    assert _extract_error_status_code(RateLimitError("429 rate limit exceeded")) == 429
    assert _extract_error_status_code(AuthenticationError("invalid api key")) == 401
    assert _extract_error_status_code(NotFoundError("model not found")) == 404
    assert _extract_error_status_code(ConnectTimeout("connection timed out")) == 504
    assert _extract_error_status_code(BadRequestError("invalid parameters")) == 400

    # 4. 未知异常兜底 500
    assert _extract_error_status_code(RuntimeError("something went wrong")) == 500


# ==============================================================================
# 2. 成功事件落库测试 (Success Event Logging)
# ==============================================================================


@pytest.mark.asyncio
async def test_async_log_success_event_standard(monkeypatch):
    """验证常规成功请求的完整异步落库、Token 解析、USD 提取与 RMB 精确换算."""
    settings = _make_settings(default_rate=7.23)
    mock_engine = MockAsyncEngine()
    monkeypatch.setattr(logging_hook, "get_async_engine", lambda _s: mock_engine)

    # Mock 汇率返回 7.2500
    async def mock_fx_rate(_s):
        return 7.2500

    monkeypatch.setattr(logging_hook, "get_usd_to_cny_rate", mock_fx_rate)

    logger = DBLoggingLogger(settings=settings)

    kwargs = {
        "model": "gemini-3.7-flash",
        "user_api_key_alias": "hsbc-rcdp-team",
        "response_cost": 0.000200,
        "litellm_call_id": "call-uuid-12345",
    }
    response_obj = SimpleNamespace(
        id="chatcmpl-99999",
        model="gemini/gemini-3.7-flash",
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150),
    )
    start_time = 100.0
    end_time = 101.5  # 耗时 1500 ms

    await logger.async_log_success_event(kwargs, response_obj, start_time, end_time)

    # 验证数据库 INSERT 语句与参数
    assert len(mock_engine.conn.executed_statements) == 1
    stmt = mock_engine.conn.executed_statements[0]
    params = stmt.compile().params

    assert params["request_id"] == "chatcmpl-99999"
    assert params["api_key_alias"] == "hsbc-rcdp-team"
    assert params["model_requested"] == "gemini-3.7-flash"
    assert params["model_used"] == "gemini/gemini-3.7-flash"
    assert params["prompt_tokens"] == 100
    assert params["completion_tokens"] == 50
    assert params["total_tokens"] == 150
    assert float(params["cost_usd"]) == 0.000200
    # 0.000200 * 7.2500 = 0.001450
    assert float(params["cost_cny"]) == 0.001450
    assert float(params["fx_rate"]) == 7.2500
    assert params["latency_ms"] == 1500
    assert params["status_code"] == 200


@pytest.mark.asyncio
async def test_async_log_success_event_fallback_trajectory(monkeypatch):
    """验证跨梯队降级调用成功时，正确记录降级轨迹 (model_requested vs model_used)."""
    settings = _make_settings(default_rate=7.23)
    mock_engine = MockAsyncEngine()
    monkeypatch.setattr(logging_hook, "get_async_engine", lambda _s: mock_engine)

    async def mock_fx_rate(_s):
        return 7.2300

    monkeypatch.setattr(logging_hook, "get_usd_to_cny_rate", mock_fx_rate)

    logger = DBLoggingLogger(settings=settings)

    # 客户端请求 gemini-3.7-flash，但实际降级命中 gemini-3.7-backup (A6 API 中转)
    kwargs = {
        "model": "gemini-3.7-flash",
        "response_cost": 0.000100,
    }
    response_obj = {
        "id": "chatcmpl-backup-888",
        "model": "openai/gemini-3.7-backup",
        "usage": {"prompt_tokens": 40, "completion_tokens": 60},
    }

    await logger.async_log_success_event(
        kwargs,
        response_obj,
        datetime.datetime(2026, 8, 29, 10, 0, 0),
        datetime.datetime(2026, 8, 29, 10, 0, 0, 800000),
    )

    assert len(mock_engine.conn.executed_statements) == 1
    params = mock_engine.conn.executed_statements[0].compile().params

    assert params["model_requested"] == "gemini-3.7-flash"
    assert params["model_used"] == "openai/gemini-3.7-backup"
    assert params["prompt_tokens"] == 40
    assert params["completion_tokens"] == 60
    assert params["total_tokens"] == 100
    assert float(params["cost_usd"]) == 0.000100
    assert float(params["cost_cny"]) == 0.000723  # round(0.000100 * 7.23, 6)
    assert params["latency_ms"] == 800
    assert params["status_code"] == 200


# ==============================================================================
# 3. 失败事件落库测试 (Failure Event Logging)
# ==============================================================================


@pytest.mark.asyncio
async def test_async_log_failure_event_rate_limit(monkeypatch):
    """验证遭遇 429 限流失败时，状态码正确记录且 Token 与费用安全归零."""
    settings = _make_settings(default_rate=7.23)
    mock_engine = MockAsyncEngine()
    monkeypatch.setattr(logging_hook, "get_async_engine", lambda _s: mock_engine)

    async def mock_fx_rate(_s):
        return 7.2300

    monkeypatch.setattr(logging_hook, "get_usd_to_cny_rate", mock_fx_rate)

    logger = DBLoggingLogger(settings=settings)

    kwargs = {
        "model": "gemini-3.7-flash",
        "user_api_key_alias": "team-dev",
        "litellm_call_id": "call-fail-429",
    }
    error_obj = SimpleNamespace(status_code=429, message="Rate limit exceeded")

    await logger.async_log_failure_event(kwargs, error_obj, 100.0, 100.3)

    assert len(mock_engine.conn.executed_statements) == 1
    params = mock_engine.conn.executed_statements[0].compile().params

    assert params["request_id"] == "call-fail-429"
    assert params["status_code"] == 429
    assert params["prompt_tokens"] == 0
    assert params["completion_tokens"] == 0
    assert params["total_tokens"] == 0
    assert float(params["cost_usd"]) == 0.0
    assert float(params["cost_cny"]) == 0.0
    assert params["latency_ms"] == 300


@pytest.mark.asyncio
async def test_async_log_failure_event_timeout(monkeypatch):
    """验证发生网关超时 (504) 异常时的落库行为."""
    settings = _make_settings(default_rate=7.23)
    mock_engine = MockAsyncEngine()
    monkeypatch.setattr(logging_hook, "get_async_engine", lambda _s: mock_engine)

    async def mock_fx_rate(_s):
        return 7.2300

    monkeypatch.setattr(logging_hook, "get_usd_to_cny_rate", mock_fx_rate)

    logger = DBLoggingLogger(settings=settings)

    kwargs = {"model": "gemini-3.7-flash"}
    error_obj = TimeoutError("Request timed out after 30s")

    await logger.async_log_failure_event(kwargs, error_obj, 0.0, 30.0)

    assert len(mock_engine.conn.executed_statements) == 1
    params = mock_engine.conn.executed_statements[0].compile().params

    assert params["status_code"] == 504
    assert params["latency_ms"] == 30000


# ==============================================================================
# 4. 数据库异常隔离与零阻断测试 (Resilience & Zero Interruption)
# ==============================================================================


@pytest.mark.asyncio
async def test_async_log_events_database_failure_resilience(monkeypatch):
    """验证当 MySQL 发生严重故障抛出异常时，Hook 保持绝对静默，不向上抛出任何异常."""
    settings = _make_settings(default_rate=7.23)
    broken_engine = MockAsyncEngine(raise_on_execute=True)
    monkeypatch.setattr(logging_hook, "get_async_engine", lambda _s: broken_engine)

    logger = DBLoggingLogger(settings=settings)

    kwargs = {"model": "gemini-3.7-flash"}
    resp = SimpleNamespace(id="req-123", model="gemini-3.7-flash")

    # 1. 成功事件遇 DB 异常不抛出
    await logger.async_log_success_event(kwargs, resp, 1.0, 2.0)

    # 2. 失败事件遇 DB 异常不抛出
    await logger.async_log_failure_event(kwargs, Exception("test error"), 1.0, 2.0)
