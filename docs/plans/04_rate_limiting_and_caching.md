# Module 4 Implementation Plan: Rate Limiting & Caching

> **Goal**: Connect LiteLLM Proxy to the existing K3s Redis 7+ instance to deliver low-latency API RPM/TPM rate limiting and exact prompt hash caching without deploying new Redis instances.

---

## 1. Architecture & Design Specification

### 1.1 Redis Responsibility Division
1. **Rate Limiting Bucket**:
   * Tracks per-minute requests (RPM) and token usage (TPM) per API Key or client IP.
   * Immediately rejects throttled requests at the gateway level with `HTTP 429 Too Many Requests`, preserving upstream provider quotas.
2. **Exact Prompt Cache**:
   * Computes cache keys as `sha256(model + prompt + temperature)`.
   * On cache hits, returns the cached JSON response from Redis, dropping latencies from >1000ms to <5ms at zero marginal cost.

---

## 2. Step-by-Step Implementation

### Step 1: Connect to Existing Redis Service
Reuse the existing K3s Redis instance without provisioning container-local Redis, host Docker Redis, or standalone `redis-server` instances. The Redis Pod runs on the OCI `free-arm-vm` node and is exposed through the existing Kong L4 TCP proxy; LiteLLM Pods connect via in-cluster Service DNS or Tailscale at `100.105.130.0:6379`.

Configure untracked `.env`:
```env
REDIS_HOST=100.105.130.0
REDIS_PORT=6379
REDIS_PASSWORD=load-f...n```

Verify connectivity before deployment with `AUTH + PING`; never commit Redis passwords to documentation, logs, or Git repositories.

### Step 2: Configure Redis Rate Limiting & Caching in `config.yaml`
```yaml
router_settings:
  redis_host: os.environ/REDIS_HOST
  redis_port: os.environ/REDIS_PORT
  enable_caching: true

litellm_settings:
  cache_type: "redis"
  redis_host: os.environ/REDIS_HOST
  redis_port: os.environ/REDIS_PORT
  redis_password: os.environ/REDIS_PASSWORD
  cache_params:
    supported_call_types: ["chat_completion"]
    ttl: 3600 # Default cache TTL: 1 hour

# Rate limiting configuration example
user_keys:
  - api_key: "***"
    max_budget: 10.0 # Maximum $10 budget cap
    rpm_limit: 10    # Max 10 requests per minute
    tpm_limit: 10000 # Max 10,000 tokens per minute
```

---

## 3. Verification & Acceptance Testing

1. **Cache Hit Verification**:
   * Issue the initial request, observing latency (e.g., `1200ms`).
   * Keeping the prompt identical, immediately dispatch the second request; observe latency (<10ms) and verify cache hit headers and logs.
2. **Rate Limiting Throttling Verification (429 Rate Limit)**:
   * Issue 12 sequential requests with `curl` or `ab` using the test key.
   * The first 10 requests return `200 OK`; the 11th and subsequent requests must be blocked with `HTTP 429`:
     ```json
     {
       "error": {
         "message": "Rate limit exceeded. RPM limit: 10",
         "type": "rate_limit_error"
       }
     }
     ```

---

## 4. Risk Control & Operational Constraints

* ⚠️ **Prompt Retention Lifecycle**: Enforce reasonable TTLs (e.g., 3600s) to prevent sensitive prompt payloads from lingering indefinitely in cache memory.
* ⚠️ **Redis Failure Degradation**: Configure strict Redis connection timeouts; if Redis, Tailscale, or Kong becomes unreachable, LiteLLM must gracefully degrade by bypassing cache and falling back to in-memory rate limiting without crashing the primary proxy service.
