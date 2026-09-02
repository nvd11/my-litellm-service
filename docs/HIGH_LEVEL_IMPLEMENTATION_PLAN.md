# High-Level Implementation Plan & Technical Specifications

**Project Name**: `my-litellm-service` (Enterprise Multi-LLM Gateway & Evaluation Middleware)  
**Author**: Jason Pan (Senior Cloud & AI Solutions Architect)  
**Version**: v1.1.0 (Updated: Deterministic & Golden Dataset Evaluation Engine)  
**Status**: Draft for Hands-On Implementation  
**Target Platforms**: Tencent Cloud K3s Cluster (OCI `free-arm-vm` Node), ArgoCD, Existing Kong Gateway, OCI MySQL HeatWave Always Free (`rin-heatwave`), Redis  

---

## 1. Background & Objectives

### 1.1 Context
With the exponential growth of enterprise demand for Large Language Models (LLMs), engineering teams face four major operational bottlenecks:
1. **Vendor Lock-in & API Fragmentation**: Inconsistent SDKs and API schemas across LLM providers (OpenAI, Google Vertex AI, Anthropic) introduce heavy coupling in application code.
2. **High Availability & Rate Limiting (Rate Limit / Failover)**: Lack of transparent failover mechanisms when a single model provider encounters `429 Too Many Requests` or `5xx Server Error` outages.
3. **Unmonitored Costs & Token Blindspots**: Absence of unified token consumption tracking and real-time USD cost audit dashboards prevents anomaly detection and budget capping.
4. **Lack of Objective Evaluation Infrastructure (Eval Harness)**: When benchmarking candidate models (e.g., Gemini 1.5 Pro vs. GPT-4o) on quality, latency, and unit economics, teams lack zero-cost, objective evaluation middleware.

### 1.2 Goals
`my-litellm-service` aims to construct a lightweight, enterprise-grade, high-availability LLM gateway and evaluation middleware on top of an existing **K3s cluster**:
* Standardize upstream LLM access behind a unified **OpenAI-compatible API**.
* Implement **OCI MySQL audit logging** to asynchronously record prompt/completion tokens and USD spend per request (persisted in OCI Always Free managed database `rin-heatwave` to prevent data loss upon compute node failure).
* Reuse existing **K3s Redis** routed via Kong L4 TCP proxy to provide rate limiting (RPM/TPM) and exact caching, without provisioning a dedicated Redis instance.
* Provide an objective, microsecond-latency, zero-API-cost benchmark engine covering accuracy, latency, and cost using **Deterministic Assertions (Option A)** and **Golden Dataset Matching (Option B)**.

---

## 2. System Architecture

The architecture adopts a **"One Repository, Two Deployments"** pattern to balance process isolation, independent lifecycles, and operational simplicity.

```
                          +-----------------------------------+
                          |       Clients / Eval Harness      |
                          +-----------------------------------+
                                            |
                                            v
                          +-----------------------------------+
                          | Existing Kong Gateway / Ingress  |
                          |   HTTP Routes: /llm and /eval     |
                          +-----------------------------------+
                                   |                    |
                                   v                    v
              +--------------------------------+  +--------------------------------+
              | Service A: LiteLLM Deployment  |  | Service B: FastAPI Deployment  |
              | ClusterIP :4000                |  | ClusterIP :8000                |
              | Router / Fallback / Cache      |  | Eval / Metrics APIs             |
              +--------------------------------+  +--------------------------------+
                         |          ^                         |
                         |          | Kubernetes DNS           |
                         |          +-------------------------+
                         v                                    |
              +----------------------+                         |
              | Existing K3s Redis  |                         |
              | OCI free-arm-vm     |                         |
              | Kong L4 :6379       |                         |
              +----------------------+                         |
                         |                                    |
                         v                                    v
              +----------------------+              +----------------------+
              | OCI MySQL HeatWave  |              | External LLM APIs    |
              | Logs / Budgets      |              | OpenAI / Vertex AI   |
              +----------------------+              +----------------------+

  ArgoCD synchronizes both Deployments, Services, ConfigMaps and Secret references
  from Git into the K3s cluster. Initial scheduling is pinned to OCI free-arm-vm.
```

### 2.1 Service & Component Responsibilities
1. **Service A: LiteLLM Deployment (ClusterIP Port 4000)**
   * Functions as the core LLM gateway: handles protocol transformation, model load balancing, and failover routing.
   * Built-in Admin UI for model key management and configuration inspection.
   * Asynchronously logs latency, token consumption, and USD spend to OCI MySQL and Redis via custom callbacks.
2. **Service B: FastAPI Deployment (ClusterIP Port 8000)**
   * Delivers application-layer APIs, custom benchmark triggers, and cost reporting aggregations.
   * Integrates the local objective evaluation engine featuring **Option A (JSON Schema / Code / Regex)** and **Option B (Golden Answer Matching)**.
   * Exposes `/v1/eval/run`, `/v1/metrics/spend`, and `/health` endpoints.
3. **Data & Storage Layer**
   * **OCI MySQL HeatWave (9.7+)**: Persistently stores request logs (`llm_request_logs`) and evaluation results (`eval_benchmarks`), ensuring survival across compute node lifecycles.
   * **Existing K3s Redis (7+)**: Pinned to the OCI `free-arm-vm` and reachable via Kong L4 TCP proxy. Handles API rate limiting counters, token bucket quotas, and response caching for frequent prompts. No new Redis instances are created.

---

## 3. Functional Specifications

### 3.1 Unified Routing & Failover
* **API Compatibility**: Fully adheres to the standard `POST /v1/chat/completions` specification.
* **Supported Models**:
  * `openai/gpt-4o` / `openai/gpt-4o-mini`
  * `gemini/gemini-1.5-pro` / `gemini/gemini-1.5-flash` (via GCP Vertex AI / Service Account)
  * `anthropic/claude-3-5-sonnet`
* **Failover Rules**:
  * Automatically switches to backup models upon receiving `429 (Rate Limit)`, `500`, or `503` upstream responses.

### 3.2 Cost Audit & Token Metering
* **Token Accounting**: Accurately records `prompt_tokens`, `completion_tokens`, and `total_tokens`.
* **USD Cost Calculation**: Real-time per-request calculation based on model unit pricing (up to 6 decimal places).
* **OCI MySQL Persistence**: Asynchronous ingestion into the `llm_request_logs` table.

### 3.3 Evaluation Engine Design (Option A + Option B)
Eliminating expensive and biased LLM-as-a-Judge dependencies, the engine runs purely local, objective evaluators:

1. **Option A: Deterministic Validation**
   * **`eval_type="json_schema"`**: Validates LLM structured outputs against schemas via `pydantic` or `jsonschema` (binary 100 or 0 score).
   * **`eval_type="code_exec"`**: Runs generated Python code against test suites in a secured sandbox.
   * **`eval_type="contains"`**: Evaluates regex patterns or keyword inclusion rules.
2. **Option B: Golden Dataset Matching**
   * **`eval_type="exact_match"`**: Performs strict string matching against expected `golden_output`.
   * **`eval_type="similarity"`**: Computes local text similarity using `difflib` or character n-gram coverage (score range: 0.00 ~ 1.00).

### 3.4 FastAPI Middleware & Eval Harness APIs
* **`POST /v1/eval/run`**: Accepts test suites, dispatches concurrent requests across candidate models, and returns latency (`latency_ms`), spend (`cost_usd`), and score (`accuracy_score`).
* **`GET /v1/metrics/spend`**: Queries cumulative USD spend grouped by date and model.

---

## 4. Database Schema

```sql
-- 1. Core Request and Cost Logging Table (MySQL 9.7 / OCI HeatWave)
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

-- 2. LLM Benchmark Evaluation Results Table (Option A + Option B)
CREATE TABLE IF NOT EXISTS eval_benchmarks (
    id VARCHAR(36) PRIMARY KEY,
    eval_run_id VARCHAR(64) NOT NULL,
    prompt_name VARCHAR(128) NOT NULL,
    model_name VARCHAR(64) NOT NULL,
    eval_type VARCHAR(32) NOT NULL, -- json_schema, exact_match, code_exec, similarity
    response_content TEXT,
    latency_ms INT NOT NULL,
    cost_usd DECIMAL(10, 6) NOT NULL,
    accuracy_score DECIMAL(5, 2), -- 0.00 ~ 100.00
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_eval_run_id (eval_run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

---

## 5. Environment Configuration

```env
# GCP & System Environment
GCP_PROJECT_ID=your-gcp-project-id
GCP_REGION=us-central1

# OCI MySQL Database Connection (rin-heatwave)
MYSQL_USER=admin_user
MYSQL_PASSWORD=your-secure-password
MYSQL_DB=litellm_db
MYSQL_HOST=10.0.0.247
MYSQL_PORT=3306

# Existing K3s Redis: OCI free-arm-vm, exposed through Kong L4 over Tailscale.
REDIS_HOST=100.105.130.0
REDIS_PORT=6379
REDIS_PASSWORD=load-from-private-env

# LLM Provider API Keys
OPENAI_API_KEY=sk-proj-...
VERTEXAI_PROJECT=your-gcp-project-id
VERTEXAI_LOCATION=us-central1

# LiteLLM Proxy Configuration
LITELLM_MASTER_KEY=sk-mas...dmin
LITELLM_PORT=4000
FASTAPI_PORT=8000
```

---

## 6. Hands-On Execution Roadmap

### Phase 1: Infrastructure Integration & LiteLLM Proxy Bootstrapping
- [ ] Verify K3s nodes can reach OCI MySQL (`10.0.0.247:3306`) and Redis Service / Kong L4 (`6379`); inject Redis secrets via Kubernetes Secrets.
- [ ] Author `config.yaml` specifying OpenAI, Gemini, and Claude routing and fallback policies.
- [ ] Start the LiteLLM Proxy process and verify `http://localhost:4000/health` and `/v1/chat/completions`.

### Phase 2: OCI MySQL Persistence & Database Hook
- [ ] Run MySQL DDL scripts to create `llm_request_logs` and `eval_benchmarks` tables.
- [ ] Configure LiteLLM custom callbacks to persist token consumption and USD spend asynchronously to OCI MySQL.

### Phase 3: FastAPI Middleware & Local Eval Engine (Option A+B) Development
- [ ] Implement the FastAPI entry application `main.py`.
- [ ] Implement Option A (JSON Schema / code assertions) and Option B (Golden Answer matching) evaluation functions.
- [ ] Expose `/v1/eval/run` for concurrent multi-model benchmarking.
- [ ] Expose `/v1/metrics/spend` to query daily and cumulative spend from OCI MySQL.

### Phase 4: K3s + ArgoCD Deployment & Kong Routing
- [ ] Build a multi-architecture Python 3.12 container image and verify ARM64 compatibility.
- [ ] Write Kubernetes manifests for LiteLLM and FastAPI: Deployments, ClusterIP Services, ConfigMaps, Secret refs, and probes.
- [ ] Configure resource requests/limits and schedule workloads to OCI `free-arm-vm` via `nodeSelector`.
- [ ] Add Kong HTTP routes for public access without introducing secondary Kong instances or using `hostNetwork`.
- [ ] Register the ArgoCD Application in `my-argocd-manifests` with automated sync, selfHeal, and controlled prune.
- [ ] Perform end-to-end integration testing, rolling updates, self-healing tests, and stress testing.

---
*End of High-Level Specification.*
