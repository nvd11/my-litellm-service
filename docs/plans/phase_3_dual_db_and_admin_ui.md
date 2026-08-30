# Phase 3 实施计划：双数据库解耦架构（PostgreSQL 控制面 + MySQL 数据面）与 LiteLLM Admin UI 全量启用

> **目标**：实现 LiteLLM 网关的“控制面 (Control Plane)”与“数据面 (Data Plane)”彻底解耦的双数据库企业级架构；引入云原生 Serverless **PostgreSQL (Neon 新加坡 / K3s)** 作为控制面承载 LiteLLM 官方 Web UI、JWT 登录会话与 Virtual Keys（虚拟密钥）的存储；保留 **OCI MySQL HeatWave (`rin-heatwave`)** 作为数据面，持续承载高并发 API 审计流水、Token 消耗、日频实时汇率与人民币高精度结算；通过 **OCI Vault + ESO + ArgoCD** 完成全自动化 GitOps 部署与双库端到端联动验收。

---

## 1. 架构设计与双数据库职责划分 (Dual-DB Architecture & Separation of Concerns)

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
                                        │ DATABASE_URL                 │ MYSQL_*
                                        ▼                              ▼
                 ┌─────────────────────────────┐              ┌─────────────────────────────────┐
                 │    PostgreSQL (Neon / K3s)  │              │     OCI MySQL HeatWave          │
                 │     (Singapore Region)      │              │      (rin-heatwave 实例)        │
                 ├─────────────────────────────┤              ├─────────────────────────────────┤
                 │ • Admin Web UI 登录会话     │              │ • 毫秒级异步写入全量 API 审计    │
                 │ • LiteLLM_UserTable 用户表  │              │ • 日频实时汇率折算 (USD->CNY)   │
                 │ • Virtual Keys 虚拟子密钥   │              │ • 路由梯队降级轨迹追溯          │
                 │ • 团队配额与预算软硬限制    │              │ • 金融级财务高精度结算 (cost_cny)│
                 └─────────────────────────────┘              └─────────────────────────────────┘
```

### 1.1 核心解耦优势
1. **规避 Prisma 类型限制**：LiteLLM 官方 Web UI 强依赖 PostgreSQL 的标量数组特性（`String[]`），通过引入独立 PG 完美满足官方 Prisma ORM 的规范。
2. **保护金融级资产**：所有核心审计日志与汇率换算数据依然牢牢沉淀在主人的 OCI MySQL 资产库中，控制台的重置、修改不会对核心财务数据造成任何污染。
3. **双库协同联动**：在 Web UI 页面生成的虚拟子 Key（如 `hebe-arm`、`yui-radxa`），在被调用时会自动穿透至 MySQL 的 `api_key_alias` 字段，实现一键账单分账。

---

## 2. 密钥管理与 OCI Vault 规范 (Secret Management)

在 **OCI Vault (`gateman-vault`)** 中新增托管 PostgreSQL 连接串，与 MySQL、Redis 及 API 密钥集中管理：

```
                    ┌────────────────────────────────────────┐
                    │       OCI Vault (gateman-vault)        │
                    │  - Secret: litellm-database-url (PG)   │  👈 [Phase 3 新增]
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

## 3. 全量实施与验收步骤清单（共 11 步）

### 🔹 阶段一：PostgreSQL 开通与 Vault 密钥登记

- [ ] **第 1 步**：在 **Neon (Singapore Region)** 创建专属 PostgreSQL 实例（或在 K3s 编排部署轻量 PG），获取标准连接串：
  `postgresql://<user>:<password>@<host>/<dbname>?sslmode=require`
- [ ] **第 2 步**：在 OCI Vault (`gateman-vault` / `litellm-prod` compartment) 中创建 Secret：`litellm-database-url`，存储上述 PG 连接串。
- [ ] **第 3 步**：更新本地 `.env.example` 与 `.env`，补充 `DATABASE_URL` 配置项。

---

### 🔹 阶段二：镜像依赖完备性校验与本地构建

- [ ] **第 4 步**：检查并确保 `Dockerfile` 在构建时具备 Prisma 运行时环境及二进制支持（`prisma-client-py` 与 Node/OpenSSL 基础库）。
- [ ] **第 5 步**：在本地/CI 启动容器预检，确认 LiteLLM 启动时输出 `Prisma Client connected` 且无报错。

---

### 🔹 阶段三：GitOps / ArgoCD 图纸编排与 K3s 同步

- [ ] **第 6 步**：更新 `my-argocd-manifests/argocd-apps/litellm-svc-app.yaml`：
  - 在 `externalSecret.data` 中增加 `DATABASE_URL` 映射（指向 Vault 的 `litellm-database-url`）；
  - 验证 Kong Gateway API `extraRoutes.ui-route` 涵盖全部登录、认证与静态资源路径（`/ui`, `/litellm-asset-prefix`, `/v2`, `/login`, `/auth`, `/models` 等）。
- [ ] **第 7 步**：提交并推送 `my-argocd-manifests` 到 GitHub，触发 ArgoCD 自动同步与 Pod 滚动更新。
- [ ] **第 8 步**：检查 K3s `llm-system` 命名空间下 `litellm-svc` Pod 运行状态及日志，确认双 DB（PostgreSQL 连接就绪 + MySQL 连接池探活正常）。

---

### 🔹 阶段四：Web UI 登录、虚拟 Key 发行与双库联动验证

- [ ] **第 9 步（Web 登录验收）**：在浏览器中打开 `http://gw.jpgcp.cloud:31850/ui`，使用用户名 `admin` 与密码（`LITELLM_MASTER_KEY`）登录，验证成功进入 Dashboard 控制台。
- [ ] **第 10 步（虚拟 Key 发行）**：在 Web UI 的 “API Keys” 面板中，创建测试 Key（别名 `hebe-ui-test`，限制模型 `gemini-3.7-flash`，预算 10 USD），获取生成的 `sk-...` 密钥。
- [ ] **第 11 步（双库联动闭环验证）**：
  - 使用新生成的 `sk-...` 向网关 `http://gw.jpgcp.cloud:31850/litellm/v1/chat/completions` 发起请求；
  - 直连 OCI MySQL `rin-heatwave` 执行 SQL 查询：
    ```sql
    SELECT request_id, api_key_alias, model_used, prompt_tokens, cost_usd, cost_cny, fx_rate, status_code 
    FROM llm_request_logs 
    ORDER BY created_at DESC LIMIT 1;
    ```
  - 确认 `api_key_alias` 精确记录为 `hebe-ui-test`，费用与实时汇率精准换算，全链路验收通过！

---

## 4. 交付产物与安全红线 (Deliverables & Guardrails)

1. **零硬编码红线**：PostgreSQL 连接串必须通过 OCI Vault 托管，严禁在 YAML、Git 代码库或日志中明文暴露。
2. **高可用容灾保障**：PostgreSQL 仅影响 Web 控制台与虚拟 Key 规则更新；即使 PG 出现短暂冷启动延迟，LiteLLM 的 API 调用主流程与 OCI MySQL 异步审计落库依然具备 100% SLA，绝不阻断线上对话。
