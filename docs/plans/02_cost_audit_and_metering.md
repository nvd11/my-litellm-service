# 模块二实施计划：精细化 Token 计量与 USD 开销审计 (Cost Audit & Token Metering)

> **目标**：实现 API 请求异步持久化落库至 OCI MySQL (`rin-heatwave`)，精准提取 Prompt/Completion Tokens，计算微美元级（USD）消费并记录耗时与状态。

---

## 1. 架构与设计说明

### 1.1 数据流架构
```
[Client Request] --> [LiteLLM Proxy (Port 4000)]
                           |
                           +---> (Async Callback Hook) ---> [OCI MySQL HeatWave: llm_request_logs (10.0.0.247:3306)]
```
1. 客户端发起请求并获得响应。
2. LiteLLM Proxy 的异步 Success Callback 触发。
3. 提取请求元数据（`request_id`, `model_requested`, `model_used`, `prompt_tokens`, `completion_tokens`, `cost_usd`, `latency_ms`）。
4. 异步通过 Tailscale 内网将数据写入 OCI 托管 MySQL 数据库，不阻塞主接口延迟。

---

## 2. 详细实施步骤 (Step-by-Step)

### Step 1: 初始化 MySQL DDL (`scripts/init_db.sql`)
```sql
CREATE TABLE IF NOT EXISTS llm_request_logs (
    id VARCHAR(36) PRIMARY KEY,
    request_id VARCHAR(128) NOT NULL,
    api_key_alias VARCHAR(64) DEFAULT 'default',
    model_requested VARCHAR(64) NOT NULL,
    model_used VARCHAR(64) NOT NULL,
    prompt_tokens INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    total_tokens INT NOT NULL DEFAULT 0,
    cost_usd DECIMAL(10, 6) NOT NULL DEFAULT 0.000000,
    latency_ms INT NOT NULL,
    status_code INT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_logs_created_at (created_at),
    INDEX idx_logs_model (model_used)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### Step 2: 配置 LiteLLM MySQL Callback
在 `config.yaml` 或自定义 Python Hook 中配置数据库连接：
```yaml
general_settings:
  database_url: "mysql+aiomysql://${MYSQL_USER}:${MYSQL_PASSWORD}@10.0.0.247:3306/${MYSQL_DB}"
```

### Step 3: FastAPI 开销统计 API 实现 (`/v1/metrics/spend`)
编写 FastAPI 路由，直连 OCI MySQL 读取花费汇总：
```python
# app/routers/metrics.py
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

router = APIRouter(prefix="/v1/metrics", tags=["Metrics"])

@router.get("/spend")
async def get_spend_metrics(db: AsyncSession = Depends(get_db)):
    query = text("""
        SELECT 
            model_used,
            COUNT(*) as total_requests,
            SUM(prompt_tokens) as total_prompt_tokens,
            SUM(completion_tokens) as total_completion_tokens,
            SUM(cost_usd) as total_cost_usd
        FROM llm_request_logs
        GROUP BY model_used;
    """)
    result = await db.execute(query)
    return [dict(row._mapping) for row in result]
```

---

## 3. 验收与测试方案 (Verification & Acceptance)

1. **落库准确性验证**：
   * 向 LiteLLM Proxy 发送 5 次请求。
   * 查询数据库：
     ```sql
     SELECT request_id, model_used, total_tokens, cost_usd, latency_ms FROM llm_request_logs ORDER BY created_at DESC LIMIT 5;
     ```
   * 核对 `total_tokens` 是否与 API 返回的 `usage` 字段一致，`cost_usd` 是否大于 0。
2. **API 开销统计看板验证**：
   * 调用 `GET http://localhost:8000/v1/metrics/spend`。
   * 确认返回 JSON 数据结构与汇总金额正确。

---

## 4. 风险控制与红线 (Risk Control)

* ⚠️ **异步隔离**：数据库写入故障（如 DB 连通超时）严禁导致 LLM API 请求失败，必须使用 `try-except` 包裹日志回调。
* ⚠️ **数据精度**：金额必须采用 MySQL `DECIMAL(10, 6)` 类型，严禁使用浮点数（`FLOAT`）存储，避免累加浮点精度丢失。
