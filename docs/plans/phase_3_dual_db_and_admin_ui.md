# Phase 3 Implementation Plan: Dual-Database Decoupled Architecture (PostgreSQL Control Plane + MySQL Data Plane) & LiteLLM Admin UI Full Enablement

---

## 1. Motivation & Root Causes (Why)

After completing Phase 2 (OCI MySQL HeatWave asynchronous audit logging, daily exchange rates, and multi-tier fallback trajectories) in production, we faced three core pain points during operational management:

### Pain Point 1: LiteLLM Official Admin Web UI Hard-Bound to PostgreSQL (Prisma Type Deadlock)
* **Symptom**: When attempting to access and log in to the LiteLLM official Web Console (`/ui`), backend APIs returned `400 Bad Request: {"error": "Authentication Error, Not connected to DB!"}`.
* **Root Cause**: LiteLLM's official Next.js console uses **Prisma ORM (`prisma-client-py`)** under the hood to manage user accounts, JWT login sessions, and virtual keys. In the official built-in `schema.prisma`, fields such as `models` and `permissions` are defined as **PostgreSQL native scalar array types (`String[]`)**. Because MySQL does not natively support array types, Prisma cannot switch its `provider` to `mysql`. Consequently, the official LiteLLM Web UI is rigidly bound to PostgreSQL and cannot directly use our existing MySQL instance.

### Pain Point 2: Single Master Key Causes Missing Cost Allocation & Zero Permission Isolation
* **Symptom**: All current clients (including test scripts, agent instances, Codex, etc.) send requests carrying the global `LITELLM_MASTER_KEY`, causing the `api_key_alias` field in OCI MySQL `llm_request_logs` to uniformly record as `default`.
* **Business Risks**:
  1. Inability to implement **fine-grained token consumption and CNY financial cost accounting** across different dedicated sub-agents and business units;
  2. Inability to configure **independent model whitelists** (e.g., restricting a specific instance to cheap Flash models only) and **hard budget limits** (e.g., automatic circuit breaking upon overrun) for distinct callers.

### Pain Point 3: Lack of Visual Console & High Operational Overhead
* **Requirement**: A visual Web console is needed to support generating/revoking sub-keys online, configuring RPM/TPM rate limits, and visualizing model invocation distributions and real-time health without handcrafting curl scripts.

---

## 2. Goals & Expected Value

To address these pain points, Phase 3 implements a dual-database architecture that completely decouples the control plane from the data plane, with the following concrete goals:

1. **Complete Architectural Decoupling (Dual-DB Architecture)**:
   * **Control Plane**: Integrate cloud-native serverless **Neon PostgreSQL (Singapore Region)** to exclusively host LiteLLM official Web UI logins, JWT sessions, Virtual Keys, and team quota storage;
   * **Data Plane**: Retain **OCI MySQL HeatWave (`rin-heatwave`)** to exclusively host high-concurrency API audit streams, token metering, daily real-time FX conversion, and high-precision CNY financial settlements;
   * **Cache & Performance Layer**: Reuse **K3s Redis** (`redis.redis.svc.cluster.local`) for response caching and L2 caching of daily FX rates.
2. **Unlock Full Official Admin Dashboard Capabilities**:
   * Access `http://gw.jpgcp.cloud:31850/ui` in a browser, logging in within seconds using `admin` + `LITELLM_MASTER_KEY`;
   * Support visual web UI management of virtual sub-keys, teams, users, quotas, and system settings.
3. **Enable Multi-Agent / Multi-Team Dedicated Keys & Precise Cost Allocation**:
   * Generate dedicated sub-keys for each agent instance via the Web UI (e.g., `key_alias: "hebe-arm"`, `key_alias: "yui-radxa"`);
   * When an agent invokes the gateway with its dedicated sub-key, the `api_key_alias` field in OCI MySQL `llm_request_logs` records the alias accurately, enabling one-click `GROUP BY api_key_alias` financial reporting!
4. **Zero Local Disk Overhead & ~10ms Ultra-Low Latency**:
   * Use the **Neon (AWS Singapore)** free serverless tier (0.5 GB storage + 100 compute hours with automatic idle scale-to-zero), **consuming 0 MB of disk space on the OCI ARM VM**;
   * Direct intra-city connection from the OCI Singapore VM to Neon Singapore achieves **a measured network latency of only ~10.95 ms**, ensuring extreme responsiveness.

---

## 3. Architecture Topology & Database Responsibilities

```
                                 ┌──────────────────────────────────────────────┐
                                 │             Browser Admin Web UI             │
                                 │          (http://gw.jpgcp.cloud:31850/ui)    │
                                 └──────────────────────┬───────────────────────┘
                                                        │
                                                        ▼
                        ┌──────────────────────────────────────────────────────────────┐
                        │             Kong API Gateway (Cluster B / Ingress)           │
                        │   • /litellm (strip-path: true) -> API Route Dispatch        │
                        │   • /ui, /litellm-asset-prefix, /v2/login -> Web Console     │
                        └──────────────────────┬───────────────────────────────────────┘
                                               │
                                               ▼
                        ┌──────────────────────────────────────────────────────────────┐
                        │                 LiteLLM Proxy Gateway Service (:4000)        │
                        │                                                              │
                        │  ┌─────────────────────────┐   ┌──────────────────────────┐  │
                        │  │ 1. Built-in Prisma Engine│  │ 2. Phase 2 SQLAlchemy    │  │
                        │  └────────────┬────────────┘   └─────────────┬────────────┘  │
                        └───────────────┼──────────────────────────────┼───────────────┘
                                        │ (Control Plane)              │ (Data Plane)
                                        │ DATABASE_URL (Direct Mode)   │ MYSQL_*
                                        ▼                              ▼
                 ┌─────────────────────────────┐              ┌─────────────────────────────────┐
                 │  Neon PostgreSQL (Singapore)│              │     OCI MySQL HeatWave          │
                 │   (Serverless Always Free)  │              │      (rin-heatwave instance)    │
                 ├─────────────────────────────┤              ├─────────────────────────────────┤
                 │ • Admin Web UI Login Session│              │ • Millisecond Async API Auditing│
                 │ • LiteLLM_UserTable Users   │              │ • Daily FX Conversion (USD->CNY)│
                 │ • Virtual Keys Sub-keys     │              │ • Routing Fallback Tracking     │
                 │ • Team Quotas & Limits      │              │ • High-precision CNY Settlement │
                 └─────────────────────────────┘              └─────────────────────────────────┘
```

---

## 4. Architecture Guardrails & Pitfall Prevention

Four critical technical requirements locked in prior to implementation:

1. **Prisma Engine & Non-Root (65532:65532) Permission Guardrail**:
   * The container runs as non-root `USER 65532:65532`. If Prisma attempts to write runtime engines to `/root/.cache/prisma`, a `Permission Denied` error occurs;
   * **Solution**: Install `openssl` and `ca-certificates` in the `Dockerfile`, and configure environment variable `PRISMA_CACHE_DIR="/tmp/prisma-cache"` to ensure full write permissions for non-root users.
2. **Neon Connection String Must Use Direct Mode**:
   * Neon's default `-pooler` (PgBouncer) transaction pool does not support DDL or prepared statements; LiteLLM must execute Prisma DDL schema migrations upon first connection;
   * **Solution**: Remove the `-pooler` suffix and use **Direct connection on port 5432** (`ep-bitter-sky-azfg5i09.c-3.ap-southeast-1.aws.neon.tech`) with `?sslmode=require`.
3. **CI/CD ARM64 + AMD64 Multi-Arch Release Closed Loop**:
   * Production K3s nodes run on OCI ARM64 (`free-arm-vm`); after modifying the Dockerfile, multi-arch images must be built and pushed via GitHub Actions / Docker Buildx, with the latest `sha256` manifest digest synced to ArgoCD.
4. **Initial Schema Migration Cold Start & Probe Toleration**:
   * The initial startup migration automatically creates 10+ Prisma tables, taking approximately 15~20 seconds; `initialDelaySeconds: 45` in `litellm-svc-app.yaml` satisfies this requirement, reserving an adequate initialization window.

---

## 5. Secret Management & OCI Vault Standards

Add direct-mode PostgreSQL connection strings under management in **OCI Vault (`gateman-vault` / `litellm-prod` compartment)**:

```
                    ┌────────────────────────────────────────┐
                    │       OCI Vault (gateman-vault)        │
                    │  - Secret: litellm-database-url (PG)   │  👈 [Phase 3 Direct Mode]
                    │  - Secret: litellm-mysql-password      │
                    │  - Secret: litellm-master-key          │
                    │  - Secret: litellm-redis-password      │
                    │  - Secret: litellm-openai-api-key-*    │
                    └───────────────────┬────────────────────┘
                                        │
                 ┌──────────────────────┴──────────────────────┐
                 │ (Automatic Sync via External Secrets Operator)│
                 ▼                                             ▼
    ┌───────────────────────────┐                 ┌───────────────────────────┐
    │  K3s Cluster (Production) │                 │  Local Dev Machine        │
    │  Secret: litellm-secrets  │                 │  .env (GitIgnored Cache)  │
    │  - DATABASE_URL           │                 │  - DATABASE_URL           │
    └───────────────────────────┘                 └───────────────────────────┘
```

---

## 6. Execution & Verification Checklist (12 Steps)

### 🔹 Phase I: PostgreSQL Integration & OCI Vault Secret Management

- [x] **Step 1**: Provision instance on **Neon (Singapore Region)**, obtain direct connection string, and verify two-way network connectivity from local machine and ARM VM (latency `10.95ms`). [Completed ✅]
- [x] **Step 2**: Create Secret `litellm-database-url` in OCI Vault (`gateman-vault` / `litellm-prod` compartment) storing direct-mode PG connection string (with `?sslmode=require`). [Completed ✅]
- [x] **Step 3**: Update local `.env.example` and `.env`, adding `DATABASE_URL` configuration item. [Completed ✅]

---

### 🔹 Phase II: Container Dependency Verification & Multi-Arch CI/CD Release

- [x] **Step 4**: Optimize `Dockerfile`:
  - Ensure `openssl` and `ca-certificates` are installed;
  - Inject `PRISMA_CACHE_DIR="/tmp/prisma-cache"` to resolve non-root `65532` write permissions.
- [x] **Step 5**: Run full pytest suite locally (25 passed) and `ruff check` to ensure no regressions. [Completed ✅]
- [x] **Step 6**: Commit and push code to GitHub to trigger GitHub Actions building `linux/amd64,linux/arm64` multi-arch image, and retrieve the latest image digest. [Completed ✅]

---

### 🔹 Phase III: GitOps / ArgoCD Manifest Orchestration & K3s Sync

- [x] **Step 7**: Update `my-argocd-manifests/argocd-apps/litellm-svc-app.yaml`:
  - Update `image.digest` to the multi-arch digest generated in Step 6;
  - Add `DATABASE_URL` mapping in `externalSecret.data` (pointing to Vault's `litellm-database-url`);
  - Confirm Kong Gateway API `extraRoutes.ui-route` covers all login, auth, and static asset routes. [Completed ✅]
- [x] **Step 8**: Commit and push `my-argocd-manifests` to trigger ArgoCD automatic synchronization and Pod rolling update. [Completed ✅]
- [x] **Step 9**: Monitor Pod logs, confirming initial startup executes Prisma DDL migrations and dual-DB connections (Neon PG ready + OCI MySQL healthy) initialize properly. [Completed ✅]

---

### 🔹 Phase IV: Web UI Login, Virtual Key Issuance & Dual-DB Closed-Loop Verification

- [ ] **Step 10 (Web Login Verification)**: Visit `http://gw.jpgcp.cloud:31850/ui` in a browser, log in using `admin` and password (`LITELLM_MASTER_KEY`), and confirm entry into Dashboard console.
- [ ] **Step 11 (Virtual Key Issuance)**: Create a test key in Web UI "API Keys" panel (alias `hebe-ui-test`, model restricted to `gemini-3.7-flash`, budget 10 USD), obtaining the generated `sk-...` token.
- [ ] **Step 12 (Dual-DB Closed-Loop Verification)**:
  - Send a request to the gateway using the newly generated `sk-...`;
  - Query OCI MySQL `rin-heatwave` directly to confirm `api_key_alias` is recorded accurately as `hebe-ui-test`, with real-time FX rate conversion and cost calculations validated 100%!

---

## 7. Deliverables & Safety Guardrails

1. **Zero Hardcoding Guardrail**: PostgreSQL connection strings must be centrally managed in OCI Vault; exposing plaintext passwords in YAML, Git repositories, or logs is strictly prohibited.
2. **High Availability Disaster Recovery**: PostgreSQL only affects the Web console and Virtual Key rule updates; even if external PG encounters temporary cold-start latency, LiteLLM's core API forwarding and OCI MySQL asynchronous audit logging maintain a 100% SLA without blocking online conversational traffic.
