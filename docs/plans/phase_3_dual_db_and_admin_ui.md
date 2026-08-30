# Phase 3 实施计划：双数据库解耦架构（PostgreSQL 控制面 + MySQL 数据面）与 LiteLLM Admin UI 全量启用

---

## 1. 改动原因与业务痛点 (Why: Motivation & Root Causes)

在完成 Phase 2（OCI MySQL HeatWave 异步审计落库、日频汇率与多级降级轨迹）上线后，我们在生产环境的实际运维与管理中面临以下三个核心痛点：

### 痛点一：LiteLLM 官方 Admin Web UI 强绑定 PostgreSQL（Prisma 类型死锁）
* **现象**：当尝试访问并登录 LiteLLM 官方 Web 控制台（`/ui`）时，后端接口返回 `400 Bad Request: {"error": "Authentication Error, Not connected to DB!"}`。
* **根因**：LiteLLM 官方的 Next.js 控制台底层使用 **Prisma ORM (`prisma-client-py`)** 来管理用户账户、JWT 登录会话与虚拟密钥（Virtual Keys）。在官方内置的 `schema.prisma` 中，`models` 和 `permissions` 等字段被定义为 **PostgreSQL 原生标量数组类型 (`String[]`)**。而 MySQL 原生不支持数组类型，导致 Prisma 无法将 `provider` 改为 `mysql`。因此，LiteLLM 官方 Web UI 强硬绑定了 PostgreSQL，无法直接使用我们现有的 MySQL。

### 痛点二：单一 Master Key 导致分账缺失与权限无隔离
* **现象**：当前所有客户端（包括测试脚本、Hebe 妹妹、Codex 等）均直接携带全局 `LITELLM_MASTER_KEY` 发起请求，导致 OCI MySQL `llm_request_logs` 中的 `api_key_alias` 字段统一记录为 `default`。
* **业务隐患**：
  1. 无法实现后宫各个妹妹（Hebe、Yui、Rin、Celia 等）以及各业务团队的**细粒度 Token 消耗与人民币财务分账统计**；
  2. 无法为不同调用方配置**独立模型白名单**（如限制某实例只能调用便宜的 Flash 模型）和**预算硬限制**（如超额自动熔断）。

### 痛点三：缺乏可视化控制台，运维交互成本高
* **需求**：需要一个直观的 Web 控制台，支持免手写 curl 脚本即可在线生成/撤销子 Key、配置 RPM/TPM 频控限额、可视化查看模型调用分布与实时健康状态。

---

## 2. 核心目标与交付价值 (Goals & Expected Value)

针对上述痛点，Phase 3 确定实施“控制面与数据面完全解耦”的双数据库架构，具体目标如下：

1. **架构彻底解耦 (Dual-DB Architecture)**：
   * **控制面 (Control Plane)**：接入云原生 Serverless **Neon PostgreSQL (Singapore Region)**，专门承载 LiteLLM 官方 Web UI 登录、JWT 会话、Virtual Keys 与团队配额存储；
   * **数据面 (Data Plane)**：保留 **OCI MySQL HeatWave (`rin-heatwave`)**，专门承载全量高并发 API 审计流水、Token 计量、日频实时汇率折算与高精度人民币财务结算；
   * **缓存与性能层 (Cache Layer)**：复用 **K3s Redis**（`redis.redis.svc.cluster.local`），负责响应缓存与日频汇率 L2 缓存。
2. **解锁官方 Admin Dashboard 完整功能**：
   * 浏览器访问 `http://gw.jpgcp.cloud:31850/ui`，使用 `admin` + `LITELLM_MASTER_KEY` 即可秒级登录；
   * 支持通过网页 UI 可视化管理虚拟子 Key、团队、用户、配额与系统设置。
3. **实现多妹妹 / 多团队专属 Key 与精准分账联动**：
   * 在 Web UI 上为后宫各个妹妹生成专属子 Key（例如 `key_alias: "hebe-arm"`, `key_alias: "yui-radxa"`）；
   * 妹妹带着专属子 Key 调用网关，请求通过后，OCI MySQL `llm_request_logs` 的 `api_key_alias` 字段自动精准落库，实现一键 `GROUP BY api_key_alias` 财务分账报表！
4. **零本地磁盘开销与 10ms 超低延迟**：
   * 采用 **Neon (AWS Singapore)** 免费 Serverless 档（0.5 GB 存储 + 100 算力小时，闲置自动休眠），**0 占用 OCI ARM VM 硬盘空间**；
   * OCI 新加坡 VM 到 Neon 新加坡同城直连，**实测网络延迟仅 ~10.95 ms**，极致丝滑。

---

## 3. 架构拓扑与双数据库职责划分 (Architecture Topology)

```
                                 ┌──────────────────────────────────────────────┐
                                 │               浏览器 Admin Web UI            │
                                 │          (http://gw.jpgcp.cloud:31850/ui)    │
                                 └──────────────────────┬───────────────────────┘
                                                        │
                                                        ▼
                        ┌──────────────────────────────────────────────────────────────┐
                        │             Kong API Gateway (Cluster B / Ingress)           │
                        │   • /litellm (strip-path: true) -> API 路由调度               │
                        │   • /ui, /litellm-asset-prefix, /v2/login -> Web 控制台路由   │
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
                                        │ DATABASE_URL (Direct Mode)   │ MYSQL_*
                                        ▼                              ▼
                 ┌─────────────────────────────┐              ┌─────────────────────────────────┐
                 │  Neon PostgreSQL (Singapore)│              │     OCI MySQL HeatWave          │
                 │   (Serverless Always Free)  │              │      (rin-heatwave 实例)        │
                 ├─────────────────────────────┤              ├─────────────────────────────────┤
                 │ • Admin Web UI 登录会话     │              │ • 毫秒级异步写入全量 API 审计    │
                 │ • LiteLLM_UserTable 用户表  │              │ • 日频实时汇率折算 (USD->CNY)   │
                 │ • Virtual Keys 虚拟子密钥   │              │ • 路由梯队降级轨迹追溯          │
                 │ • 团队配额与预算软硬限制    │              │ • 金融级财务高精度结算 (cost_cny)│
                 └─────────────────────────────┘              └─────────────────────────────────┘
```

---

## 4. 关键技术避坑与架构防线 (Architecture Guardrails & Pitfall Prevention)

在实施前必须锁定的 4 大技术要点：

1. **Prisma 引擎与 Non-Root (65532:65532) 权限防线**：
   * 容器使用 `USER 65532:65532` 非 root 运行。若 Prisma 在运行时尝试向 `/root/.cache/prisma` 写入引擎，会发生 `Permission Denied`；
   * **解决方案**：在 `Dockerfile` 中安装 `openssl` 与 `ca-certificates`，并配置环境变量 `PRISMA_CACHE_DIR="/tmp/prisma-cache"`，确保非 root 用户拥有完全可写权限。
2. **Neon 连接串必须采用 Direct（直连模式）**：
   * Neon 默认的 `-pooler` (PgBouncer) 事务池不支持 DDL 与预编译语句；LiteLLM 首次连接空白库时需执行 Prisma DDL 建表；
   * **解决方案**：连接串必须去除 `-pooler` 后缀，采用 **Direct 直连 5432 端口**（`ep-bitter-sky-azfg5i09.c-3.ap-southeast-1.aws.neon.tech`），并携带 `?sslmode=require`。
3. **CI/CD ARM64 + AMD64 多架构发布闭环**：
   * 生产 K3s 节点运行在 OCI ARM64 (`free-arm-vm`)；Dockerfile 修改后，必须通过 GitHub Actions / Docker Buildx 构建推送多架构镜像，并提取最新的 `sha256` 摘要同步至 ArgoCD。
4. **首次建表冷启动与探针容忍度**：
   * 首次启动自动执行 DDL 迁移创建 10+ 张 Prisma 数据表约需 15~20 秒；`litellm-svc-app.yaml` 的 `initialDelaySeconds: 45` 满足需求，首次启动观察日志时需预留建表窗口。

---

## 5. 密钥管理与 OCI Vault 规范 (Secret Management)

在 **OCI Vault (`gateman-vault` / `litellm-prod` compartment)** 中新增托管 Direct 模式的 PostgreSQL 连接串：

```
                    ┌────────────────────────────────────────┐
                    │       OCI Vault (gateman-vault)        │
                    │  - Secret: litellm-database-url (PG)   │  👈 [Phase 3 Direct 模式]
                    │  - Secret: litellm-mysql-password      │
                    │  - Secret: litellm-master-key          │
                    │  - Secret: litellm-redis-password      │
                    │  - Secret: litellm-openai-api-key-*    │
                    └───────────────────┬────────────────────┘
                                        │
                 ┌──────────────────────┴──────────────────────┐
                 │ (通过 External Secrets Operator 自动同步)    │
                 ▼                                             ▼
    ┌───────────────────────────┐                 ┌───────────────────────────┐
    │  K3s 集群 (生产环境)        │                 │  Local 开发机 (本地联调)     │
    │  Secret: litellm-secrets  │                 │  .env (GitIgnored 本地缓存)  │
    │  - DATABASE_URL           │                 │  - DATABASE_URL           │
    └───────────────────────────┘                 └───────────────────────────┘
```

---

## 6. 全量实施与验收步骤清单（共 12 步）

### 🔹 阶段一：PostgreSQL 接入与 OCI Vault 密钥托管

- [x] **第 1 步**：在 **Neon (Singapore Region)** 开通实例，获取 Direct 直连串，本地与 ARM VM 双向实测（延迟 `10.95ms`）通过。[已完成 ✅]
- [x] **第 2 步**：在 OCI Vault (`gateman-vault` / `litellm-prod` compartment) 中创建 Secret：`litellm-database-url`，存储 Direct 模式 PG 连接串（带 `?sslmode=require`）。[已完成 ✅]
- [x] **第 3 步**：更新本地 `.env.example` 与 `.env`，补充 `DATABASE_URL` 配置项。[已完成 ✅]

---

### 🔹 阶段二：镜像依赖完备性校验与多架构 CI/CD 发布

- [x] **第 4 步**：优化 `Dockerfile`：
  - 确保安装 `openssl ca-certificates`；
  - 注入 `PRISMA_CACHE_DIR="/tmp/prisma-cache"` 解决非 root `65532` 用户写权限问题。
- [x] **第 5 步**：本地运行全量 pytest (25 passed) 与 ruff check 确保代码无回归。[已完成 ✅]
- [x] **第 6 步**：提交并推送代码至 GitHub，触发 GitHub Actions 构建 `linux/amd64,linux/arm64` 双架构镜像，并获取最新镜像 Digest。[已完成 ✅]

---

### 🔹 阶段三：GitOps / ArgoCD 图纸编排与 K3s 同步

- [x] **第 7 步**：更新 `my-argocd-manifests/argocd-apps/litellm-svc-app.yaml`：
  - 更新 `image.digest` 为第 6 步生成的最强多架构镜像摘要；
  - 在 `externalSecret.data` 中增加 `DATABASE_URL` 映射（指向 Vault 的 `litellm-database-url`）；
  - 确认 Kong Gateway API `extraRoutes.ui-route` 涵盖全部登录、认证与静态资源路径。[已完成 ✅]
- [ ] **第 8 步**：提交并推送 `my-argocd-manifests`，触发 ArgoCD 自动同步与 Pod 滚动更新。
- [ ] **第 9 步**：监控 Pod 日志，确认首次启动自动完成 Prisma DDL 建表，双 DB（Neon PG 连接就绪 + OCI MySQL 探活正常）输出。

---

### 🔹 阶段四：Web UI 登录、虚拟 Key 发行与双库联动闭环实测

- [ ] **第 10 步（Web 登录验收）**：浏览器访问 `http://gw.jpgcp.cloud:31850/ui`，使用 `admin` 与密码（`LITELLM_MASTER_KEY`）登录，验证成功进入 Dashboard 控制台。
- [ ] **第 11 步（虚拟 Key 发行）**：在 Web UI “API Keys” 面板创建测试 Key（别名 `hebe-ui-test`，限制模型 `gemini-3.7-flash`，预算 10 USD），获取生成的 `sk-...` 密钥。
- [ ] **第 12 步（双库联动闭环验证）**：
  - 使用新生成的 `sk-...` 向网关发起请求；
  - 直连 OCI MySQL `rin-heatwave` 查库，确认 `api_key_alias` 精确记录为 `hebe-ui-test`，费用与实时汇率精准换算，全链路 100% 验收通过！

---

## 7. 交付产物与安全红线 (Deliverables & Guardrails)

1. **零硬编码红线**：PostgreSQL 连接串必须统一由 OCI Vault 托管，严禁在 YAML、Git 仓库或日志中暴露明文密码。
2. **高可用容灾保障**：PostgreSQL 仅影响 Web 控制台与虚拟 Key 规则更新；即使外部 PG 出现短暂冷启动延迟，LiteLLM 的核心 API 转发与 OCI MySQL 异步审计落库依然具备 100% SLA，绝不阻断线上对话。
