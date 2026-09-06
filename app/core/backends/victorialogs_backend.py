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

# 单块日志大小上限 (1.5MB 字符，严格避开 VictoriaLogs 1.9MB/2MB 硬限制)
DEFAULT_CHUNK_SIZE = 1_500_000


class VictoriaLogsBackend(PayloadBackend):
    """VictoriaLogs 存储后端，内置分块防超限与动态聚合还原能力."""

    def __init__(
        self,
        settings: Settings,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self.settings = settings
        self.endpoint = settings.victorialogs_url.rstrip("/")
        self.timeout = httpx.Timeout(15.0, connect=5.0)
        self.chunk_size = chunk_size

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
                return resp.status_code in (200, 204)
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
                unique_shards: dict[int, dict[str, Any]] = {}
                for d in parsed_docs:
                    idx = int(d.get("shard_index", 1))
                    if idx not in unique_shards or len(
                        d.get("prompt_chunk") or d.get("prompt") or ""
                    ) >= len(
                        unique_shards[idx].get("prompt_chunk")
                        or unique_shards[idx].get("prompt")
                        or ""
                    ):
                        unique_shards[idx] = d

                # 2. 动态排序分片 (1..N)
                sorted_shards = [unique_shards[k] for k in sorted(unique_shards.keys())]

                # 3. 提取 response (优先从第1片或带response字段的记录提取)
                raw_response_str = ""
                for d in sorted_shards:
                    if d.get("response"):
                        raw_response_str = d["response"]
                        break

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
