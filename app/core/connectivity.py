"""Read-only health checks for external infrastructure dependencies."""

import inspect
from dataclasses import dataclass
from time import perf_counter

import aiomysql
from redis import asyncio as redis_asyncio

from app.core.config import Settings


@dataclass(frozen=True)
class CheckResult:
    """Stable, non-sensitive result returned by an infrastructure check."""

    name: str
    ok: bool
    latency_ms: float | None
    detail: str


def _elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000.0


def _error_detail(error: BaseException) -> str:
    """Map client errors to a stable allow-listed summary."""

    error_name = type(error).__name__.lower()
    if "auth" in error_name or "access" in error_name:
        return "authentication_failed"
    if "timeout" in error_name:
        return "timeout"
    if "refused" in error_name or "connection" in error_name:
        return "connection_refused"
    return "unexpected_error"


async def check_mysql(settings: Settings) -> CheckResult:
    """Connect to MySQL and execute only ``SELECT 1``."""

    started = perf_counter()
    connection = None
    cursor = None
    try:
        connection = await aiomysql.connect(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password.get_secret_value(),
            db=settings.mysql_db,
            connect_timeout=settings.connect_timeout_seconds,
        )
        cursor = await connection.cursor()
        await cursor.execute("SELECT 1")
        row = await cursor.fetchone()
        if not row or row[0] != 1:
            return CheckResult("mysql", False, _elapsed_ms(started), "unexpected_result")
        return CheckResult("mysql", True, _elapsed_ms(started), "connected")
    except Exception as error:  # noqa: BLE001 - health checks must return stable results.
        return CheckResult("mysql", False, _elapsed_ms(started), _error_detail(error))
    finally:
        if cursor is not None:
            close_result = cursor.close()
            if inspect.isawaitable(close_result):
                await close_result
        if connection is not None:
            connection.close()


async def check_redis(settings: Settings) -> CheckResult:
    """Authenticate with Redis and execute ``PING``."""

    started = perf_counter()
    client = redis_asyncio.Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password.get_secret_value(),
        socket_connect_timeout=settings.connect_timeout_seconds,
        socket_timeout=settings.connect_timeout_seconds,
        decode_responses=True,
    )
    try:
        if not await client.ping():
            return CheckResult("redis", False, _elapsed_ms(started), "unexpected_result")
        return CheckResult("redis", True, _elapsed_ms(started), "connected")
    except Exception as error:  # noqa: BLE001 - health checks must return stable results.
        return CheckResult("redis", False, _elapsed_ms(started), _error_detail(error))
    finally:
        await client.aclose()


async def check_all(settings: Settings) -> list[CheckResult]:
    """Run dependency checks concurrently and return them in a deterministic order."""

    mysql_result, redis_result = await aiomysql_gather_checks(
        check_mysql(settings),
        check_redis(settings),
    )
    return [mysql_result, redis_result]


async def aiomysql_gather_checks(*tasks):
    import asyncio

    return await asyncio.gather(*tasks)
