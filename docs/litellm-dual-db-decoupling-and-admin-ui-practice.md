# LiteLLM 控制台与 MySQL 兼容性问题排查及双数据库架构改造记录

## 1. 问题背景

在 LiteLLM Proxy 的日常维护中，我们通过自定义的 `CustomLogger`（基于 SQLAlchemy 2.0 Core）将所有 API 请求的审计流水、Token 消耗以及折算后的人民币费用异步写入 OCI MySQL（`litellm_db.llm_request_logs`）。这套数据面落库方案运行稳定。

但在尝试启用 LiteLLM 官方 Admin Web 控制台（`/ui`）时，遇到了无法正常使用的问题。访问 `/ui` 并提交管理员密钥登录时，接口返回 `400 Bad Request`：

```json
{"error": "Authentication Error, Not connected to DB!"}
```

---

## 2. 根因排查：为什么 LiteLLM UI 无法直接使用 MySQL

阅读 LiteLLM 源码后发现，LiteLLM 的架构中存在两个不同层次的数据需求：

1. **数据面（Data Plane）**：请求转发、流式 Token 聚合、日志审计与费用核算。这一层可以通过 LiteLLM 的 Custom Callback 机制灵活对接任何数据库（如 MySQL、ClickHouse、DynamoDB）。
2. **控制面（Control Plane）**：官方 Next.js Web 控制台的用户认证、JWT 会话维护、Virtual Key（虚拟子密钥）生成与团队配额管理。

控制面强依赖 Python 版本的 Prisma ORM（`prisma-client-py`）。查看 LiteLLM 内置的 `litellm/proxy/schema.prisma` 定义：

```prisma
model LiteLLM_VerificationToken {
  token       String   @id
  key_name    String?
  key_alias   String?
  models      String[] // PostgreSQL 标量数组
  spend       Float    @default(0.0)
  max_budget  Float?
  permissions Json?
  // ...
}
```

在 Prisma 的类型系统中：
- `models String[]` 被定义为**标量数组**（Scalar Array）。
- PostgreSQL 原生支持标量数组类型。
- MySQL 原生不支持标量数组（只有 JSON 或字符串）。
- Prisma 编译器强制要求：如果数据源配置为 `provider = "mysql"`，则 schema 中严禁出现标量数组语法。

由于 LiteLLM 官方 schema 将 `models` 等核心字段强行声明为 `String[]`，直接将 `DATABASE_URL` 指向 MySQL 会在启动初始化（`prisma generate` / `prisma migrate`）时直接抛错，导致官方 UI 无法在 MySQL 环境下运行。

此外，没有控制面数据库还会带来权限管理上的问题：
- 缺少 Virtual Key 生成机制，所有调用方只能共用全局 `LITELLM_MASTER_KEY`。
- 审计日志中无法按调用方（如不同的应用或开发团队）记录 `api_key_alias`，难以进行多租户维度的用量与财务分账。

---

## 3. 架构方案：控制面与数据面解耦

为了在保留现有 MySQL 审计体系的同时启用官方 UI，我们采用了双数据库解耦方案：

```
                    客户端 / 浏览器
                          │
                          ▼
            Kong Gateway Ingress (KIC)
            - API 路由: /litellm/v1/... (strip-path: true)
            - UI 路由:  /ui, /_next, /login, /v2, /key 等 (透传)
                          │
                          ▼
             LiteLLM Proxy Service (:4000)
             ┌───────────────────────────┐
             │ Uvicorn / FastAPI Runtime │
             └─────────────┬─────────────┘
                           │
             ┌─────────────┴─────────────┐
             ▼                           ▼
  [控制面 Control Plane]        [数据面 Data Plane]
  Neon Serverless PostgreSQL    OCI MySQL HeatWave
  - Admin Web UI 登录会话       - 全量 API 异步审计日志
  - Virtual Keys 虚拟子密钥     - Token 消耗精确计量
  - 团队预算与模型白名单        - 实时汇率折算与人民币结算
```

各组件分工如下：
- **控制面（Neon PostgreSQL）**：选用 Neon Serverless PostgreSQL（AWS 新加坡 region），同城直连 OCI 新加坡 ARM 节点，网络延迟实测约 10.95ms。仅用于存储控制台元数据与虚拟 Key，不承载高频日志写入。
- **数据面（OCI MySQL HeatWave）**：继续承载全部对话的异步审计流水落库，完全由自研的 `logging_hook.py` 接管，与控制面隔离。
- **缓存层（Redis）**：K3s 集群内网 Redis，承载响应缓存与日频汇率二级缓存。

---

## 4. 实施过程与踩坑记录

### 4.1 Neon 连接池（PgBouncer）与 DDL 迁移冲突

Neon 默认生成的连接串包含 `-pooler` 后缀（即通过 PgBouncer 代理）。

LiteLLM 在初次连接空数据库时，会自动执行 `prisma migrate deploy` 或 `prisma db push` 创建十几张元数据表。但 PgBouncer 在 Transaction 模式下不支持预编译语句（Prepared Statements）和部分 DDL 操作，导致容器启动时报错退出：

```text
Prepared statements not supported in transaction mode
```

**处理方式**：
在配置 `DATABASE_URL` 时，去除主机名中的 `-pooler` 关键字，直连 5432 端口，并附带 `?sslmode=require` 参数：

```text
postgresql://neondb_owner:<PASSWORD>@ep-bitter-sky-azfg5i09.c-3.ap-southeast-1.aws.neon.tech:5432/neondb?sslmode=require
```

---

### 4.2 ARM64 镜像构建中 Prisma CLI 依赖缺失

在 `pyproject.toml` 中引入 `prisma>=0.15.0` 并构建 Docker 镜像时，GitHub Actions 在执行多架构构建（`linux/amd64,linux/arm64`）过程中抛出异常：

```text
subprocess.CalledProcessError: Command '['/root/.cache/prisma-python/nodeenv/bin/npm', 'install', 'prisma@5.17.0']' returned non-zero exit status 127.
```

**原因分析**：
`python:3.12-slim` 基础镜像未安装 Node.js 与 npm。Python 的 `prisma` 包在找不到系统 Node.js 时会尝试用 `nodeenv` 在缓存目录下载并执行预编译的 Node 二进制，而在精简镜像中缺少对应依赖导致执行失败（退出码 127）。

**处理方式**：
1. 在 Dockerfile 中通过系统包管理器安装 `nodejs`、`npm`、`openssl` 和 `ca-certificates`。
2. 设置环境变量 `PRISMA_USE_GLOBAL_NODE="true"`，强制 Prisma 直接使用系统的 Node.js。
3. 为非 root 用户（UID `65532`）预创建可写缓存目录 `/tmp/prisma-cache`，并设置 `PRISMA_HOME_DIR="/tmp/prisma-cache"`。

调整后的 Dockerfile 关键部分如下：

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

### 4.3 密钥管理与 GitOps 编排

数据库连接串通过 OCI Vault 托管，禁止硬编码在清单或代码中：

1. 在 OCI Vault（`litellm-prod` compartment）中登记 Secret `litellm-database-url`。
2. 在 `my-argocd-manifests/argocd-apps/litellm-svc-app.yaml` 中配置 ExternalSecret：
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
3. 代码推送到 `my-litellm-service` 仓库后，GitHub Actions 触发多架构构建，并通过 `repository_dispatch` 远程更新 ArgoCD 清单中的 `image.digest`，实现全自动平滑更新。

---

### 4.4 Kong Ingress Controller 路由配置

LiteLLM 的 Admin 控制台基于 Next.js 开发，除了 `/ui` 入口路径外，前端还会请求大量静态资源（`/_next/static/...`、`/litellm-asset-prefix/...`）以及管理接口（`/login`、`/key`、`/user`、`/models` 等）。

在 ArgoCD Application 的 Helm values 中添加 `extraRoutes.ui-route`，确保相关路径正常透传至 LiteLLM Pod：

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

## 5. 端到端功能验证

### 5.1 启动与建表验证
观察 Pod 日志，确认 Prisma 自动连接 Neon PostgreSQL 完成表结构初始化：

```text
All migrations have been successfully applied.
2026-08-30 08:54:21,253 - litellm_proxy_extras - INFO - prisma migrate deploy completed
2026-08-30 08:54:50,896 - litellm_proxy_extras - INFO - Migration diff applied successfully
2026-08-30 08:54:50,896 - litellm_proxy_extras - INFO - Post-migration sanity check completed
INFO: Application startup complete. Uvicorn running on http://0.0.0.0:4000
```

### 5.2 控制台登录与虚拟 Key 创建
1. 访问 `https://gw.jppwl.asia/ui`，使用管理员 Master Key 登录成功，控制台正常展示各模块概览。
2. 调用 `/key/generate` 接口（或通过 UI 页面）创建具有额度限制的虚拟 Key：
   ```bash
   curl -X POST "http://gw.jpgcp.cloud:31850/key/generate" \
     -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
     -H "Content-Type: application/json" \
     -d '{
       "key_alias": "team-test-key",
       "models": ["gemini-3.7-flash"],
       "max_budget": 10.0
     }'
   ```
   返回生成的新 Key：`sk-fnhh6MRvMu-sZ3FElYhaOg`。

### 5.3 携带虚拟 Key 发起调用与 MySQL 审计验证
使用新生成的子 Key 发送请求：

```bash
curl -X POST "http://gw.jpgcp.cloud:31850/litellm/v1/chat/completions" \
  -H "Authorization: Bearer sk-fnhh6MRvMu-sZ3FElYhaOg" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.7-flash",
    "messages": [{"role": "user", "content": "hello"}],
    "max_tokens": 10
  }'
```

接口正常返回响应，并在响应头中返回该 Key 的预算消耗数据：
```http
HTTP/1.1 200 OK
X-Litellm-Key-Max-Budget: 10.0
X-Litellm-Key-Spend: 0.000032
```

直连 OCI MySQL 查询 `litellm_db.llm_request_logs`：
```sql
SELECT request_id, api_key_alias, model_requested, total_tokens, cost_usd, cost_cny, fx_rate 
FROM llm_request_logs 
ORDER BY id DESC LIMIT 1;
```

查询结果显示该笔调用的 Token 数量、实时汇率以及人民币费用已精确入库，且 `api_key_alias` 正确记录为 `team-test-key`。

---

## 6. 总结

通过控制面（PostgreSQL）与数据面（MySQL）解耦的方式，我们解决了 LiteLLM 官方 UI 对 PostgreSQL 标量数组强依赖导致的兼容性问题。

改造后的架构具备以下特点：
1. **职责分离**：控制面负责用户认证、虚拟 Key 生成和配额管控；数据面负责高并发请求的异步落库与人民币计费，互不影响。
2. **故障隔离**：控制面数据库的偶尔抖动或冷启动不会阻塞核心 API 的转发流程和 MySQL 审计日志写入，保障了线上调用链路的高可用。
3. **零额外成本**：利用 Neon Serverless PostgreSQL 的免费额度搭配现有 OCI MySQL 与 K3s 集群，在不新增服务器资源的前提下完成了全套功能的搭建。
