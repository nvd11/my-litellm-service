# Phase 4 实施计划与架构复盘：LiteLLM 控制面数据库从 Neon PG 迁移 CockroachDB 调研与最终回滚记录

---

## 1. 改动原因与背景 (Why: Motivation & Background)

在 Phase 3 中，我们成功搭建了“双数据库解耦架构”：利用 **Neon Serverless PostgreSQL** 作为控制面（Control Plane）承载 LiteLLM 官方 Admin Web UI 和 Prisma ORM 元数据；利用 **OCI MySQL HeatWave** 作为数据面（Data Plane）记录全量 API 审计流水与日频汇率。

为了进一步探索更大容量的长期免维护方案，我们针对 **CockroachDB Serverless (10GB Always Free)** 展开了控制面迁移的工程实践与探活评估。

---

## 2. 迁移实施过程与踩坑发现 (Implementation & Root Cause Analysis)

在将控制面数据库连接串切换为 CockroachDB 并通过 GitOps 自动触发滚动部署后，LiteLLM Pod 启动时 Prisma ORM 抛出了底层硬校验拦截：

### 🚨 核心拦截日志 (Prisma CLI Hard Check)
```text
2026-08-30 14:22:38,246 - litellm_proxy_extras - INFO - Running prisma migrate deploy
2026-08-30 14:22:53,343 - litellm_proxy_extras - INFO - prisma db error: 
Error: You are trying to connect to a CockroachDB database, but the provider in your Prisma schema is `postgresql`. 
Please change it to `cockroachdb`.
   0: schema_core::state::ApplyMigrations at schema-engine/core/src/state.rs:226
```

### 🔍 根因技术剖析 (Root Cause)
1. **Prisma ORM 方言探针强绑定**：
   * LiteLLM 官方源码内部打包的 `schema.prisma` 硬编码声明为 `datasource db { provider = "postgresql" }`；
   * 虽然 CockroachDB 具备良好的 PostgreSQL 协议兼容性，但 **Prisma Rust 底层引擎在握手时会探针识别远端数据库内核**。当识别到远端为 CockroachDB 时，Prisma 拒绝使用 `postgresql` provider 执行 DDL 迁移建表。
2. **导致后果**：
   * CockroachDB 中无法自动生成 71 张 Prisma 元数据表，导致控制面无法初始化 `LiteLLM_UserTable` 等核心表。

---

## 3. 架构决策与最终结论：100% 优雅回滚至 Neon PostgreSQL (Final Decision & Rollback Outcome)

经综合架构权衡，我们决定**不采用黑客式魔改 LiteLLM 镜像内置 Prisma Schema 的高风险方案**，而是**严格执行回滚预案，全链路平滑回滚至原生标准兼容的 Neon PostgreSQL**：

### ✅ 最终落地状态 (Current Production State)
1. **控制面 (Control Plane)**：**100% 保持在 Neon PostgreSQL (AWS Singapore)**。
   * **运行容量**：71 张 Prisma 元数据表已全部就绪，11 位后宫专属 Virtual Keys 正常生效，当前存储占用仅 **12 MB / 500 MB（利用率仅 2.4%）**；
   * **免维护性**：全量高频 API 审计流水已 100% 分流至 OCI MySQL，控制面只写入极低频的登录 Session 与 Virtual Keys，500 MB 额度足够支撑长期无忧运行。
2. **数据面 (Data Plane)**：**继续由 OCI MySQL HeatWave (`rin-heatwave`, 50GB) 独立承载**。
   * 毫秒级记录 API 请求日志、Token 计量、日频汇率与人民币高精度结算；数据面与控制面完全物理隔离，本次回滚过程中历史审计数据 **0 丢失、0 影响**。
3. **Admin Web UI**：`https://gw.jppwl.asia/ui` 登录与管理完全恢复正常。

---

## 4. 全量实施与回滚闭环清单 (Execution & Rollback Checklist)

- [x] **第 1 步**：在 CockroachDB Cloud 创建集群 `brief-titan-32937` (AWS Singapore)，获取连接串并完成 Python / psycopg2 直连探活（实测延迟 `~12 ms`）。[已完成 ✅]
- [x] **第 2 步**：在 CockroachDB 中执行 Prisma 核心标量数组 DDL 测试，确认原生 SQL 兼容。[已完成 ✅]
- [x] **第 3 步**：在 `users-it-assests/database_services.md` 登记 CockroachDB 完整资产档案，并全量同步推送到 11 位后宫 GitHub 仓库。[已完成 ✅]
- [x] **第 4 步**：在 OCI Vault (`gateman-vault` / `litellm-prod` compartment) 中热更 Secret `litellm-database-url` 为 CockroachDB 连接串。[已完成 ✅]
- [x] **第 5 步**：更新本地开发机 `.env` 配置。[已完成 ✅]
- [x] **第 6 步**：执行结衣妹妹建议的 ESO 强制立即同步（`annotate externalsecret force-sync`）并校验 Secret 更新成功。[已完成 ✅]
- [x] **第 7 步**：提交 `my-argocd-manifests` 触发 ArgoCD 自动滚动更新，捕获并记录 Prisma ORM 拦截 CockroachDB 的日志根因。[已完成 ✅]
- [x] **第 8 步（执行回滚）**：在 OCI Vault 中将 `litellm-database-url` 恢复为 Neon PostgreSQL 连接串（版本 3）。[已完成 ✅]
- [x] **第 9 步（GitOps 自动回滚同步）**：更新 `my-argocd-manifests` 触发 ArgoCD 自动回滚部署，ESO 强制刷新生效。[已完成 ✅]
- [x] **第 10 步（Pod 启动与建表验证）**：观测 Pod 日志确认 `prisma migrate deploy completed`、`Application startup complete`，Liveness/Readiness 探针全绿。[已完成 ✅]
- [x] **第 11 步（端到端 API 与 Virtual Key 实测）**：使用 Virtual Key 发起真实调用，验证 API 正常返回，OCI MySQL `llm_request_logs` 毫秒级记录流水与人民币计费，全链路 100% 恢复！[已完成 ✅]

---

## 5. 经验沉淀与技术原则 (Key Takeaways)

1. **ORM 方言强校验的不可预见性**：即使底层数据库完美支持 PG Wire 协议与 SQL 语法，上层 ORM（特别是编译型 Schema 如 Prisma）若存在服务端版本握手探针，仍会产生方言锁。
2. **解耦架构的容灾韧性**：得益于控制面与数据面的彻底解耦，即便控制面发生数据库切换与回滚，数据面的全量 API 审计与线上流量转发依然保持 100% SLA，体现了极高的架构容灾设计水平。
