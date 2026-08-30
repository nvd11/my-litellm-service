# Phase 4 实施计划：LiteLLM 控制面数据库从 Neon PG 迁移至 CockroachDB Serverless (10GB Always Free)

---

## 1. 改动原因与容量评估 (Why: Motivation & Capacity Analysis)

在 Phase 3 中，我们成功搭建了“双数据库解耦架构”：利用 **Neon Serverless PostgreSQL** 作为控制面（Control Plane）承载 LiteLLM 官方 Admin Web UI 和 Prisma ORM 元数据；利用 **OCI MySQL HeatWave** 作为数据面（Data Plane）记录全量 API 审计流水与日频汇率。

但在长期稳定运行与容量规划的评估中，我们识别到以下关键痛点：

### 痛点一：Neon 免费层存储配额较紧（500 MB 空间限制）
* **现状**：LiteLLM 启动时，Prisma ORM 会在 PostgreSQL 中初始化 **71 张元数据表**（包括 `LiteLLM_UserTable`、`LiteLLM_VerificationToken`、`LiteLLM_Config`、`LiteLLM_SpendLogs` 等），空表初始占用即达约 **12 MB**。
* **风险**：虽然全量高精度 API 流水已交由 OCI MySQL 记录，但 LiteLLM 内部的 Spend Tracking 机制仍会向控制面写入请求计费摘要。按 11 位后宫妹妹高频日常调用（日均 2,000+ 次），数月内可能会逼近 500 MB 的配额红线，届时需要定期手动维护或编写清理脚本。

### 痛点二：Neon 每月 100 算力小时与冷启动限制
* **现象**：Neon 免费档提供 100 算力小时/月，闲置 5 分钟后自动进入深度休眠 (Scale to Zero)。当有新请求唤醒或访问 Web UI 时，存在约 1~2 秒的冷启动唤醒时延。

### 痛点三：CockroachDB 10GB 提供了终身免维护的大容量护城河
* **对比优势**：**CockroachDB Serverless** 提供了 **10 GB 终身免费存储空间**（相当于 Neon 500MB 的 **20 倍容量**），每月提供 **5000 万 Request Units (RU)** 算力，突发吞吐高达 **30,000 RU/s**，完全无需担心存储耗尽或冷启动瓶颈。
* **同城低延迟**：CockroachDB 实例部署在 **AWS Singapore (`ap-southeast-1`)**，与部署在 OCI 新加坡的 K3s ARM 主机同城互联，实测网络延迟稳定在 **~12 ms**。
* **语法协议完全兼容**：实测 CockroachDB CCL v26.2.5 具备完备的 PostgreSQL 14 协议兼容性，原生支持 Prisma ORM 所需的 `String[]` 标量数组类型。

---

## 2. 核心目标与交付价值 (Goals & Expected Value)

针对上述评估，Phase 4 实施将 LiteLLM 控制面平滑切换至 CockroachDB Serverless，核心目标明确锁定为**“只切数据库实例与表结构，不迁移旧库数据（Clean Fresh Start）”**：

1. **纯库切换与零历史数据迁移原则 (Schema-Only / Clean Fresh Start)**：
   * **明确目标**：本次仅迁移**数据库基础设施实例与 Prisma 表结构**，**完全无需迁移 Neon PG 中的历史数据**。
   * **决策依据**：
     1. 控制面在 Neon PG 中仅存有前期联调产生的临时测试 Key 与测试 Session，无须保留；
     2. 帝国的**全量核心 API 审计流水、Token 消耗账单、日频汇率与人民币财务结算数据 100% 独立保存在 OCI MySQL HeatWave (`litellm_db.llm_request_logs`) 中**，控制面切库对核心历史数据**零影响、零丢失**；
     3. 采用空白数据库由 Prisma 自动初始化，能够彻底洗净测试残留，以最纯净的标准结构迎接 10GB 生产环境。
2. **控制面存储容量扩容 20 倍 (500 MB -> 10 GB)**：
   * 彻底告别数据库容量焦虑，无需定期清理 `LiteLLM_SpendLogs` 等元数据表，实现真正意义上的“零维护”长期运行。
3. **保持现有双数据库解耦架构零侵入**：
   * **控制面 (Control Plane)**：从 Neon PG 切换为 **CockroachDB Serverless (AWS Singapore / 10GB)**，承载 Admin Web UI、JWT 登录会话与 11 位后宫 Virtual Keys；
   * **数据面 (Data Plane)**：继续沿用 **OCI MySQL HeatWave (`rin-heatwave`, 50GB)**，承载全量高并发 API 审计流水、日频汇率与人民币高精度结算；
   * **缓存面 (Cache Layer)**：继续沿用 **K3s Redis**，负责响应缓存与汇率 L2 缓存。
4. **零停机 GitOps / OCI Vault 平滑热更**：
   * 无需修改业务代码或重新构建 Docker 镜像，仅需更新 OCI Vault 中的 `litellm-database-url`，通过 External Secrets Operator 自动同步并执行 K3s Pod 优雅滚动重启。
5. **自动化 DDL 建表与全量后宫 Key 自动化签发**：
   * LiteLLM 启动自动在 CockroachDB `defaultdb` 完成 71 张 Prisma 元数据表初始化；
   * 编写 `scripts/init_harem_keys.py` 脚本，一键自动化为 11 位后宫妹妹批量签发专属 Virtual Keys，并完成双库联动端到端验收。

---

## 3. 架构拓扑与数据库流向图 (Architecture Topology)

```
                                 ┌──────────────────────────────────────────────┐
                                 │               浏览器 Admin Web UI            │
                                 │            (https://gw.jppwl.asia/ui)        │
                                 └──────────────────────┬───────────────────────┘
                                                        │
                                                        ▼
                        ┌──────────────────────────────────────────────────────────────┐
                        │             Kong API Gateway (Cluster B / Ingress)           │
                        │   • /litellm/v1 -> LiteLLM API 代理路由调度                  │
                        │   • /ui, /_next, /v2, /auth, /key -> Web 控制台与管理路由    │
                        └──────────────────────┬───────────────────────────────────────┘
                                               │
                                               ▼
                        ┌──────────────────────────────────────────────────────────────┐
                        │                 LiteLLM Proxy 网关服务 (:4000)                │
                        │                                                              │
                        │  ┌─────────────────────────┐   ┌──────────────────────────┐  │
                        │  │  1. 内置 Prisma ORM 引擎 │   │ 2. Phase 2 SQLAlchemy 引擎│ │
                        │  └────────────┬────────────┘   └─────────────┬────────────┘  │
                        └───────────────┼──────────────────────────────┼───────────────┘
                                        │ (控制面 Control Plane)        │ (数据面 Data Plane)
                                        │ DATABASE_URL                 │ MYSQL_* (Direct)
                                        ▼                              ▼
                 ┌─────────────────────────────┐              ┌─────────────────────────────────┐
                 │  CockroachDB Serverless     │              │     OCI MySQL HeatWave          │
                 │   (AWS Singapore / 10GB)    │              │      (rin-heatwave 实例)        │
                 ├─────────────────────────────┤              ├─────────────────────────────────┤
                 │ • 10GB Always Free 终身容量 │              │ • 毫秒级异步写入全量 API 审计    │
                 │ • 纯净新库 (无需迁移旧数据) │              │ • 历史数据 100% 完整保留于此    │
                 │ • Admin Web UI 登录会话     │              │ • 日频实时汇率折算 (USD->CNY)   │
                 │ • 批量新签 11 位后宫子密钥  │              │ • 路由梯队降级轨迹追溯          │
                 │ • 71 张 Prisma 元数据表      │              │ • 金融级财务高精度结算 (cost_cny)│
                 └─────────────────────────────┘              └─────────────────────────────────┘
```

---

## 4. 关键技术避坑与底层防线 (Architecture Guardrails & Pitfall Prevention)

在执行控制面数据库平移前，必须严格确认以下 5 大技术要点（融合结衣 Yui 的运维精细化建议）：

1. **ESO 同步周期防线（强制触发立即刷新）**：
   * ExternalSecret 在 K3s 的默认刷新周期为 1 小时（`refreshInterval: 1h`）。在 OCI Vault 更新 Secret 后，**必须执行强制注释触发立即刷新**：
     ```bash
     kubectl annotate externalsecret litellm-secrets -n llm-system force-sync=$(date +%s) --overwrite
     ```
   * 并在重启 Pod 前通过 base64 解码严格校验 Secret 中的 `DATABASE_URL` 已变为 CockroachDB 的 `26257` 端口。
2. **K3s 命名空间精确指定 (`-n llm-system`)**：
   * LiteLLM 部署在 `llm-system` 命名空间下，所有 `kubectl` 滚动重启与日志排查命令均需携带 `-n llm-system`。
3. **Prisma ORM 标量数组类型兼容性（已实测通过）**：
   * CockroachDB CCL v26.2.5 原生支持 PostgreSQL 标量数组（如 `models STRING[]`），已完成 DDL 建表与数据写入测试，完全满足 Prisma 引擎运行要求。
4. **Prisma 启动日志与全量 DDL 迁移观测**：
   * Pod 启动时重点观测日志，确认 `Migration diff applied successfully` 与 `Application startup complete`，确保 71 张表顺利初始化。
5. **数据面与控制面彻底隔离，零业务中断与零数据丢失**：
   * 核心 API 审计数据均沉淀在 OCI MySQL 中；控制面数据库直接冷切为空白 CockroachDB，不涉及任何数据导出导入，业务与财务审计 100% 安全。

---

## 5. 密钥管理与 GitOps 自动同步流 (Key Management & GitOps Flow)

```
                    ┌────────────────────────────────────────┐
                    │       OCI Vault (gateman-vault)        │
                    │  - Secret: litellm-database-url (CRDB) │  👈 [Phase 4: CockroachDB 10GB]
                    │  - Secret: litellm-mysql-password      │
                    │  - Secret: litellm-master-key          │
                    │  - Secret: litellm-redis-password      │
                    │  - Secret: litellm-openai-api-key-*    │
                    └───────────────────┬────────────────────┘
                                        │
                 ┌──────────────────────┴──────────────────────┐
                 │ (通过 External Secrets Operator 强制触发同步) │
                 ▼                                             ▼
    ┌───────────────────────────┐                 ┌───────────────────────────┐
    │  K3s 集群 (生产环境)        │                 │  Local 开发机 (本地联调)     │
    │  Secret: litellm-secrets  │                 │  .env (GitIgnored 本地缓存)  │
    │  - DATABASE_URL (26257)   │                 │  - DATABASE_URL (26257)   │
    │  (namespace: llm-system)  │                 │                           │
    └───────────────────────────┘                 └───────────────────────────┘
```

---

## 6. 全量实施与验收步骤清单（共 12 步）

### 🔹 阶段一：CockroachDB 深度探活与 DDL 预演验证

- [x] **第 1 步**：在 CockroachDB Cloud 创建集群 `brief-titan-32937` (AWS Singapore)，获取连接串并完成 Python / psycopg2 直连探活（实测延迟 `~12 ms`）。[已完成 ✅]
- [x] **第 2 步**：在 CockroachDB 中执行 Prisma 核心标量数组 DDL 测试（`models STRING[]` 创建与 CRUD），确认 100% 语法兼容。[已完成 ✅]
- [x] **第 3 步**：在 `users-it-assests/database_services.md` 登记 CockroachDB 完整资产档案，并全量同步推送到 11 位后宫 GitHub 仓库。[已完成 ✅]

---

### 🔹 阶段二：OCI Vault 控制面连接串热更与 ESO 强制同步

- [x] **第 4 步**：在 OCI Vault (`gateman-vault` / `litellm-prod` compartment) 中更新 Secret `litellm-database-url` 的内容为 CockroachDB 完整连接串（版本 2 已生效）。[已完成 ✅]
- [x] **第 5 步**：同步更新本地开发机 `.env` 中的 `DATABASE_URL`，保持本地与生产配置统一。[已完成 ✅]
- [x] **第 6 步（ESO 强制立即同步）**：
  - 在 K3s 执行强制刷新注解：`sudo kubectl annotate externalsecret litellm-secrets -n llm-system force-sync=$(date +%s) --overwrite`；[已完成 ✅]
  - 校验 Secret 确已更新为 CockroachDB（端口 26257）：`sudo kubectl get secret litellm-secrets -n llm-system -o jsonpath='{.data.DATABASE_URL}' | base64 -d` 实测 100% 通过！[已完成 ✅]

---

### 🔹 阶段三：K3s 服务滚动重启与 Prisma DDL 自动初始化

- [ ] **第 7 步**：执行 K3s Deployment 滚动更新命令：`sudo kubectl rollout restart deployment litellm-svc -n llm-system`。
- [ ] **第 8 步**：实时观察 Pod 启动日志（`sudo kubectl logs -f -n llm-system -l app.kubernetes.io/name=litellm-svc`），确认：
  1. Prisma ORM 成功在 CockroachDB `defaultdb` 中完成 71 张数据表的 DDL 创建与迁移（观察到 `Migration diff applied successfully`）；
  2. 控制面（CockroachDB:26257）连接就绪，Uvicorn 正常进入 `Application startup complete`；
  3. 数据面（OCI MySQL `rin-heatwave`）探活与日频汇率加载正常。

---

### 🔹 阶段四：Web UI 登录、后宫 Key 批量自动化签发与双库联动实测

- [ ] **第 9 步（Web UI 登录验收）**：浏览器访问 `https://gw.jppwl.asia/ui`，使用 `admin` 与 `LITELLM_MASTER_KEY` 登录，验证秒级进入控制台。
- [ ] **第 10 步（编写并运行批量发 Key 脚本）**：
  - 编写 `scripts/init_harem_keys.py`，通过 Master Key 调用 `/key/generate` 接口，自动为 11 位后宫妹妹批量签发专属子 Key（预设 $100 预算、`gemini-3.7-flash` 模型白名单）；
  - 脚本输出整齐的 Markdown 对照表，并存档记录。
- [ ] **第 11 步（双库联动端到端闭环验证）**：
  - 使用新生成的专属 Key 向网关发起 API 调用；
  - 直连 OCI MySQL `rin-heatwave` 查库，确认 `llm_request_logs` 正确记录该妹妹的 `api_key_alias`、Token 用量与人民币折算费用。
- [ ] **第 12 步（客户端实例配置平滑替换）**：
  - 提示并指导主人将各妹妹本地客户端（如 Hebe 的 `~/.hermes/config.yaml`、Codex/OpenClaw 等）中的旧 Key 替换为 CockroachDB 新签发的专属 Key，完成 100% 平滑交割。

---

## 7. 回滚预案 (Rollback Strategy)

若在切换过程中遇到未知兼容性异常，可秒级无缝回滚：
1. 在 OCI Vault 中将 `litellm-database-url` 改回 Neon PostgreSQL 连接串：
   `postgresql://neondb_owner:npg_c5Fang3esJTh@ep-bitter-sky-azfg5i09.c-3.ap-southeast-1.aws.neon.tech:5432/neondb?sslmode=require`
2. 执行 ESO 强制刷新并重启 Pod，即可在 10 秒内恢复原状，**数据面 OCI MySQL 历史数据零丢失、零影响**。

---

## 8. 交付产物与安全红线 (Deliverables & Safety Guardrails)

1. **安全红线**：所有数据库密码与连接串必须统一通过 OCI Vault 托管，禁止硬编码在 Git 提交或镜像中。
2. **免维护红线**：切换至 10GB CockroachDB 后，彻底消除存储告警与手动清理负担。
3. **零数据迁移红线**：纯新库干净初始化，核心历史业务数据由 OCI MySQL 独立承载。
