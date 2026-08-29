"""USD to CNY 每日汇率获取与双级高可用缓存模块 (Dual-Tier FX Rate Caching).

业务背景与设计说明:
1. 业务目标:
   LiteLLM Proxy 默认计算并返回大模型调用的美金开销 (USD Cost)。
   为了满足国内财务结算与费用审计需求，系统需要在落库时将 USD 精准折算为人民币开销 (CNY Cost)。
2. 双级缓存 (L1 Memory + L2 Redis) 架构:
   - L1 进程内内存缓存: 读取耗时 0ms，避免高并发大模型请求对 Redis 造成连接与网络压力。
   - L2 K3s Redis 集中缓存: 跨 Pod 共享每日最新汇率，TTL 设置为 24 小时 (86400秒)。
   - 外部汇率 API (open.er-api.com): 当双级缓存失效时，异步拉取公开基准汇率。
   - 保底降级 (Fallback): 若网络隔离或第三方 API 故障，平滑降级至历史缓存或默认汇率。
3. 绝对异常隔离原则:
   所有外部 IO (Redis、HTTP API) 均由细粒度 try-except 严格包裹，任何异常仅记录 WARNING 日志，
   绝不允许因汇率换算失败而中断上游 LLM 的正常推理主流程。
"""

import logging
import time
from typing import Any

import httpx
from redis import asyncio as redis_asyncio

from app.core.config import Settings, get_settings

# 日志记录器
logger = logging.getLogger(__name__)

# ==============================================================================
# 常量定义 (Constants)
# ==============================================================================
# Redis 中缓存汇率的键名
FX_CACHE_KEY = "fx:usd_cny_rate"

# 汇率更新周期 / 缓存有效时长: 24小时 (86400秒)
FX_CACHE_TTL_SECONDS = 86400

# 公开免费实时外汇汇率 API (无须鉴权，高可用)
FX_API_URL = "https://open.er-api.com/v6/latest/USD"

# API 网络请求超时限制 (秒)
FX_API_TIMEOUT_SECONDS = 5.0

# ==============================================================================
# L1 内存缓存状态 (In-Memory Global State)
# ==============================================================================
# 进程内缓存的最新汇率数值
_l1_rate: float | None = None

# 上次成功更新 L1 缓存的单调时钟时间戳 (monotonic timestamp)
_l1_timestamp: float = 0.0

# 共享单例 Redis 异步客户端实例
_redis_client: redis_asyncio.Redis | None = None


def get_redis_client(settings: Settings) -> redis_asyncio.Redis:
    """获取或懒加载单例 Redis 异步客户端.

    参数:
        settings: 系统全局配置对象 (包含 Redis 主机、端口、密码与超时参数)

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
            decode_responses=True,  # 自动将 Redis 返回的 bytes 解码为 UTF-8 字符串
        )
    return _redis_client


def reset_fx_cache() -> None:
    """重置 L1 内存缓存并清空 Redis 客户端实例.

    主要用于单元测试中的状态隔离，确保每个用例都在纯净的环境下执行。
    """
    global _l1_rate, _l1_timestamp, _redis_client
    _l1_rate = None
    _l1_timestamp = 0.0
    _redis_client = None


async def _fetch_from_api() -> float | None:
    """向第三方汇率开放接口请求最新的 USD->CNY 汇率.

    流程:
    1. 使用 httpx.AsyncClient 异步发送 GET 请求至 open.er-api.com;
    2. 解析 JSON 响应中的 `rates.CNY` 字段;
    3. 校验数据类型与正数值有效性;
    4. 遇网络超时、DNS 错误、HTTP 非 200 或非法数据时，捕获异常并记录 Warning，返回 None。

    返回:
        float | None: 成功返回有效浮点汇率值，失败返回 None。
    """
    try:
        async with httpx.AsyncClient(timeout=FX_API_TIMEOUT_SECONDS) as client:
            response = await client.get(FX_API_URL)
            response.raise_for_status()

            data: dict[str, Any] = response.json()
            rates = data.get("rates", {})
            cny_rate = rates.get("CNY")

            # 校验字段是否存在且为大于 0 的有效数值
            if isinstance(cny_rate, (int, float)) and cny_rate > 0:
                return float(cny_rate)

            logger.warning("第三方汇率 API 返回非法或空数据: %s", data)
    except Exception as err:
        # 网络异常或接口变动时的平滑记录，不抛出异常阻断业务
        logger.warning("从汇率接口 (%s) 获取数据失败: %s", FX_API_URL, err)

    return None


async def get_usd_to_cny_rate(settings: Settings | None = None) -> float:
    """获取当日 USD 到 CNY 的兑换汇率 (具备 L1 内存 -> L2 Redis -> API -> 保底 4 级保障).

    执行流说明:
    1. 【L1 内存检查】:
       检查当前进程内 `_l1_rate` 是否有效且仍在 24 小时 TTL 内。
       若命中，直接返回 (耗时 ~0ms，零 IO 损耗)。
    2. 【L2 Redis 检查】:
       若 L1 缺失或过期，尝试从共享 Redis 读取 `fx:usd_cny_rate`。
       若命中，自动同步回填当前进程的 L1 缓存，并返回汇率。
    3. 【外部 API 刷新】:
       若 L1 与 L2 均未命中 (如每日首次启动或缓存过期)，异步发起 HTTP 请求获取最新汇率。
       获取成功后，立即更新 L1 内存，并以 86400s TTL 异步写入 L2 Redis，最后返回。
    4. 【保底降级兜底】:
       若外部 API 请求失败 (超时或无网络)，优先返回历史 L1 旧汇率；若仍无，则返回
       系统配置中的默认保底汇率 `settings.default_usd_to_cny_rate` (默认 7.2300)。

    参数:
        settings: 可选的配置实例；若未传入则自动通过 `get_settings()` 读取。

    返回:
        float: 当日生效的 USD->CNY 汇率数值。
    """
    global _l1_rate, _l1_timestamp

    if settings is None:
        settings = get_settings()

    # 使用单调时钟计算时间差，不受系统时间调整影响
    now = time.monotonic()

    # --------------------------------------------------------------------------
    # 步骤 1: 优先检查 L1 进程内存缓存 (0ms 极致性能)
    # --------------------------------------------------------------------------
    if _l1_rate is not None and (now - _l1_timestamp) < FX_CACHE_TTL_SECONDS:
        return _l1_rate

    # --------------------------------------------------------------------------
    # 步骤 2: 检查 L2 Redis 集中共享缓存 (跨 Pod 汇率一致性)
    # --------------------------------------------------------------------------
    try:
        redis = get_redis_client(settings)
        cached_val = await redis.get(FX_CACHE_KEY)
        if cached_val is not None:
            rate = float(cached_val)
            if rate > 0:
                # Redis 命中成功：同步刷新本地 L1 缓存
                _l1_rate = rate
                _l1_timestamp = now
                return rate
    except Exception as err:
        # Redis 异常时不影响后续流程，记录日志后平滑穿透至 API
        logger.warning("从 Redis 读取汇率缓存失败: %s", err)

    # --------------------------------------------------------------------------
    # 步骤 3: 请求开放汇率 API 并写入双级缓存
    # --------------------------------------------------------------------------
    api_rate = await _fetch_from_api()
    if api_rate is not None:
        # 更新本地 L1 内存缓存
        _l1_rate = api_rate
        _l1_timestamp = now

        # 异步回写 L2 Redis (设置 24 小时过期，包裹异常防止 Redis 写入报错干扰)
        try:
            redis = get_redis_client(settings)
            await redis.set(FX_CACHE_KEY, str(api_rate), ex=FX_CACHE_TTL_SECONDS)
        except Exception as err:
            logger.warning("写入汇率到 Redis 缓存失败: %s", err)

        return api_rate

    # --------------------------------------------------------------------------
    # 步骤 4: 降级容灾兜底 (历史内存缓存 -> 配置文件默认值)
    # --------------------------------------------------------------------------
    if _l1_rate is not None:
        # 优先使用过期的历史内存汇率 (比硬编码默认值更贴近近期真实汇率)
        return _l1_rate

    # 最终保底：使用 .env / Settings 中配置的安全默认汇率 (例如 7.2300)
    return settings.default_usd_to_cny_rate
