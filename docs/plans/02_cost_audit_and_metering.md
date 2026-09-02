# Module 2 Implementation Plan: Granular Token Metering & USD Cost Auditing

> **Goal**: Implement asynchronous request persistence to OCI MySQL (`rin-heatwave`), precisely extracting Prompt/Completion tokens, calculating micro-dollar USD spend, and logging latencies and status codes.

---

## 1. Architecture & Design Specification

### 1.1 Data Flow Architecture
```
[Client Request] --> [LiteLLM Proxy (Port 4000)]
                           |
                           +---> (Async Callback Hook) ---> [OCI MySQL HeatWave: llm_request_logs (10.0.0.247:3306)]
```
1. The client issues a request and receives the response payload.
2. LiteLLM Proxy triggers the asynchronous Success Callback.
3. The hook extracts metadata (`request_id`, `model_requested`, `model_used`, `prompt_tokens`, `completion_tokens`, `cost_usd`, `latency_ms`).
4. Telemetry is written asynchronously to the managed OCI MySQL database over Tailscale without impacting client-perceived latency.

---

## 2. Step-by-Step Implementation

### Step 1: Initialize MySQL Schema (`scripts/init_db.sql`)
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

### Step 2: Configure LiteLLM MySQL Callback
Configure database connectivity within `config.yaml` or a custom Python hook:
```yaml
general_settings:
  database_url: "mysql+aiomysql://${MYSQL_USER}:***@10.0.0.247:3306/${MYSQL_DB}"
```

### Step 3: Implement Spend Metrics API (`/v1/metrics/spend`)
Author a FastAPI endpoint connecting directly to OCI MySQL to aggregate spending:
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

## 3. Verification & Acceptance Testing

1. **Persistence Accuracy Verification**:
   * Dispatch 5 test requests to LiteLLM Proxy.
   * Query database:
     ```sql
     SELECT request_id, model_used, total_tokens, cost_usd, latency_ms FROM llm_request_logs ORDER BY created_at DESC LIMIT 5;
     ```
   * Confirm `total_tokens` matches the API response `usage` payload and `cost_usd` is greater than 0.
2. **Metrics Dashboard API Verification**:
   * Issue `GET http://localhost:8000/v1/metrics/spend`.
   * Verify JSON response formatting and aggregated totals.

---

## 4. Risk Control & Operational Constraints

* ⚠️ **Asynchronous Fault Isolation**: Database write failures (e.g., connection timeouts) must never fail upstream LLM API requests; wrap all logging callbacks in strict `try-except` blocks.
* ⚠️ **Precision Requirements**: Monetary fields must use MySQL `DECIMAL(10, 6)` rather than floating-point types (`FLOAT`/`DOUBLE`) to prevent accumulated rounding errors.
