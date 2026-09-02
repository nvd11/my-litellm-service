# LiteLLM and Redis Architecture: From Placement to Exact Response Caching

Integrating Redis into LiteLLM Proxy often leads to two major misunderstandings:

First, Redis is not deployed by LiteLLM itself; it is an independent infrastructure service.

Second, LiteLLM natively provides exact response caching, not semantic-aware intelligent caching. Minor variations in request payloads result in distinct cache keys.

This document details how LiteLLM integrates with Redis in our project, outlining network topologies and operational boundaries.

## 1. Redis Architectural Placement

In our architecture, Redis is hosted within the Tencent Cloud K3s cluster on the OCI `free-arm-vm` worker node:

```text
Tencent K3s Cluster
└── OCI free-arm-vm
    └── Redis Pod
```

The Redis Pod is pinned to `free-arm-vm` using Kubernetes node affinity and persists data via K3s local storage. Redis is not deployed locally on the client host or inside the application source tree.

Verified cluster runtime state:

```text
Redis Pod: Running
Node: free-arm-vm
Redis Service: 6379
```

Deployment, authentication, persistence, PVC management, and health checks belong to the Kubernetes infrastructure tier; LiteLLM consumes Redis purely as a client.

## 2. LiteLLM Redis Configuration

The project `config.yaml` utilizes LiteLLM's native Redis Cache:

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

Configuration breakdown:

| Parameter | Meaning |
|---|---|
| `cache: true` | Enables LiteLLM response caching |
| `type: redis` | Stores cache entries in Redis |
| `host` | Loads Redis host from environment variables |
| `port` | Loads Redis port from environment variables |
| `password` | Loads Redis password from environment variables |
| `supported_call_types` | Caches chat completion requests |
| `ttl: 3600` | Sets cache time-to-live to 3600 seconds (1 hour) |

Sensitive credentials reside in uncommitted `.env` files:

```env
REDIS_HOST=100.105.130.0
REDIS_PORT=6379
REDIS_PASSWORD=...
```

Passwords must never be hardcoded into `config.yaml`, test scripts, or Git commits.

## 3. Does LiteLLM Manage Redis Automatically?

Yes. Provided LiteLLM Proxy's Redis credentials are valid and the network path is open, LiteLLM handles all client-side lifecycle operations automatically:

1. Establishes Redis connection pools.
2. Computes hash keys for incoming requests.
3. Checks Redis for cached responses.
4. Returns cached responses immediately upon cache hits.
5. Invokes upstream models upon cache misses.
6. Writes upstream responses to Redis.
7. Evicts expired entries based on TTL.

No custom Redis client code or caching wrappers are needed in `app/`.

However, LiteLLM does not manage Redis infrastructure. The following remain the responsibility of Kubernetes manifests and platform operations:

- Deploying Redis Pods
- Creating and mounting PVCs
- Configuring Redis authentication
- Managing RDB/AOF persistence
- Configuring memory limits and eviction policies (`maxmemory-policy`)
- Node scheduling and disaster recovery
- Backups, monitoring, and horizontal scaling

Distinguish clearly between:

```text
LiteLLM: Automatically consumes Redis
K3s/Redis: Operates and maintains Redis
```

## 4. Exact Matching vs. Semantic Matching

The default configuration operates as an exact string-hashed Redis Response Cache. It does not evaluate semantic similarity or invoke Embedding models for vector similarity search.

LiteLLM computes cache keys from all parameters affecting generation output:

- Model alias / model name
- `messages` / prompt contents
- System prompt
- Message ordering
- `temperature`
- `max_tokens`
- Tools / functions definitions
- `response_format`
- Additional generation hyperparameters

Even a single whitespace difference produces a distinct cache key:

```text
Request A: "Hello"
Request B: "Hello "
```

The following variations also trigger cache misses:

```text
Different model
Different system prompt
Different temperature
Different max_tokens
Altered messages order
```

Summary:

```text
Identical request parameters → Potential Cache Hit
Semantically similar queries  → Guaranteed Cache Miss
```

For instance:

```text
"What is the weather today?"
"How is the weather in Guangzhou today?"
```

These queries share similar intent, but will never share a cached response under exact matching.

## 5. Practical Value of Exact Response Caching

While exact caching has lower hit rates for open-ended conversational prompts, it provides immense value in specific engineering scenarios:

### 5.1 Duplicate Submissions
When users click submit multiple times or frontends retry due to timeouts, identical requests are served instantly from cache rather than re-querying upstream models.

### 5.2 Network Retries
When clients re-transmit requests due to transient transport glitches, exact caching prevents redundant token consumption.

### 5.3 Benchmarking and Eval Suites
Evaluation harnesses repeatedly execute static prompts with fixed parameters across model iterations. Exact caching yields huge latency and cost savings here.

### 5.4 Batch Processing & Structured Extraction
Enterprise pipelines (classification, summarization, JSON schema extraction) execute repetitive system prompts on structured inputs.

### 5.5 Latency & Cost Optimization
Upon a cache hit:
- Zero upstream API calls
- Zero upstream token quota consumed
- Zero model spend incurred
- Sub-5ms response latencies

The core purpose of Redis exact caching:

```text
Eliminate Redundant Duplicate Invocations
```

## 6. Why Not Semantic Caching Immediately?

Semantic caching requires complex pipeline additions:

```text
Prompt
  ↓
Embedding Model
  ↓
Vector Storage
  ↓
Similarity Search
  ↓
Reuse response only if similarity > threshold
```

This introduces significant overhead:
- Embedding model inference costs and latency
- Vector database / Redis Vector Search dependencies
- Dynamic similarity threshold calibration
- Vector index version management
- Cache invalidation complexity
- False-positive risks (hallucinating incorrect context)

Most critically:

```text
Semantic similarity does not guarantee answer interchangeability.
```

For example, two prompts querying weather may differ by city, date, or constraints. Loose thresholds return erroneous data, while strict thresholds degrade back into exact matching.

Starting with predictable exact caching is the right engineering choice. Semantic caching can be evaluated once traffic profiles warrant it.

## 7. Redis Network Paths

### 7.1 In-Cluster Internal Access

When LiteLLM runs inside K3s, route traffic via internal Kubernetes DNS rather than external IPs:

```env
REDIS_HOST=redis.redis.svc.cluster.local
REDIS_PORT=6379
```

In-cluster ClusterIP:

```text
10.43.120.222:6379
```

This IP is routable strictly within K3s.

### 7.2 Out-of-Cluster Nodes via Tailscale

For out-of-cluster nodes on the shared Tailscale network, connect directly to the OCI node Tailscale IP:

```text
100.105.130.0
```

Environment config:

```env
REDIS_HOST=100.105.130.0
REDIS_PORT=6379
```

Network path:

```text
LiteLLM
  → Tailscale
  → OCI free-arm-vm: 100.105.130.0
  → Redis Service / Redis Pod
```

Direct Tailscale routing to the OCI node provides the lowest cross-cloud latency without unnecessary multi-hop overlay traversal.

### 7.3 Workstation Setup (Main PC)

If a developer machine fails to reach `100.105.130.0:6379`, verify Tailscale routing:

```bash
sudo tailscale up
```

Once connected to the Tailscale mesh, `100.105.130.0:6379` becomes routable.

## 8. Kong Ingress Controller vs. Direct Redis Access

Kong Ingress Controller (KIC) reconciles Kubernetes manifests into Kong configurations, while Kong Proxy Services handle actual L4/L7 traffic.

For out-of-cluster Redis access, direct Tailscale routing to `free-arm-vm` is preferred over hairpinned Kong proxy hops.

Address distinctions:

```text
Redis ClusterIP
  Internal to K3s cluster only

Kong LoadBalancer / NodePort
  Proxies L4 TCP traffic

OCI free-arm-vm Tailscale IP
  Recommended direct cross-node path (100.105.130.0)
```

## 9. Never Expose Redis (6379) to the Public Internet

LiteLLM API is exposed publicly via HTTPS:

```text
Clients
  → Public HTTPS
  → Kong / Cloud Load Balancer
  → LiteLLM :4000
```

Redis must remain strictly internal:

```text
LiteLLM
  → Tailscale or K3s Service
  → Redis :6379
```

Never expose:

```text
Public_IP:6379
```

Password authentication alone is insufficient protection against brute-force attacks and protocol exploits. Public and private entrypoints must be strictly isolated:

```text
LiteLLM API: Public HTTPS for client consumers
Redis Cache: Private Tailscale/K3s for internal service use
```

## 10. Diagnosing Redis Connection Failures

When Redis is unreachable, LiteLLM logs:

```text
Error connecting to Sync Redis client
Timeout connecting to server
LiteLLM Redis Caching: async set() ... Timeout connecting to server
```

This will not necessarily prevent LiteLLM Proxy from starting up:

```text
LiteLLM Proxy: Starts successfully
Redis Config: Enabled
Redis Connection: Failed
Caching: Disabled / Graceful Fallback
```

Seeing:

```text
Setting Cache on Proxy
```

Means LiteLLM is initializing cache modules, not that Redis connectivity succeeded. Verify connectivity via authenticated probe:

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

Validating:

```text
AUTH + PING → PONG
```

## 11. Separation of Concerns: Redis vs. MySQL

Redis handles caching; MySQL handles durable audit logging:

```text
Gemini: Model inference
Redis: Ephemeral response caching
MySQL: Persistent financial cost auditing
```

Phase 1 verifies MySQL connectivity via `SELECT 1`. Phase 2 implements schema tables, token tracking, USD/CNY costs, and async database hooks.

Redis is an ephemeral cache with eviction policies and cannot substitute for an audit database.

## 12. Redis Rate Limiting Roadmap

Redis is well-suited for distributed rate limiting:
- RPM (Requests Per Minute)
- TPM (Tokens Per Minute)
- Virtual API Key counters
- Temporary quota tracking
- Cross-pod shared state

Phase 1 enables Redis Response Caching (`supported_call_types: [chat_completion]`). Rate limiting policies are layered in subsequently.

## 13. Verifying Cache Hits

Submit identical requests sequentially:

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITEL...KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.7-flash",
    "messages": [
      {"role": "user", "content": "Reply with exactly: OK"}
    ],
    "max_tokens": 128
  }'
```

On the second request, observe:
- Significant drop in response latency (<10ms)
- Absence of upstream model logs
- LiteLLM logging `cache_hit: true`
- Existence of matching keys in Redis

## 14. Architecture Positioning

Exact Redis Response Caching is optimized for:

```text
Static Prompts
Duplicate Retries
Benchmark Harnesses
Batch Inference Jobs
Deduplicating Network Bursts
```

It is not designed for semantic intent matching across conversational chat.

Exact caching provides deterministic behavior, minimal overhead, straightforward testing, and tangible cost savings for automated pipelines.
