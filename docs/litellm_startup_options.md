# LiteLLM Proxy Startup Options & Python Environment Management

This document outlines the startup strategies for LiteLLM Proxy in `my-litellm-service`, explaining Python virtual environments, `uv`, `pip`, and package installation mechanics.

Commands are assumed to run from the repository root:

```bash
cd /home/gateman/projects/github/my-litellm-service
```

Project structure:

```text
my-litellm-service/
├── app/                    # Python code for FastAPI Service B
├── config.yaml             # LiteLLM Proxy model and cache configuration
├── pyproject.toml          # Project metadata and dependency definitions
├── .env                    # Local secrets (excluded from Git)
├── .env.example            # Environment template
└── .venv/                  # Virtual environment
```

## 1. LiteLLM Proxy vs. `main.py`

LiteLLM Proxy is an upstream gateway application. It is distinct from `app/main.py`. Bootstrapping LiteLLM uses the installed CLI entrypoint:

```bash
litellm --config config.yaml --port 4000
```

The system hosts two distinct workloads:

```text
Service A: LiteLLM Proxy
Startup: litellm --config config.yaml --port 4000
Responsibilities: Model routing, API normalization, caching, failover

Service B: FastAPI
Startup: uvicorn app.main:app --port 8000
Responsibilities: Benchmark evaluation, spend metrics, business APIs
```

Running `python main.py` will not start LiteLLM Proxy.

## 2. Why `uv run` Is Preferred

The project relies on `uv` for dependency management and virtual environments. The standard invocation is:

```bash
uv run litellm --config config.yaml --port 4000
```

Benefits of `uv run`:
- Executes within the project's `.venv`
- Enforces dependencies from `pyproject.toml`
- Eliminates manual `source .venv/bin/activate` steps
- Avoids leaking into global system Python
- Guarantees reproducibility via `uv.lock`

With port variables in `.env`:

```bash
set -a
source .env
set +a

uv run litellm --config config.yaml --port "$LITELLM_PORT"
```

Port definition:

```env
LITELLM_PORT=4000
```

Avoid declaring conflicting port definitions across `.env`, `config.yaml`, and CLI flags. Store the port in `.env` and pass it to LiteLLM via `--port`.

## 3. Startup Without `uv`

`uv` is not a runtime requirement for LiteLLM. Any standard Python virtual environment can run it once dependencies are installed.

### 3.1 Activating the Virtual Environment

```bash
source .venv/bin/activate

litellm --config config.yaml --port 4000
```

Once activated, `python`, `pip`, and `litellm` resolve to:

```text
/home/gateman/projects/github/my-litellm-service/.venv/bin/
```

Verify with:

```bash
which python
which pip
which litellm
```

### 3.2 Direct Absolute Path Invocation

Invoke the binary directly without modifying shell `$PATH`:

```bash
.venv/bin/litellm --config config.yaml --port 4000
```

Ideal for systemd units, container entrypoints, and CI automation scripts.

### 3.3 Bootstrapping Fresh Virtual Environments with pip

If `.venv` does not exist:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

Install the project in editable mode:

```bash
python -m pip install -e .
```

Start the proxy:

```bash
litellm --config config.yaml --port 4000
```

Using `python -m pip` ensures packages are installed to the exact Python interpreter running the command.

## 4. Understanding `pip install`

`pip install` does more than downloading pre-built wheels from PyPI.

Executing:

```bash
pip install .
```

Instructs pip to parse `pyproject.toml` in the current working directory (`.`) to:
1. Build and install the local package
2. Resolve runtime dependencies
3. Install third-party packages (including LiteLLM)
4. Generate console script entrypoints

Project dependencies:

```toml
dependencies = [
    "aiomysql>=0.2.0,<1.0.0",
    "fastapi>=0.115.0,<1.0.0",
    "httpx>=0.27.0,<1.0.0",
    "litellm>=1.74.0,<2.0.0",
    "pydantic-settings>=2.6.0,<3.0.0",
    "redis[hiredis]>=5.2.0,<7.0.0",
    "uvicorn[standard]>=0.32.0,<1.0.0",
]
```

Running `pip install .` generates the proxy executable in:

```text
.venv/bin/litellm
```

## 5. Editable Installs: `pip install -e .`

The `-e` flag stands for `--editable`:

```bash
pip install -e .
```

This installs dependencies while creating a `.pth` link to the local source directory rather than copying static files into `site-packages`.

```text
pip install .    = Installs static copy into site-packages
pip install -e . = Links source directory for live development
```

Edits to `app/core/config.py` take effect immediately without requiring reinstallation.

Editable vs. Standard Installation:

| Command | Project Code | On Code Modification | Typical Use Case |
|---|---|---|---|
| `pip install .` | Standard build installed | Requires reinstallation | Production / Packaging |
| `pip install -e .` | Symlinked source directory | Live updates | Local Development |

Always ensure `.venv` is active or use `.venv/bin/python -m pip install -e .`.

## 6. Why Install the Project If Imports Work Locally?

Running:

```bash
python -c "from app.core.config import Settings; print(Settings)"
```

From the repository root works because Python appends CWD to `sys.path`.

However:
- Source visibility does not mean dependencies are satisfied;
- Running from another directory (e.g., `/tmp`) fails unless the package is installed in the virtual environment or `PYTHONPATH` is set.

To run reliably from arbitrary paths:

```bash
/home/gateman/projects/github/my-litellm-service/.venv/bin/python script.py
```

Or using `uv`:

```bash
uv run --project /home/gateman/projects/github/my-litellm-service python script.py
```

## 7. Development Dependencies: `dependency-groups` vs. `.[dev]`

Project development dependencies:

```toml
[dependency-groups]
dev = [
    "pytest>=8.3.0,<9.0.0",
    "pytest-asyncio>=0.24.0,<2.0.0",
    "pyyaml>=6.0.0,<7.0.0",
    "ruff>=0.8.0,<1.0.0",
]
```

With `uv`:

```bash
uv sync --dev
```

`pip install -e ".[dev]"` targets `[project.optional-dependencies]`, not `[dependency-groups]`.

Without `uv`:

```bash
python -m pip install -e .
python -m pip install pytest pytest-asyncio pyyaml ruff
```

Configuration comparison:

| Configuration Table | Recommended Command |
|---|---|
| `[dependency-groups].dev` | `uv sync --dev` |
| `[project.optional-dependencies].dev` | `pip install -e ".[dev]"` |

## 8. Gemini Configuration & Proxy Routing

LiteLLM targets the Google Gemini API:

```yaml
model_list:
  - model_name: gemini-3.7-flash
    litellm_params:
      model: gemini/gemini-3.7-flash
      api_key: os.environ/OPENAI_API_KEY_FREE_1
```

The environment variable `OPENAI_API_KEY_FREE_1` holds the Gemini API Key. The upstream provider is determined by `model: gemini/gemini-3.7-flash`.

When routing outbound traffic through a local proxy:

```env
HTTP_PROXY=http://10.0.1.105:7890
HTTPS_PROXY=http://10.0.1.105:7890
NO_PROXY=localhost,127.0.0.1,10.0.0.0/8,100.105.130.0
```

`10.0.0.0/8` ensures internal subnet traffic bypasses the proxy. No `api_base` override is required since standard HTTP/HTTPS transport proxies are used.

Export variables before launching:

```bash
set -a
source .env
set +a
```

## 9. Launching LiteLLM Proxy

### Option A: `uv run` (Recommended for Local Development)

```bash
cd /home/gateman/projects/github/my-litellm-service

set -a
source .env
set +a

uv run litellm \
  --config config.yaml \
  --port "$LITELLM_PORT"
```

### Option B: Active Virtual Environment

```bash
source .venv/bin/activate
set -a
source .env
set +a

litellm --config config.yaml --port "$LITELLM_PORT"
```

### Option C: Direct Path Invocation

```bash
set -a
source .env
set +a

.venv/bin/litellm \
  --config config.yaml \
  --port "$LITELLM_PORT"
```

### Option D: Out-of-Directory Execution via `uv`

```bash
uv run \
  --project /home/gateman/projects/github/my-litellm-service \
  litellm \
  --config /home/gateman/projects/github/my-litellm-service/config.yaml \
  --port 4000
```

## 10. OpenAI-Compatible API Verification

Verify local endpoints before invoking upstream providers:

### 10.1 Health Check

```bash
curl http://127.0.0.1:4000/health
```

### 10.2 Model Catalog

```bash
curl http://127.0.0.1:4000/v1/models \
  -H "Authorization: Bearer $LITEL...n### 10.3 Smoke Test Script

```bash
uv run python -m scripts.smoke_proxy \
  --base-url http://127.0.0.1:4000
```

To issue an live chat completion to Gemini:

```bash
uv run python -m scripts.smoke_proxy \
  --base-url http://127.0.0.1:4000 \
  --model gemini-3.7-flash \
  --send-chat
```

### 10.4 Direct cURL Completions

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer *** \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.7-flash",
    "messages": [
      {
        "role": "user",
        "content": "Reply with exactly: OK"
      }
    ],
    "max_tokens": 128
  }'
```

Request the external model alias (`gemini-3.7-flash`), not the upstream provider string (`gemini/gemini-3.7-flash`).

## 11. Real Gemini Integration Tests

Integration test suite:

```text
tests/test_real_gemini_conn.py
```

Skipped by default to prevent unintended spend. To execute:

```bash
set -a
source .env
set +a

RUN_REAL_GEMINI_TESTS=1 \
uv run pytest -q -s \
  tests/test_real_gemini_conn.py::test_real_gemini_chat_completion
```

The test validates:
- Reading Gemini credentials from `OPENAI_API_KEY_FREE_1`
- Dispatching to `gemini/gemini-3.7-flash`
- Inheriting HTTP/HTTPS proxy configuration
- Validating choices, model metadata, and usage objects
- Formatting failure logs for 429 and 5xx errors

`max_tokens` limits output tokens, not input context windows. The `timeout` parameter sets client wait thresholds to prevent tests from hanging on network partition.

## 12. Troubleshooting Common Issues

### 12.1 `litellm: command not found`
Virtualenv is unactivated or LiteLLM is not installed. Verify:

```bash
which python
python -m pip show litellm
```

Or invoke directly via `.venv/bin/litellm`.

### 12.2 Module Imports Succeed but LiteLLM Won't Start
Importing `app.core.config` only validates local source tree presence; running the proxy requires the complete `litellm[proxy]` package.

### 12.3 Tests Hang Without Returning
Common causes:
- Process failed to inherit proxy settings from `.env`
- Proxy host or port unreachable
- Upstream proxy does not support `HTTPS CONNECT`
- Upstream Gemini API timeout
- Missing client timeout parameters

Source `.env` before running tests in terminal sessions.

### 12.4 Truncated Outputs with `max_tokens=64`
Gemini 3 reasoning models consume `max_tokens` on internal thinking. If the budget is too low, the response finishes prematurely:

```text
finish_reason=length
content=None
```

Ensure tests allocate adequate output token budgets.

## 13. Summary Checklist

1. Run LiteLLM inside the dedicated `.venv`.
2. Keep sensitive `.env` files untracked in `.gitignore`.
3. Verify proxy environment variables are inherited when upstream proxies are needed.
4. Validate `/health` and `/v1/models` before triggering upstream inferences.
5. Explicitly control live API calls and pytest runs to prevent unexpected quota consumption.
