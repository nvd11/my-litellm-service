#!/usr/bin/env python3
"""极简 MinIO -> VictoriaLogs Payload 迁移脚本 (Pure Payload Chunking Mode).

设计原则:
1. 彻底解耦 MySQL，MySQL 保持为业务指标唯一真理源。
2. 纯粹通过 request_id 关联。
3. 严格契约与防超限分片:
   - 单行日志严格控制在 1.5MB (1,536,000 bytes) 以内，避开 VictoriaLogs 2MB 硬上限。
   - 当 prompt/response 文本总大小 <= 1.5MB 时:
       写入单条记录: shard_index=1, total_shards=1, prompt_chunk=..., response=...
   - 当 prompt 文本 > 1.5MB 时:
       按 1.5MB (1,500,000 字符) 线性切片:
       shard_index=1..N, total_shards=N, prompt_chunk=..., response (仅在第1片保留)
4. 读取还原逻辑:
   按 request_id 检索全部分片，按 shard_index 升序拼接 prompt_chunk 还原完整 JSON。
"""

import argparse
import json
import time
import xml.etree.ElementTree as ET

import requests

MINIO_BASE_URL = "http://10.0.1.113:31850"
MINIO_HOST = "payloads.jppwl.asia"
VLOGS_ENDPOINT = "http://localhost:9428"
CHUNK_SIZE = 1_500_000  # 1.5MB 字符分块，安全低于 1.9MB 硬限制


class PureChunkMigrator:
    def __init__(self, dry_run=False):
        self.minio_base_url = MINIO_BASE_URL
        self.minio_host = MINIO_HOST
        self.vlogs_endpoint = VLOGS_ENDPOINT
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.headers.update({"Host": self.minio_host})

    def list_all_requests(self, limit=None):
        """遍历 MinIO 列出所有包含 prompt.json 的日期与 request_id."""
        print("正在从 MinIO 扫描所有对象索引...", flush=True)
        t0 = time.time()
        continuation_token = None
        seen_requests = {}

        while True:
            url = f"{self.minio_base_url}/payloads?list-type=2"
            if continuation_token:
                url += f"&continuation-token={continuation_token}"

            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()

            root = ET.fromstring(resp.text)
            ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

            for content in root.findall("s3:Contents", ns):
                key = content.find("s3:Key", ns).text
                last_modified = content.find("s3:LastModified", ns).text
                parts = key.split("/")
                if len(parts) == 3 and parts[2] == "prompt.json":
                    date_part, rid = parts[0], parts[1]
                    if rid not in seen_requests:
                        seen_requests[rid] = {
                            "date": date_part,
                            "request_id": rid,
                            "last_modified": last_modified,
                        }
                        if limit and len(seen_requests) >= limit:
                            break

            if limit and len(seen_requests) >= limit:
                break

            is_truncated = root.find("s3:IsTruncated", ns)
            if is_truncated is not None and is_truncated.text.lower() == "true":
                next_token = root.find("s3:NextContinuationToken", ns)
                if next_token is not None:
                    continuation_token = next_token.text
                else:
                    break
            else:
                break

        res = list(seen_requests.values())
        print(
            f"扫描完毕! 共发现 {len(res)} 条待迁移请求 (耗时: {time.time() - t0:.2f}s)", flush=True
        )
        return res

    def fetch_payload(self, date_part, request_id):
        """从 MinIO 抓取 prompt.json 与 response.json 原文."""
        p_url = f"{self.minio_base_url}/payloads/{date_part}/{request_id}/prompt.json"
        r_url = f"{self.minio_base_url}/payloads/{date_part}/{request_id}/response.json"

        p_text = "{}"
        r_text = "{}"

        try:
            p_res = self.session.get(p_url, timeout=30)
            if p_res.status_code == 200:
                p_text = p_res.text
        except Exception as e:
            print(f"[{request_id}] 抓取 prompt 失败: {e}", flush=True)

        try:
            r_res = self.session.get(r_url, timeout=30)
            if r_res.status_code == 200:
                r_text = r_res.text
        except Exception as e:
            print(f"[{request_id}] 抓取 response 失败: {e}", flush=True)

        return p_text, r_text

    def send_to_vlogs(self, doc):
        """将单个文档发送至 VictoriaLogs."""
        if self.dry_run:
            return True, "DryRun OK"

        line = json.dumps(doc, ensure_ascii=False)
        try:
            resp = requests.post(
                f"{self.vlogs_endpoint}/insert/jsonline",
                data=(line + "\n").encode("utf-8"),
                headers={"Content-Type": "application/stream+json"},
                timeout=30,
            )
            if resp.status_code in (200, 204):
                return True, "OK"
            return False, f"HTTP {resp.status_code}: {resp.text}"
        except Exception as e:
            return False, f"写入异常: {e}"

    def migrate_one(self, item):
        rid = item["request_id"]
        date_part = item["date"]
        last_mod = item.get("last_modified")
        iso_time = last_mod if last_mod else f"{date_part}T00:00:00Z"

        p_text, r_text = self.fetch_payload(date_part, rid)
        if p_text == "{}" and r_text == "{}":
            return False, "空 Payload"

        # 检查是否需要分片
        p_len = len(p_text.encode("utf-8"))
        if p_len <= CHUNK_SIZE:
            # 单片入库
            doc = {
                "_time": iso_time,
                "_stream": '{env="prod",service="litellm",type="payload"}',
                "_msg": f"request_id={rid}",
                "env": "prod",
                "service": "litellm",
                "type": "payload",
                "request_id": rid,
                "shard_index": 1,
                "total_shards": 1,
                "prompt_chunk": p_text,
                "response": r_text,
            }
            return self.send_to_vlogs(doc)
        else:
            # 超大文本分块 (线性切片)
            chunks = [p_text[i : i + CHUNK_SIZE] for i in range(0, len(p_text), CHUNK_SIZE)]
            total_chunks = len(chunks)

            for idx, chunk in enumerate(chunks, 1):
                doc = {
                    "_time": iso_time,
                    "_stream": '{env="prod",service="litellm",type="payload"}',
                    "_msg": f"request_id={rid} [shard {idx}/{total_chunks}]",
                    "env": "prod",
                    "service": "litellm",
                    "type": "payload",
                    "request_id": rid,
                    "shard_index": idx,
                    "total_shards": total_chunks,
                    "prompt_chunk": chunk,
                    "response": r_text if idx == 1 else "",
                }
                ok, msg = self.send_to_vlogs(doc)
                if not ok:
                    return False, f"分片 {idx}/{total_chunks} 写入失败: {msg}"

            return True, f"已分 {total_chunks} 片写入"


def main():
    parser = argparse.ArgumentParser(description="Pure Payload Chunking Migrator")
    parser.add_argument("--limit", type=int, default=None, help="限制迁移数量")
    parser.add_argument("--dry-run", action="store_true", help="演练模式，不实际写入")
    args = parser.parse_args()

    migrator = PureChunkMigrator(dry_run=args.dry_run)
    items = migrator.list_all_requests(limit=args.limit)

    total = len(items)
    print(f"准备开始迁移，目标总数: {total} 条...", flush=True)
    t0 = time.time()
    success_count = 0
    fail_count = 0
    chunked_count = 0

    for idx, it in enumerate(items, 1):
        rid = it["request_id"]
        ok, msg = migrator.migrate_one(it)
        if ok:
            success_count += 1
            if "已分" in msg:
                chunked_count += 1
        else:
            fail_count += 1
            print(f"[{idx}/{total}] {rid} -> 失败: {msg}", flush=True)

        if idx % 20 == 0 or idx == total:
            elapsed = time.time() - t0
            rate = idx / elapsed if elapsed > 0 else 0
            eta = (total - idx) / rate if rate > 0 else 0
            print(
                f"进度: [{idx}/{total}] ({(idx / total) * 100:.1f}%) | "
                f"成功: {success_count} (分片: {chunked_count}), 失败: {fail_count} | "
                f"速率: {rate:.1f} 条/s | 预估剩余: {eta:.0f}s",
                flush=True,
            )

    total_time = time.time() - t0
    print("\n" + "=" * 40, flush=True)
    print(f"🎉 迁移完成! 总耗时: {total_time:.2f}s", flush=True)
    print(
        f"统计: 成功 {success_count} 条 (包含大报文分片 {chunked_count} 条), 失败 {fail_count} 条",
        flush=True,
    )
    print("=" * 40, flush=True)


if __name__ == "__main__":
    main()
