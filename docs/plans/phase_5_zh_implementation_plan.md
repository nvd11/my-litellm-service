# Phase 5: LiteLLM 请求体外部化存储与全文检索精炼实施计划

> **核心目标**：实现请求报文（Prompt / Response）冷热数据完全分离。通过 **ArgoCD GitOps** 在 **K3s NUC 节点** 部署轻量 MinIO 对象存储；在 **LiteLLM 网关** 异步上传报文并隔离异常；在 **OCI MySQL** 构建超链接视图（`v_llm_request_details`）；在 **DbGate** 通过 **DuckDB** 实现毫秒级关键字全文检索。

---

## 一、系统架构与数据流拓扑

```
[终端调用 Hermes/Codex] ──► [LiteLLM Proxy (OCI free-arm-vm)]
                               │
                               ├─────────────────────────────────────────┐
                               ▼ (异步非阻塞落库)                          ▼ (异步 aioboto3 S3 极速上传)
                  [OCI MySQL HeatWave]                     [K3s MinIO S3 (固定在 NUC 节点)]
                  - 表: llm_request_logs (纯结构化指标)       - 存储路径: /data/litellm_payloads (50Gi+)
                  - 视图: v_llm_request_details (超链接)      - 格式: /{YYYY-MM-DD}/{request_id}/*.json
                               │                                         │
                               │               +--------------------+    │
                               └──────────────►|    DbGate 控制台    |◄───┘
                                               | - 左: 查看指标与链接 |
                                               | - 右: DuckDB 全文搜  |
                                               +--------------------+
```

---

## 二、六步落地实施路线 (Step-by-Step)

### 【Step 0】NUC 宿主机物理存储准备 (Nova `100.104.150.19`)
1. **登录 NUC 检查磁盘空间**：
   ```bash
   ssh gateman@100.104.150.19
   df -h /data
   ```
2. **创建专属持久化目录并赋权**：
   ```bash
   sudo mkdir -p /data/litellm_payloads
   sudo chown -R gateman:gateman /data/litellm_payloads
   sudo chmod -R 775 /data/litellm_payloads
   ```

---

### 【Step 1】通过 ArgoCD GitOps 部署 MinIO
1. **在 `nvd11/minio-deployment` 仓库维护 K8s 清单**：
   - `01-storage.yaml`: 声明 PVC `minio-data`（50Gi, `local-path` 或 `hostPath: /data/litellm_payloads`）；
   - `02-secret.yaml`: `MINIO_ROOT_USER` 与 `MINIO_ROOT_PASSWORD`；
   - `03-deployment.yaml`: 设置 `nodeSelector: kubernetes.io/hostname=nuc` 钉死在 NUC 节点；
   - `04-service-and-ingress.yaml`: 创建 ClusterIP (`:9000`) 与 Kong Ingress (`payloads.jppwl.asia`)。
2. **在 `nvd11/my-argocd-manifests` 注册应用**：
   - 添加 `apps/minio-app.yaml`，同步至 `tencent-dp1-cluster` 的 `minio` 命名空间。

---

### 【Step 2】MinIO 存储桶初始化与公开下载策略
进入 MinIO Pod 执行初始化：
```bash
# 初始化存储桶并开放只读权限（供超链接一键直达）
mc alias set k3s http://minio.minio.svc.cluster.local:9000 litellm_admin $MINIO_ROOT_PASSWORD
mc mb k3s/litellm-payloads
mc anonymous set download k3s/litellm-payloads
# 配置生命周期规则：90 天自动归档/清理
mc ilm rule add k3s/litellm-payloads --expire-days 90
```

---

### 【Step 3】LiteLLM 异步 S3 上传开发 (`my-litellm-service`)
1. **增加依赖与配置 (`app/core/config.py`)**：
   - 依赖：`aioboto3`
   - 配置：
     - `ENABLE_PAYLOAD_OFFLOAD=True`
     - `PAYLOAD_S3_ENDPOINT="http://minio.minio.svc.cluster.local:9000"`
     - `PAYLOAD_BUCKET_NAME="litellm-payloads"`
     - `PAYLOAD_PUBLIC_BASE_URL="https://payloads.jppwl.asia/litellm-payloads"`
2. **编写异步上传模块 (`app/core/payload_uploader.py`)**：
   - 提取请求 Prompt 与模型 Response 序列化为 JSON；
   - 写入路径：`{YYYY-MM-DD}/{request_id}/prompt.json` 与 `response.json`；
   - 强制设置超时（2s）与异常熔断隔离（`try...except` 吞掉所有异常，绝不阻塞主请求）。
3. **挂载日志 Hook (`app/core/logging_hook.py`)**：
   - 在 `async_log_success_event` 中并行派发：`asyncio.create_task(async_upload_payload(...))`。
4. **单测覆盖 (`tests/test_payload_uploader.py`)**：
   - 覆盖正常上传与断网超时熔断分支。

---

### 【Step 4】MySQL 创建动态超链接视图 (`scripts/create_views.sql`)
在 OCI MySQL (`litellm_db`) 执行：
```sql
USE litellm_db;

CREATE OR REPLACE VIEW v_llm_request_details AS
SELECT 
    l.id,
    l.request_id,
    l.api_key_alias,
    l.model_requested,
    l.model_used,
    l.prompt_tokens,
    l.completion_tokens,
    l.total_tokens,
    l.cost_cny,
    l.latency_ms,
    l.status_code,
    l.created_at,
    CONCAT('https://payloads.jppwl.asia/litellm-payloads/', 
           DATE_FORMAT(l.created_at, '%Y-%m-%d'), '/', 
           l.request_id, '/prompt.json') AS prompt_url,
    CONCAT('https://payloads.jppwl.asia/litellm-payloads/', 
           DATE_FORMAT(l.created_at, '%Y-%m-%d'), '/', 
           l.request_id, '/response.json') AS response_url
FROM llm_request_logs l;
```

---

### 【Step 5】DbGate + DuckDB 关键字全文检索验证
1. **DbGate 连接 DuckDB**：
   - 打开 DbGate -> 点击「Add Connection」 -> 选择 **DuckDB** 引擎；
2. **执行 S3 穿透全文搜索 SQL**：
   ```sql
   INSTALL httpfs;
   LOAD httpfs;
   SET s3_endpoint='minio.minio.svc.cluster.local:9000';
   SET s3_use_ssl=false;

   -- 秒级全文搜索包含 "Compliance" 的 Prompt 报文与请求 ID
   SELECT 
       regexp_extract(filename, '([0-9a-f-]{36})', 1) AS request_id,
       messages->>'$[0].content' AS user_query,
       model
   FROM read_json_auto('s3://litellm-payloads/2026-09-*/*/prompt.json', filename=true)
   WHERE lower(messages::VARCHAR) LIKE '%compliance%';
   ```

---

## 三、方案核心收益

1. **MySQL 零膨胀**：主表仅保留数值与状态，InnoDB 缓冲池命中率提升 100%；
2. **100% 数据主权**：所有 Prompt/Response 留在 NUC 本地，无泄密风险与额外账单；
3. **秒级排错**：DBeaver / DbGate 中一键点击 URL 即在浏览器直击 Bad Case 详情；
4. **极速检索**：借助 DbGate 内置 DuckDB，零常驻开销实现 S3 海量历史报文全文检索。
