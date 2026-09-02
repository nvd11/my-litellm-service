# Phase 1 Low-Level Implementation Plan: Infrastructure Integration & LiteLLM Proxy Startup

> **Goal**: Complete network verification in the K3s deployment environment, connect to existing OCI MySQL and K3s Redis instances, configure LiteLLM Proxy model routing/fallbacks/caching, and complete startup acceptance without incurring LLM costs.

> **Phase Boundary**: This phase only verifies basic connectivity to the database and Redis, and starts the LiteLLM Proxy. Asynchronous request cost logging, MySQL business tables, FastAPI, evaluation engine, and formal rate limiting strategies belong to Phase 2/3/4 respectively, and are not implemented in this phase.

## 1. Phase 1 Acceptance Criteria

Upon completion, the following criteria must be met:

1. K3s application nodes can access OCI MySQL `10.0.0.247:3306` and existing Redis `100.105.130.0:6379` (or in-cluster Redis Service).
2. MySQL executes `SELECT 1` successfully with real credentials; Redis executes authenticated `PING` returning `PONG`.
3. LiteLLM Proxy starts on `:4000`, and `GET /health` returns success.
4. `/v1/models` displays the model aliases declared by the project.
5. An OpenAI-compatible chat completion request is executed with a clearly specified model and verified for correct response structure; this request is a paid call and must be explicitly executed.
6. When the primary model fails, LiteLLM attempts fallback models according to configuration; this phase requires at least static configuration checks, while real fault drills must strictly control costs.
7. All passwords, API keys, and database connection strings reside solely in the untracked `.env` file; the repository maintains only placeholders.

## 2. Directory and File Inventory

After Phase 1 implementation, new or modified files must be strictly limited to:

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

This phase does not create `app/main.py`, `app/routers/`, `app/eval/`, or database hooks; these files belong to the FastAPI/Auditing phases.

## 3. Python Dependencies & Virtual Environment

### 3.1 `pyproject.toml`

Use Python 3.12, utilizing `uv` for dependency resolution and installation; project metadata and dependency declarations are consolidated in `pyproject.toml`, with locked results written to `uv.lock`.

Runtime dependencies:

- `litellm`: Proxy CLI and model routing.
- `pydantic-settings`: Reads `.env` and validates configurations.
- `aiomysql`: Asynchronously executes MySQL connectivity checks.
- `redis[hiredis]`: Async Redis client with fast RESP parsing.
- `httpx`: Invokes LiteLLM health checks, model lists, and chat endpoints.

Development dependencies using PEP 735 dependency groups:

- `pytest`, `pytest-asyncio`: Unit testing.
- `ruff`: Static code analysis and linting.
- `pyyaml`: Reads and statically checks `config.yaml`.

`pyproject.toml` must contain at least `[project]`, `[dependency-groups]`, and `[tool.ruff]`; PyPI package publishing configuration is not required.

The following `uv run` commands must be supported:

```bash
uv run python -m scripts.check_phase1
uv run python -m scripts.smoke_proxy --base-url http://127.0.0.1:4000
uv run pytest -q
uv run ruff check app scripts tests
```

### 3.2 Virtual Environment

Create a single virtual environment `.venv` at the repository root, shared across LiteLLM Proxy, future FastAPI services, and connectivity check scripts. Do not create separate virtualenvs for the two service processes. The two services start via their respective entrypoints: LiteLLM uses `.venv/bin/litellm`, FastAPI uses `.venv/bin/uvicorn`. Manage environment and lockfiles with `uv`:

```bash
uv venv --python 3.12
uv lock
uv sync --dev
```

Daily operations execute via `uv run ...` without requiring manual `source .venv/bin/activate`. If entering an interactive shell is needed, execute `source .venv/bin/activate`.

## 4. Environment Variable Template

### 4.1 `.env.example`

Declare variable names with placeholder values only:

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

The real `.env` must be added to `.gitignore`. Passwords must never appear in `config.yaml`, Python defaults, test fixtures, logs, or commit messages.

## 5. Configuration Loading Module

### 5.1 `app/core/config.py`

This file is responsible solely for configuration parsing and validation; it does not perform network connections.

#### `class Settings(BaseSettings)`

Fields and constraints:

- `mysql_host: str`, `mysql_port: int`, `mysql_user: str`, `mysql_password: SecretStr`, `mysql_db: str`.
- `redis_host: str`, `redis_port: int`, `redis_password: SecretStr`.
- `openai_api_key: SecretStr`, `anthropic_api_key: SecretStr`.
- `vertexai_project: str`, `vertexai_location: str`.
- `litellm_master_key: SecretStr`, `litellm_port: int = 4000`.
- `connect_timeout_seconds: float = 5.0`.

The configuration class must use `SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')`, rejecting invalid ports and empty required values via field types.

#### `get_settings() -> Settings`

Uses `functools.lru_cache` to return a singleton configuration instance, preventing each check function from re-reading `.env`. Tests clear cache via `get_settings.cache_clear()`.

#### `redacted_summary(settings: Settings) -> dict[str, object]`

Returns safely printable hosts, ports, database names, and proxy ports; all `SecretStr` fields output `***` only, never calling `get_secret_value()` in logs.

## 6. Infrastructure Connectivity Module

### 6.1 `app/core/connectivity.py`

This file provides read-only health checks and guarantees connection failures do not leak passwords or full connection strings.

#### `@dataclass(frozen=True) class CheckResult`

Fields: `name: str`, `ok: bool`, `latency_ms: float | None`, `detail: str`. `detail` permits whitelisted error summaries only, e.g., `connected`, `authentication_failed`, `timeout`, `connection_refused`.

#### `async def check_mysql(settings: Settings) -> CheckResult`

Implementation requirements:

1. Establish connection using `aiomysql.connect(host=..., port=..., user=..., password=..., db=..., connect_timeout=...)`.
2. Create cursor, executing fixed parameter `SELECT 1`; do not execute table creation, writes, or transaction commits.
3. Fetch one row and confirm the result equals `1`.
4. Close cursor and connection in `finally` block.
5. Map `OperationalError` to `connection_refused`, `timeout`, or `authentication_failed`; map other exceptions to `unexpected_error`, omitting raw exception text containing passwords or DSNs.
6. Calculate millisecond latency using `time.perf_counter()`.

#### `async def check_redis(settings: Settings) -> CheckResult`

Implementation requirements:

1. Create client using `redis.asyncio.Redis(host=..., port=..., password=..., socket_connect_timeout=..., socket_timeout=..., decode_responses=True)`.
2. Execute `await client.ping()`, which must evaluate to true.
3. Execute `await client.aclose()` in `finally` block.
4. Map authentication errors, timeouts, and refused connections to stable summary strings without printing passwords.

#### `async def check_all(settings: Settings) -> list[CheckResult]`

Use `asyncio.gather(check_mysql(settings), check_redis(settings))` to concurrently check both remote dependencies; return order is fixed as MySQL, Redis for predictable CLI and test assertions.

## 7. Phase 1 Verification CLI

### 7.1 `scripts/check_phase1.py`

#### `async def run_checks() -> int`

1. Call `get_settings()`.
2. Print `redacted_summary()`.
3. Call `check_all()`.
4. Print tabular output with `OK/FAIL`, latency, and safety summaries for each dependency.
5. Return `0` if both checks succeed; return `1` if either fails.

#### `def main() -> None`

Invoke `asyncio.run(run_checks())` and provide module entrypoint via `raise SystemExit(main())`. Uncaught exceptions must output generic errors without displaying `.env` contents.

## 8. LiteLLM Configuration File

### 8.1 `config.yaml`

The configuration must declare the following model aliases, ensuring every alias referenced in fallbacks exists in `model_list`:

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

When enabling Redis Response Cache:

- `REDIS_HOST`, `REDIS_PORT`, and `REDIS_PASSWORD` must be read from environment variables.
- `supported_call_types` must contain at least `chat_completion`.
- Initial TTL is set to `3600` seconds.
- Redis connection timeout must be strictly less than API request timeout; on Redis failure, caching is bypassed without blocking Proxy startup.

Phase 1 does not configure cost database callbacks or `database_url` in `config.yaml`; that will be implemented alongside Phase 2 table schemas and async hooks.

## 9. Proxy Smoke Test Script

### 9.1 `scripts/smoke_proxy.py`

#### `async def get_health(client: httpx.AsyncClient) -> dict`

Calls `GET /health`, requiring HTTP 2xx; returns status code and JSON summary without printing keys.

#### `async def get_models(client: httpx.AsyncClient) -> set[str]`

Calls `GET /v1/models`, parses `data[].id`, and returns a set of model aliases; raises readable validation errors on malformed responses.

#### `async def send_chat(client: httpx.AsyncClient, model: str, prompt: str) -> dict`

Invoked only when `--send-chat` is explicitly passed. Sends minimal request: single user message, low `max_tokens`; records response status, model name, and usage presence; writing full response bodies to logs is prohibited.

#### `async def run(base_url: str, master_key: str, model: str | None, send_chat: bool) -> int`

Executes health, models, and optional chat in sequence; incurs zero upstream LLM cost by default. Returns `1` if target model is missing, `0` if all pass.

#### `def main() -> None`

Parses `--base-url`, `--model`, `--send-chat` arguments, reads `LITELLM_MASTER_KEY`, and starts via `asyncio.run(run(...))`.

## 10. Unit Tests

### 10.1 `tests/test_config.py`

#### `def test_settings_load_from_environment(monkeypatch)`

Injects minimal valid environment variables, asserting host, port, and proxy port load correctly; asserts Secrets do not appear in `redacted_summary()`.

#### `def test_settings_rejects_invalid_port(monkeypatch)`

Injects non-numeric or out-of-range ports, asserting `ValidationError`.

#### `def test_redacted_summary_does_not_expose_secrets(monkeypatch)`

Uses sentinel string as password, asserting the string does not exist in summary.

### 10.2 `tests/test_connectivity.py`

All network clients must be injected via monkeypatch/mock; unit tests must not access real MySQL, Redis, or LLM endpoints.

#### `async def test_check_mysql_success(mock_aiomysql)`

Mocks `SELECT 1` returning `(1,)`, asserting `ok is True` and verifying connection is closed.

#### `async def test_check_mysql_auth_failure(mock_aiomysql)`

Mocks authentication error, asserting `ok is False` and detail contains no password.

#### `async def test_check_redis_success(mock_redis)`

Mocks `ping()` returning truthy, asserting client executes close.

#### `async def test_check_all_has_stable_order(mock_dependencies)`

Mocks both dependencies succeeding, asserting return order is `mysql`, `redis`.

## 11. Execution Sequence

1. Verify network/Tailscale status on the node hosting the application Pod; confirm routes to `10.0.0.247` and `100.105.130.0` or Redis in-cluster Service are reachable.
2. Run `uv venv --python 3.12`, `uv lock`, and `uv sync --dev` to install dependencies from `pyproject.toml`.
3. Copy `.env` from `.env.example`, populating real connection credentials locally only.
4. Run `uv run python -m scripts.check_phase1`; resolve any network, account, or authentication issues before proceeding.
5. Run `uv run pytest -q` and `uv run ruff check app scripts tests`.
6. Run configuration checks using the command corresponding to the LiteLLM version; if supported, run pre-startup validation with `litellm --config config.yaml --port 4000 --detailed_debug`.
7. Start the Proxy:

   ```bash
   litellm --config config.yaml --port "${LITELLM_PORT:-4000}"
   ```

8. In a separate terminal, run `uv run python -m scripts.smoke_proxy --base-url http://127.0.0.1:4000`.
9. Only after confirming provider quota and model selection, explicitly run `uv run python -m scripts.smoke_proxy --send-chat --model gpt-4o-mini` for paid acceptance.
10. Record Proxy startup logs, health check results, and model lists; logs must contain zero API keys, Redis passwords, or MySQL credentials.

## 12. Out of Scope for Phase 1

- No MySQL `CREATE TABLE` executions; DDL belongs to Phase 2.
- No LiteLLM cost database callback configuration; prevents untraceable data before schema finalization.
- No FastAPI service listening on port `8000`; FastAPI belongs to Phase 3.
- No re-deploying Redis inside application Pods, node Docker, or OCI VM; reuse existing K3s Redis only.
- No triggering fallbacks repeatedly with real faults; static configuration checks and a single controlled test suffice.
- No writing real Secrets to YAML, Python source, test fixtures, or Git history.

## 13. Acceptance Record Template

Save the following results to local records during execution without committing sensitive values:

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
