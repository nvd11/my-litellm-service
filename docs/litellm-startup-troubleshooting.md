# LiteLLM Proxy Bootstrapping and API Integration Troubleshooting

This document details the troubleshooting process and resolutions during the initial local bootstrapping of `my-litellm-service`'s LiteLLM Proxy and its OpenAI-compatible invocations to Google Gemini.

Key technical areas covered: Python dependency isolation, `uv` environments, FastAPI version constraints, Redis network topologies, Tailscale routing, LiteLLM gateway authentication, health probes, token budget accounting, and Gemini thinking/reasoning quotas and 429 throttling.

## 1. LiteLLM Proxy Startup Mechanism

In this architecture, LiteLLM is not bootstrapped via `python main.py`. LiteLLM Proxy is a CLI executable provided by the upstream package, residing at:

```text
.venv/bin/litellm
```

Recommended startup command:

```bash
cd /home/gateman/projects/github/my-litellm-service

uv run --env-file .env \
  litellm \
  --config config.yaml \
  2>&1 | tee -a /var/log/my-litellm-service/litellm.log
```

Here, `--env-file .env` injects environment variables into the LiteLLM child process:

```env
OPENAI_API_KEY_FREE_1=...
LITELLM_MASTER_KEY=...
REDIS_HOST=...
REDIS_PASSWORD=...
```

It does not mutate the active shell's environment variables. Testing endpoints with `curl` from a separate shell requires sourcing `.env` explicitly:

```bash
set -a
source .env
set +a
```

Otherwise, environment variables like `$LITELLM_MASTER_KEY` remain unbound.

Core distinction:

```text
uv --env-file .env → Injects variables into LiteLLM process
source .env       → Injects variables into current shell for curl
```

## 2. Issue 1: Missing LiteLLM Proxy Extra Dependencies

The initial dependency declaration:

```toml
"litellm>=1.74.0,<2.0.0"
```

Bootstrapping the proxy triggered:

```text
ModuleNotFoundError: No module named 'backoff'
```

LiteLLM's core SDK package does not bundle the full set of dependencies required by the Proxy server runtime.

Updated dependency declaration:

```toml
"litellm[proxy]>=1.74.0,<2.0.0"
```

Re-syncing the virtual environment:

```bash
uv lock
uv sync --dev
```

`litellm[proxy]` installs essential proxy dependencies including `backoff`, proxy server components, Redis drivers, and web framework libraries.

## 3. Issue 2: FastAPI and LiteLLM Version Incompatibility

After installing proxy extras, launching LiteLLM encountered:

```text
ImportError: cannot import name 'get_flat_dependant'
from fastapi.dependencies.utils
```

Version inspection:

```text
LiteLLM 1.97.0
FastAPI 0.141.1
```

LiteLLM Proxy still imported `get_flat_dependant`, which was deprecated and removed in recent FastAPI releases.

Pinned FastAPI to a compatible release:

```toml
"fastapi>=0.136.3,<0.137.0"
```

Re-locking and syncing:

```bash
uv lock
uv sync --dev
```

Verification:

```bash
.venv/bin/python -c \
  'from fastapi.dependencies.utils import get_flat_dependant; print("compatible")'
```

LiteLLM initialized successfully:

```text
Application startup complete.
Uvicorn running on http://0.0.0.0:4000
```

Key Takeaway: Always pin FastAPI/Starlette/Uvicorn versions according to LiteLLM Proxy's specific framework dependencies.

## 4. Redirecting Logs to File

By default, LiteLLM streams logs to stdout. To persist logs:

```bash
2>&1 | tee -a /var/log/my-litellm-service/litellm.log
```

If directory creation fails (`No such file or directory`), configure directory ownership:

```bash
sudo mkdir -p /var/log/my-litellm-service
sudo chown gateman:gateman /var/log/my-litellm-service
sudo chmod 750 /var/log/my-litellm-service
```

Startup command:

```bash
uv run --env-file .env \
  litellm --config config.yaml \
  2>&1 | tee -a /var/log/my-litellm-service/litellm.log
```

LiteLLM runs in the foreground; holding the terminal session is expected behavior. The service is ready when logging:

```text
Application startup complete.
Uvicorn running on http://0.0.0.0:4000
```

## 5. Redis Cache Configuration & Networking

The `config.yaml` enables LiteLLM's native Redis Response Cache:

```yaml
litellm_settings:
  cache: true
  cache_params:
    type: redis
    host: os.environ/REDIS_HOST
    port: os.environ/REDIS_PORT
    password: os.environ/REDIS_PASSWORD
    supported_call_types: [chat_completion]
    ttl: 3600
```

### 5.1 Redis Infrastructure Location

Redis is hosted on the OCI `free-arm-vm` node in the Tencent K3s cluster:

```text
free-arm-vm
└── Redis Pod
```

Cluster verification:

```text
free-arm-vm        Ready
Redis Pod          Running
Redis Service      6379
```

Tailscale IP of the node:

```text
100.105.130.0
```

### 5.2 Correcting Host IP Configuration

Initial configuration erroneously pointed to:

```env
REDIS_HOST=100.104.150.19
```

This IP belonged to a NUC node rather than the OCI Redis host. Corrected to:

```env
REDIS_HOST=100.105.130.0
REDIS_PORT=6379
```

### 5.3 Local Connection Timeout Resolution

Probing `100.105.130.0:6379` from the local development host timed out because the host lacked an active Tailscale route.

Installed and enabled Tailscale locally:

```text
tailscaled: active
Startup: enabled
Tailscale IP: 100.121.12.126
```

LiteLLM can now seamlessly route to the OCI Redis instance over the Tailscale mesh.

### 5.4 Kong / KIC and Redis Routing

When LiteLLM runs inside K3s, connect via in-cluster DNS (`redis.redis.svc.cluster.local`). For external nodes connected over Tailscale, connect directly to `100.105.130.0:6379`.

Never expose Redis directly to the public internet; public exposure is reserved exclusively for the LiteLLM API gateway.

## 6. `Setting Cache on Proxy` Does Not Mean Connected

Seeing:

```text
Setting Cache on Proxy
```

Indicates LiteLLM is initializing cache handlers. If Redis is unreachable, subsequent logs display:

```text
Timeout connecting to server
Error connecting to Sync Redis client
```

In such cases:
- LiteLLM Proxy starts up successfully;
- Cache features are disabled or gracefully degraded.

Validate active connectivity using authenticated checks:

```bash
set -a
source .env
set +a

uv run python -m scripts.check_phase1
```

Expected output:

```text
redis      | OK       | ... | connected
```

## 7. Differentiating API Keys

The system manages two distinct API key domains:

```env
OPENAI_API_KEY_FREE_1 = Upstream Gemini API Key
LITELLM_MASTER_KEY     = LiteLLM Gateway Authentication Key
```

Invocation flow:

```text
Client
  (Passes LITELLM_MASTER_KEY)
      ↓
LiteLLM Proxy
  (Passes OPENAI_API_KEY_FREE_1)
      ↓
Gemini API
```

Clients must query LiteLLM with:

```http
Authorization: Bearer $LITELLM_MASTER_KEY
```

### 7.1 Placeholder Master Key Error

Leaving a placeholder value in `.env`:

```text
replace-with-private-master-key
```

Causes LiteLLM to throw:

```text
Malformed API Key passed in.
```

Generate a valid `sk-...` token and restart LiteLLM.

### 7.2 Header Formatting Syntax

Ensure header strings do not append trailing typos (e.g. `-H "Authorization: Bearer $KEY"1`), which invalidates the Bearer token.

## 8. Root Cause of 500 Errors on `/health` and `/v1/models`

Anonymous requests:

```bash
curl http://127.0.0.1:4000/health
```

Trigger:

```text
No api key passed in.
```

Followed by an error in the exception handler attempting to load Prisma:

```text
ModuleNotFoundError: No module named 'prisma'
```

Returning:

```json
{"type":"internal_server_error"}
```

The root cause was unauthenticated access rather than database or Redis failures; the Prisma error was a secondary exception in error-handling code.

Authenticated invocation:

```bash
set -a
source .env
set +a

curl http://127.0.0.1:4000/v1/models \
  -H "Authorization: Bearer $LITEL...KEY"
```

Returns:

```json
{
  "data": [
    {
      "id": "gemini-3.7-flash"
    }
  ],
  "object": "list"
}
```

## 9. Model Aliases vs. Upstream Provider Names

In `config.yaml`:

```yaml
model_list:
  - model_name: gemini-3.6-flash-freelayer
    litellm_params:
      model: gemini/gemini-3.6-flash
      api_key: os.environ/OPENAI_API_KEY_FREE_1
```

The client requests:

```text
gemini-3.6-flash-freelayer
```

LiteLLM dispatches to the upstream provider model:

```text
gemini/gemini-3.6-flash
```

The `freelayer` suffix is an arbitrary project alias and does not modify Gemini quota rules.

## 10. `max_tokens` and Gemini Reasoning/Thinking Tokens

Issuing requests with:

```json
"max_tokens": 128
```

Resulted in:

```text
finish_reason: length
content: [Truncated / Incomplete]
```

Gemini 3.x reasoning tokens count against the `max_tokens` allocation. Increasing the limit to:

```json
"max_tokens": 1024
```

Allowed completions to finish cleanly:

```text
finish_reason: stop
```

Token breakdown:

```json
{
  "completion_tokens": 553,
  "reasoning_tokens": 526,
  "text_tokens": 27
}
```

`thought_signatures` in the payload represent cryptographic reasoning metadata and are not corrupted characters.

## 11. Cost Map Warnings

During startup:

```text
model=... not in built-in cost map
cache cost fields will default to 0
```

This warning affects cache cost analytics only and does not impact proxy routing or upstream inference.

## 12. End-to-End Validation Commands

### 12.1 Query Model Catalog

```bash
set -a
source .env
set +a

curl http://127.0.0.1:4000/v1/models \
  -H "Authorization: Bearer $LITEL...KEY"
```

### 12.2 Chat Completions

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITEL...KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.6-flash-freelayer",
    "messages": [
      {"role": "user", "content": "Hello, please introduce yourself in one sentence."}
    ],
    "max_tokens": 1024
  }'
```

### 12.3 Extract Completion Content

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITEL...KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.6-flash-freelayer",
    "messages": [
      {"role": "user", "content": "Reply with exactly: OK"}
    ],
    "max_tokens": 1024
  }' | jq -r '.choices[0].message.content'
```

## 13. Handling 429 Rate Limits

`429 Too Many Requests` originates upstream from provider quota exhaustion (RPM/TPM limits).

When only a single model entry is declared, LiteLLM has no fallback target and surfaces the 429 directly to the client. Multi-tier fallback routing with multiple keys must be configured in `router_settings` to guarantee high availability.

## 14. Summary

Validated execution chain:

```text
curl
  → LiteLLM Proxy :4000
  → LITELLM_MASTER_KEY Gateway Auth
  → gemini-3.6-flash-freelayer Model Alias
  → gemini/gemini-3.6-flash Provider
  → Gemini API
```

Core Conclusions:
- `litellm[proxy]` is required for the proxy runtime.
- FastAPI versions must align with LiteLLM Proxy constraints.
- Redis runs on the OCI node and routes across clusters via Tailscale.
- Redis operates as an exact response cache.
- Gateway Master Key and Provider API Key are distinct credentials.
- Requests to `/v1/models` and `/v1/chat/completions` require Master Key authentication.
- Gemini 3.x reasoning tokens consume `max_tokens` quota.
- 429 mitigation requires multi-tier fallback routing.
