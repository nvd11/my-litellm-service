# Phase 4 Implementation Plan & Architectural Review: LiteLLM Control Plane Database Migration from Neon PG to CockroachDB - Investigation & Final Rollback Record

---

## 1. Motivation & Background (Why)

In Phase 3, we successfully built the "dual-database decoupled architecture": utilizing **Neon Serverless PostgreSQL** as the Control Plane to host LiteLLM official Admin Web UI and Prisma ORM metadata; utilizing **OCI MySQL HeatWave** as the Data Plane to record full API audit logs and daily foreign exchange rates.

To further explore a larger-capacity, long-term maintenance-free solution, we conducted engineering practice and liveness evaluation on migrating the Control Plane to **CockroachDB Serverless (10GB Always Free)**.

---

## 2. Implementation & Root Cause Analysis

After switching the Control Plane database connection string to CockroachDB and triggering rolling deployment via GitOps, Prisma ORM threw an underlying hard-validation interception upon LiteLLM Pod startup:

### 🚨 Prisma CLI Hard Check Interception Log
```text
2026-08-30 14:22:38,246 - litellm_proxy_extras - INFO - Running prisma migrate deploy
2026-08-30 14:22:53,343 - litellm_proxy_extras - INFO - prisma db error: 
Error: You are trying to connect to a CockroachDB database, but the provider in your Prisma schema is `postgresql`. 
Please change it to `cockroachdb`.
   0: schema_core::state::ApplyMigrations at schema-engine/core/src/state.rs:226
```

### 🔍 Root Cause Technical Analysis
1. **Prisma ORM Dialect Probe Hard-Binding**:
   * The `schema.prisma` bundled inside LiteLLM official source code hardcodes `datasource db { provider = "postgresql" }`;
   * Although CockroachDB offers good PostgreSQL wire-protocol compatibility, **the underlying Prisma Rust engine probes and identifies the remote database kernel during handshake**. When it detects that the remote target is CockroachDB, Prisma refuses to execute DDL migration table creation with the `postgresql` provider.
2. **Consequence**:
   * 71 Prisma metadata tables could not be automatically generated in CockroachDB, preventing the Control Plane from initializing core tables such as `LiteLLM_UserTable`.

---

## 3. Architecture Decision & Final Outcome: 100% Graceful Rollback to Neon PostgreSQL

After comprehensive architectural trade-off analysis, we decided **not to adopt the high-risk approach of hack-modifying the Prisma Schema bundled inside the LiteLLM image**, but rather to **strictly execute the rollback plan, smoothly reverting end-to-end to standard-compatible native Neon PostgreSQL**:

### ✅ Current Production State
1. **Control Plane**: **100% maintained on Neon PostgreSQL (AWS Singapore)**.
   * **Runtime Capacity**: All 71 Prisma metadata tables are ready; 11 dedicated Virtual Keys are active; current storage usage is only **12 MB / 500 MB (2.4% utilization)**;
   * **Maintenance-Free**: High-frequency API audit logs are 100% offloaded to OCI MySQL; the Control Plane only writes extremely low-frequency login sessions and Virtual Keys, making the 500 MB quota sufficient for long-term worry-free operation.
2. **Data Plane**: **Continues to be hosted independently by OCI MySQL HeatWave (`rin-heatwave`, 50GB)**.
   * Records API request logs, token metering, daily exchange rates, and high-precision CNY settlement in milliseconds; the Data Plane is completely physically isolated from the Control Plane, resulting in **zero loss and zero impact** on historical audit data during this rollback.
3. **Admin Web UI**: Login and administration at `https://gw.jppwl.asia/ui` are completely restored to normal.

---

## 4. Execution & Rollback Checklist

- [x] **Step 1**: Create cluster `brief-titan-32937` (AWS Singapore) in CockroachDB Cloud, obtain the connection string, and complete Python / psycopg2 direct connection liveness checks (measured latency `~12 ms`). [Completed ✅]
- [x] **Step 2**: Execute Prisma core scalar array DDL tests in CockroachDB, confirming native SQL compatibility. [Completed ✅]
- [x] **Step 3**: Register the full CockroachDB asset profile in `users-it-assests/database_services.md` and push updates to the 11 dedicated GitHub repositories. [Completed ✅]
- [x] **Step 4**: Hot-update Secret `litellm-database-url` in OCI Vault (`gateman-vault` / `litellm-prod` compartment) to the CockroachDB connection string. [Completed ✅]
- [x] **Step 5**: Update local development machine `.env` configuration. [Completed ✅]
- [x] **Step 6**: Execute ESO forced immediate sync (`annotate externalsecret force-sync`) and verify successful Secret update. [Completed ✅]
- [x] **Step 7**: Commit `my-argocd-manifests` to trigger ArgoCD automated rolling update; capture and record Prisma ORM log root cause intercepting CockroachDB. [Completed ✅]
- [x] **Step 8 (Execute Rollback)**: Restore `litellm-database-url` in OCI Vault to the Neon PostgreSQL connection string (Version 3). [Completed ✅]
- [x] **Step 9 (GitOps Automated Rollback Sync)**: Update `my-argocd-manifests` to trigger ArgoCD automated rollback deployment; ESO forced refresh takes effect. [Completed ✅]
- [x] **Step 10 (Pod Startup & Schema Verification)**: Monitor Pod logs to confirm `prisma migrate deploy completed`, `Application startup complete`, and all Liveness/Readiness probes green. [Completed ✅]
- [x] **Step 11 (End-to-End API & Virtual Key Validation)**: Initiate real calls using Virtual Keys, verifying normal API responses, millisecond-level logging to OCI MySQL `llm_request_logs` with CNY billing, and 100% full-link recovery! [Completed ✅]

---

## 5. Key Takeaways & Technical Principles

1. **Unpredictability of ORM Dialect Hard Validation**: Even if the underlying database perfectly supports the PG Wire protocol and SQL syntax, upper-layer ORMs (especially compiled schemas like Prisma) will impose a dialect lock if server-side version handshake probing is present.
2. **Disaster Recovery Resilience of Decoupled Architecture**: Thanks to the complete decoupling of Control Plane and Data Plane, even when database switching and rollback occurred on the Control Plane, full API auditing and online traffic forwarding on the Data Plane maintained a 100% SLA, demonstrating superior architectural resilience.
