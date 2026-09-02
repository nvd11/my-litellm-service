# Phase 2 Implementation Plan: OCI MySQL Enablement, Daily FX Rate Conversion & API Request Logging (MySQL Logging, Daily FX Rate & OCI Vault)

> **Goal**: Enable and initialize OCI MySQL (`rin-heatwave`), establish centralized secret management standards based on **OCI Vault (`gateman-vault`)**; implement automated daily (By Day) USD/CNY foreign exchange rate retrieval with a "L1 Local In-Memory + L2 K3s Redis" high-availability two-tier caching mechanism; update the **Dockerfile** to support custom Python Hook imports; and seamlessly log metadata, token consumption, USD cost, equivalent RMB/CNY cost, and response latency across all API calls (standard + streaming, success + failure, routing fallback trajectories) via LiteLLM Proxy asynchronous Success/Failure Callbacks.

---

## 1. Architecture & Data Flow Design

```
[Client Request (Sync/Stream)] --> [LiteLLM Proxy (:4000)]
                                         |
                                         | (Async Non-blocking Callbacks)
                                         v
                      [app.core.logging_hook.DBLoggingLogger]
                      (async_log_success_event / async_log_failure_event)
                                         |
            +----------------------------+----------------------------+
            |                                                         |
            v                                                         v
[app.core.fx_rate (L1 Memory / L2 Redis / API)]           [aiomysql Lazy Connection Pool]
 (Get Today USD->CNY FX Rate)                            (pool_recycle=300, autocommit=True)
            |                                                         |
            +----------------------------+----------------------------+
                                         |
                                         v
                      [OCI MySQL HeatWave: litellm_db.llm_request_logs]
```

1. **Zero Latency Main Flow**: Clients send API requests (supporting regular and streaming `stream=True`) and receive responses normally; primary interface response logic incurs zero additional blocking or latency.
2. **Dual-Hook Asynchronous Triggering**:
   - Successful requests trigger `async_log_success_event`;
   - Failed requests (429 rate limit, 500 server error, timeouts, etc.) trigger `async_log_failure_event`.
3. **Tiered Foreign Exchange Rate Settlement (`fx_rate`)**:
   - **L1 Local Memory**: 0ms retrieval directly from Python in-memory global variables;
   - **L2 K3s Redis**: Reuses shared Redis connection pool, key `fx:usd_cny_rate` (TTL 24 hours, isolated with `try-except` protection; transparent pass-through upon Redis outage);
   - **External FX API**: Asynchronously queries `open.er-api.com` when both caches miss, backfilling L1/L2;
   - **Safety Fallback**: Automatically falls back to `DEFAULT_USD_TO_CNY_RATE` (default `7.2300`) from environment variables upon network errors.
4. **CNY Cost Conversion & High-Precision Calculations**: Computes `cost_cny = round(cost_usd * fx_rate, 6)` using `Decimal`, safely defaulting to `0.000000` for failed or free calls.
5. **Fallback Trajectory & Metadata Extraction**:
   - Records `model_requested` (client-requested alias, e.g., `gemini-3.7-flash`) and `model_used` (actual upstream model hit, e.g., `gemini-3.7-pro-plan` or `gemini-3.7-backup`), enabling full-link visual fallback tracing.
   - Extracts `prompt_tokens`, `completion_tokens`, `total_tokens`, `cost_usd`, `cost_cny`, `fx_rate`, `latency_ms`, and `status_code`.
6. **Full-Link Exception Isolation & Connection Keep-Alive**:
   - Database writes and exchange rate operations are wrapped entirely within `try...except` blocks with warning logs, never bubbling exceptions up to callers.
   - `aiomysql` connection pool is configured with a 5-minute auto-reconnect keep-alive (`pool_recycle=300`).

---

## 2. Secret Management & OCI Vault Standards

### 2.1 Centralized Secret Distribution Topology

```
                   ┌────────────────────────────────────────┐
                   │       OCI Vault (gateman-vault)        │
                   │  - Secret: litellm/mysql-password      │
                   │  - Secret: litellm/mysql-user          │
                   └───────────────────┬────────────────────┘
                                       │
                ┌──────────────────────┴──────────────────────┐
                │                                             │
                ▼ (Automatic Sync via ESO)                    ▼ (Read via OCI CLI Identity)
   ┌───────────────────────────┐                 ┌───────────────────────────┐
   │  K3s Cluster (Production) │                 │  Local Dev Machine        │
   │  External Secrets (ESO)   │                 │  .env (GitIgnored Cache)  │
   │            ↓              │                 │            ↓              │
   │  Kubernetes Secret        │                 │  Pydantic Settings Read   │
   │            ↓              │                 └───────────────────────────┘
   │  LiteLLM Pod Env Vars     │
   └───────────────────────────┘
```

1. **Single Source of Truth**:
   - MySQL database passwords and credentials are centrally hosted in OCI Singapore `gateman-vault`.
   - Secret naming conventions: `litellm/mysql-password`, `litellm/mysql-user`, etc.
2. **K3s Production Cluster (External Secrets Operator - ESO)**:
   - The cluster declares `ExternalSecret` resources referencing OCI Vault Secrets.
   - ESO automatically retrieves and generates native Kubernetes Secrets (e.g., `litellm-mysql-secret`), injecting them as environment variables into LiteLLM Pods.
   - No plaintext passwords exist in Git repositories, supporting automatic rotation and ArgoCD disaster recovery.
3. **Local Development Environment (.env Local Isolation)**:
   - Local dev machines retrieve credentials using OCI CLI identities or local `.env` files (strictly ignored by `.gitignore`).
   - Code loads values with strict types and auto-redaction via Pydantic Settings in `app/core/config.py`.

---

## 3. Database Schema & Initialization (`scripts/init_db.py`)

### 3.1 Database Schema Structure (`llm_request_logs`)

```sql
CREATE DATABASE IF NOT EXISTS litellm_db DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE litellm_db;

CREATE TABLE IF NOT EXISTS llm_request_logs (
    id VARCHAR(36) PRIMARY KEY,                        -- Unique record identifier (UUID4)
    request_id VARCHAR(128) NOT NULL,                  -- LiteLLM API Request ID
    api_key_alias VARCHAR(64) DEFAULT 'default',       -- Client Key alias / Team identifier
    model_requested VARCHAR(64) NOT NULL,             -- Client-requested model alias (e.g., gemini-3.7-flash)
    model_used VARCHAR(64) NOT NULL,                  -- Actual upstream model ID used (e.g., gemini-3.7-pro-plan)
    prompt_tokens INT NOT NULL DEFAULT 0,             -- Input / Prompt token count
    completion_tokens INT NOT NULL DEFAULT 0,         -- Output / Completion token count
    total_tokens INT NOT NULL DEFAULT 0,              -- Total token count
    cost_usd DECIMAL(10, 6) NOT NULL DEFAULT 0.000000, -- Cost in USD
    cost_cny DECIMAL(10, 6) NOT NULL DEFAULT 0.000000, -- Cost in CNY (RMB)
    fx_rate DECIMAL(8, 4) NOT NULL DEFAULT 7.2300,    -- USD/CNY exchange rate used at settlement
    latency_ms INT NOT NULL DEFAULT 0,                -- Request response latency (ms)
    status_code INT NOT NULL DEFAULT 200,             -- HTTP response status code (200, 429, 500, etc.)
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,     -- Record timestamp
    INDEX idx_logs_created_at (created_at),            -- Index for time-range queries
    INDEX idx_logs_model_used (model_used),           -- Index for model-usage stats
    INDEX idx_logs_status_code (status_code)          -- Index for status code filtering
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 3.2 Automated Database Initialization Script (`scripts/init_db.py`)

- Reads MySQL configuration from `app/core/config.py`.
- Step 1: Connect to MySQL server and execute `CREATE DATABASE IF NOT EXISTS litellm_db`.
- Step 2: Connect to `litellm_db` database and execute `CREATE TABLE IF NOT EXISTS llm_request_logs`.
- Idempotency design: Repeated runs will not alter existing data or throw errors.

---

## 4. Daily Automated FX Rate Module & Two-Tier Caching (`app/core/fx_rate.py`)

### 4.1 Two-Tier Cache & Retrieval Logic (L1 Memory + L2 Redis)

```
[Get FX Rate]
     |
     v
[L1 In-Memory Cache Hit?] --Yes--> Return Rate (0ms)
     | No
     v
[L2 K3s Redis Cache Hit?] (Shared Redis Client, try-except guard) --Yes--> Backfill L1 and Return Rate
     | No / Redis Outage
     v
[Request Public FX API (open.er-api.com)] --Success--> Async write L1 + L2 (TTL 86400s) & Return
     | Failure
     v
[Silent Fallback] --> Prefer stale L1 value or .env DEFAULT_USD_TO_CNY_RATE (7.2300)
```

1. **L1 Local In-Memory Cache**:
   - Maintains process-level variables `_cached_rate` and `_last_fetch_time`.
   - 0ms retrieval latency; high-frequency intra-pod requests impose zero network or Redis load.
2. **L2 K3s Redis Shared Cache**:
   - Reuses existing K3s Redis (`100.105.130.0:6379`) singleton connection pool.
   - Key: `fx:usd_cny_rate`, TTL set to 86400 seconds (24 hours).
   - **Isolated Exception Protection**: Redis reads and writes are wrapped in fine-grained `try-except` blocks; Redis downtime never impairs primary requests.
3. **Public API Refresh & Fallback**:
   - When both cache tiers expire, asynchronously queries `https://open.er-api.com/v6/latest/USD` via `httpx.AsyncClient`.
   - Any network timeout or parsing error is automatically captured and silently degraded to the configured fallback `DEFAULT_USD_TO_CNY_RATE` (default `7.2300`), never disrupting core traffic.

---

## 5. Async Logging Hook & LiteLLM Configuration (`app/core/logging_hook.py`)

### 5.1 Async CustomLogger Implementation Details

Inherits from LiteLLM `CustomLogger`:

1. **Success Callback (`async_log_success_event`)**:
   - Extracts `request_id`, `model_requested`, `model_used`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `response_cost`, and `response_time_ms`.
   - Asynchronously calls `get_usd_to_cny_rate()` to calculate `cost_cny = round(cost_usd * fx_rate, 6)`.
   - Parameterized insert of log record with `status_code=200`.
2. **Failure Callback (`async_log_failure_event`)**:
   - Captures `429 (Rate Limit)`, `500 (Upstream Error)`, `Timeout`, and other error status codes.
   - Records request duration `latency_ms` and `model_requested`, setting tokens and costs to `0` while capturing exact status codes.
3. **aiomysql Connection Pool Lifecycle & Keep-Alive**:
   - Employs **async singleton lazy loading** `get_db_pool()`, initialized on the running asyncio loop upon first logging event.
   - Parameters: `minsize=1`, `maxsize=10`, `pool_recycle=300` (reconnects every 5 minutes, preventing idle connection drops causing `2006 MySQL server has gone away`), `autocommit=True`.
   - Provides `close_db_pool()` for unit testing and graceful shutdown.
4. **Complete Fault Isolation**: All callback methods are wrapped in global `try ... except Exception as err` blocks.

### 5.2 LiteLLM Gateway Configuration (`config.yaml`)

```yaml
router_settings:
  routing_strategy: "least-busy"
  num_retries: 3
  allowed_fails: 1
  cooldown_time: 60
  fallbacks:
    - gemini-3.7-flash: ["gemini-3.7-pro-plan", "gemini-3.7-backup"]

litellm_settings:
  callbacks: ["app.core.logging_hook.custom_logger"]
  stream_usage: true  # 🌟 CRITICAL: Ensures streaming (stream=True) requests compute and return Tokens & Cost upon completion
  cache: true
  cache_params:
    type: redis
    host: os.environ/REDIS_HOST
    port: os.environ/REDIS_PORT
    password: os.environ/REDIS_PASSWORD
    supported_call_types: [chat_completion]
    ttl: 3600
```

---

## 6. Container Image & Dockerfile Updates (`Dockerfile`)

### 6.1 `Dockerfile` Enhancements

- **Python Module Search Path**: Added `PYTHONPATH="/app"` to `ENV` in `Dockerfile`, ensuring that the `litellm` CLI process reliably loads and executes custom callback classes from `app.core.logging_hook`.
- **Multi-Architecture Build**: Retains existing `linux/amd64` and `linux/arm64` dual-architecture build pipeline, running as secure non-root user `65532:65532`.

---

## 7. File Modification Inventory

| No. | File Path | Change Type | Responsibility Description |
| :--- | :--- | :--- | :--- |
| 1 | `Dockerfile` | Modify | Add `PYTHONPATH="/app"` to environment variables to ensure LiteLLM CLI runtime correctly loads custom Python Hooks. |
| 2 | `config.yaml` | Modify | Configure `callbacks: ["app.core.logging_hook.custom_logger"]` and `stream_usage: true`. |
| 3 | `.env.example` & `.env` | Modify | Configure `MYSQL_PASSWORD` environment variable and add `DEFAULT_USD_TO_CNY_RATE=7.23`. |
| 4 | `app/core/config.py` | Modify | Add `default_usd_to_cny_rate: float = 7.23` validation field to `Settings` class. |
| 5 | `scripts/init_db.py` | Create | One-click idempotent initialization of OCI MySQL `litellm_db` database and `llm_request_logs` table. |
| 6 | `app/core/fx_rate.py` | Create | Daily asynchronous USD/CNY FX rate retrieval with L1 memory + L2 Redis two-tier cache and fallback module. |
| 7 | `app/core/logging_hook.py` | Create | Subclasses LiteLLM `CustomLogger`, implementing async MySQL logging for success/failure hooks, fallback tracing, and connection pool keep-alive. |
| 8 | `scripts/verify_db_logging.py` | Create | Smoke test script: sends standard and streaming API requests, verifying OCI MySQL persistence and RMB cost conversion. |
| 9 | `tests/test_fx_rate.py` | Create | Unit tests for FX rate API requests, L1/L2 two-tier cache, and fallback mechanisms. |
| 10 | `tests/test_logging_hook.py` | Create | Unit tests for async hook field extraction (success/failure/stream/fallback), RMB calculations, and MySQL exception decoupling. |

---

## 8. Step-by-Step Milestones

### Phase I: Dockerfile Adjustments, Vault Secret Management & DB Automation (Step 1)
1. Add `PYTHONPATH="/app"` to `Dockerfile`.
2. Register and host MySQL credentials in OCI Vault (`gateman-vault`).
3. Populate `MYSQL_PASSWORD` and FX parameters in local `.env` from Vault.
4. Run `uv run python -m scripts.init_db`, verifying console output `Database litellm_db and table llm_request_logs initialized successfully.`.
5. Run `uv run python -m scripts.check_phase1` to verify connectivity pass (`mysql: OK`).

### Phase II: Two-Tier Cached FX Rate Module Development & Testing (Step 2)
1. Write `app/core/fx_rate.py` (implementing L1 memory + L2 Redis caching with isolated exception handling).
2. Write `tests/test_fx_rate.py` unit tests, covering L1 hit, L2 Redis hit, Redis outage recovery, API refresh, and fallback scenarios.
3. Run `uv run pytest -q tests/test_fx_rate.py` to confirm pass.

### Phase III: Async Logging Hook Implementation & Testing (Step 3)
1. Write `app/core/logging_hook.py` implementing `DBLoggingLogger(CustomLogger)` (including success/failure dual event handling, fallback tracking, and `pool_recycle` keep-alive).
2. Write `tests/test_logging_hook.py` unit tests, verifying standard/streaming response parsing, error status code capturing, and USD-to-RMB calculation logic (Mock DB).
3. Run `uv run pytest -q tests/test_logging_hook.py` to confirm pass.

### Phase IV: Proxy Configuration Integration & End-to-End Acceptance (Step 4)
1. Configure `callbacks` and `stream_usage: true` in `config.yaml`.
2. Run smoke test script `scripts/verify_db_logging.py` to send standard and streaming test requests.
3. Query OCI MySQL:
   ```sql
   SELECT request_id, model_requested, model_used, prompt_tokens, completion_tokens, total_tokens, cost_usd, cost_cny, fx_rate, latency_ms, status_code 
   FROM llm_request_logs 
   ORDER BY created_at DESC 
   LIMIT 5;
   ```
4. Verify standard and streaming requests correctly record tokens, latency, and costs, with `cost_cny` strictly equal to `round(cost_usd * fx_rate, 6)` and fallback trajectories clearly visible.
5. Run `uv run pytest` and `uv run ruff check app scripts tests` to ensure all automated tests and code quality checks pass.

---

## 9. Risk Control & Standards

1. **Absolute Non-Blocking Isolation**: Database writes and exchange rate retrieval must execute fully asynchronously; underlying network timeouts, database errors, or Redis failures are strictly forbidden from causing LLM API errors or disruptions.
2. **Streaming Token Integrity**: `stream_usage: true` must be explicitly enabled; silent loss bugs resulting in 0-token or 0-cost logs for streaming requests are strictly unacceptable.
3. **Connection Keep-Alive**: `aiomysql` connection pool must configure `pool_recycle=300` and `autocommit=True` to completely eliminate `MySQL 2006 server has gone away` errors caused by firewall silent drops on idle connections.
4. **Monetary Precision Standards**: `cost_usd` and `cost_cny` database fields must use `DECIMAL(10, 6)`, and exchange rates must use `DECIMAL(8, 4)`; storing direct floating-point numbers via MySQL `FLOAT/DOUBLE` or raw Python floats is strictly prohibited.
5. **Secret Security & Zero Hardcoding**: Database passwords are centrally hosted in **OCI Vault (`gateman-vault`)** as the single source of truth, synced automatically to K8s Secrets via **ESO** in production, and isolated via `.env` locally; committing plaintext secrets in code, test cases, YAML, or Git history is strictly prohibited.
