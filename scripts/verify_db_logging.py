"""端到端烟囱验证脚本：验证 LiteLLM 普通/流式请求落库与 OCI MySQL 计费数据.

使用方法:
1. 完整验证模式 (发送 1 个常规 + 1 个流式请求并验证 MySQL 落库):
   uv run python -m scripts.verify_db_logging --base-url http://127.0.0.1:4000

2. 纯查询模式 (仅直连 OCI MySQL 查询最新 N 条审计日志):
   uv run python -m scripts.verify_db_logging --query-only --limit 5
"""

import argparse
import asyncio
import json
import sys
import time
from typing import Any

import aiomysql
import httpx

from app.core.config import get_settings


def _auth_headers(master_key: str) -> dict[str, str]:
    """生成 LiteLLM Proxy 鉴权请求头."""
    return {
        "Authorization": f"Bearer {master_key}",
        "Content-Type": "application/json",
    }


async def send_standard_chat(
    client: httpx.AsyncClient,
    model: str,
    prompt: str = "Reply with exactly: PONG_STANDARD",
) -> dict[str, Any]:
    """发送常规 (stream=False) API 请求."""
    print(f"\n[1/3] 发送常规 API 请求 (model={model})...")
    start_t = time.perf_counter()
    response = await client.post(
        "/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 32,
            "stream": False,
        },
    )
    elapsed_ms = (time.perf_counter() - start_t) * 1000
    response.raise_for_status()
    data = response.json()

    choice = data.get("choices", [{}])[0]
    content = choice.get("message", {}).get("content") or ""
    usage = data.get("usage", {})
    req_id = data.get("id", "")
    actual_model = data.get("model", "")

    print(f"  -> HTTP {response.status_code} ({elapsed_ms:.1f}ms)")
    print(f"  -> Request ID: {req_id}")
    print(f"  -> Model: {actual_model}")
    print(f"  -> Response: {str(content).strip()!r}")
    print(f"  -> Usage: {usage}")

    return {
        "request_id": req_id,
        "model": actual_model,
        "usage": usage,
        "elapsed_ms": elapsed_ms,
    }


async def send_streaming_chat(
    client: httpx.AsyncClient,
    model: str,
    prompt: str = "Reply with exactly: PONG_STREAMING",
) -> dict[str, Any]:
    """发送流式 (stream=True) API 请求并聚合响应."""
    print(f"\n[2/3] 发送流式 API 请求 (model={model}, stream=True)...")
    start_t = time.perf_counter()
    chunks = []
    req_id = None
    actual_model = None

    async with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 32,
            "stream": True,
        },
    ) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            line = line.strip()
            if not line or not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                chunk_json = json.loads(data_str)
                if not req_id and "id" in chunk_json:
                    req_id = chunk_json["id"]
                if not actual_model and "model" in chunk_json:
                    actual_model = chunk_json["model"]
                delta = (
                    chunk_json.get("choices", [{}])[0]
                    .get("delta", {})
                    .get("content")
                )
                if delta:
                    chunks.append(str(delta))
            except Exception:
                pass

    elapsed_ms = (time.perf_counter() - start_t) * 1000
    aggregated_content = "".join(chunks)

    print(f"  -> HTTP {response.status_code} ({elapsed_ms:.1f}ms)")
    print(f"  -> Request ID: {req_id}")
    print(f"  -> Model: {actual_model}")
    print(f"  -> Aggregated Stream: {aggregated_content.strip()!r}")

    return {
        "request_id": req_id,
        "model": actual_model,
        "aggregated_content": aggregated_content,
        "elapsed_ms": elapsed_ms,
    }


async def query_mysql_logs(limit: int = 5) -> list[dict[str, Any]]:
    """直连 OCI MySQL 查询最新的审计日志记录."""
    settings = get_settings()
    target_desc = f"{settings.mysql_host}:{settings.mysql_port}"
    print(f"\n[3/3] 直连 OCI MySQL ({target_desc}) 查询最新 {limit} 条日志...")

    conn = await aiomysql.connect(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password.get_secret_value(),
        db=settings.mysql_db,
        connect_timeout=settings.connect_timeout_seconds,
        cursorclass=aiomysql.DictCursor,
    )

    query = """
    SELECT 
        request_id,
        api_key_alias,
        model_requested,
        model_used,
        prompt_tokens,
        completion_tokens,
        total_tokens,
        cost_usd,
        cost_cny,
        fx_rate,
        latency_ms,
        status_code,
        created_at
    FROM llm_request_logs
    ORDER BY created_at DESC
    LIMIT %s;
    """

    async with conn.cursor() as cursor:
        await cursor.execute(query, (limit,))
        rows = await cursor.fetchall()

    conn.close()
    return rows


def print_logs_table(rows: list[dict[str, Any]]) -> None:
    """格式化打印 MySQL 日志记录表格."""
    if not rows:
        print("\n⚠️ MySQL 中暂无日志记录。")
        return

    print("\n" + "=" * 120)
    header = (
        f"{'Request ID':<22} | {'Model Req':<18} | {'Model Used':<20} | "
        f"{'Tokens':<12} | {'USD':<10} | {'CNY':<10} | {'Rate':<7} | {'Lat':<6} | {'Code':<4}"
    )
    print(header)
    print("-" * 120)

    for r in rows:
        req_id_short = str(r["request_id"])[:20]
        tokens_str = f"{r['prompt_tokens']}/{r['completion_tokens']}/{r['total_tokens']}"
        cost_usd_str = f"${float(r['cost_usd']):.6f}"
        cost_cny_str = f"¥{float(r['cost_cny']):.6f}"
        rate_str = f"{float(r['fx_rate']):.4f}"
        lat_str = f"{r['latency_ms']}ms"
        code_str = str(r["status_code"])

        row_str = (
            f"{req_id_short:<22} | {r['model_requested']:<18} | {r['model_used']:<20} | "
            f"{tokens_str:<12} | {cost_usd_str:<10} | {cost_cny_str:<10} | "
            f"{rate_str:<7} | {lat_str:<6} | {code_str:<4}"
        )
        print(row_str)
    print("=" * 120 + "\n")


async def run_verification(
    base_url: str,
    model: str,
    query_only: bool,
    limit: int,
) -> int:
    """执行端到端验证核心逻辑."""
    settings = get_settings()
    master_key = settings.litellm_master_key.get_secret_value()

    if not query_only:
        headers = _auth_headers(master_key)
        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=30.0) as client:
            # 1. 验证 Proxy 健康状态
            health_resp = await client.get("/health")
            if health_resp.status_code != 200:
                print(f"❌ Proxy 健康检查失败: HTTP {health_resp.status_code}")
                return 1
            print(f"✅ Proxy 健康检查通过: HTTP {health_resp.status_code}")

            # 2. 发送常规请求
            await send_standard_chat(client, model)

            # 3. 发送流式请求
            await send_streaming_chat(client, model)

        # 稍等以确保异步落库 Hook 完成写入
        print("\n⏳ 等待异步落库 Hook 写入 MySQL (1.5s)...")
        await asyncio.sleep(1.5)

    # 4. 直连 MySQL 查询校验
    try:
        rows = await query_mysql_logs(limit)
        print_logs_table(rows)
        return 0
    except Exception as err:
        print(f"❌ 查询 MySQL 失败: {type(err).__name__}: {err}")
        return 1


def main() -> int:
    """CLI 入口."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:4000",
        help="LiteLLM 网关地址",
    )
    parser.add_argument(
        "--model",
        default="gemini-3.7-flash",
        help="测试请求的模型别名",
    )
    parser.add_argument(
        "--query-only",
        action="store_true",
        help="跳过发流，仅查询 MySQL 日志",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="查询日志条数",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(
            run_verification(
                base_url=args.base_url,
                model=args.model,
                query_only=args.query_only,
                limit=args.limit,
            )
        )
    except KeyboardInterrupt:
        print("\n用户手动中断。")
        return 130
    except httpx.HTTPError as err:
        print(f"❌ HTTP 请求异常: {type(err).__name__}: {err}")
        return 1
    except Exception as err:
        print(f"❌ 未知异常: {type(err).__name__}: {err}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
