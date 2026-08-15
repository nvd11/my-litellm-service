# Phase 1 低层实施计划：基础设施接入与 LiteLLM Proxy 启动

> **目标**：在 GCE VM 上完成 Tailscale 网络确认，连接现有 OCI MySQL 与 K3s Redis，配置 LiteLLM Proxy 的模型路由/降级/缓存，并完成不产生 LLM 费用的启动验收。

> **阶段边界**：本阶段只验证数据库和 Redis 的基础连通性并启动 LiteLLM Proxy。请求费用异步落库、MySQL 业务表、FastAPI、评测引擎和正式限流策略分别属于 Phase 2/3/4，不在本阶段实现。

## 1. Phase 1 完成标准

完成后必须满足：

1. GCE VM 已加入 Tailscale，能访问 OCI MySQL `10.0.0.247:3306` 与现有 Redis `100.105.130.0:6379`。
2. MySQL 使用真实账号执行 `SELECT 1` 成功；Redis 使用认证执行 `PING` 返回 `PONG`。
3. LiteLLM Proxy 能在 `:4000` 启动，`GET /health` 返回成功。
4. `/v1/models` 能看到项目声明的模型别名。
5. 使用一个明确指定的模型完成一次 OpenAI 兼容聊天请求，并验证返回结构；该请求属于付费调用，必须显式执行。
6. 主模型故障时，LiteLLM 按配置尝试备用模型；本阶段至少完成配置静态检查，真实故障演练需控制费用。
7. 所有密码、API Key 和数据库连接信息只存在于未提交的 `.env`，仓库中只保留占位符。

## 2. 目录与文件清单

Phase 1 实现后，新增或修改的文件应限定为：

```text
.
├── .env.example
├── .gitignore
├── config.yaml
├── pyproject.toml
├── app/
│   ├── __init__.py
│   └── core/
│       ├── __init__.py
│       ├── config.py
│       └── connectivity.py
├── scripts/
│   ├── __init__.py
│   ├── check_phase1.py
│   └── smoke_proxy.py
└── tests/
    ├── __init__.py
    ├── test_config.py
    └── test_connectivity.py
```

本阶段不创建 `app/main.py`、`app/routers/`、`app/eval/` 或数据库 Hook；这些文件属于 FastAPI/审计阶段。

## 3. Python 依赖与虚拟环境

### 3.1 `pyproject.toml`

使用 Python 3.12，使用 `uv` 解析和安装依赖；项目元数据与依赖声明统一放在 `pyproject.toml`，锁定结果写入 `uv.lock`。

运行依赖：

- `litellm`：Proxy CLI 和模型路由。
- `pydantic-settings`：读取 `.env` 并校验配置。
- `aiomysql`：异步执行 MySQL 连通性查询。
- `redis[hiredis]`：异步 Redis 客户端与快速 RESP 解析。
- `httpx`：调用 LiteLLM 健康检查、模型列表和聊天接口。

开发依赖使用 PEP 735 dependency group：

- `pytest`、`pytest-asyncio`：单元测试。
- `ruff`：静态检查。
- `pyyaml`：读取并静态检查 `config.yaml`。

`pyproject.toml` 至少包含 `[project]`、`[dependency-groups]` 和 `[tool.ruff]`；不需要配置发布到 PyPI 的流程。

必须提供以下 `uv run` 命令：

```bash
uv run python -m scripts.check_phase1
uv run python -m scripts.smoke_proxy --base-url http://127.0.0.1:4000
uv run pytest -q
uv run ruff check app scripts tests
```

### 3.2 虚拟环境

在仓库根目录创建唯一虚拟环境 `.venv`，LiteLLM Proxy 与连接检查脚本共用，不为两个进程创建两个 venv。使用 `uv` 管理环境和锁文件：

```bash
uv venv --python 3.12
uv lock
uv sync --dev
```

日常运行通过 `uv run ...` 使用该环境，不要求手动 `source .venv/bin/activate`。如果需要进入交互式 shell，再执行 `source .venv/bin/activate`。

## 4. 环境变量模板

### 4.1 `.env.example`

只声明变量名和无效占位符：

```env
# OCI MySQL HeatWave
MYSQL_HOST=10.0.0.247
MYSQL_PORT=3306
MYSQL_USER=replace-with-private-user
MYSQL_PASSWORD=replace-with-private-password
MYSQL_DB=litellm_db

# Existing K3s Redis via Tailscale + Kong L4
REDIS_HOST=100.105.130.0
REDIS_PORT=6379
REDIS_PASSWORD=replace-with-private-password

# LLM providers
OPENAI_API_KEY=replace-with-private-key
ANTHROPIC_API_KEY=replace-with-private-key
VERTEXAI_PROJECT=replace-with-gcp-project
VERTEXAI_LOCATION=us-central1

# LiteLLM Proxy
LITELLM_MASTER_KEY=replace-with-private-master-key
LITELLM_PORT=4000
FASTAPI_PORT=8000
```

真实 `.env` 必须加入 `.gitignore`。密码不允许出现在 `config.yaml`、Python 默认值、测试 fixture、日志或 commit message 中。

## 5. 配置加载模块

### 5.1 `app/core/config.py`

该文件只负责配置解析和校验，不执行网络连接。

#### `class Settings(BaseSettings)`

字段与约束：

- `mysql_host: str`、`mysql_port: int`、`mysql_user: str`、`mysql_password: SecretStr`、`mysql_db: str`。
- `redis_host: str`、`redis_port: int`、`redis_password: SecretStr`。
- `openai_api_key: SecretStr`、`anthropic_api_key: SecretStr`。
- `vertexai_project: str`、`vertexai_location: str`。
- `litellm_master_key: SecretStr`、`litellm_port: int = 4000`。
- `connect_timeout_seconds: float = 5.0`。

配置类必须使用 `SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')`，并通过字段类型拒绝非法端口和空必填值。

#### `get_settings() -> Settings`

使用 `functools.lru_cache` 返回单例配置，避免每个检查函数重新读取 `.env`。测试时通过 `get_settings.cache_clear()` 清理缓存。

#### `redacted_summary(settings: Settings) -> dict[str, object]`

返回可安全打印的主机、端口、数据库名和 Proxy 端口；所有 `SecretStr` 只输出 `***`，绝不调用 `get_secret_value()` 生成日志。

## 6. 基础设施连通性模块

### 6.1 `app/core/connectivity.py`

该文件提供只读健康检查，并保证连接失败不会泄露密码或完整连接串。

#### `@dataclass(frozen=True) class CheckResult`

字段：`name: str`、`ok: bool`、`latency_ms: float | None`、`detail: str`。`detail` 只允许白名单错误摘要，例如 `connected`、`authentication_failed`、`timeout`、`connection_refused`。

#### `async def check_mysql(settings: Settings) -> CheckResult`

实现要求：

1. 使用 `aiomysql.connect(host=..., port=..., user=..., password=..., db=..., connect_timeout=...)` 建立连接。
2. 创建 cursor，执行参数固定为 `SELECT 1`，不执行建表、写入或事务提交。
3. 读取一行并确认结果为 `1`。
4. 在 `finally` 中关闭 cursor 和 connection。
5. 将 `OperationalError` 映射为 `connection_refused`、`timeout` 或 `authentication_failed`；其他异常映射为 `unexpected_error`，不返回异常原文中的密码或 DSN。
6. 使用 `time.perf_counter()` 计算毫秒延迟。

#### `async def check_redis(settings: Settings) -> CheckResult`

实现要求：

1. 使用 `redis.asyncio.Redis(host=..., port=..., password=..., socket_connect_timeout=..., socket_timeout=..., decode_responses=True)` 创建客户端。
2. 执行 `await client.ping()`，结果必须为真。
3. 在 `finally` 中执行 `await client.aclose()`。
4. 将认证错误、超时、拒绝连接分别映射为稳定的摘要字符串，不打印密码。

#### `async def check_all(settings: Settings) -> list[CheckResult]`

使用 `asyncio.gather(check_mysql(settings), check_redis(settings))` 并发检查两个远端依赖；返回顺序固定为 MySQL、Redis，便于 CLI 和测试断言。

## 7. Phase 1 检查 CLI

### 7.1 `scripts/check_phase1.py`

#### `async def run_checks() -> int`

1. 调用 `get_settings()`。
2. 打印 `redacted_summary()`。
3. 调用 `check_all()`。
4. 以表格形式打印每个依赖的 `OK/FAIL`、延迟和安全摘要。
5. 两项均成功返回 `0`，任一失败返回 `1`。

#### `def main() -> None`

使用 `asyncio.run(run_checks())`，并通过 `raise SystemExit(main())` 提供模块入口。未捕获异常只能输出通用错误，不输出 `.env` 内容。

## 8. LiteLLM 配置文件

### 8.1 `config.yaml`

配置必须包含以下模型别名，并确保 fallback 引用的每个别名都存在于 `model_list`：

```yaml
model_list:
  - model_name: gpt-4o
    litellm_params:
      model: openai/gpt-4o
      api_key: os.environ/OPENAI_API_KEY
  - model_name: gpt-4o-mini
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY
  - model_name: gemini-1.5-pro
    litellm_params:
      model: vertex_ai/gemini-1.5-pro
      vertex_project: os.environ/VERTEXAI_PROJECT
      vertex_location: os.environ/VERTEXAI_LOCATION
  - model_name: gemini-1.5-flash
    litellm_params:
      model: vertex_ai/gemini-1.5-flash
      vertex_project: os.environ/VERTEXAI_PROJECT
      vertex_location: os.environ/VERTEXAI_LOCATION
  - model_name: claude-3-5-sonnet
    litellm_params:
      model: anthropic/claude-3-5-sonnet-20240620
      api_key: os.environ/ANTHROPIC_API_KEY

router_settings:
  fallbacks:
    - gpt-4o: [gemini-1.5-pro, gemini-1.5-flash, gpt-4o-mini, claude-3-5-sonnet]
  allowed_fails: 2
  cooldown_time: 30
  num_retries: 1
```

启用 Redis Response Cache 时：

- `REDIS_HOST`、`REDIS_PORT` 和 `REDIS_PASSWORD` 必须从环境变量读取。
- `supported_call_types` 至少包含 `chat_completion`。
- TTL 初始值为 `3600` 秒。
- Redis 连接超时必须小于 API 请求超时，Redis 故障时允许跳过缓存，不得阻止 Proxy 启动。

Phase 1 不在 `config.yaml` 中配置费用数据库 callback 或 `database_url`；那部分要等 Phase 2 的表结构和异步 Hook 一起实现。

## 9. Proxy 烟囱测试脚本

### 9.1 `scripts/smoke_proxy.py`

#### `async def get_health(client: httpx.AsyncClient) -> dict`

调用 `GET /health`，要求 HTTP 2xx；返回状态码和 JSON 摘要，不打印任何密钥。

#### `async def get_models(client: httpx.AsyncClient) -> set[str]`

调用 `GET /v1/models`，解析 `data[].id`，返回模型别名集合；响应格式错误时抛出可读的校验异常。

#### `async def send_chat(client: httpx.AsyncClient, model: str, prompt: str) -> dict`

只有显式传入 `--send-chat` 才调用。发送最小请求：一个 user message、低 `max_tokens`，记录响应状态、模型名和 usage 是否存在；禁止把完整响应内容写入日志。

#### `async def run(base_url: str, master_key: str, model: str | None, send_chat: bool) -> int`

按顺序执行 health、models、可选 chat；默认不产生上游 LLM 费用。缺少目标模型时返回 `1`，全部成功返回 `0`。

#### `def main() -> None`

解析 `--base-url`、`--model`、`--send-chat` 参数，读取 `LITELLM_MASTER_KEY`，使用 `asyncio.run(run(...))` 启动。

## 10. 单元测试

### 10.1 `tests/test_config.py`

#### `def test_settings_load_from_environment(monkeypatch)`

注入最小合法环境变量，断言主机、端口和 Proxy 端口读取正确；断言 Secret 不会出现在 `redacted_summary()` 中。

#### `def test_settings_rejects_invalid_port(monkeypatch)`

注入非数字或越界端口，断言 `ValidationError`。

#### `def test_redacted_summary_does_not_expose_secrets(monkeypatch)`

使用标记字符串作为密码，断言摘要中不存在该字符串。

### 10.2 `tests/test_connectivity.py`

所有网络客户端必须通过 monkeypatch/mock 注入，单元测试不得访问真实 MySQL、Redis 或 LLM。

#### `async def test_check_mysql_success(mock_aiomysql)`

模拟 `SELECT 1` 返回 `(1,)`，断言 `ok is True` 并验证连接关闭。

#### `async def test_check_mysql_auth_failure(mock_aiomysql)`

模拟认证异常，断言 `ok is False` 且 detail 不含密码。

#### `async def test_check_redis_success(mock_redis)`

模拟 `ping()` 返回真值，断言客户端执行关闭。

#### `async def test_check_all_has_stable_order(mock_dependencies)`

模拟两个依赖成功，断言返回顺序为 `mysql`、`redis`。

## 11. 实际执行顺序

1. 确认 GCE VM 的 Tailscale 状态为 running；确认路由可达 `10.0.0.247` 和 `100.105.130.0`。
2. 执行 `uv venv --python 3.12`、`uv lock` 和 `uv sync --dev`，安装 `pyproject.toml` 依赖。
3. 从 `.env.example` 复制 `.env`，只在本机填入真实连接信息。
4. 运行 `uv run python -m scripts.check_phase1`，先修复网络、账号或认证问题，再继续。
5. 运行 `uv run pytest -q` 与 `uv run ruff check app scripts tests`。
6. 使用 LiteLLM 的版本对应命令执行配置校验；若版本支持，先运行 `litellm --config config.yaml --port 4000 --detailed_debug` 的启动前检查。
7. 启动 Proxy：

   ```bash
   litellm --config config.yaml --port "${LITELLM_PORT:-4000}"
   ```

8. 另开终端运行 `uv run python -m scripts.smoke_proxy --base-url http://127.0.0.1:4000`。
9. 只在确认供应商额度和模型选择后，显式运行 `uv run python -m scripts.smoke_proxy --send-chat --model gpt-4o-mini` 做一次付费请求验收。
10. 记录 Proxy 启动日志、健康检查结果和模型列表；日志不得包含 API Key、Redis 密码或 MySQL 密码。

## 12. Phase 1 不做的事情

- 不执行 MySQL `CREATE TABLE`；DDL 属于 Phase 2。
- 不配置 LiteLLM 成本数据库 callback；避免在表结构未确定时产生不可追踪数据。
- 不创建 FastAPI 监听端口 `8000` 的服务；FastAPI 属于 Phase 3。
- 不在 GCE、本地 Docker 或 OCI VM 重新部署 Redis；只复用现有 K3s Redis。
- 不用真实故障反复触发 fallback；先做静态配置检查和一次受控演练。
- 不把真实 Secret 写入 YAML、Python 源码、测试 fixture 或 Git 历史。

## 13. 验收记录模板

在实施时将以下结果保存到本地工作记录，不提交任何敏感值：

```text
Date:
Git commit:
Tailscale: PASS/FAIL
OCI MySQL SELECT 1: PASS/FAIL (latency_ms=)
Existing Redis AUTH + PING: PASS/FAIL (latency_ms=)
LiteLLM /health: PASS/FAIL
LiteLLM /v1/models: PASS/FAIL
Controlled chat request: PASS/FAIL (model=, request_id=)
Fallback static validation: PASS/FAIL
pytest: PASS/FAIL
ruff: PASS/FAIL
```
