"""LiteLLM 异步审计日志落库 Hook 模块 (Async MySQL Audit Logging Hook).

业务背景与架构设计:
1. 业务目标:
   捕获 LiteLLM Proxy 接收到的所有普通请求与流式 (stream=True) 请求，
   无论调用成功 (HTTP 200) 还是失败 (HTTP 429, 500, Timeout 等)，均在后台无感落库至
   OCI MySQL HeatWave (litellm_db.llm_request_logs)。
2. 基于 SQLAlchemy 2.0 Core:
   - 使用 `app.db.tables.llm_request_logs` 表定义与 `sqlalchemy.insert()` 查询构建器;
   - 使用 `app.db.engine.get_async_engine()` 获取具备 `pool_pre_ping=True` 与 `pool_recycle=300`
     高可用保活特性的异步引擎;
   - 彻底避免字符串 SQL 拼接，享受参数化安全与强类型保障。
3. 记录核心指标:
   - 全链路追踪 ID 与团队 Key 别名 (request_id, api_key_alias)
   - 路由与降级轨迹 (model_requested vs model_used)
   - 准确 Token 计量 (prompt_tokens, completion_tokens, total_tokens)
   - 财务费用结算 (美金 cost_usd, 当日汇率 fx_rate, 折合人民币 cost_cny)
   - 性能与可用性 (响应耗时 latency_ms, HTTP 状态码 status_code)
4. 绝对无感与异常隔离 (Zero Interruption):
   所有落库与汇率换算逻辑完全运行在 asyncio 后台协程中，任何数据库抖动或异常均被静默捕获记录，
   绝对不向调用方客户端抛出异常，保障 100% SLA。
"""

import datetime
import logging
import uuid
from typing import Any

from litellm.integrations.custom_logger import CustomLogger
from sqlalchemy import insert

from app.core.config import Settings, get_settings
from app.core.fx_rate import get_usd_to_cny_rate
from app.db import get_async_engine, llm_request_logs

# 日志记录器
logger = logging.getLogger(__name__)


def _calculate_latency_ms(
    start_time: Any,
    end_time: Any,
    kwargs: dict[str, Any],
) -> int:
    """计算请求响应耗时 (毫秒)."""
    if isinstance(start_time, datetime.datetime) and isinstance(end_time, datetime.datetime):
        return int((end_time - start_time).total_seconds() * 1000)
    if isinstance(start_time, (int, float)) and isinstance(end_time, (int, float)):
        return int(round((end_time - start_time) * 1000))
    if "response_time_ms" in kwargs:
        return int(round(float(kwargs["response_time_ms"])))
    return 0


def _extract_request_id(kwargs: dict[str, Any], response_obj: Any) -> str:
    """安全提取请求唯一 ID (优先取 response.id，其次取 litellm_call_id，最后生成 UUID)."""
    if hasattr(response_obj, "id") and response_obj.id:
        return str(response_obj.id)
    if isinstance(response_obj, dict) and response_obj.get("id"):
        return str(response_obj["id"])
    if "litellm_call_id" in kwargs and kwargs["litellm_call_id"]:
        return str(kwargs["litellm_call_id"])
    if "call_id" in kwargs and kwargs["call_id"]:
        return str(kwargs["call_id"])
    return str(uuid.uuid4())


def _extract_api_key_alias(kwargs: dict[str, Any]) -> str:
    """提取客户端调用方 Key 别名或团队身份.

    深度支持 LiteLLM 官方回调的多层嵌套结构:
    1. standard_logging_object.metadata (LiteLLM 官方标准日志对象)
    2. litellm_params.metadata / litellm_params
    3. 顶层 metadata / litellm_metadata / user_api_key_dict / user_api_key_metadata
    4. 顶层字段 (user_api_key_alias / key_alias / api_key_alias / user_api_key_user_id)
    5. 兜底返回 'default'.
    """
    candidates: list[Any] = []

    # 1. LiteLLM standard_logging_object (标准日志对象)
    std_obj = kwargs.get("standard_logging_object")
    if isinstance(std_obj, dict):
        std_meta = std_obj.get("metadata")
        if isinstance(std_meta, dict):
            candidates.extend([
                std_meta.get("user_api_key_alias"),
                std_meta.get("key_alias"),
                std_meta.get("user_api_key_user_id"),
            ])
        candidates.extend([
            std_obj.get("user_api_key_alias"),
            std_obj.get("key_alias"),
        ])

    # 2. litellm_params (路由与请求参数)
    litellm_params = kwargs.get("litellm_params")
    if isinstance(litellm_params, dict):
        lp_meta = litellm_params.get("metadata")
        if isinstance(lp_meta, dict):
            candidates.extend([
                lp_meta.get("user_api_key_alias"),
                lp_meta.get("key_alias"),
                lp_meta.get("user_api_key_user_id"),
            ])
        candidates.extend([
            litellm_params.get("user_api_key_alias"),
            litellm_params.get("key_alias"),
            litellm_params.get("api_key_alias"),
        ])

    # 3. 顶层 metadata 与各类元数据字典
    for meta_key in ("metadata", "litellm_metadata", "user_api_key_metadata"):
        meta_dict = kwargs.get(meta_key)
        if isinstance(meta_dict, dict):
            candidates.extend([
                meta_dict.get("user_api_key_alias"),
                meta_dict.get("key_alias"),
                meta_dict.get("user_api_key_user_id"),
            ])

    # 4. user_api_key_dict 对象或字典
    key_dict = kwargs.get("user_api_key_dict")
    if isinstance(key_dict, dict):
        candidates.extend([
            key_dict.get("key_alias"),
            key_dict.get("user_id"),
        ])
    elif key_dict is not None:
        candidates.extend([
            getattr(key_dict, "key_alias", None),
            getattr(key_dict, "user_id", None),
        ])

    # 5. 顶层直取
    candidates.extend([
        kwargs.get("user_api_key_alias"),
        kwargs.get("key_alias"),
        kwargs.get("api_key_alias"),
        kwargs.get("user_api_key_user_id"),
    ])

    for item in candidates:
        if item is not None:
            s = str(item).strip()
            if s and s.lower() != "none":
                return s[:64]

    return "default"


def _extract_model_names(kwargs: dict[str, Any], response_obj: Any) -> tuple[str, str]:
    """提取请求模型别名 (model_requested) 与实际命中上游模型 (model_used).

    用于清晰展示模型路由和梯队降级轨迹 (如请求 gemini-3.7-flash -> 降级为 gemini-3.7-backup).
    """
    model_requested = (
        kwargs.get("model")
        or kwargs.get("model_requested")
        or kwargs.get("litellm_params", {}).get("model")
        or "unknown"
    )

    model_used = "unknown"
    if hasattr(response_obj, "model") and response_obj.model:
        model_used = str(response_obj.model)
    elif isinstance(response_obj, dict) and response_obj.get("model"):
        model_used = str(response_obj["model"])
    else:
        model_used = str(model_requested)

    return str(model_requested)[:64], str(model_used)[:64]


def _extract_tokens(response_obj: Any) -> tuple[int, int, int]:
    """从响应对象中安全提取 Prompt, Completion, Total Token 数量."""
    usage = getattr(response_obj, "usage", None)
    if usage is None and isinstance(response_obj, dict):
        usage = response_obj.get("usage")

    if usage is None:
        return 0, 0, 0

    if isinstance(usage, dict):
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
    else:
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(
            getattr(usage, "total_tokens", 0) or (prompt_tokens + completion_tokens)
        )

    return prompt_tokens, completion_tokens, total_tokens


def _extract_error_status_code(response_obj: Any) -> int:
    """根据异常对象推断 HTTP 错误状态码 (429 限流, 500 服务端错误, 504 超时等)."""
    if hasattr(response_obj, "status_code") and isinstance(response_obj.status_code, int):
        return response_obj.status_code
    if isinstance(response_obj, dict) and "status_code" in response_obj:
        return int(response_obj["status_code"])

    error_name = type(response_obj).__name__.lower()
    error_msg = str(response_obj).lower()

    if "ratelimit" in error_name or "429" in error_msg:
        return 429
    if "auth" in error_name or "401" in error_msg:
        return 401
    if "notfound" in error_name or "404" in error_msg:
        return 404
    if "timeout" in error_name or "504" in error_msg or "timed out" in error_msg:
        return 504
    if "badrequest" in error_name or "400" in error_msg:
        return 400

    return 500


class DBLoggingLogger(CustomLogger):
    """LiteLLM 异步数据库日志审计 Hook (基于 SQLAlchemy 2.0 Core).

    继承 CustomLogger，无缝挂接在 LiteLLM 请求生命周期的成功与失败事件节点。
    """

    def __init__(self, settings: Settings | None = None) -> None:
        super().__init__()
        self.settings = settings

    async def async_log_success_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        """成功请求异步落库钩子 (HTTP 200).

        流程:
        1. 提取请求元数据、Token 消耗与美金开销;
        2. 异步调用双级缓存汇率模块获取当日 USD/CNY 汇率;
        3. 高精度计算人民币开销 cost_cny = round(cost_usd * fx_rate, 6);
        4. 通过 SQLAlchemy AsyncEngine 异步执行参数化 INSERT 语句;
        5. 全程包裹在 try-except 中，保障零业务中断。
        """
        try:
            settings = self.settings or get_settings()

            record_id = str(uuid.uuid4())
            request_id = _extract_request_id(kwargs, response_obj)
            api_key_alias = _extract_api_key_alias(kwargs)
            model_requested, model_used = _extract_model_names(kwargs, response_obj)
            prompt_tokens, completion_tokens, total_tokens = _extract_tokens(response_obj)

            # 提取美金成本 (LiteLLM 会在 kwargs 或 response 中注入 response_cost)
            raw_cost_usd = (
                kwargs.get("response_cost")
                or getattr(response_obj, "response_cost", 0.0)
                or 0.0
            )
            cost_usd = round(float(raw_cost_usd), 6)

            # 获取当日汇率并折算人民币 (四舍五入保留6位小数)
            fx_rate = await get_usd_to_cny_rate(settings)
            cost_cny = round(cost_usd * fx_rate, 6)

            latency_ms = _calculate_latency_ms(start_time, end_time, kwargs)
            status_code = 200

            # 基于 SQLAlchemy Core insert() 语句进行异步落库
            stmt = insert(llm_request_logs).values(
                id=record_id,
                request_id=request_id,
                api_key_alias=api_key_alias,
                model_requested=model_requested,
                model_used=model_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost_usd=cost_usd,
                cost_cny=cost_cny,
                fx_rate=fx_rate,
                latency_ms=latency_ms,
                status_code=status_code,
            )

            engine = get_async_engine(settings)
            async with engine.begin() as conn:
                await conn.execute(stmt)

            logger.debug(
                "Logged request %s (model=%s, cost_usd=%s)",
                request_id,
                model_used,
                cost_usd,
            )
        except Exception as err:
            # 记录警告日志，绝不向客户端抛出异常
            logger.warning("Failed to async log success event to MySQL: %s", err)

    async def async_log_failure_event(
        self,
        kwargs: dict[str, Any],
        response_obj: Any,
        start_time: Any,
        end_time: Any,
    ) -> None:
        """失败请求异步落库钩子 (HTTP 429, 500, Timeout 等).

        流程:
        1. 提取请求元数据、错误状态码与耗时;
        2. Tokens 计 0，费用计 0.000000;
        3. 获取保底汇率并通过 SQLAlchemy 异步参数化写入 MySQL;
        4. 全程包裹在 try-except 中，保障异常隔离。
        """
        try:
            settings = self.settings or get_settings()

            record_id = str(uuid.uuid4())
            request_id = _extract_request_id(kwargs, response_obj)
            api_key_alias = _extract_api_key_alias(kwargs)
            model_requested, model_used = _extract_model_names(kwargs, response_obj)

            # 失败请求 Token 与费用归零
            prompt_tokens, completion_tokens, total_tokens = 0, 0, 0
            cost_usd, cost_cny = 0.0, 0.0

            fx_rate = await get_usd_to_cny_rate(settings)
            latency_ms = _calculate_latency_ms(start_time, end_time, kwargs)
            status_code = _extract_error_status_code(response_obj)

            stmt = insert(llm_request_logs).values(
                id=record_id,
                request_id=request_id,
                api_key_alias=api_key_alias,
                model_requested=model_requested,
                model_used=model_used,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost_usd=cost_usd,
                cost_cny=cost_cny,
                fx_rate=fx_rate,
                latency_ms=latency_ms,
                status_code=status_code,
            )

            engine = get_async_engine(settings)
            async with engine.begin() as conn:
                await conn.execute(stmt)

            logger.debug(
                "Logged failure %s (status_code=%s)",
                request_id,
                status_code,
            )
        except Exception as err:
            logger.warning("Failed to async log failure event to MySQL: %s", err)


# ==============================================================================
# LiteLLM 默认导入实例 (LiteLLM Callback Entrypoint)
# ==============================================================================
# LiteLLM 在 config.yaml 中配置 `callbacks` 时自动加载此单例
custom_logger = DBLoggingLogger()
