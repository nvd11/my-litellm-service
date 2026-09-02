# Troubleshooting LiteLLM Admin UI MySQL Incompatibility and Implementing a Dual-Database Decoupled Architecture

## 1. Background

In daily operations of the LiteLLM Proxy, we use a custom `CustomLogger` (built on SQLAlchemy 2.0 Core) to asynchronously persist all API audit logs, token usage, and converted CNY spend into OCI MySQL (`litellm_db.llm_request_logs`). This data-plane logging solution has been operating reliably.

However, attempting to enable the official LiteLLM Admin Web UI (`/ui`) caused failures. Accessing `/ui` and attempting to log in with the administrator Master Key returned `400 Bad Request`:

```json
{"error": "Authentication Error, Not connected to DB!"}
```

---

## 2. Root Cause Analysis: Why LiteLLM UI Cannot Run Directly on MySQL

Inspecting the LiteLLM source code reveals two distinct data tiers in its architecture:

1. **Data Plane**: Request routing, streaming token aggregation, audit logging, and cost accounting. This layer integrates flexibly with any database (e.g., MySQL, ClickHouse, DynamoDB) via LiteLLM's Custom Callback mechanism.
2. **Control Plane**: User authentication for the official Next.js Web UI, JWT session persistence, Virtual Key generation, and team quota management.

The control plane strictly relies on Python's Prisma ORM (`prisma-client-py`). Reviewing LiteLLM's built-in `litellm/proxy/schema.prisma` definition:

```prisma
model LiteLLM_VerificationToken {
  token       String   @id
  key_name    String?
  key_alias   String?
  models      String[] // PostgreSQL scalar array
  spend       Float    @default(0.0)
  max_budget  Float?
  permissions Json?
  // ...
}
```

Within Prisma's type system:
- `models String[]` is declared as a **Scalar Array**.
- PostgreSQL natively supports scalar arrays.
- MySQL does not support native scalar arrays (only JSON or strings).
- The Prisma schema compiler enforces a strict constraint: if `provider = "mysql"`, scalar array syntax is strictly forbidden.

Because LiteLLM's official schema hardcodes core fields like `models` as `String[]`, pointing `DATABASE_URL` directly to MySQL triggers immediate compilation and runtime errors during bootstrapping (`prisma generate` / `prisma migrate`), making the official UI unusable on MySQL.

Furthermore, running without a control plane database introduces administrative drawbacks:
- Without Virtual Key generation, all consumers must share the global `LITELLM_MASTER_KEY`.
- Audit logs cannot attribute `api_key_alias` to specific clients, preventing multi-tenant spend and accounting attribution.

---

## 3. Architecture: Decoupled Control Plane and Data Plane

To enable the official Admin UI while preserving the existing MySQL audit architecture, we implemented a dual-database decoupled design:

```
                    Clients / Browsers
                          │
                          ▼
            Kong Gateway Ingress (KIC)
            - API Routes: /litellm/v1/... (strip-path: true)
            - UI Routes:  /ui, /_next, /login, /v2, /key, etc. (pass-through)
                          │
                          ▼
             LiteLLM Proxy Service (:4000)
             ┌───────────────────────────┐
             │ Uvicorn / FastAPI Runtime │
             └─────────────┬─────────────┘
                           │
             ┌─────────────┴─────────────┐
             ▼                           ▼
  [Control Plane]               [Data Plane]
  Neon Serverless PostgreSQL    OCI MySQL HeatWave
  - Admin Web UI sessions       - Async audit logs
  - Virtual sub-keys            - Token accounting
  - Team budgets & allowlists   - Real-time FX & CNY conversion
```

Component division:
- **Control Plane (Neon PostgreSQL)**: Serverless PostgreSQL on Neon (AWS Singapore region) with direct co-located networking to the OCI Singapore ARM node (measured latency ~10.95ms). Used exclusively for Admin UI metadata and Virtual Key management without receiving high-frequency logging writes.
- **Data Plane (OCI MySQL HeatWave)**: Continues processing asynchronous audit streams for all chat sessions via our custom `logging_hook.py`, isolated from the control plane.
- **Cache Layer (Redis)**: Internal K3s Redis instance handling response caching and daily exchange rate L2 caching.

---

## 4. Implementation Details & Lessons Learned

### 4.1 Neon Connection Pooling (PgBouncer) vs. DDL Migration Conflicts

Neon connection strings default to a `-pooler` hostname suffix (routed via PgBouncer).

When LiteLLM connects to a fresh database, it automatically runs `prisma migrate deploy` or `prisma db push` to initialize schema tables. However, PgBouncer running in Transaction mode does not support Prepared Statements or certain DDL operations, causing startup crashes:

```text
Prepared statements not supported in transaction mode
```

**Resolution**:
Configure `DATABASE_URL` using the direct endpoint (removing `-pooler` from the hostname), targeting port 5432 with `?sslmode=require`:

```text
postgresql://neondb_owner:***@ep-bitter-sky-azfg5i09.c-3.ap-southeast-1.aws.neon.tech:5432/neondb?sslmode=require
```

---

### 4.2 Missing Prisma CLI Dependencies in ARM64 Docker Builds

Adding `prisma>=0.15.0` to `pyproject.toml` during multi-arch GitHub Actions builds (`linux/amd64,linux/arm64`) triggered the following error:

```text
subprocess.CalledProcessError: Command '['/root/.cache/prisma-python/nodeenv/bin/npm', 'install', 'prisma@5.17.0']' returned non-zero exit status 127.
```

**Root Cause**:
The `python:3.12-slim` base image lacks Node.js and npm. When Python's `prisma` package cannot locate a system Node.js binary, it uses `nodeenv` to download precompiled Node binaries into cache. In slim images, missing dynamic libraries lead to execution failure (exit code 127).

**Resolution**:
1. Install `nodejs`, `npm`, `ca-certificates`, and `openssl` via `apt-get` in the Dockerfile.
2. Set environment variable `PRISMA_USE_GLOBAL_NODE="true"` to force Prisma to use system Node.js.
3. Pre-create writable cache directory `/tmp/prisma-cache` for non-root user (UID `65532`) and export `PRISMA_HOME_DIR="/tmp/prisma-cache"`.

Key Dockerfile configuration:

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH="/app" \
    PATH="/app/.venv/bin:$PATH" \
    PRISMA_HOME_DIR="/tmp/prisma-cache" \
    PRISMA_USE_GLOBAL_NODE="true"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    openssl \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md ./

RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-install-project \
    && mkdir -p /tmp/prisma-cache \
    && PRISMA_HOME_DIR=/tmp/prisma-cache uv run prisma generate --schema=/app/.venv/lib/python3.12/site-packages/litellm/proxy/schema.prisma \
    && chmod -R 777 /tmp/prisma-cache \
    && rm -rf /root/.cache

COPY config.yaml ./config.yaml
COPY app ./app

USER 65532:65532
EXPOSE 4000

ENTRYPOINT ["litellm"]
CMD ["--config", "/app/config.yaml", "--host", "0.0.0.0", "--port", "4000"]
```

---

### 4.3 Secret Management & GitOps Workflow

Database connection strings are securely managed in OCI Vault:

1. Register secret `litellm-database-url` in OCI Vault (`litellm-prod` compartment).
2. Configure ExternalSecret mapping in `my-argocd-manifests/argocd-apps/litellm-svc-app.yaml`:
   ```yaml
   externalSecret:
     enabled: true
     secretStoreRef:
       name: oci-litellm-vault-store
       kind: SecretStore
     target:
       name: litellm-secrets
     data:
       - secretKey: DATABASE_URL
         remoteRef:
           key: litellm-database-url
       - secretKey: MYSQL_PASSWORD
         remoteRef:
           key: litellm-mysql-password
   ```
3. Pushes to `my-litellm-service` trigger multi-arch CI builds, updating `image.digest` in the ArgoCD repository via `repository_dispatch` for automated GitOps reconciliation.

---

### 4.4 Kong Ingress Controller Routing Configuration

LiteLLM's Next.js Admin UI requires routing static assets (`/_next/static/...`, `/litellm-asset-prefix/...`) and administrative endpoints (`/login`, `/key`, `/user`, `/models`, etc.) in addition to the root `/ui` path.

Configure `extraRoutes.ui-route` in the Helm values to pass these routes to the LiteLLM Pod:

```yaml
extraRoutes:
  ui-route:
    parentGateway: kong-main-gateway
    parentGatewayNamespace: default
    rules:
      - matches:
          - path: /ui
          - path: /litellm-asset-prefix
          - path: /_next
          - path: /fallback
          - path: /swagger
          - path: /get_favicon
          - path: /get_logo_url
          - path: /favicon.ico
      - matches:
          - path: /login
          - path: /v2
          - path: /v3
          - path: /auth
          - path: /sso
          - path: /onboarding
          - path: /invitation
      - matches:
          - path: /key
          - path: /user
          - path: /team
          - path: /spend
          - path: /budget
      - matches:
          - path: /models
          - path: /model
          - path: /routes
          - path: /config
          - path: /settings
      - matches:
          - path: /health
          - path: /cache
          - path: /alerting
```

---

## 5. End-to-End Functional Verification

### 5.1 Startup and Migration Verification
Inspecting Pod logs confirms Prisma connects to Neon PostgreSQL and completes schema migration:

```text
All migrations have been successfully applied.
2026-08-30 08:54:21,253 - litellm_proxy_extras - INFO - prisma migrate deploy completed
2026-08-30 08:54:50,896 - litellm_proxy_extras - INFO - Migration diff applied successfully
2026-08-30 08:54:50,896 - litellm_proxy_extras - INFO - Post-migration sanity check completed
INFO: Application startup complete. Uvicorn running on http://0.0.0.0:4000
```

### 5.2 Admin UI Login & Virtual Key Generation
1. Log into `https://gw.jppwl.asia/ui` using the master key to inspect system status.
2. Generate a rate-limited Virtual Key via `/key/generate` or UI:
   ```bash
   curl -X POST "http://gw.jpgcp.cloud:31850/key/generate" \
     -H "Authorization: Bearer $LITEL...KEY" \
     -H "Content-Type: application/json" \
     -d '{
       "key_alias": "team-test-key",
       "models": ["gemini-3.7-flash"],
       "max_budget": 10.0
     }'
   ```
   Returns generated key: `sk-fnh...haOg`.

### 5.3 Virtual Key Invocation & MySQL Audit Logging
Issue a completion request using the new Virtual Key:

```bash
curl -X POST "http://gw.jpgcp.cloud:31850/litellm/v1/chat/completions" \
  -H "Authorization: Bearer sk-fnh...haOg" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.7-flash",
    "messages": [{"role": "user", "content": "hello"}],
    "max_tokens": 10
  }'
```

The response returns valid output alongside budget headers:
```http
HTTP/1.1 200 OK
X-Litellm-Key-Max-Budget: 10.0
X-Litellm-Key-Spend: 0.000032
```

Querying OCI MySQL table `litellm_db.llm_request_logs`:
```sql
SELECT request_id, api_key_alias, model_requested, total_tokens, cost_usd, cost_cny, fx_rate 
FROM llm_request_logs 
ORDER BY id DESC LIMIT 1;
```

The record accurately reflects token count, real-time FX rate, converted CNY spend, and `api_key_alias` attributed to `team-test-key`.

---

## 6. Summary

By decoupling the Control Plane (PostgreSQL) from the Data Plane (MySQL), we resolved LiteLLM Admin UI's hard dependency on PostgreSQL scalar arrays while retaining high-performance MySQL audit logging.

Key advantages:
1. **Separation of Concerns**: The control plane handles auth, Virtual Keys, and quotas, while the data plane processes high-throughput asynchronous request logging and billing.
2. **Fault Isolation**: Control plane cold-starts or network blips never block proxy request routing or MySQL audit logging.
3. **Zero Extra Cost**: Combining Neon Serverless PostgreSQL Free Tier with existing OCI MySQL and K3s infrastructure delivers full enterprise functionality without additional hardware spend.
