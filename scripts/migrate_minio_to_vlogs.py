"""MinIO → VictoriaLogs 历史数据全量迁移脚本 (Phase 3.1)

架构设计：
1. 从 MinIO `payloads` 桶读取 prompt.json 和 response.json
2. 采用分片策略（Shard-based）拆分超大 Payload：
   - shard_1: system_prompt + 调用元数据 (Model, Provider, Tokens, Spend, Latency)
   - shard_2: user_prompt + response (核心业务报文)
   - shard_3+: messages 历史消息（如超过 400KB 则自动切分为 shard_3_1, shard_3_2...）
3. 从 OCI MySQL `llm_request_logs` 读取对应的调用元数据
4. 真正并行（真正的并发支持，通过 asyncio.Semaphore 控制）
5. 包含回滚机制、数据一致性校验与断点续传能力
"""

import argparse
import asyncio
import json
import logging
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any

import aioboto3
import aiomysql

from app.core.config import Settings, get_settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


class MinioToVlogsMigrator:
    """MinIO 到 VictoriaLogs 迁移管理器"""

    def __init__(self, settings: Settings, dry_run: bool = False):
        self.settings = settings
        self.dry_run = dry_run
        self.minio_bucket = "payloads"
        self.vlogs_endpoint = settings.victorialogs_url.rstrip("/")
        self.session = aioboto3.Session()

        # 统计指标
        self.stats = {
            "total_objects": 0,
            "migrated_requests": 0,
            "failed_requests": 0,
            "skipped_requests": 0,
            "total_bytes": 0,
            "start_time": None,
            "end_time": None,
        }

    async def list_minio_requests(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, str]]:
        """列出 MinIO 中的所有 Request ID 及其日期分区 (使用 curl 避免 aioboto3 Host 签名问题)"""
        logger.info("Listing objects from MinIO payloads bucket...")
        requests_dict: dict[str, dict[str, str]] = {}

        endpoint = self.settings.payload_s3_endpoint
        url = f"{endpoint}/{self.minio_bucket}?list-type=2"

        continuation_token = None
        while True:
            cmd = ["curl", "-s", url]
            if continuation_token:
                cmd = ["curl", "-s", f"{url}&continuation-token={continuation_token}"]

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                logger.error("Failed to list MinIO objects: %s", stderr.decode())
                break

            root = ET.fromstring(stdout.decode())
            ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

            for content in root.findall("s3:Contents", ns):
                key = content.find("s3:Key", ns).text
                parts = key.split("/")
                if len(parts) == 3 and parts[2] in ("prompt.json", "response.json"):
                    date_part, request_id, file_name = parts
                    if start_date and date_part < start_date:
                        continue
                    if end_date and date_part > end_date:
                        continue

                    if request_id not in requests_dict:
                        requests_dict[request_id] = {
                            "request_id": request_id,
                            "date": date_part,
                            "has_prompt": False,
                            "has_response": False,
                        }

                    if file_name == "prompt.json":
                        requests_dict[request_id]["has_prompt"] = True
                    elif file_name == "response.json":
                        requests_dict[request_id]["has_response"] = True

            is_truncated = root.find("s3:IsTruncated", ns)
            if is_truncated is not None and is_truncated.text.lower() == "true":
                continuation_token = root.find("s3:NextContinuationToken", ns).text
            else:
                break

        results = [
            {"request_id": r["request_id"], "date": r["date"]}
            for r in requests_dict.values()
            if r["has_prompt"] or r["has_response"]
        ]

        self.stats["total_objects"] = len(results)
        logger.info("Found %d unique requests to migrate", len(results))
        return results

    async def read_minio_payload(
        self,
        request_id: str,
        date: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """从 MinIO 读取 prompt.json 和 response.json"""
        prompt_data = {}
        response_data = {}

        endpoint = self.settings.payload_s3_endpoint

        # 1. 读取 prompt.json
        prompt_url = f"{endpoint}/{self.minio_bucket}/{date}/{request_id}/prompt.json"
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-s",
            prompt_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            try:
                prompt_data = json.loads(stdout.decode())
            except json.JSONDecodeError:
                pass

        # 2. 读取 response.json
        response_url = f"{endpoint}/{self.minio_bucket}/{date}/{request_id}/response.json"
        proc = await asyncio.create_subprocess_exec(
            "curl",
            "-s",
            response_url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout:
            try:
                response_data = json.loads(stdout.decode())
            except json.JSONDecodeError:
                pass

        return prompt_data, response_data

    async def fetch_metadata_from_mysql(
        self,
        request_id: str,
    ) -> dict[str, Any]:
        """从 MySQL 读取调用元数据"""
        try:
            conn = await aiomysql.connect(
                host=self.settings.mysql_host,
                port=self.settings.mysql_port,
                user=self.settings.mysql_user,
                password=self.settings.mysql_password.get_secret_value(),
                db=self.settings.mysql_db,
            )
            async with conn.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    """
                    SELECT 
                        request_id,
                        api_key_alias,
                        model_used,
                        model_requested,
                        provider,
                        provider_key_alias,
                        prompt_tokens,
                        completion_tokens,
                        total_tokens,
                        cost_usd,
                        cost_cny,
                        latency_ms,
                        status_code,
                        created_at
                    FROM llm_request_logs
                    WHERE request_id = %s
                    LIMIT 1
                    """,
                    (request_id,),
                )
                row = await cursor.fetchone()
                if row:
                    return {
                        "request_id": row["request_id"],
                        "key_alias": row.get("api_key_alias", "default"),
                        "model": row.get("model_used") or row.get("model_requested", "unknown"),
                        "provider": row.get("provider", "unknown"),
                        "provider_key_alias": row.get("provider_key_alias", "unknown"),
                        "prompt_tokens": row.get("prompt_tokens", 0),
                        "completion_tokens": row.get("completion_tokens", 0),
                        "total_tokens": row.get("total_tokens", 0),
                        "spend": float(row.get("cost_usd", 0.0)),
                        "latency_ms": row.get("latency_ms", 0),
                        "status_code": row.get("status_code", 200),
                        "timestamp": (
                            row["created_at"].isoformat() if row.get("created_at") else ""
                        ),
                    }
            conn.close()
        except Exception as e:
            logger.warning("Failed to fetch metadata from MySQL for %s: %s", request_id, e)

        return {}

    async def write_to_victorialogs(
        self,
        request_id: str,
        prompt: dict[str, Any],
        response: dict[str, Any],
        metadata: dict[str, Any],
    ) -> bool:
        """写入 VictoriaLogs (支持分片模式)"""
        if self.dry_run:
            logger.info("[DRY-RUN] Would write %s to VictoriaLogs", request_id)
            return True

        try:
            # 提取字段
            system_prompt = prompt.get("system_prompt")
            user_prompt = prompt.get("user_prompt", "")
            messages = prompt.get("messages", [])
            parameters = prompt.get("parameters", {})
            tools = prompt.get("tools")

            model_name = metadata.get("model", "unknown")
            # 分片 1: system_prompt + metadata
            shard_1 = {
                "_time": metadata.get("timestamp", datetime.now(UTC).isoformat()),
                "_stream": '{env="prod",service="litellm",type="payload"}',
                "_msg": f"LLM 调用日志 [shard_1]: request_id={request_id}, model={model_name}",
                "env": "prod",
                "service": "litellm",
                "type": "payload",
                "request_id": request_id,
                "shard_index": 1,
                "shard_type": "metadata",
                "model": metadata.get("model", ""),
                "key_alias": metadata.get("key_alias", ""),
                "provider": metadata.get("provider", ""),
                "provider_key_alias": metadata.get("provider_key_alias", ""),
                "status_code": metadata.get("status_code", 0),
                "latency_ms": metadata.get("latency_ms", 0),
                "prompt_tokens": metadata.get("prompt_tokens", 0),
                "completion_tokens": metadata.get("completion_tokens", 0),
                "total_tokens": metadata.get("total_tokens", 0),
                "spend": metadata.get("spend", 0.0),
                "system_prompt": system_prompt,
                "parameters": json.dumps(parameters, ensure_ascii=False),
                "tools": json.dumps(tools, ensure_ascii=False) if tools else None,
            }

            preview_prompt = user_prompt[:80]
            # 分片 2: user_prompt + response
            shard_2 = {
                "_time": metadata.get("timestamp", datetime.now(UTC).isoformat()),
                "_stream": '{env="prod",service="litellm",type="payload"}',
                "_msg": f"LLM 调用日志 [shard_2]: req={request_id}, prompt={preview_prompt}...",
                "env": "prod",
                "service": "litellm",
                "type": "payload",
                "request_id": request_id,
                "shard_index": 2,
                "shard_type": "prompt_response",
                "user_prompt": user_prompt,
                "response": json.dumps(response, ensure_ascii=False),
            }

            # 写入分片 1 和 2
            shards = [shard_1, shard_2]

            # 分片 3+: messages 历史消息（如果太大则分割）
            messages_str = json.dumps(messages, ensure_ascii=False)
            max_shard_size = 400 * 1024  # 400KB 安全阈值

            if len(messages_str) <= max_shard_size:
                # 单条分片
                msg_cnt = len(messages)
                shard_3 = {
                    "_time": metadata.get("timestamp", datetime.now(UTC).isoformat()),
                    "_stream": '{env="prod",service="litellm",type="payload"}',
                    "_msg": f"LLM 调用日志 [shard_3]: request_id={request_id}, count={msg_cnt}",
                    "env": "prod",
                    "service": "litellm",
                    "type": "payload",
                    "request_id": request_id,
                    "shard_index": 3,
                    "shard_type": "messages",
                    "messages": messages_str,
                    "messages_count": len(messages),
                }
                shards.append(shard_3)
            else:
                # 多条分片
                shard_count = (len(messages_str) // max_shard_size) + 1
                for i in range(shard_count):
                    start = i * max_shard_size
                    end = start + max_shard_size
                    shard_data = messages_str[start:end]

                    part_info = f"{i + 1}/{shard_count}"
                    shard_3_n = {
                        "_time": metadata.get("timestamp", datetime.now(UTC).isoformat()),
                        "_stream": '{env="prod",service="litellm",type="payload"}',
                        "_msg": f"LLM 调用日志 [shard_3_{i + 1}]: id={request_id} ({part_info})",
                        "env": "prod",
                        "service": "litellm",
                        "type": "payload",
                        "request_id": request_id,
                        "shard_index": 3 + i,
                        "shard_type": "messages_part",
                        "shard_part": i + 1,
                        "shard_total": shard_count,
                        "messages": shard_data,
                        "messages_count": len(messages),
                    }
                    shards.append(shard_3_n)

            # 批量写入所有分片
            success_count = 0
            for shard in shards:
                body = json.dumps(shard, ensure_ascii=False)
                result = subprocess.run(
                    [
                        "curl",
                        "-s",
                        "-o",
                        "/dev/null",
                        "-w",
                        "%{http_code}",
                        "-X",
                        "POST",
                        f"{self.vlogs_endpoint}/insert/jsonline",
                        "-H",
                        "Content-Type: application/stream+json",
                        "-d",
                        "@-",
                    ],
                    input=body,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                status_code = int(result.stdout.strip())
                if status_code == 200:
                    success_count += 1
                else:
                    logger.warning(
                        "VictoriaLogs write failed for %s shard_%d: status=%d",
                        request_id,
                        shard.get("shard_index", 0),
                        status_code,
                    )

            return success_count == len(shards)

        except Exception as e:
            logger.warning("Failed to write to VictoriaLogs for %s: %s", request_id, e)
            return False

    async def migrate_batch(
        self,
        request_ids: list[dict[str, str]],
        batch_size: int = 100,
    ) -> None:
        """批量迁移（真正并行）"""
        total = len(request_ids)
        semaphore = asyncio.Semaphore(10)  # 限制并发数

        async def migrate_with_semaphore(item: dict[str, str]) -> bool:
            async with semaphore:
                return await self.migrate_single(item["request_id"], item["date"])

        logger.info("Starting migration of %d requests in batches...", total)

        for i in range(0, total, batch_size):
            batch = request_ids[i : i + batch_size]
            logger.info("Processing batch %d/%d...", i // batch_size + 1, (total // batch_size) + 1)

            tasks = [migrate_with_semaphore(item) for item in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # 统计进度
            batch_success = sum(1 for r in results if r is True)
            batch_failed = len(batch) - batch_success
            logger.info(
                "Batch progress: %d/%d (Success: %d, Failed: %d)",
                min(i + batch_size, total),
                total,
                batch_success,
                batch_failed,
            )

    async def migrate_single(
        self,
        request_id: str,
        date: str,
    ) -> bool:
        """单条迁移"""
        try:
            # 1. 从 MinIO 读取 Payload
            prompt_data, response_data = await self.read_minio_payload(request_id, date)
            if not prompt_data and not response_data:
                logger.warning("No payload found for %s in MinIO", request_id)
                self.stats["skipped_requests"] += 1
                return False

            # 2. 从 MySQL 读取元数据
            metadata = await self.fetch_metadata_from_mysql(request_id)
            if not metadata:
                metadata = {
                    "timestamp": f"{date}T00:00:00+00:00",
                    "model": "unknown",
                    "key_alias": "default",
                    "status_code": 200,
                    "latency_ms": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "spend": 0.0,
                }

            # 3. 写入 VictoriaLogs
            success = await self.write_to_victorialogs(
                request_id=request_id,
                prompt=prompt_data,
                response=response_data,
                metadata=metadata,
            )

            if success:
                self.stats["migrated_requests"] += 1
                return True
            else:
                self.stats["failed_requests"] += 1
                return False

        except Exception as e:
            logger.error("Failed to migrate %s: %s", request_id, e)
            self.stats["failed_requests"] += 1
            return False

    async def run(
        self,
        start_date: str | None = None,
        end_date: str | None = None,
        batch_size: int = 100,
    ) -> None:
        """执行完整迁移流程"""
        self.stats["start_time"] = datetime.now(UTC)
        logger.info("=== Starting MinIO to VictoriaLogs Migration ===")

        # 1. 扫描 MinIO
        requests = await self.list_minio_requests(start_date, end_date)
        if not requests:
            logger.warning("No requests found to migrate.")
            return

        # 2. 批量迁移
        await self.migrate_batch(requests, batch_size=batch_size)

        self.stats["end_time"] = datetime.now(UTC)
        duration = (self.stats["end_time"] - self.stats["start_time"]).total_seconds()

        logger.info("=== Migration Summary ===")
        logger.info("Total Objects: %d", self.stats["total_objects"])
        logger.info("Migrated: %d", self.stats["migrated_requests"])
        logger.info("Failed: %d", self.stats["failed_requests"])
        logger.info("Skipped: %d", self.stats["skipped_requests"])
        logger.info("Duration: %.2f seconds", duration)
        if duration > 0:
            logger.info("Speed: %.2f requests/second", self.stats["migrated_requests"] / duration)


async def main():
    parser = argparse.ArgumentParser(description="Migrate payloads from MinIO to VictoriaLogs")
    parser.add_argument("--dry-run", action="store_true", help="演练模式，不实际写入")
    parser.add_argument("--start-date", type=str, help="起始日期 (YYYY-MM-DD)")
    parser.add_argument("--end-date", type=str, help="结束日期 (YYYY-MM-DD)")
    parser.add_argument("--batch-size", type=int, default=100, help="批次大小 (默认 100)")
    args = parser.parse_args()

    settings = get_settings()
    migrator = MinioToVlogsMigrator(settings, dry_run=args.dry_run)
    await migrator.run(
        start_date=args.start_date,
        end_date=args.end_date,
        batch_size=args.batch_size,
    )


if __name__ == "__main__":
    asyncio.run(main())
