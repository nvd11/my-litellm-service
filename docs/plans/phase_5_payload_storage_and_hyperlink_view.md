# Phase 5 实施计划：LLM Payload (Prompt/Response) 外部化存储与 MySQL 超链接视图 (Payload Offloading to NUC & MySQL Hyperlink View)

> **目标**：实现大模型请求载荷（Prompt 输入与 Response 输出）与 MySQL 审计主表（`llm_request_logs`）的彻底冷热分离；利用家庭局域网 **Intel NUC (`100.104.150.19`)** 搭建高性能、大容量且隐私闭环的 Payload 存储服务（轻量 MinIO S3 / Nginx 静态服务）；在 LiteLLM Proxy 异步 Hook 中扩展非阻塞 Payload 上传机制；在 MySQL 中构建带 HTTPS/Tailscale 直达超链接的数据库视图（`v_llm_request_details`），实现“指标秒级分析 + 详情一键点开”的极简排查与审计体验。

---

## 1. 架构演进与设计背景 (Architecture & Motivation)

### 1.1 为什么必须做 Payload 冷热分离？
在当前的 Phase 2/3/4 架构中，MySQL HeatWave 主表 `llm_request_logs` 承载了 Token 统计、人民币/美金结算、耗时、状态码与模型降级轨迹等核心指标。
随着 Agent 任务（如 Hermes、Codex）引入上万乃至数十万 Token 的长上下文，若将 Prompt 和 Response 的完整报文直接塞入 MySQL（`LONGTEXT` / `JSON`），会引发以下严重问题：
1. **Buffer Pool 污染**：大字段频繁读写会挤占 InnoDB 缓冲池，严重劣化主键与常用索引的命中率；
2. **表体积失控与备份困难**：月均数十万次调用会产生数十 GB 的非结构化文本，导致数据库备份、还原与迁移极度缓慢；
3. **网络与查询开销**：日常财务报表和指标监控仅需数值聚合，大字段会导致全表扫描时 I/O 吞吐急剧下降。

---

### 1.2 整体拓扑与数据流图 (Topology & Data Flow)

```
[Client (Hermes/Codex/User)]
            │
            ▼ (HTTP Post :4000)
[LiteLLM Proxy on K3s (free-arm-vm)]
            │
            ├──────────────────────────────────────────────────────┐
            │ (Async Non-blocking Event)                           │ (Async Non-blocking Event)
            ▼                                                      ▼
[app.core.logging_hook.custom_logger]             [app.core.payload_uploader]
            │                                                      │
            │ (SQLAlchemy Async INSERT)                            │ (HTTP/S3 Async PUT)
            ▼                                                      ▼
[OCI MySQL HeatWave / Neon PG]                   [Intel NUC Homelab (100.104.150.19)]
  (llm_request_logs 纯结构化指标)                 - MinIO S3 Bucket: `litellm-payloads`
            │                                     - Path: `/payloads/{date}/{request_id}/`
            │                                             ├── prompt.json
            │                                             └── response.json
            ▼
[MySQL View: v_llm_request_details]
  (动态 CONCAT 生成 prompt_url & response_url)
            │
            ▼ (浏览器点击)
[Browser on Tailscale / Cloudflare Tunnel] ───► 实时高亮渲染查看原始 Prompt/Response
```

---

## 2. NUC 存储节点方案选型与配置 (NUC Storage Node Setup)

NUC 作为家庭 Homelab 核心节点，已接入 Tailscale 大内网（IP: `100.104.150.19`）。本方案推荐采用 **轻量 MinIO S3 容器** 作为标准对象存储实现。

### 2.1 方案 A：NUC MinIO S3 容器化部署（首选，标准 S3 协议）

在 NUC (`100.104.150.19`) 上使用 Docker Compose 启动 MinIO：

```yaml
# /opt/minio/docker-compose.yml on NUC
version: '3.8'

services:
  minio:
    image: quay.io/minio/minio:latest
    container_name: litellm-minio
    restart: unless-stopped
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: "litellm_admin"
      MINIO_ROOT_PASSWORD: "YOUR_STRONG_PASSWORD"
      MINIO_BROWSER_REDIRECT_URL: "http://100.104.150.19:9001"
    volumes:
      - /data/litellm_payloads:/data
    ports:
      - "9000:9000"   # S3 API 端口
      - "9001:9001"   # Web Console 管理端口
```

#### 初始化 Bucket 与公开只读权限 (Read-Only Policy)
为了让 MySQL View 生成的超链接在浏览器中点开即看，需将 `litellm-payloads` Bucket 设置为 **Public Read-Only**：

```bash
# 在 NUC 上配置客户端并设置策略
mc alias set local http://localhost:9000 litellm_admin YOUR_STRONG_PASSWORD
mc mb local/litellm-payloads
mc anonymous set download local/litellm-payloads
```

---

### 2.2 存储目录与文件命名规范

每次调用按日期与 `request_id` 进行分级归档，避免单目录小文件过多：
```
litellm-payloads/
  └── 2026-09-02/
      └── {request_id}/
          ├── prompt.json        # 客户端请求 messages 列表、系统提示词、温度等参数
          └── response.json      # LLM 完整输出、工具调用 (tool_calls)、usage 信息
```

---

## 3. LiteLLM Proxy 异步 Hook 改造 (Async Hook Implementation)

在 `my-litellm-service` 现有代码中引入 `app.core.payload_uploader` 模块，确保上传过程 **100% 异步、零延迟阻塞、异常完全隔离**。

### 3.1 Pydantic 配置扩展 (`app/core/config.py`)

```python
class Settings(BaseSettings):
    # 现有配置项 ...
    
    # === Payload 存储配置 ===
    ENABLE_PAYLOAD_OFFLOAD: bool = True
    PAYLOAD_STORAGE_ENDPOINT: str = "http://100.104.150.19:9000"
    PAYLOAD_BUCKET_NAME: str = "litellm-payloads"
    PAYLOAD_ACCESS_KEY: str = "litellm_admin"
    PAYLOAD_SECRET_KEY: str = "YOUR_STRONG_PASSWORD"
    PAYLOAD_PUBLIC_BASE_URL: str = "http://100.104.150.19:9000/litellm-payloads"
```

---

### 3.2 异步上传模块 (`app/core/payload_uploader.py`)

使用 `aioboto3` 或异步 HTTP `httpx` 实现高并发非阻塞上传：

```python
"""LiteLLM 异步 Payload 上传模块 (Async Payload Uploader)."""

import json
import logging
from datetime import datetime, timezone
from typing import Any
import httpx

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


async def async_upload_payload(
    request_id: str,
    kwargs: dict[str, Any],
    response_obj: Any,
    settings: Settings | None = None,
) -> None:
    """异步将 Prompt 与 Response 报文上传至 NUC 存储端."""
    try:
        settings = settings or get_settings()
        if not settings.ENABLE_PAYLOAD_OFFLOAD:
            return

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        base_path = f"{date_str}/{request_id}"

        # 1. 提取 Prompt 载荷
        prompt_data = {
            "model": kwargs.get("model"),
            "messages": kwargs.get("messages", []),
            "optional_params": kwargs.get("optional_params", {}),
            "litellm_params": kwargs.get("litellm_params", {}),
        }

        # 2. 提取 Response 载荷
        response_data: Any = {}
        if hasattr(response_obj, "model_dump"):
            response_data = response_obj.model_dump()
        elif hasattr(response_obj, "dict"):
            response_data = response_obj.dict()
        elif isinstance(response_obj, dict):
            response_data = response_obj
        else:
            response_data = {"raw": str(response_obj)}

        prompt_bytes = json.dumps(prompt_data, ensure_ascii=False, indent=2).encode("utf-8")
        response_bytes = json.dumps(response_data, ensure_ascii=False, indent=2).encode("utf-8")

        # 3. 异步 HTTP PUT 直传 MinIO
        async with httpx.AsyncClient(timeout=3.0) as client:
            headers = {"Content-Type": "application/json; charset=utf-8"}
            
            prompt_url = f"{settings.PAYLOAD_STORAGE_ENDPOINT}/{settings.PAYLOAD_BUCKET_NAME}/{base_path}/prompt.json"
            response_url = f"{settings.PAYLOAD_STORAGE_ENDPOINT}/{settings.PAYLOAD_BUCKET_NAME}/{base_path}/response.json"

            await client.put(prompt_url, content=prompt_bytes, headers=headers)
            await client.put(response_url, content=response_bytes, headers=headers)

        logger.debug("Successfully offloaded payloads for request %s", request_id)
    except Exception as err:
        # 异常完全隔离，绝不影响主调用业务
        logger.warning("Failed to upload LLM payload for %s: %s", request_id, err)
```

---

### 3.3 挂接至 `DBLoggingLogger` (`app/core/logging_hook.py`)

在 `async_log_success_event` 与 `async_log_failure_event` 中添加后台任务调用：

```python
# app/core/logging_hook.py
from app.core.payload_uploader import async_upload_payload

# 在写库操作前后，发起异步上传（并发进行）：
asyncio.create_task(
    async_upload_payload(
        request_id=request_id,
        kwargs=kwargs,
        response_obj=response_obj,
        settings=settings,
    )
)
```

---

## 4. MySQL 视图构建与超链接设计 (MySQL View & Hyperlink Design)

利用 MySQL 8.0 / HeatWave 的 `CONCAT()` 与日期格式化函数，构建免维护、免存冗余字段的动态视图。

### 4.1 视图创建 SQL 脚本 (`scripts/create_views.sql`)

```sql
USE litellm_db;

-- 动态超链接详情视图
CREATE OR REPLACE VIEW v_llm_request_details AS
SELECT 
    l.id,
    l.request_id,
    l.api_key_alias,
    l.model_requested,
    l.model_used,
    l.provider,
    l.provider_key_alias,
    l.prompt_tokens,
    l.completion_tokens,
    l.total_tokens,
    l.cost_usd,
    l.cost_cny,
    l.latency_ms,
    l.status_code,
    l.created_at,
    -- 动态拼接 NUC Tailscale 内网直达 JSON 超链接
    CONCAT(
        'http://100.104.150.19:9000/litellm-payloads/',
        DATE_FORMAT(l.created_at, '%Y-%m-%d'), '/',
        l.request_id, '/prompt.json'
    ) AS prompt_url,
    CONCAT(
        'http://100.104.150.19:9000/litellm-payloads/',
        DATE_FORMAT(l.created_at, '%Y-%m-%d'), '/',
        l.request_id, '/response.json'
    ) AS response_url
FROM llm_request_logs l;
```

---

### 4.2 查询效果演示

在 DBeaver、Navicat 或 Web 报表系统中执行简单查询：

```sql
SELECT 
    created_at,
    api_key_alias,
    model_used,
    total_tokens,
    cost_cny,
    prompt_url,
    response_url
FROM v_llm_request_details
ORDER BY created_at DESC
LIMIT 10;
```

**查询结果示例**：
| created_at | api_key_alias | model_used | total_tokens | cost_cny | prompt_url | response_url |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 2026-09-02 10:15:20 | hebe | gemini-3.7-flash | 152,396 | 0.089120 | `http://100.104.150.19:9000/litellm-payloads/2026-09-02/req_abc123/prompt.json` | `http://100.104.150.19:9000/litellm-payloads/2026-09-02/req_abc123/response.json` |

在数据库客户端中直接点击链接，即可在浏览器中一秒打开结构化、带高亮的 JSON 数据！

---

## 5. 存储生命周期与自动化归档 (Lifecycle & Data Retention)

为了保证 NUC 磁盘健康，需配置存储生命周期管理。

### 5.1 MinIO ILM (Information Lifecycle Management) 规则
在 NUC 上配置自动过期规则，例如 **只保留最近 90 天的调用明细**：

```bash
# 设置 90 天后自动删除历史 Payload
mc ilm rule add local/litellm-payloads --expire-days 90
```

---

## 6. 实施步骤与验收标准 (Implementation Steps & Checklist)

### 6.1 实施里程碑 (Milestones)

- [ ] **Step 1: NUC 存储环境初始化**
  - 在 NUC (`100.104.150.19`) 上拉起 MinIO 容器；
  - 创建 `litellm-payloads` 桶并开启公共只读策略。
- [ ] **Step 2: 编写并测试上传模块**
  - 在 `app/core/payload_uploader.py` 实现异步上传；
  - 编写单元测试模拟流式与常规请求的 Payload 结构。
- [ ] **Step 3: 改造 Logging Hook**
  - 在 `app/core/logging_hook.py` 中非阻塞触发 Payload 上传；
  - 确保上传超时或失败时不影响 MySQL 主表落库。
- [ ] **Step 4: 执行 MySQL View DDL**
  - 在 `litellm_db` 中执行 `create_views.sql` 生成 `v_llm_request_details` 视图。
- [ ] **Step 5: 端到端联调验证**
  - 发起一条包含长上下文的 API 调用；
  - 验证 MySQL 主表记录成功，且点击 View 中的 `prompt_url` 和 `response_url` 能正常显示 JSON 内容。

---

## 7. 总结 (Summary)

本方案通过 **“MySQL 存轻量指标 + NUC 存海量载荷 + 动态 View 拼接超链接”** 的黄金组合：
1. **彻底规避 MySQL 性能劣化**，保证数据库轻量高效；
2. **100% 保障调用数据私密安全**，所有敏感 Prompt 停留在本地 Homelab；
3. **带来极致的排查体验**，排查 Bad Case 时点击超链接秒级溯源！
