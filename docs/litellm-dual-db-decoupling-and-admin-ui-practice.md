# 实战：彻底解决 LiteLLM 与 MySQL 兼容死锁——PostgreSQL 控制面 + MySQL 数据面双库解耦架构落地

## 1. 业务痛点与技术冲突（Why & Root Causes）

在多模型 AI 网关（LiteLLM Proxy）的企业级落地实践中，我们原本已构建了一套基于 **OCI MySQL HeatWave** 的高并发异步审计流水与日频汇率人民币结算系统（Phase 2）。但在尝试启用 LiteLLM 官方 Admin Web 控制台（`/ui`）以实现可视化运维与多租户分账时，遭遇了底层架构上的核心死锁：

### 痛点一：LiteLLM 官方 Admin UI 强绑定 PostgreSQL（Prisma 类型死锁）
- **现象**：当访问 LiteLLM 官方 Web 控制台（`/ui`）并尝试输入管理员密码登录时，后端接口直接报错返回 `400 Bad Request: {"error": "Authentication Error, Not connected to DB!"}`。
- **源码根因**：LiteLLM 官方的 Next.js 控制台底层使用 Python 版本的 **Prisma ORM (`prisma-client-py`)** 来管理用户表、JWT 会话与虚拟密钥（Virtual Keys）。在 LiteLLM 内置的 `schema.prisma` 模型定义中，`models`、`permissions`、`access_group_ids` 等字段被强定义为 **PostgreSQL 原生的标量数组类型 (`String[]`)**：
  ```prisma
  model LiteLLM_VerificationToken {
    token       String   @id
    key_name    String?
    key_alias   String?
    models      String[] // 👈 PostgreSQL 特有标量数组类型！
    spend       Float    @default(0.0)
    max_budget  Float?
    // ...
  }
  ```
  而 MySQL 原生根本不支持标量数组类型（仅支持 JSON 或逗号拼接字符串）。Prisma 编译器明确禁止在 `provider = "mysql"` 的情况下声明 `String[]` 标量数组。
  如果强行将 LiteLLM 的 `DATABASE_URL` 指向 MySQL，Prisma 客户端在启动执行 DDL 迁移或生成代码时会直接 Panic 崩溃，导致 LiteLLM 官方 Web UI 与 MySQL 存在**无法调和的原生类型兼容死锁**。

### 痛点二：单一 Master Key 导致分账缺失与权限无隔离
- **现象**：在没有控制面数据库的情况下，所有客户端（包括各后端实例、后宫妹妹 Hebe/Yui、Codex、自动化脚本等）均直接携带全局根密钥 `LITELLM_MASTER_KEY` 发起请求。
- **业务隐患**：
  1. MySQL 中的 `llm_request_logs` 审计流水中，`api_key_alias` 字段全部记录为 `default`，无法按调用方（如 Hebe 妹妹、Codex 助手、生产流水线）实现精细化 Token 消耗统计与人民币财务分账；
  2. 无法为不同调用方配置独立模型白名单（如限制某实例仅允许调用便宜的 Flash 模型）和预算硬限制（超额自动熔断）。

### 痛点三：运维交互成本高
- 缺乏直观的可视化 Web 面板，每次新增 Key、修改 RPM/TPM 频控或查看各模型健康度时，都需要手动编写 curl 脚本或修改 YAML。

---

## 2. 破局之道：双数据库解耦架构设计（Dual-DB Architecture）

针对上述痛点，我们确定了**“控制面与数据面完全解耦”**的双数据库企业级架构：

```
                                  ┌─────────────────────────────────────────┐
                                  │      客户端 / 浏览器 / 各实例妹妹       │
                                  └────────────────────┬────────────────────┘
                                                       │
                                                       ▼
                      ┌─────────────────────────────────────────────────────────────────┐
                      │                   Kong Ingress Gateway (KIC)                    │
                      │  - API 路由: /litellm/v1/... (strip-path: true)                  │
                      │  - UI 路由: /ui, /_next, /login, /v2, /key, /models (透传)       │
                      └────────────────────────────────┬────────────────────────────────┘
                                                       │
                                                       ▼
                      ┌─────────────────────────────────────────────────────────────────┐
                      │                 LiteLLM Proxy Service (:4000)                   │
                      │                                                                 │
                      │   ┌─────────────────────────────────────────────────────────┐   │
                      │   │               LiteLLM FastAPI / Uvicorn                 │   │
                      │   └───────────────┬─────────────────────────┬───────────────┘   │
                      │                   │                         │                   │
                      └───────────────────┼─────────────────────────┼───────────────────┘
                                          │                         │
                 ┌────────────────────────┴────────┐       ┌────────┴────────────────────────┐
                 │                                 │       │                                 │
                 ▼                                 ▼       ▼                                 ▼
┌─────────────────────────────────┐ ┌───────────────────────────────┐ ┌─────────────────────────────────┐
│     控制面 (Control Plane)      │ │      缓存层 (Cache Layer)     │ │      数据面 (Data Plane)        │
│   Neon Serverless PostgreSQL    │ │       K3s 内网 Redis          │ │      OCI MySQL HeatWave         │
│   (AWS Singapore 同城低延迟)    │ │ (redis.redis.svc.cluster.local)│ │        (rin-heatwave)           │
├─────────────────────────────────┤ ├───────────────────────────────┤ ├─────────────────────────────────┤
│ • LiteLLM Admin Web UI 登录会话 │ │ • 响应级语义缓存              │ │ • 全量高并发 API 异步审计流水   │
│ • Virtual Keys 虚拟子 Key 存储  │ │ • 日频汇率 L2 缓存 (12h TTL)  │ │ • 实时 Token 计量与延迟追踪     │
│ • 多租户配额与预算熔断规则      │ │ • RPM/TPM 频控计数器          │ │ • 每日实时汇率与人民币财务分账  │
│ • 模型白名单与路由策略持久化    │ │                               │ │ • 降级轨迹追踪与失败异常记录    │
└─────────────────────────────────┘ └───────────────────────────────┘ └─────────────────────────────────┘
```

### 职责清晰划分：
1. **控制面 (Control Plane) —— Neon Serverless PostgreSQL**：
   - 专门承载 LiteLLM 官方 Web UI、JWT 登录、Prisma Schema、Virtual Keys 与团队预算；
   - 选用 AWS 新加坡节点（`ap-southeast-1`），与部署在 OCI 新加坡节点的 K3s 集群同城互联，**实测 Direct 直连网络延迟仅 ~10.95ms**；
   - 采用 Neon 免费 Serverless 档（0.5 GB 存储 + 闲置自动休眠），**0 占用本地 VM 磁盘空间**。
2. **数据面 (Data Plane) —— OCI MySQL HeatWave (`rin-heatwave`)**：
   - 专门承载全量高并发 API 审计流水、Token 计量、日频实时汇率折算与高精度人民币财务结算；
   - 基于我们自研的 SQLAlchemy 2.0 Core 异步落库 Hook（`app.core.logging_hook`），全程在后台 asyncio 协程中执行，数据库任何抖动 100% 隔离，绝不影响客户端正常响应。
3. **缓存层 (Cache Layer) —— K3s In-Cluster Redis**：
   - 承载日频汇率 L2 缓存与分布式限流。

---

## 3. 全流程实战落地的关键踩坑与攻坚

### 踩坑一：Neon 事务连接池（PgBouncer）与 Prisma DDL 迁移死锁
- **问题分析**：
  Neon 默认提供的连接串通常带有 `-pooler` 后缀（如 `ep-xxx-pooler.c-3.ap-southeast-1.aws.neon.tech`），底层通过 PgBouncer 事务连接池工作。但 PgBouncer 在 Transaction 模式下**不支持预编译语句（Prepared Statements）以及复杂的 DDL 表结构迁移（`CREATE TABLE`, `ALTER TABLE`）**。LiteLLM 首次启动连接空白库时必须执行 Prisma DDL 迁移，如果使用 `-pooler` 连接串，容器启动时会抛出 `Prepared statements not supported in transaction mode` 错误并直接退出。
- **解决方案**：
  在配置 `DATABASE_URL` 时，**必须去除 `-pooler` 后缀**，采用 Direct 直连 5432 端口，并显式指定 SSL 模式：
  ```text
  postgresql://neondb_owner:<PASSWORD>@ep-bitter-sky-azfg5i09.c-3.ap-southeast-1.aws.neon.tech:5432/neondb?sslmode=require
  ```

---

### 踩坑二：OCI ARM64 多架构 Docker 构建中的 Node.js 运行时缺失
- **问题分析**：
  LiteLLM 底层所依赖的 Python 包 `prisma`（`prisma-client-py`）在执行 `prisma generate` 或 `prisma db push` 时，本质上是通过内置的 `nodeenv` 或系统环境下的 Node.js 来调用 Prisma CLI 二进制文件。
  我们的基础镜像是精简的 `python:3.12-slim`。在 GitHub Actions 执行 `linux/amd64,linux/arm64` 双架构构建时，由于缺少 Node.js 运行时与解压环境，构建过程报出了子进程退出错误：
  ```text
  subprocess.CalledProcessError: Command '['/root/.cache/prisma-python/nodeenv/bin/npm', 'install', 'prisma@5.17.0']' returned non-zero exit status 127.
  ```
- **解决方案**：
  1. 在 `Dockerfile` 中通过 `apt-get` 全局安装官方 `ca-certificates`, `openssl`, `nodejs`, `npm`；
  2. 显式注入环境变量 `PRISMA_USE_GLOBAL_NODE="true"`，让 Prisma 直接复用系统 Node.js，跳过 nodeenv 的动态下载；
  3. 为非 root 用户（`65532:65532`）创建可读写的 Prisma 缓存目录 `/tmp/prisma-cache`，并设置 `PRISMA_HOME_DIR="/tmp/prisma-cache"`：
  ```dockerfile
  FROM python:3.12-slim

  ENV PYTHONDONTWRITEBYTECODE=1 \
      PYTHONUNBUFFERED=1 \
      PYTHONPATH="/app" \
      PATH="/app/.venv/bin:$PATH" \
      PRISMA_HOME_DIR="/tmp/prisma-cache" \
      PRISMA_USE_GLOBAL_NODE="true"

  WORKDIR /app

  # 安装 OpenSSL, CA 证书, Node.js 与 npm，满足 Neon SSL 连接与 Prisma Engine 运行依赖
  RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates \
      openssl \
      nodejs \
      npm \
      && rm -rf /var/lib/apt/lists/*

  COPY pyproject.toml uv.lock README.md ./

  # 安装依赖并提前预生成 Prisma 客户端代码
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

### 踩坑三：安全托管与 GitOps 零明文闭环
- **OCI Vault 托管**：
  严禁将包含密码的 PostgreSQL 连接串写入 Git 或明文 YAML 中。我们在 OCI Vault（`gateman-vault` / `litellm-prod` compartment）中创建 Secret `litellm-database-url`。
- **External Secrets Operator (ESO) 自动同步**：
  在 ArgoCD 清单 `my-argocd-manifests/argocd-apps/litellm-svc-app.yaml` 中配置 mapping：
  ```yaml
  externalSecret:
    enabled: true
    secretStoreRef:
      name: oci-litellm-vault-store
      kind: SecretStore
    target:
      name: litellm-secrets
      creationPolicy: Owner
    data:
      - secretKey: DATABASE_URL
        remoteRef:
          key: litellm-database-url
      - secretKey: MYSQL_PASSWORD
        remoteRef:
          key: litellm-mysql-password
      - secretKey: LITELLM_MASTER_KEY
        remoteRef:
          key: litellm-master-key
  ```
- **CI/CD Digest 联动**：
  代码推送到 `my-litellm-service` 后，GitHub Actions 自动构建双架构镜像，并通过 `repository_dispatch` 跨仓库通知 `my-argocd-manifests` 更新镜像 Digest，ArgoCD 自动完成 Pod 滚动发布。

---

### 踩坑四：Kong Gateway API (KIC) 完整 UI 与静态资源路由编排
- **问题分析**：
  LiteLLM 官方 Admin 控制台是一个由 Next.js 构建的完整 SPA 应用，除了 `/ui` 入口路径外，还需要请求 `/_next/static/...` 前端静态资源、`/litellm-asset-prefix/...` 样式，以及 `/login`, `/v2`, `/v3`, `/auth`, `/key`, `/user`, `/models` 等数十个后端 API 接口。如果只在 Kong 里配置 `/ui`，会导致网页样式完全丢失、登录接口 404。
- **解决方案**：
  在 ArgoCD 清单的 `extraRoutes.ui-route` 中，为 Kong Ingress 编排完整的透传规则列表：
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
            - path: /customer
            - path: /organization
            - path: /project
            - path: /spend
            - path: /budget
        - matches:
            - path: /models
            - path: /model
            - path: /model_group
            - path: /model_hub
            - path: /routes
            - path: /global
            - path: /config
            - path: /settings
        - matches:
            - path: /health
            - path: /cache
            - path: /alerting
            - path: /audit
  ```

---

## 4. 全链路端到端闭环验证（E2E Verification）

### 1. Pod 启动与 Prisma 自动建表验证
查看生产 Pod 日志，确认 Prisma 自动连接 Neon PostgreSQL 并完成全量 DDL 迁移：
```text
All migrations have been successfully applied.
2026-08-30 08:54:21,253 - litellm_proxy_extras - INFO - prisma migrate deploy completed
2026-08-30 08:54:50,896 - litellm_proxy_extras - INFO - ✅ Migration diff applied successfully
2026-08-30 08:54:50,896 - litellm_proxy_extras - INFO - ✅ Post-migration sanity check completed
INFO: Application startup complete. Uvicorn running on http://0.0.0.0:4000
```

### 2. Web UI 访问与登录验证
- 打开浏览器访问：`https://gw.jppwl.asia/ui`（或 `http://gw.jpgcp.cloud:31850/ui`）；
- 输入管理员 Master Key（`sk-WtkFx0QQBE8A6sNHKrzOWPsx8GQlcK0Dtzx6ptHwW2Q`）；
- 成功秒级登入 LiteLLM Dashboard 控制台，界面组件、图表与导航正常渲染。

### 3. 在线发行租户专属虚拟 Key
在控制台的 **API Keys** 页面中，创建一个测试虚拟子 Key（如 `key_alias: "claire-test-key"`, 限制模型 `gemini-3.7-flash`, 预算限额 `10 USD`），成功获取生成的子 Key：`sk-fnhh6MRvMu-sZ3FElYhaOg`。

### 4. 携带虚拟 Key 发起调用与双库联动核验
使用生成的虚拟 Key 调用 LiteLLM 网关：
```bash
curl -X POST "http://gw.jpgcp.cloud:31850/litellm/v1/chat/completions" \
  -H "Authorization: Bearer sk-fnhh6MRvMu-sZ3FElYhaOg" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.7-flash",
    "messages": [{"role": "user", "content": "Hello dual DB!"}],
    "max_tokens": 10
  }'
```
- **控制面 Neon PG 行为**：扣减该 Key 的已用预算（响应头返回 `X-Litellm-Key-Spend: 3.225e-05`，`X-Litellm-Key-Max-Budget: 10.0`）；
- **数据面 OCI MySQL 行为**：直连 MySQL 查询 `llm_request_logs` 表，该笔请求流水已实时入库，包含准确的 Tokens 计数、当日实时汇率 `6.7421` 以及高精度人民币开销！

---

## 5. 核心收益与架构最佳实践总结

1. **彻底打破组件锁死，实现控制面与数据面解耦**：
   - 既享受了 LiteLLM 官方 Web 控制台、多租户 Virtual Keys、JWT 鉴权的强大生态（PostgreSQL 控制面）；
   - 又保留了轻量级、零成本、高并发且适配国内财务习惯的 MySQL 数据面与实时汇率结算系统。
2. **零硬件成本的云原生“白嫖”典范**：
   - **Neon Serverless PG**：AWS 新加坡免费档，闲置自动休眠，0 磁盘占用；
   - **OCI MySQL HeatWave**：Oracle Cloud Always Free 终身免费托管实例；
   - **K3s + Kong Ingress + ESO + ArgoCD**：全自动 GitOps 运维闭环，省心稳健。
3. **高可用容灾护城河**：
   - 外部 PostgreSQL 即使发生冷启动延迟或网络抖动，仅影响 Web 控制台页面与虚拟 Key 的新增/编辑；
   - LiteLLM 的核心大模型 API 转发与 OCI MySQL 异步落库具备 100% 独立 SLA，真正做到业务高可用、零中断！
