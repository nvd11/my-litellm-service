"""VictoriaLogs Payload 后端实现.

支持超大报文智能切片（分块 Chunking，单块控制在 1.5MB 以内，避开 VictoriaLogs 2MB 硬上限），
并在读取时动态检索全部分片按序号升序还原完整 Payload。
"""

import json
import logging
from typing import Any

import httpx

from app.core.config import Settings
from app.core.payload_backend import PayloadBackend

logger = logging.getLogger(__name__)

# 单块日志大小上限 (字符数)
# 注意：VictoriaLogs 限制的是字节数（1,999,000 bytes），不是字符数！
# 根据实际测试，平均每个字符约 1.34 字节（中英文混合）
# 为了安全，设置 chunk_size = 1,300,000 字符（约 1.74MB 字节），预留 200KB 给 metadata
DEFAULT_CHUNK_SIZE = 1_300_000

# VictoriaLogs 单行硬上限 (1.9MB，实际为 1,999,000 字节)
# 预留 99KB 给 metadata 和 JSON 结构开销
SAFE_SINGLE_ENTRY_LIMIT = 1_900_000


class VictoriaLogsBackend(PayloadBackend):
    """VictoriaLogs 存储后端，内置分块防超限与动态聚合还原能力."""

    def __init__(
        self,
        settings: Settings,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        safe_single_entry_limit: int = SAFE_SINGLE_ENTRY_LIMIT,
    ) -> None:
        self.settings = settings
        self.endpoint = settings.victorialogs_url.rstrip("/")
        # 写入超时：大 payload (1.3MB+ 分片) 在网络抖动时需要更长时间
        # 之前 15s 太短导致 VictoriaLogs 端报 "unexpected EOF" 客户端提前断开
        self.timeout = httpx.Timeout(60.0, connect=10.0)
        self.chunk_size = chunk_size
        self.safe_single_entry_limit = safe_single_entry_limit

    def _split_into_chunks(self, text: str) -> list[str]:
        """将长字符串按 chunk_size 拆分成连续分片列表."""
        if not text:
            return [""]
        if len(text) <= self.chunk_size:
            return [text]
        return [text[i : i + self.chunk_size] for i in range(0, len(text), self.chunk_size)]

    async def write_payload(
        self,
        request_id: str,
        prompt: dict[str, Any],
        response: dict[str, Any],
        metadata: dict[str, Any],
    ) -> bool:
        """写入 VictoriaLogs（支持按 1.5MB 分块切片，单批批量发送）.

        当 prompt 序列化后 <= chunk_size 时，单条记录入库 (shard_index=1, total_shards=1)。
        当 prompt 序列化后 > chunk_size 时，自动线性分块为多个分片，
        各分片携带 shard_index 和 total_shards，单次 HTTP 请求以 JSONLine 批量推入 VictoriaLogs。

        Args:
            request_id: 请求唯一标识
            prompt: 用户输入报文
            response: 模型输出报文
            metadata: 调用元数据（model, key_alias, tokens, latency 等）

        Returns:
            bool: 写入是否成功
        """
        timestamp = metadata.get("timestamp", "")
        model = str(metadata.get("model", "unknown"))
        key_alias = str(metadata.get("key_alias", ""))
        status_code = int(metadata.get("status_code", 0))
        latency_ms = int(metadata.get("latency_ms", 0))

        prompt_str = json.dumps(prompt, ensure_ascii=False)
        response_str = json.dumps(response, ensure_ascii=False)

        # 关键修复：检查单条日志总大小（prompt + response + metadata 开销）
        # 如果总大小超过安全阈值，即使 prompt 本身 <= chunk_size 也要强制分块
        metadata_overhead = 500  # metadata 字段约 500 字符
        single_entry_total_size = len(prompt_str) + len(response_str) + metadata_overhead

        if single_entry_total_size > self.safe_single_entry_limit:
            # 总大小超限，强制分块（即使 prompt 本身可能 <= chunk_size）
            # 使用更保守的 chunk_size 确保每片加上 metadata 后仍低于硬上限
            conservative_chunk_size = self.chunk_size - len(response_str) - metadata_overhead
            if conservative_chunk_size <= 0:
                # response 本身太大，使用更小的 chunk_size 确保能分块
                # 至少保证每片 prompt 不超过 chunk_size 的一半
                conservative_chunk_size = max(1, self.chunk_size // 2)
            prompt_chunks = [
                prompt_str[i : i + conservative_chunk_size]
                for i in range(0, len(prompt_str), conservative_chunk_size)
            ]
        else:
            # 总大小安全，使用标准分块逻辑
            prompt_chunks = self._split_into_chunks(prompt_str)

        total_shards = len(prompt_chunks)

        log_entries: list[dict[str, Any]] = []
        for idx, chunk in enumerate(prompt_chunks, 1):
            shard_msg = (
                f"LLM 调用日志: request_id={request_id}, model={model}, "
                f"status={status_code}, latency={latency_ms}ms"
            )
            if total_shards > 1:
                shard_msg += f" [shard {idx}/{total_shards}]"

            entry: dict[str, Any] = {
                "_time": timestamp,
                "_stream": '{env="prod",service="litellm",type="payload"}',
                "_msg": shard_msg,
                "env": "prod",
                "service": "litellm",
                "type": "payload",
                "request_id": request_id,
                "model": model,
                "key_alias": key_alias,
                "status_code": status_code,
                "latency_ms": latency_ms,
                "prompt_tokens": metadata.get("prompt_tokens", 0),
                "completion_tokens": metadata.get("completion_tokens", 0),
                "total_tokens": metadata.get("total_tokens", 0),
                "spend": float(metadata.get("spend", 0.0)),
                "shard_index": idx,
                "total_shards": total_shards,
                "prompt_chunk": chunk,
                # 兼容旧单行读取字段 prompt
                "prompt": chunk if total_shards == 1 else "",
                # response 通常较小，挂在第 1 分片
                "response": response_str if idx == 1 else "",
            }
            log_entries.append(entry)

        # 批量 JSONLine 序列化，一次网络请求批量写入
        body = "\n".join(json.dumps(e, ensure_ascii=False) for e in log_entries) + "\n"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.endpoint}/insert/jsonline",
                    content=body.encode("utf-8"),
                    headers={"Content-Type": "application/stream+json"},
                )
                if resp.status_code not in (200, 204):
                    logger.warning(
                        "VictoriaLogs write failed for %s: HTTP %s, body_preview=%s",
                        request_id,
                        resp.status_code,
                        resp.text[:500] if resp.text else "",
                    )
                    return False
                logger.info(
                    "VictoriaLogs write success for %s: shards=%d, total_size=%d bytes",
                    request_id,
                    total_shards,
                    len(body.encode("utf-8")),
                )
                return True
        except Exception as e:
            logger.warning("VictoriaLogs write failed for %s: %s", request_id, e)
            return False

    async def read_payload(
        self,
        request_id: str,
        date: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """从 VictoriaLogs 查询并动态聚合还原分片（LogsQL）.

        支持动态查询该 request_id 下的所有分片记录，按 shard_index 升序重组拼接 prompt_chunk，
        还原为完整的 Prompt 和 Response 结构。

        Args:
            request_id: 请求唯一标识
            date: 可选日期分区（YYYY-MM-DD），用于加速时间过滤

        Returns:
            tuple: (prompt_data, response_data)，不存在时返回空 dict
        """
        # 使用精确匹配与前缀过滤
        query = f'env: "prod" AND type: "payload" AND request_id: exact("{request_id}")'
        if date:
            query += f" AND _time: {date}"

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(
                    f"{self.endpoint}/select/logsql/query",
                    data={"query": query, "limit": "1000"},
                )
                if resp.status_code != 200:
                    logger.warning(
                        "VictoriaLogs query failed for %s: status=%s",
                        request_id,
                        resp.status_code,
                    )
                    return {}, {}

                lines = [line.strip() for line in resp.text.strip().split("\n") if line.strip()]
                if not lines:
                    return {}, {}

                parsed_docs: list[dict[str, Any]] = []
                for line in lines:
                    try:
                        parsed_docs.append(json.loads(line))
                    except Exception as json_err:
                        logger.warning("Failed to parse log line for %s: %s", request_id, json_err)

                if not parsed_docs:
                    return {}, {}

                # 1. 严格按 shard_index 去重（避免重试写入产生相同分片）
                # 竞选规则（元组字典序逐项比较）：
                #   1) response 非空且更长者优先 —— LiteLLM Router 重试场景下，
                #      同一 request_id 可能被写入两次（第一次上游断流只拿到截断回复，
                #      第二次重试成功拿到完整回复），必须优先保留完整版；
                #   2) response 同长时 _time 更新者优先 —— 最新写入的记录最接近最终状态；
                #   3) 兜底 prompt_chunk 更长者优先（兼容历史行为）。
                unique_shards: dict[int, dict[str, Any]] = {}
                for d in parsed_docs:
                    idx = int(d.get("shard_index", 1))
                    if idx not in unique_shards:
                        unique_shards[idx] = d
                        continue

                    def _rank(doc: dict[str, Any]) -> tuple[int, str, int]:
                        return (
                            len(doc.get("response") or ""),
                            str(doc.get("_time") or ""),
                            len(doc.get("prompt_chunk") or doc.get("prompt") or ""),
                        )

                    if _rank(d) > _rank(unique_shards[idx]):
                        unique_shards[idx] = d

                # 2. 动态排序分片 (1..N)
                sorted_shards = [unique_shards[k] for k in sorted(unique_shards.keys())]

                # 3. 提取 response：优先选取 _time 最新的非空 response。
                # 双保险：即使 shard 去重后某分片 response 为空，也绝不用旧的截断版
                # 覆盖重试成功后写入的完整版。
                raw_response_str = ""
                latest_ts = ""
                for d in sorted_shards:
                    resp = d.get("response")
                    ts = str(d.get("_time") or "")
                    if resp and (not raw_response_str or ts >= latest_ts):
                        raw_response_str = resp
                        latest_ts = ts

                # 4. 拼接 prompt 分片 (支持新格式 prompt_chunk 与兼容旧格式 prompt)
                prompt_parts: list[str] = []
                for d in sorted_shards:
                    chunk = d.get("prompt_chunk") or d.get("prompt") or ""
                    if chunk:
                        prompt_parts.append(chunk)

                full_prompt_str = "".join(prompt_parts)

                prompt_dict = json.loads(full_prompt_str) if full_prompt_str else {}
                response_dict = json.loads(raw_response_str) if raw_response_str else {}

                return prompt_dict, response_dict
        except Exception as e:
            logger.warning("VictoriaLogs read failed for %s: %s", request_id, e)
            return {}, {}

    async def search_payloads(
        self,
        keyword: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int = 500,
    ) -> list[str]:
        """全文检索 Prompt 和 Response 内容，返回匹配的 request_id 列表.

        构建带时间区间与全文检索过滤的 LogsQL:
        _stream:{type="payload"} AND (prompt_chunk:~"(?i)<keyword>" OR response:~"(?i)<keyword>")
        | uniq by (request_id)
        | limit <limit>
        """
        if not keyword or not keyword.strip():
            return []

        clean_kw = keyword.strip()
        query_parts = ['_stream:{type="payload"}']

        if start_date and end_date and start_date == end_date:
            query_parts.append(f"_time: {start_date}")
        elif start_date:
            query_parts.append(f"_time: [{start_date}, {end_date or 'now'}]")
        elif end_date:
            query_parts.append(f"_time: <= {end_date}")

        # 匹配 prompt_chunk 或 response (优先使用 VictoriaLogs 倒排分词索引，词组或含空格时加引号)
        if " " in clean_kw:
            quoted_kw = json.dumps(clean_kw, ensure_ascii=False)
            content_filter = (
                f"(prompt_chunk:{quoted_kw} OR response:{quoted_kw} OR prompt:{quoted_kw})"
            )
        else:
            content_filter = (
                f"(prompt_chunk:{clean_kw} OR response:{clean_kw} OR prompt:{clean_kw})"
            )
        query_parts.append(content_filter)

        limit_num = max(1, min(limit, 200))
        full_query = f"{' AND '.join(query_parts)} | uniq by (request_id) | limit {limit_num}"

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=5.0)) as client:
                resp = await client.post(
                    f"{self.endpoint}/select/logsql/query",
                    data={"query": full_query},
                )
                if resp.status_code != 200:
                    logger.warning(
                        "VictoriaLogs search_payloads failed: status=%s", resp.status_code
                    )
                    return []

                lines = [line.strip() for line in resp.text.strip().split("\n") if line.strip()]
                matched_rids: list[str] = []
                for line in lines:
                    try:
                        doc = json.loads(line)
                        rid = doc.get("request_id")
                        if rid and rid not in matched_rids:
                            matched_rids.append(rid)
                    except Exception:
                        pass
                return matched_rids
        except Exception as e:
            logger.warning("VictoriaLogs search_payloads exception: %s", e)
            return []

    async def health_check(self) -> bool:
        """VictoriaLogs 健康检查.

        Returns:
            bool: 后端是否可用
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.get(f"{self.endpoint}/health")
                return resp.status_code == 200
        except Exception as e:
            logger.warning("VictoriaLogs health check failed: %s", e)
            return False
