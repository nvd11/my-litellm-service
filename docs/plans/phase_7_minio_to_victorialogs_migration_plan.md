# 架构演进方案：从 MinIO (S3) 迁移至 VictoriaLogs 紧凑列式日志存储

> 📅 **规划日期**：2026-09-06  
> 🎯 **目标服务**：`my-litellm-service` (K3s 业务集群 `tencent-dp1-cluster`)  
> 👤 **架构设计**：Jason (Boss) & Hebe (小女仆兼秘书)  
> 📌 **方案定位**：生产级演进规划（Phase 7 Implementation Plan）

* **相关历史规划**：原设计基于 DuckDB 边缘扫描微服务的方案已归档废弃，参见 [`phase_7_nuc_edge_duckdb_search_service_plan_outdated.md`](./phase_7_nuc_edge_duckdb_search_service_plan_outdated.md)。

---

## 1. 演进背景与痛点复盘 (Why We Migrate)

在 Phase 5 & 6 的实现中，平台采用了 **“OCI MySQL (结构化指标) + NUC MinIO (非结构化 JSON 报文)”** 的动静分离架构。该架构成功解决了 MySQL `LONGTEXT` 导致的 Buffer Pool 污染与数据库膨胀问题，但在持续运行和可观测性深化过程中，暴露出以下三个核心痛点：

### 1.1 磁盘小文件碎屑惩罚与近乎为零的压缩比
* **小文件爆炸**：LiteLLM 每完成一次 API 交互，均产生独立的 `prompt.json` 与 `response.json`。年化百万级请求将导致数百万个微小文件，对 NUC 本地文件系统的 Inode 消耗和 IOPS 构成巨大损耗；
* **孤立对象压缩收益极低**：MinIO 针对单一 Object 独立落盘，无法利用 LLM 请求中高度重复的 System Prompt、JSON 模板标签进行跨请求字典共享压缩，磁盘压缩比接近 1:1。

### 1.2 全文检索与模糊搜索能力缺失
* MinIO 是纯粹的 Key-Value 键值文件存储，**完全不具备文本内容索引能力**；
* 当 Dashboard 需要按关键字（如“退款”、“广发信用卡”、“RateLimitError”）过滤历史会话时，MinIO 无法通过 SQL/API 直接检索，必须将成千上万个 JSON 全部拉取到内存中单线程遍历，性能直接退化至灾难级。

### 1.3 上层妥协性组件的维护负担（DuckDB / Payload-Lens）
* 为了在 MinIO 基础之上实现报文计算与提取，系统不得不考虑引入 **DuckDB 跨网络挂载 S3** 或构建专用的 **Payload-Lens 透镜转换服务**；
* 该模式拉长了调用链路（`LiteLLM -> aioboto3 -> MinIO -> DuckDB -> Payload-Lens -> Dashboard`），增加了网络跨度、超时重试复杂度与内存开销。

---

## 2. 核心架构升级蓝图 (Architecture Evolution)

将存储底座平滑切换至 **VictoriaLogs (专为结构化文本与日志深度优化的轻量级列式引擎)**，利用其极其强悍的 **ZSTD 列式块压缩** 与 **原生毫秒级全文分词索引**，一举消除 MinIO 与中间层外挂引擎。

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              原架构 (MinIO 模式)                             │
│                                                                             │
│  LiteLLM Hook ──┬──► MySQL (litellm_request_logs 结构化指标)                 │
│                 └──► aioboto3 (网络上传) ──► MinIO (分散 JSON 小文件)       │
│                                                     │                       │
│  Dashboard ◄──── FastAPI ◄──── (DuckDB / Lens 拼装) ┘                       │
└─────────────────────────────────────────────────────────────────────────────┘
                                      ▼
                               [ 深度降维重构 ]
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           新架构 (VictoriaLogs 模式)                         │
│                                                                             │
│  LiteLLM Hook ──┬──► MySQL (轻量保留: 核心计费流水与对账)                    │
│                 └──► HTTP POST /insert/jsonline (单次异步流式推入)          │
│                              │                                              │
│                              ▼                                              │
│                     VictoriaLogs 列式数据湖                                 │
│          (ZSTD 10:1 压缩 · 自动提取标签 · 原生 LogsQL 检索)                  │
│                              │                                              │
│  Dashboard ◄──── FastAPI (Python 应用层内存组装 / LogsQL 管道直出)           │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.1 核心收益指标对比

| 评估指标 | 原方案：MinIO (S3) | 新方案：VictoriaLogs (列式引擎) | 提升收益 |
| :--- | :---: | :---: | :---: |
| **磁盘占用 (100 万次调用)** | ~15 GB (几乎无压缩) | **~1.5 GB (ZSTD 10:1 块压缩)** | 💾 **节约 90% 存储空间** |
| **文件系统 Inode 消耗** | 2,000,000+ 个 Inode | **数十个 Big Block 文件** | 🛡️ **彻底杜绝文件碎片** |
| **Prompt 关键字检索** | 需全量下载到内存遍历 (几分钟) | **LogsQL 毫秒级返回 (< 30ms)** | ⚡ **性能提升百倍** |
| **中间层开销** | 依赖 DuckDB / Payload-Lens | **零中间件 (FastAPI 直连查询)** | ✂️ **精简 2 个系统实体** |
| **服务内存底噪** | MinIO (~150MB) + S3 SDK | **VictoriaLogs (~50MB - 100MB)** | 🪶 **超低资源开销** |

---

## 3. VictoriaLogs 数据模型与接口规范 (Data Contract)

### 3.1 写入规范 (`POST /insert/jsonline`)
VictoriaLogs 原生支持以 JSON 流式追加写入。在 `app/core/logging_hook.py` 中，每次调用成功或捕获异常后，组装统一的单行结构：

```json
{
  "_time": "2026-09-06T05:32:46.000Z",
  "_stream": "{env=\"prod\",service=\"litellm\",type=\"payload\"}",
  "_msg": "LLM 调用日志: request_id=req-9c8f2b3e, model=gemini-3.8-flash, status=200, latency=782ms",
  "env": "prod",
  "service": "litellm",
  "type": "payload",
  "request_id": "req-9c8f2b3e-5a12-4d3a-b8e7-112233445566",
  "model": "gemini-3.8-flash",
  "key_alias": "yui-radxa",
  "spend": 0.000215,
  "currency": "USD",
  "prompt_tokens": 1420,
  "completion_tokens": 380,
  "total_tokens": 1800,
  "latency_ms": 782,
  "status_code": 200,
  "is_streaming": false,
  "cache_hit": false,
  "prompt": "{\"system\": \"你是女仆 Hebe...\", \"messages\": [{\"role\": \"user\", \"content\": \"广发信用卡提醒...\"}]}",
  "response": "{\"role\": \"assistant\", \"content\": \"已为您完成账单识别...\"}"
}
```

* **`_time`**：精确到毫秒的时间戳，VictoriaLogs 依此自动进行时序分区与生命周期淘汰；
* **`_stream`**：流标签，定义物理流隔离，**采用方案 B 三层标签设计**：
  * `env="prod"`：环境标识（prod / staging / dev）；
  * `service="litellm"`：服务标识（litellm / fastapi / quarkus 等）；
  * `type="payload"`：数据类型标识（payload / metric / trace），**核心区分字段**，用于隔离 LLM 报文与 K3s 系统日志；
* **`_msg`**：**必须字段**，日志消息摘要，VictoriaLogs 依此显示日志内容，缺失会导致 `missing _msg field` 警告；
* **`env` / `service` / `type`**：**普通字段副本**，与 `_stream` 标签保持一致，用于 LogsQL 查询过滤（`_stream` 查询语法复杂，普通字段更直观）；
* **其他所有字段**：自动作为高吞吐列式字段存储，`prompt` 与 `response` 自动纳入流式倒排全文分词索引。

#### 3.1.1 `_stream` 流标签设计规范（方案 B）

VictoriaLogs 使用 `_stream` 字段实现多租户逻辑隔离，本系统采用三层标签设计：

| 标签 | 取值示例 | 作用 | 基数 |
| :--- | :--- | :--- | :--- |
| `env` | `prod` / `staging` / `dev` | 环境隔离 | 低（~3） |
| `service` | `litellm` / `fastapi` / `quarkus` | 服务隔离 | 低（~5） |
| `type` | `payload` / `metric` / `trace` | **数据类型隔离（核心）** | 低（~3） |

**设计原则**：
1. **低基数优先**：`_stream` 标签组合数应控制在 100 以内，避免高基数导致索引膨胀；
2. **type 为核心区分字段**：K3s 系统日志由 Fluent Bit 自动采集，`_stream` 为 `{kubernetes.container_name="...", ...}`；LLM Payload 使用 `{env="prod", service="litellm", type="payload"}`，两者天然隔离；
3. **查询时显式指定**：所有 LogsQL 查询必须显式包含 `_stream` 过滤，避免误查系统日志。

#### 3.1.2 `_msg` 字段必要性说明

**VictoriaLogs 强制要求每条日志必须包含 `_msg` 字段**，否则：
- 日志会被标记为 `missing _msg field`；
- Web UI 中日志内容显示为警告信息而非实际内容；
- 数据仍可查询，但可读性极差。

**`_msg` 字段设计规范**：
```python
# 推荐格式：包含关键标识信息，便于快速浏览
_msg = f"LLM 调用日志: request_id={request_id}, model={model}, status={status_code}, latency={latency_ms}ms"

# 错误示例（缺失 _msg）：
# 日志显示: "missing _msg field; see https://docs.victoriametrics.com/victorialogs/keyconcepts/#message-field"
```

#### 3.1.4 报文超限分块切片与动态聚合还原规范 (Chunking & Dynamic Reassembly)

##### 1. VictoriaLogs 底层硬限制发现与实测
* **默认单行上限**：默认参数 `-insert.maxLineSizeBytes=262144`（256 KB），未配置该参数时超过 256KB 会被静默丢弃。
* **物理绝对上限**：即使配置 `-insert.maxLineSizeBytes=2MB`，VictoriaLogs 代码内部十进制严格硬上限为 **1,999,000 字节 (~1.90 MB)**，超过 2,000,000 字节仍会被强行丢弃。
* **MinIO 真实报文数据特征**：
  * `<= 1.5MB` 普通请求占 **87.8%**；
  * `> 1.5MB` 超长多轮上下文及包含高清图片 Base64 的报文占 **12.2%**，最大单条请求可达 **12.07 MB**（627 条对话轮次）。

##### 2. 动态分块切片策略 (Chunking)
* **分块阈值**：以 **1.5MB 字符（1,500,000 chars）** 为安全阈值，绝对避开 1.9MB 丢弃红线。
* **按需切片机制**：
  * **未超限请求（<= 1.5MB）**：**不切片**，单条整存（`shard_index=1, total_shards=1`），保持干净纯粹；
  * **超限请求（> 1.5MB）**：按 1.5MB 线性切块（`shard_index=1..N, total_shards=N`）。
  * 字段 `prompt_chunk` 存放分块文本，`response` 存放在第 1 分片。
  * 单条请求的所有分片在一次网络交互中以 `\n` 分隔批量推送到 `/insert/jsonline`，保证原子性。

##### 3. 动态聚合还原规范 (Dynamic Reassembly)
* **LogsQL 查询**：`env: "prod" AND type: "payload" AND request_id: exact("<request_id>")`；
* **分片重组**：检索该 `request_id` 下的全部分片，按 `shard_index` 升序排列，通过 `"".join(chunks)` 秒级拼接并无损执行 `json.loads()` 还原结构化 Prompt 与 Response；
* **双向兼容**：同时兼容旧版单行 `prompt` 字段与新版分块 `prompt_chunk` 字段。

**核心发现**：VictoriaLogs 的 `_stream` 标签与普通字段是**独立存储**的：
- `_stream` 标签：显示在 Web UI 左侧 Stream fields，用于流隔离；
- 普通字段：用于 LogsQL 查询过滤，语法更直观。

**双写机制**：
```json
{
  "_stream": "{env=\"prod\",service=\"litellm\",type=\"payload\"}",
  "env": "prod",
  "service": "litellm",
  "type": "payload"
}
```

**查询方式对比**：

| 查询方式 | 语法 | 优点 | 缺点 |
| :--- | :--- | :--- | :--- |
| `_stream` 过滤 | `_stream: "{env=\"prod\"}"` | 精确匹配流 | 语法复杂，需转义 |
| 普通字段过滤 | `env: "prod"` | 语法直观 | 需确保字段与标签一致 |

**推荐**：生产环境使用**普通字段过滤**（语法直观），同时保持 `_stream` 标签用于流隔离和 UI 展示。

**与 K3s 系统日志的区分**：

| 数据类型 | `_stream` 示例 | 采集方式 |
| :--- | :--- | :--- |
| **K3s 系统日志** | `{kubernetes.container_name="proxy", kubernetes.namespace_name="kong-system", kubernetes.pod_name="kong-ingress-controller-kong-hksgp"}` | Fluent Bit DaemonSet 自动采集，注入 K8s 元数据 |
| **LLM Payload** | `{env="prod", service="litellm", type="payload"}` | 应用代码主动上报，自定义业务标签 |

**查询示例**：
```sql
-- ✅ 正确：只查 LLM Payload，排除 K3s 系统日志
_stream: "{env=\"prod\", service=\"litellm\", type=\"payload\"}" AND model: "kimi-k3"

-- ❌ 错误：未指定 _stream，可能误查系统日志
model: "kimi-k3"

-- ✅ 正确：查 K3s 系统日志（调试用）
_stream: "{kubernetes.container_name=\"proxy\", kubernetes.namespace_name=\"kong-system\"}"
```

### 3.2 检索场景与 LogsQL 映射

| 业务场景 | 前端 API 路由 | 底层 VictoriaLogs LogsQL 语句 |
| :--- | :--- | :--- |
| **根据 ID 点查报文** | `GET /api/logs/{request_id}/payload` | `env: "prod" AND type: "payload" AND request_id: "req-xxx"` |
| **关键字搜 Prompt** | `GET /api/logs?q={keyword}` | `env: "prod" AND type: "payload" AND _time: 7d AND prompt: "广发信用卡"` |
| **模型耗时分布统计** | `GET /api/metrics/latency-dist` | `env: "prod" AND type: "payload" AND _time: 24h \| stats by (model) quantile(0.5, latency_ms) as p50, quantile(0.99, latency_ms) as p99` |
| **各 Agent 花销排行** | `GET /api/metrics/top-spenders` | `env: "prod" AND type: "payload" AND _time: 30d \| stats by (key_alias) sum(spend) as total_spend \| sort by (total_spend) desc` |

---

## 4. 实施阶段与操作指南 (Implementation Steps)

### Phase 7.1：基础设施部署与联通 (Infra Setup)
* **利用现存节点**：
  * 主人已在 Starfive RISC-V (`10.0.1.227:9428` / Tailscale `100.95.20.57:9428`) 部署有工业级 `VictoriaLogs`；
  * 或者在 K3s 集群 B (`tencent-dp1-cluster`) 中部署专用轻量 Pod（镜像：`victoriametrics/victoria-logs:latest`，内存限制 `256Mi`，挂载 NUC 本地卷 `/data/victorialogs`）。
* **网络打通**：
  * 配置 K3s Service `victorialogs.monitoring.svc.cluster.local:9428`。

### Phase 7.2：微服务代码重构与解耦 (Code Refactoring)

#### 4.2.1 核心设计：抽象基类 + 双子类模式

为支持 **MinIO → VictoriaLogs 平滑迁移** 及未来可能的存储后端扩展（如 Elasticsearch、ClickHouse），采用 **抽象基类 + 策略模式** 设计：

```
┌─────────────────────────────────────┐
│      PayloadBackend (ABC)           │
│  ├─ write_payload(request_id, ...)  │
│  ├─ read_payload(request_id, ...)   │
│  └─ health_check()                  │
└─────────────────────────────────────┘
              △
              │
    ┌─────────┴─────────┐
    │                   │
┌───┴───┐          ┌───┴────────┐
│MinIO  │          │VictoriaLogs│
│Backend│          │Backend     │
└───────┘          └────────────┘
```

**设计优势**：
| 原则 | 说明 |
| :--- | :--- |
| **开闭原则** | 新增后端（如 Elasticsearch）只需加子类，不改核心逻辑 |
| **灰度切换** | 配置一键切换 `payload_backend=minio/victorialogs` |
| **双写对比** | 可同时实例化两个子类，并行写入对比 |
| **测试友好** | Mock 抽象类即可单元测试 |
| **职责清晰** | 读写分离，各子类专注自身协议 |

#### 4.2.2 抽象基类定义 (`app/core/payload_backend.py`)

```python
"""Payload 存储后端抽象基类"""

from abc import ABC, abstractmethod
from typing import Any


class PayloadBackend(ABC):
    """Payload 存储后端抽象基类，定义统一读写接口"""

    @abstractmethod
    async def write_payload(
        self,
        request_id: str,
        prompt: dict[str, Any],
        response: dict[str, Any],
        metadata: dict[str, Any],
    ) -> bool:
        """写入 payload，返回是否成功

        Args:
            request_id: 请求唯一标识
            prompt: 用户输入报文
            response: 模型输出报文
            metadata: 调用元数据（model, key_alias, tokens, latency 等）

        Returns:
            bool: 写入是否成功
        """
        pass

    @abstractmethod
    async def read_payload(
        self,
        request_id: str,
        date: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """读取 payload，返回 (prompt, response)

        Args:
            request_id: 请求唯一标识
            date: 可选日期分区（YYYY-MM-DD），用于加速查询

        Returns:
            tuple: (prompt_data, response_data)，不存在时返回空 dict
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """后端健康检查

        Returns:
            bool: 后端是否可用
        """
        pass
```

#### 4.2.3 MinIO 子类实现 (`app/core/backends/minio_backend.py`)

```python
"""MinIO S3 Payload 后端实现"""

import json
import logging
from typing import Any

import aioboto3
from botocore.config import Config

from app.core.config import Settings
from app.core.payload_backend import PayloadBackend

logger = logging.getLogger(__name__)


class MinIOBackend(PayloadBackend):
    """MinIO S3 存储后端"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = aioboto3.Session()
        self.boto_config = Config(
            connect_timeout=5.0,
            read_timeout=5.0,
            retries={"max_attempts": 2},
        )

    async def write_payload(
        self,
        request_id: str,
        prompt: dict[str, Any],
        response: dict[str, Any],
        metadata: dict[str, Any],
    ) -> bool:
        """写入 MinIO S3（保持现有行为）"""
        date_str = metadata.get("date", "")
        prefix = f"{date_str}/{request_id}"

        try:
            async with self.session.client(
                "s3",
                endpoint_url=self.settings.payload_s3_endpoint,
                aws_access_key_id=self.settings.payload_s3_access_key,
                aws_secret_access_key=self.settings.payload_s3_secret_key.get_secret_value(),
                config=self.boto_config,
            ) as s3_client:
                # 写入 prompt.json
                await s3_client.put_object(
                    Bucket=self.settings.payload_bucket_name,
                    Key=f"{prefix}/prompt.json",
                    Body=json.dumps(prompt, ensure_ascii=False).encode("utf-8"),
                )
                # 写入 response.json
                await s3_client.put_object(
                    Bucket=self.settings.payload_bucket_name,
                    Key=f"{prefix}/response.json",
                    Body=json.dumps(response, ensure_ascii=False).encode("utf-8"),
                )
            return True
        except Exception as e:
            logger.warning("MinIO write failed for %s: %s", request_id, e)
            return False

    async def read_payload(
        self,
        request_id: str,
        date: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """从 MinIO S3 读取（保持现有行为）"""
        # ... 现有读取逻辑 ...
        pass

    async def health_check(self) -> bool:
        """MinIO 健康检查"""
        try:
            async with self.session.client(
                "s3",
                endpoint_url=self.settings.payload_s3_endpoint,
                # ... credentials ...
            ) as s3_client:
                await s3_client.list_buckets()
            return True
        except Exception:
            return False
```

#### 4.2.4 VictoriaLogs 子类实现 (`app/core/backends/victorialogs_backend.py`)

```python
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

DEFAULT_CHUNK_SIZE = 1_500_000  # 1.5MB 字符分块安全阈值


class VictoriaLogsBackend(PayloadBackend):
    """VictoriaLogs 存储后端，内置分块防超限与动态聚合还原能力."""

    def __init__(self, settings: Settings, chunk_size: int = DEFAULT_CHUNK_SIZE) -> None:
        self.settings = settings
        self.endpoint = settings.victorialogs_url.rstrip("/")
        self.timeout = httpx.Timeout(15.0, connect=5.0)
        self.chunk_size = chunk_size

    def _split_into_chunks(self, text: str) -> list[str]:
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
        """写入 VictoriaLogs（支持按 1.5MB 分块切片，单批批量发送）"""
        timestamp = metadata.get("timestamp", "")
        model = str(metadata.get("model", "unknown"))
        prompt_str = json.dumps(prompt, ensure_ascii=False)
        response_str = json.dumps(response, ensure_ascii=False)

        prompt_chunks = self._split_into_chunks(prompt_str)
        total_shards = len(prompt_chunks)

        log_entries: list[dict[str, Any]] = []
        for idx, chunk in enumerate(prompt_chunks, 1):
            shard_msg = f"LLM 调用日志: request_id={request_id}, model={model}"
            if total_shards > 1:
                shard_msg += f" [shard {idx}/{total_shards}]"

            entry = {
                "_time": timestamp,
                "_stream": '{env="prod",service="litellm",type="payload"}',
                "_msg": shard_msg,
                "env": "prod",
                "service": "litellm",
                "type": "payload",
                "request_id": request_id,
                "shard_index": idx,
                "total_shards": total_shards,
                "prompt_chunk": chunk,
                "prompt": chunk if total_shards == 1 else "",
                "response": response_str if idx == 1 else "",
            }
            log_entries.append(entry)

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
        """从 VictoriaLogs 查询并动态聚合还原分片（LogsQL）"""
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
                    return {}, {}

                lines = [l.strip() for l in resp.text.strip().split("\n") if l.strip()]
                if not lines:
                    return {}, {}

                docs = [json.loads(l) for l in lines]
                docs.sort(key=lambda d: int(d.get("shard_index", 1)))

                raw_resp = next((d["response"] for d in docs if d.get("response")), "")
                raw_prompt = "".join(d.get("prompt_chunk") or d.get("prompt", "") for d in docs)

                return (
                    json.loads(raw_prompt) if raw_prompt else {},
                    json.loads(raw_resp) if raw_resp else {},
                )
        except Exception as e:
            logger.warning("VictoriaLogs read failed for %s: %s", request_id, e)
            return {}, {}
```

#### 4.2.5 工厂模式与配置切换 (`app/core/backends/factory.py`)

```python
"""Payload 后端工厂"""

from app.core.config import Settings
from app.core.payload_backend import PayloadBackend
from app.core.backends.minio_backend import MinIOBackend
from app.core.backends.victorialogs_backend import VictoriaLogsBackend


def get_payload_backend(settings: Settings) -> PayloadBackend:
    """根据配置获取 Payload 后端实例"""
    backend_type = getattr(settings, "payload_backend", "minio").lower()

    if backend_type == "victorialogs":
        return VictoriaLogsBackend(settings)
    elif backend_type == "minio":
        return MinIOBackend(settings)
    else:
        raise ValueError(f"Unknown payload backend: {backend_type}")


def get_dual_write_backend(settings: Settings) -> PayloadBackend:
    """获取双写后端（灰度期使用）"""
    primary = MinIOBackend(settings)
    secondary = VictoriaLogsBackend(settings)
    return DualWriteBackend(primary, secondary)
```

#### 4.2.6 双写后端（灰度期对比）(`app/core/backends/dual_write_backend.py`)

```python
"""双写后端：主写 + 异步副写，用于灰度期数据对比"""

import asyncio
import logging
from typing import Any

from app.core.payload_backend import PayloadBackend

logger = logging.getLogger(__name__)


class DualWriteBackend(PayloadBackend):
    """双写后端：主写 MinIO，异步副写 VictoriaLogs"""

    def __init__(self, primary: PayloadBackend, secondary: PayloadBackend) -> None:
        self.primary = primary
        self.secondary = secondary

    async def write_payload(
        self,
        request_id: str,
        prompt: dict[str, Any],
        response: dict[str, Any],
        metadata: dict[str, Any],
    ) -> bool:
        """主写 + 异步副写"""
        # 主写（阻塞，确保成功）
        primary_ok = await self.primary.write_payload(
            request_id, prompt, response, metadata
        )

        # 异步副写（不阻塞主流程）
        asyncio.create_task(
            self._secondary_write(request_id, prompt, response, metadata)
        )

        return primary_ok

    async def _secondary_write(
        self,
        request_id: str,
        prompt: dict[str, Any],
        response: dict[str, Any],
        metadata: dict[str, Any],
    ) -> None:
        """异步副写，失败只记日志"""
        try:
            ok = await self.secondary.write_payload(
                request_id, prompt, response, metadata
            )
            if not ok:
                logger.warning("Secondary write failed for %s", request_id)
        except Exception as e:
            logger.warning("Secondary write error for %s: %s", request_id, e)

    async def read_payload(
        self,
        request_id: str,
        date: str | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """优先从主后端读取"""
        return await self.primary.read_payload(request_id, date)

    async def health_check(self) -> bool:
        """双后端健康检查"""
        primary_ok = await self.primary.health_check()
        secondary_ok = await self.secondary.health_check()
        return primary_ok and secondary_ok
```

#### 4.2.7 配置项扩展 (`app/core/config.py`)

```python
class Settings(BaseSettings):
    # ... 现有配置 ...

    # Payload 后端配置
    payload_backend: str = "minio"  # minio / victorialogs / dual
    victorialogs_url: str = "http://100.95.20.57:9428"  # Starfive Tailscale
    victorialogs_timeout: float = 10.0

    # 双写模式开关
    enable_vlogs_dual_write: bool = False
```

#### 4.2.8 重构 `logging_hook.py` 与 `payload.py`

**改造点**：
1. 注入 `PayloadBackend` 依赖（通过 FastAPI `Depends`）；
2. 替换直接调用 `aioboto3` 的代码为 `backend.write_payload()`；
3. 替换 S3 查询逻辑为 `backend.read_payload()`；
4. 保持接口签名不变，对调用方透明。

```python
# app/api/payload.py 改造示例
from app.core.payload_backend import PayloadBackend
from app.core.backends.factory import get_payload_backend

@router.get("/logs/{request_id}/payload")
async def get_request_payload(
    request_id: str,
    backend: PayloadBackend = Depends(get_payload_backend),  # ← 注入抽象基类
) -> Any:
    prompt, response = await backend.read_payload(request_id, date_str)
    # ... 后续处理 ...
```

---

### Phase 7.3：存量数据迁移 (Historical Data Migration)
编写自动化迁移脚本 `scripts/migrate_minio_to_vlogs.py`：
1. 遍历 MinIO `litellm-payloads` Bucket 中的日期前缀目录；
2. 聚合对应目录下的 `prompt.json` 与 `response.json`，并从 OCI MySQL `llm_request_logs` 读取对应的调用元数据；
3. 打包生成 JSONLine 格式数据流；
4. 批量 `POST /insert/jsonline` 灌入 VictoriaLogs。

**迁移脚本核心逻辑**：
```python
async def migrate_batch(minio_backend: MinIOBackend, vlogs_backend: VictoriaLogsBackend, request_ids: list[str]):
    """批量迁移：从 MinIO 读取 → 转换格式 → 写入 VictoriaLogs"""
    for rid in request_ids:
        prompt, response = await minio_backend.read_payload(rid)
        metadata = await fetch_metadata_from_mysql(rid)  # 从 MySQL 补全元数据
        await vlogs_backend.write_payload(rid, prompt, response, metadata)
```

---

### Phase 7.4：MinIO 下线与存储回收 (Demise & Clean Up)
1. 验证 Dashboard 报文穿透与关键字搜索 100% 正常；
2. 在 ArgoCD 中优雅注销 `minio-app`；
3. 清理 NUC 节点上的 `local-path` 临时卷目录，释放物理磁盘。

---

## 5. 风险控制与应急回滚 (Risk & Rollback)

1. **写路径异常隔离**：
   * VictoriaLogs 上报任务封装在 `asyncio.create_task()` 内部，设有 2 秒强制超时；
   * 无论网络或存储节点出现任何故障，直接记录本地 Warning 日志，**绝不影响客户端正常的 LLM 响应流**。
2. **灰度双写机制 (Dual-Writing Window)**：
   * 上线初期开启环境变量 `ENABLE_VLOGS_DUAL_WRITE=true`；
   * 同时写入 MinIO 与 VictoriaLogs，对比 48 小时数据一致性与写入延迟，确认无误后再正式切断 MinIO 写入链路。

---

## 5. 维护者与签章

* **编制人**：Hebe (混血小女仆 · Hermes Agent)
* **审批人**：Jason (Boss)
* **状态**：`Draft Approved` (随时可按指令进入 Phase 7.1 开发实施)
