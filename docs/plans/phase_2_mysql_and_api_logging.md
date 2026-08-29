# Phase 2 实施计划：OCI MySQL 启用、日频汇率换算与 API 调用日志落库 (MySQL Logging, Daily FX Rate & OCI Vault)

> **目标**：完成 OCI MySQL (`rin-heatwave`) 的启用与数据库初始化，建立基于 **OCI Vault (`gateman-vault`)** 的集中密钥管理规范；实现按天（By Day）自动获取最新 USD/CNY 汇率与“L1 本地内存 + L2 K3s Redis”双级高可用缓存机制；改造 **Dockerfile** 支持自定义 Python Hook 导入；并通过 LiteLLM Proxy 异步 Success/Failure Callback 钩子无感落库全量（普通+流式、成功+失败、梯队降级轨迹）API 调用的请求元数据、Token 消耗、美金开销 (USD)、折合人民币开销 (RMB) 与响应延迟。

---

## 1. 架构与数据流设计 (Architecture & Data Flow)

```
[Client Request (Sync/Stream)] --> [LiteLLM Proxy (:4000)]
                                         |
                                         | (Async Non-blocking Callbacks)
                                         v
                      [app.core.logging_hook.DBLoggingLogger]
                      (async_log_success_event / async_log_failure_event)
                                         |
            +----------------------------+----------------------------+
            |                                                         |
            v                                                         v
[app.core.fx_rate (L1 内存 / L2 Redis / API)]           [aiomysql Lazy Connection Pool]
 (Get Today USD->CNY FX Rate)                            (pool_recycle=300, autocommit=True)
            |                                                         |
            +----------------------------+----------------------------+
                                         |
                                         v
                      [OCI MySQL HeatWave: litellm_db.llm_request_logs]
```

1. **零延迟主流程**：客户端发送 API 请求（支持常规及流式 `stream=True`）并正常获得响应，主接口响应逻辑无任何额外阻塞与延迟。
2. **双钩子异步触发**：
   - 成功请求触发 `async_log_success_event`；
   - 失败请求（429 限流、500 服务端错误、超时等）触发 `async_log_failure_event`。
3. **分级汇率结算 (`fx_rate`)**：
   - **L1 本地内存**：0ms 直接读取 Python 内存全局变量；
   - **L2 K3s Redis**：复用共享 Redis 连接池，键 `fx:usd_cny_rate`（TTL 24 小时，独立 `try-except` 防护，Redis 故障透明穿透）；
   - **外部汇率 API**：缓存均失效时异步拉取 `open.er-api.com` 并回填 L1/L2；
   - **保底降级**：网络异常时自动使用环境变量中的 `DEFAULT_USD_TO_CNY_RATE`（默认 `7.2300`）。
4. **人民币开销换算与高精度处理**：使用 `Decimal` 计算 `cost_cny = round(cost_usd * fx_rate, 6)`，失败或免费调用安全归零 `0.000000`。
5. **降级轨迹与元数据提取**：
   - 记录 `model_requested`（客户端请求别名如 `gemini-3.7-flash`）与 `model_used`（实际命中上游模型如 `gemini-3.7-pro-plan` 或 `gemini-3.7-backup`），实现降级全链路可视化追溯。
   - 提取 `prompt_tokens`、`completion_tokens`、`total_tokens`、`cost_usd`、`cost_cny`、`fx_rate`、`latency_ms` 与 `status_code`。
6. **全链路异常隔离与连接保活**：
   - 写库与汇率异常全程包裹在 `try...except` 中记录告警日志，绝不向客户端抛出异常。
   - `aiomysql` 连接池配置 5 分钟自动重连保活 (`pool_recycle=300`)。

---

## 2. 密钥管理与 OCI Vault 规范 (Secret Management & OCI Vault)

### 2.1 集中密钥分发拓扑

```
                   ┌────────────────────────────────────────┐
                   │       OCI Vault (gateman-vault)        │
                   │  - Secret: litellm/mysql-password       │
                   │  - Secret: litellm/mysql-user           │
                   └───────────────────┬────────────────────┘
                                       │
                ┌──────────────────────┴──────────────────────┐
                │                                             │
                ▼ (通过 ESO 自动同步)                           ▼ (通过 OCI CLI 身份读取)
   ┌───────────────────────────┐                 ┌───────────────────────────┐
   │  K3s 集群 (生产环境)        │                 │  Local 开发机 (本地联调)     │
   │  External Secrets (ESO)   │                 │  .env (GitIgnored 本地缓存)  │
   │            ↓              │                 │            ↓              │
   │  Kubernetes Secret        │                 │  Pydantic Settings 读取   │
   │            ↓              │                 └───────────────────────────┘
   │  LiteLLM Pod 环境变量     │
   └───────────────────────────┘
```

1. **唯一真理源 (Single Source of Truth)**：
   - MySQL 数据库密码与凭证统一托管在 OCI 新加坡区 `gateman-vault`。
   - 密钥条目命名规范：`litellm/mysql-password`、`litellm/mysql-user` 等。
2. **K3s 生产集群 (External Secrets Operator - ESO)**：
   - 集群声明 `ExternalSecret` 资源引用 OCI Vault Secret。
   - ESO 自动拉取并生成 Kubernetes 原生 Secret（如 `litellm-mysql-secret`），以环境变量注入 LiteLLM Pod。
   - Git 仓库中绝无明文密码，支持自动轮换与 ArgoCD 容灾重建。
3. **本地开发环境 (.env 本地隔离)**：
   - 本地开发机利用 OCI CLI 身份与脚本读取或本地填充 `.env`（被 `.gitignore` 严格忽略）。
   - 代码通过 `app/core/config.py` 中的 Pydantic Settings 强类型读取并自动脱敏。

---

## 3. 数据库 Schema 与初始化 (`scripts/init_db.py`)

### 3.1 数据库 Schema 结构 (`llm_request_logs`)

```sql
CREATE DATABASE IF NOT EXISTS litellm_db DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE litellm_db;

CREATE TABLE IF NOT EXISTS llm_request_logs (
    id VARCHAR(36) PRIMARY KEY,                        -- 记录唯一标识符 (UUID4)
    request_id VARCHAR(128) NOT NULL,                  -- LiteLLM API 请求 ID
    api_key_alias VARCHAR(64) DEFAULT 'default',       -- 客户端 Key 别名 / 团队标识
    model_requested VARCHAR(64) NOT NULL,             -- 客户端请求的模型别名 (如 gemini-3.7-flash)
    model_used VARCHAR(64) NOT NULL,                  -- 实际命中的上游模型 ID (如 gemini-3.7-pro-plan)
    prompt_tokens INT NOT NULL DEFAULT 0,             -- 输入/提示 Token 数
    completion_tokens INT NOT NULL DEFAULT 0,         -- 输出/补全 Token 数
    total_tokens INT NOT NULL DEFAULT 0,              -- 总 Token 数
    cost_usd DECIMAL(10, 6) NOT NULL DEFAULT 0.000000, -- 美金开销 (USD)
    cost_cny DECIMAL(10, 6) NOT NULL DEFAULT 0.000000, -- 折合人民币开销 (RMB)
    fx_rate DECIMAL(8, 4) NOT NULL DEFAULT 7.2300,    -- 结算时使用的当日 USD/CNY 汇率
    latency_ms INT NOT NULL DEFAULT 0,                -- 请求响应延迟 (毫秒)
    status_code INT NOT NULL DEFAULT 200,             -- HTTP 响应状态码 (200, 429, 500 等)
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,     -- 记录落库时间
    INDEX idx_logs_created_at (created_at),            -- 按时间范围查询索引
    INDEX idx_logs_model_used (model_used),           -- 按实际模型统计索引
    INDEX idx_logs_status_code (status_code)          -- 按状态码筛选索引
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 3.2 自动建库建表脚本 (`scripts/init_db.py`)

- 读取 `app/core/config.py` 中的 MySQL 配置。
- 第一步：连接 MySQL 服务端执行 `CREATE DATABASE IF NOT EXISTS litellm_db`。
- 第二步：连接 `litellm_db` 数据库执行 `CREATE TABLE IF NOT EXISTS llm_request_logs`。
- 幂等性设计：重复执行不影响现有数据，不报错。

---

## 4. 按天自动汇率模块与双级缓存 (`app/core/fx_rate.py`)

### 4.1 双级缓存与获取逻辑 (L1 Memory + L2 Redis)

```
[Get FX Rate]
     |
     v
[L1 本地内存缓存命中?] --Yes--> 返回汇率 (0ms)
     | No
     v
[L2 K3s Redis 缓存命中?] (复用 Redis Client, try-except 防护) --Yes--> 同步回填 L1 并返回汇率
     | No / Redis 故障
     v
[请求公开汇率 API (open.er-api.com)] --Success--> 异步写入 L1 + L2 (TTL 86400s) 并返回
     | Failure
     v
[静默降级] --> 优先使用 L1 旧值或 .env 保底汇率 DEFAULT_USD_TO_CNY_RATE (7.2300)
```

1. **L1 本地内存缓存 (In-Memory)**：
   - 维护进程级变量 `_cached_rate` 与 `_last_fetch_time`。
   - 读取延迟 0 毫秒，单 Pod 内高频调用无任何网络与 Redis 负载。
2. **L2 K3s Redis 集中缓存 (Redis Shared Cache)**：
   - 复用现有 K3s Redis (`100.105.130.0:6379`) 单例连接池。
   - 键名：`fx:usd_cny_rate`，过期时间 TTL 设置为 86400 秒（24 小时）。
   - **独立异常防护**：Redis 读写均由细粒度 `try-except` 包裹，Redis 离线不影响主请求。
3. **公开 API 刷新与降级兜底**：
   - 双级缓存均过期后，使用 `httpx.AsyncClient` 异步拉取 `https://open.er-api.com/v6/latest/USD`。
   - 任何网络超时或解析错误自动捕获，静默降级为配置中的保底汇率 `DEFAULT_USD_TO_CNY_RATE`（默认 `7.2300`），绝不影响主流程。

---

## 5. 异步落库 Hook 与 LiteLLM 配置 (`app/core/logging_hook.py`)

### 5.1 异步 CustomLogger 实现细节

继承 LiteLLM `CustomLogger` 类：

1. **成功回调 (`async_log_success_event`)**：
   - 提取 `request_id`、`model_requested`、`model_used`、`prompt_tokens`、`completion_tokens`、`total_tokens`、`response_cost` 及 `response_time_ms`。
   - 异步调用 `get_usd_to_cny_rate()` 计算 `cost_cny = round(cost_usd * fx_rate, 6)`。
   - 参数化插入 `status_code=200` 的日志记录。
2. **失败回调 (`async_log_failure_event`)**：
   - 捕获 `429 (Rate Limit)`、`500 (Upstream Error)`、`Timeout` 等异常状态码。
   - 记录请求耗时 `latency_ms`、`model_requested`，Tokens 与费用记 `0`，精准记录错误状态码。
3. **aiomysql 连接池生命周期与连接保活**：
   - 采用 **异步单例懒加载** `get_db_pool()`，在首次收到日志事件时于运行中的 asyncio loop 初始化。
   - 配置参数：`minsize=1`、`maxsize=10`、`pool_recycle=300`（5分钟自动重连，防止 OCI/socat 长时间空闲连接断开导致 `2006 MySQL server has gone away`）、`autocommit=True`。
   - 提供 `close_db_pool()` 便于测试和优雅退出。
4. **全隔离保障**：所有回调方法包裹在 `try ... except Exception as err` 全局防护块中。

### 5.2 LiteLLM 网关配置 (`config.yaml`)

```yaml
router_settings:
  routing_strategy: "least-busy"
  num_retries: 3
  allowed_fails: 1
  cooldown_time: 60
  fallbacks:
    - gemini-3.7-flash: ["gemini-3.7-pro-plan", "gemini-3.7-backup"]

litellm_settings:
  callbacks: ["app.core.logging_hook.custom_logger"]
  stream_usage: true  # 🌟 关键：确保流式 (stream=True) 请求在结束时计算并返回 Token 与 Cost
  cache: true
  cache_params:
    type: redis
    host: os.environ/REDIS_HOST
    port: os.environ/REDIS_PORT
    password: os.environ/REDIS_PASSWORD
    supported_call_types: [chat_completion]
    ttl: 3600
```

---

## 6. 容器镜像与 Dockerfile 改造 (`Dockerfile`)

### 6.1 `Dockerfile` 改造点

- **补充 Python 模块查找路径**：在 `Dockerfile` 的 `ENV` 中添加 `PYTHONPATH="/app"`，确保容器内启动 `litellm` CLI 进程时能够稳定加载并执行 `app.core.logging_hook` 自定义回调类。
- **构建与多架构支持**：保持现有 `linux/amd64` 与 `linux/arm64` 双架构构建流程，容器安全非 root 用户 `65532:65532` 运行。

---

## 7. 详细文件修改清单

| 序号 | 文件路径 | 修改类型 | 职责描述 |
| :--- | :--- | :--- | :--- |
| 1 | `Dockerfile` | 修改 | 环境变量增加 `PYTHONPATH="/app"`，保障 LiteLLM CLI 运行时正确加载自定义 Python Hook。 |
| 2 | `config.yaml` | 修改 | 配置 `callbacks: ["app.core.logging_hook.custom_logger"]` 与 `stream_usage: true`。 |
| 3 | `.env.example` & `.env` | 修改 | 配置 `MYSQL_PASSWORD` 环境变量，添加 `DEFAULT_USD_TO_CNY_RATE=7.23` 配置项。 |
| 4 | `app/core/config.py` | 修改 | `Settings` 类新增 `default_usd_to_cny_rate: float = 7.23` 校验字段。 |
| 5 | `scripts/init_db.py` | 新建 | 一键幂等初始化 OCI MySQL `litellm_db` 数据库与 `llm_request_logs` 数据表。 |
| 6 | `app/core/fx_rate.py` | 新建 | 按天异步获取最新 USD/CNY 汇率与“L1 本地内存 + L2 Redis”双级缓存及降级模块。 |
| 7 | `app/core/logging_hook.py` | 新建 | 继承 LiteLLM `CustomLogger`，实现成功/失败双钩子异步 MySQL 落库、降级轨迹记录及连接池保活。 |
| 8 | `scripts/verify_db_logging.py` | 新建 | 烟囱验证脚本：发送常规及流式 API 请求并查询校验 OCI MySQL 数据落库与 RMB 换算。 |
| 9 | `tests/test_fx_rate.py` | 新建 | 汇率 API 请求、L1/L2 双级缓存与降级机制单元测试。 |
| 10 | `tests/test_logging_hook.py` | 新建 | 异步 Hook 字段提取（成功/失败/流式/降级）、RMB 计算与 MySQL 异常解耦单元测试。 |

---

## 8. 逐阶段实施与验收步骤 (Step-by-Step Milestones)

### 阶段一：Dockerfile 调整、Vault 密码托管与建库自动化 (Step 1)
1. 在 `Dockerfile` 中补充 `PYTHONPATH="/app"`。
2. 在 OCI Vault (`gateman-vault`) 中登记托管 MySQL 凭证。
3. 本地 `.env` 从 Vault 获取或配置 `MYSQL_PASSWORD` 及汇率参数。
4. 运行 `uv run python -m scripts.init_db`，控制台打印 `Database litellm_db and table llm_request_logs initialized successfully.`。
5. 运行 `uv run python -m scripts.check_phase1` 确认连通性检查通过 (`mysql: OK`)。

### 阶段二：双级缓存汇率模块开发与测试 (Step 2)
1. 编写 `app/core/fx_rate.py`（实现 L1 内存 + L2 Redis 双级缓存与独立异常保护）。
2. 编写 `tests/test_fx_rate.py` 单元测试，覆盖 L1 命中、L2 Redis 命中、Redis 异常容灾、API 刷新及降级兜底场景。
3. 运行 `uv run pytest -q tests/test_fx_rate.py` 验证通过。

### 阶段三：异步落库 Hook 编写与测试 (Step 3)
1. 编写 `app/core/logging_hook.py` 实现 `DBLoggingLogger(CustomLogger)`（含成功/失败双事件处理、降级追踪及 `pool_recycle` 保活）。
2. 编写 `tests/test_logging_hook.py` 单元测试，验证常规/流式响应对象字段解析、错误状态码捕获、USD 转 RMB 计算逻辑（Mock DB）。
3. 运行 `uv run pytest -q tests/test_logging_hook.py` 验证通过。

### 阶段四：Proxy 配置集成与端到端实测验收 (Step 4)
1. 在 `config.yaml` 中配置 `callbacks` 与 `stream_usage: true`。
2. 运行烟囱测试脚本 `scripts/verify_db_logging.py` 发送常规及流式测试请求。
3. 查询 OCI MySQL：
   ```sql
   SELECT request_id, model_requested, model_used, prompt_tokens, completion_tokens, total_tokens, cost_usd, cost_cny, fx_rate, latency_ms, status_code 
   FROM llm_request_logs 
   ORDER BY created_at DESC 
   LIMIT 5;
   ```
4. 验证常规请求与流式请求均能正确记录 Token、耗时与金额，且 `cost_cny` 精准等于 `round(cost_usd * fx_rate, 6)`，降级轨迹清晰可见。
5. 运行 `uv run pytest` 与 `uv run ruff check app scripts tests` 确保全量自动化测试与代码规范通过。

---

## 9. 风险控制与红线 (Risk Control & Standards)

1. **绝对无感隔离**：写库与汇率获取逻辑必须全程异步执行，底层任何网络超时、数据库或 Redis 异常，绝对严禁引发大模型 API 报错或阻断。
2. **流式 Token 完整性**：必须显式开启 `stream_usage: true`，严禁产生流式调用 Token/开销落库为 0 的静默丢失缺陷。
3. **连接保活与防断开**：`aiomysql` 连接池必须设置 `pool_recycle=300` 与 `autocommit=True`，彻底杜绝 OCI 堡垒机空闲连接被防火墙静默中断引发的 `MySQL 2006` 报错。
4. **金额精度防护**：数据库中 `cost_usd` 与 `cost_cny` 字段必须统一采用 `DECIMAL(10, 6)` 类型，汇率采用 `DECIMAL(8, 4)`，严禁使用 MySQL `FLOAT/DOUBLE` 或 Python 浮点直接存储。
5. **密钥安全与零硬编码**：数据库密码统一由 **OCI Vault (`gateman-vault`)** 作为唯一真理源托管，生产环境通过 **ESO** 自动同步至 K8s Secret，本地环境通过 `.env` 隔离；严禁在代码、测试用例、YAML 或 Git 历史中提交任何真实明文密码。
