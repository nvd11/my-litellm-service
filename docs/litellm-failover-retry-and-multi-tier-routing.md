# LiteLLM Routing, Circuit Breaking, Multi-Tier Retries, and High-Availability Fallback Architecture

## 1. Background & Concurrency Pain Points

In real-world LLM gateway engineering, relying on a single model endpoint or a single provider API key introduces critical operational bottlenecks:

1. **Bursty Concurrency and 429 Rate Limiting**:
   - For example, Google AI Studio's Gemini Free Tier enforces a quota limit of **15 RPM** (which can dynamically throttle to **5 RPM** during peak loads or for specific models).
   - When upstream Coding Agents (such as Codex, OpenCode, or Claude Code) execute heavy tasks like "scan and refactor entire repository," the Agent can trigger 10–15 consecutive tool-calling roundtrips within 10–20 seconds.
   - A single key's token bucket is depleted in 2–3 seconds, causing the provider to return `429 Too Many Requests` with mandatory backoff periods exceeding 30 seconds.
2. **Mid-Stream Transport Failures**:
   - Coding assistants rely heavily on Server-Sent Events (`stream: true`). Without rapid circuit breaking, if upstream emits 429 after sending a few initial chunks, the connection aborts mid-flight (`stream closed before response.completed`), forcing clients into multi-minute reconnect loops.
3. **Coordinating Heterogeneous Account Tiers**:
   - Teams typically possess keys with varying service tiers (e.g., personal legacy free accounts, high-reputation Google AI Pro subscriptions, and ephemeral test projects).
   - Route strategies must be carefully orchestrated to keep 99.9% of traffic free while providing seamless load distribution, automated tier escalation, and emergency fallback.

This document analyzes LiteLLM's retry, circuit breaker, and fallback mechanics based on production K3s operations, providing an enterprise multi-tier failover implementation.

---

## 2. Core Routing Parameters & Mathematical Mechanics

Within LiteLLM's `router_settings`, three core parameters dictate gateway fault tolerance behavior:

```yaml
router_settings:
  routing_strategy: "least-busy"
  num_retries: 5
  allowed_fails: 1
  cooldown_time: 60
  fallbacks:
    - gemini-3.7-flash: ["gemini-3.7-pro-plan", "gemini-3.7-backup"]
```

### 2.1 `num_retries`: Retry Budget and Total Attempts (Why 5 instead of 3?)

In networking theory and LiteLLM's internals, `num_retries` represents the **number of additional retry attempts allowed after the initial request attempt fails**.

The maximum total attempts allowed for a single request is:

$$\text{Total Attempts} = 1 \text{ (Initial Call)} + \text{num\_retries}$$

#### Why 4 Keys Require at Least `num_retries: 5`
Consider 4 keys distributed across 3 tiers (Tier 1: Key 1 + Key 2, Tier 2: Key 4, Tier 3: Key 3):

* **If `num_retries: 3`** (Total Attempts = 4):
  - Attempt 1: Key 1 fails (429);
  - Attempt 2 (Retry 1): Key 2 fails (429);
  - Attempt 3 (Retry 2): Key 4 (Pro Plan) fails;
  - Attempt 4 (Retry 3): Key 3 (Backup) is attempted.
  - **Risk**: The retry budget is on the razor's edge. A single transient network handshake blip on any intermediary key exhausts the retry budget before Key 3 can execute, returning an error to the client before the backup tier even gets a turn.
* **If `num_retries: 5`** (Total Attempts = 6):
  - Even if Key 1, Key 2, and Key 4 experience throttling or transient drops, the retry budget retains 2+ safety margins, **guaranteeing Key 3 is reached and executed**.

---

### 2.2 `allowed_fails`: Circuit Breaker Threshold (Why 1 instead of 3?)

`allowed_fails` defines **how many consecutive failures a specific Deployment/Key can encounter before being marked Unhealthy and quarantined into cooldown**.

* **Default Value = 3**: If set to 3, when Key 1 hits a 429, LiteLLM attempts 2 more retries on Key 1. Because Key 1 is already throttled, these 2 retries will continue returning 429, wasting the retry budget and adding client-side latency.
* **Optimized Value = `allowed_fails: 1`**: When Key 1 returns 429 or 5xx, LiteLLM **immediately marks Key 1 as cooling down within 10ms and removes it from the active pool**. Retries are immediately routed to healthy Key 2 or backup tiers, achieving instantaneous failover.

---

### 2.3 `cooldown_time`: Quarantine Duration (Why 60s instead of 30s?)

Google AI Studio Free Tier rate limits are evaluated against a **1-minute (60-second) sliding window**.

* **If `cooldown_time: 30` (Flapping Hazard)**:
  - Second 0: Key 1 is throttled with 429;
  - Second 30: Key 1 is un-quarantined and returned to the pool. However, Google's 60-second sliding window is only halfway elapsed; only 2–3 tokens have replenished;
  - Second 31: Agent sends batch calls, Key 1 accepts 2 requests and immediately fails with 429 again;
  - **Result**: Key 1 enters a rapid rate-limiting flapping loop (`Cooldown -> Rejoin -> Immediate Crash`).
* **Optimized Value = `cooldown_time: 60` (Full Quota Replenishment)**:
  - Quarantines the failed key for a full 60 seconds, ensuring it completely spans the 1-minute penalty window;
  - Upon rejoining the pool, Key 1 has **100% replenished its token bucket (full 15 RPM capacity)**, ready for the next task batch;
  - During quarantine, remaining pool keys seamlessly service traffic with zero client disruption.

---

## 3. Three-Tier Hierarchical Failover Architecture

To maximize account utility, we architected a **"Dual-Primary Load Balancing + Pro Plan Escalation + Backup Project Safety Net"** strategy:

```
[ Client Request: model="gemini-3.7-flash" ]
                   │
                   ▼
┌──────────────────────────────────────────────────────────────────┐
│ 🟢 Tier 1: Primary Daily Pool (gemini-3.7-flash)                 │
│   ├── Key 1 (Primary Gmail Account, RPM: 15)                     │
│   └── Key 2 (Secondary Gmail Account, RPM: 15)                   │
│   • Strategy: least-busy, 50/50 balanced load (Combined 30 RPM)  │
│   • Covers 99% of daily coding and chat traffic                  │
└──────────────────────────────────┬───────────────────────────────┘
                                   │ (If both Key 1 and Key 2 hit 429)
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│ 🟡 Tier 2: Escalation Pool (gemini-3.7-pro-plan)                 │
│   └── Key 4 (Google AI Pro Account, High Trust Score)            │
│   • Consumes 0 traffic during normal operation                   │
│   • Triggered only when primary tier is saturated                │
└──────────────────────────────────┬───────────────────────────────┘
                                   │ (If Pro Plan encounters anomalies)
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│ 🛡️ Tier 3: Emergency Backup Pool (gemini-3.7-backup)             │
│   └── Key 3 (Ephemeral / Standby Project Account)                │
│   • Always fully replenished; serves as final defense line       │
│   • Future retirement of this project won't impact Tiers 1 & 2   │
└──────────────────────────────────────────────────────────────────┘
```

### 3.1 Complete Configuration Implementation (`config.yaml`)

```yaml
# ==============================================================================
# LiteLLM Proxy Model & Multi-Tier Routing Configuration
# ==============================================================================
# Key Roles & Tier Definitions:
# - OPENAI_API_KEY_FREE_1  : Primary Free Account 1 (Owner) - Tier 1 Daily Rotation
# - OPENAI_API_KEY_FREE_2  : Primary Free Account 2 (Spouse) - Tier 1 Daily Rotation
# - OPENAI_API_KEY_PRO_PLAN: Primary Google AI Pro Account - Tier 2 Escalation
# - OPENAI_API_KEY_FREE_3  : Emergency Backup Account - Tier 3 Final Safety Net
# ==============================================================================

model_list:
  # === 🟢 Tier 1: Primary Daily Rotation Pool (50/50 Balanced Load) ===
  - model_name: gemini-3.7-flash
    litellm_params:
      model: gemini/gemini-3.6-flash
      api_key: os.environ/OPENAI_API_KEY_FREE_1
      rpm: 15

  - model_name: gemini-3.7-flash
    litellm_params:
      model: gemini/gemini-3.6-flash
      api_key: os.environ/OPENAI_API_KEY_FREE_2
      rpm: 15

  # === 🟡 Tier 2: Escalation Pool (Triggered when Tier 1 encounters 429) ===
  - model_name: gemini-3.7-pro-plan
    litellm_params:
      model: gemini/gemini-3.6-flash
      api_key: os.environ/OPENAI_API_KEY_PRO_PLAN
      rpm: 15

  # === 🛡️ Tier 3: Emergency Backup Pool (Standby / Zero Baseline Traffic) ===
  - model_name: gemini-3.7-backup
    litellm_params:
      model: gemini/gemini-3.6-flash
      api_key: os.environ/OPENAI_API_KEY_FREE_3
      rpm: 15

router_settings:
  routing_strategy: "least-busy" # Load balancing: routes to the key with lowest active concurrent load

  # Generous retry budget across keys and tiers (6 total attempts)
  num_retries: 5

  # Instant circuit breaking: quarantine key on first failure
  allowed_fails: 1

  # Full cooldown window: quarantine failed key for 60s to replenish quota
  cooldown_time: 60

  fallbacks:
    # Cascading fallback chain: Primary Pool -> Pro Plan -> Backup Key
    - gemini-3.7-flash: ["gemini-3.7-pro-plan", "gemini-3.7-backup"]

litellm_settings:
  cache: true
  cache_params:
    type: redis
    host: redis.redis.svc.cluster.local
    port: 6379
    password: os.environ/REDIS_PASSWORD
    supported_call_types: [chat_completion]
    ttl: 3600
```

---

## 4. Streaming Exceptions and Client Reconnect Mechanics

When using CLI tools like Codex or OpenCode, clients rely on streaming (`stream: true`). Understanding the demarcation between gateway retries and client retries is essential:

### 4.1 Non-Streaming vs. Streaming Retry Behaviors

1. **Non-Streaming Requests**:
   - Client issues request ➔ Gateway queries upstream;
   - If Key 1 returns 429 before headers or bytes are sent to the client, LiteLLM transparently retries on Key 2;
   - The client receives a single `200 OK` JSON response with **zero awareness of upstream retries**.
2. **Streaming Requests (SSE)**:
   - The connection is established immediately (HTTP 200 SSE Stream), with initial chunk headers delivered to the client;
   - If upstream terminates during token generation (throwing `MidStreamFallbackError`), the TCP stream is severed mid-response;
   - The client (Codex) detects that the stream closed before receiving `response.completed` and **triggers client-side reconnects**:
     ```text
     • Reconnecting... 2/5 (stream closed before response.completed)
     ```

### 4.2 Codex Client Pause & Protection State

When encountering `Reconnecting... waiting for network (esc to interrupt)`:
- Codex enters an input-protection pause state to prevent user prompt loss;
- Press **`Esc`** (or `Ctrl + C`) in the terminal to interrupt the wait and resubmit.

---

## 5. Benchmark & Traffic Distribution Results

Under sustained Agent execution across 3 intensive tasks (scanning 30+ files in parallel):

1. **Total Requests**:
   - 3 user-facing interactions generated **9 independent LLM Tool Calling roundtrips**.
2. **Traffic Distribution**:
   - **Key 1 (Primary 1)**: Handled **5 requests** (55.5%);
   - **Key 2 (Primary 2)**: Handled **4 requests** (44.5%);
   - **Key 4 (Pro Plan)**: 0 requests (Tier 1 handled load without saturation);
   - **Key 3 (Emergency Backup)**: 0 requests (Fully replenished on standby).
3. **Performance Metrics**:
   - All 9 requests returned within 0.8–1.2 seconds, achieving a **100% success rate**;
   - Dual-key rotation kept peak request rates well under provider thresholds with zero 429 throttling events.

---

## 6. Summary

Building an enterprise-grade LLM gateway requires engineering for resilience through **traffic distribution, instant circuit breaking, and multi-tier failovers**:

1. **`num_retries: 5`**: Allocates adequate attempt budgets to ensure deep fallback chains are fully traversed;
2. **`allowed_fails: 1`**: Instantly isolates failing endpoints rather than wasting retry budgets on throttled keys;
3. **`cooldown_time: 60`**: Synchronizes quarantine intervals with provider quota windows for complete token bucket replenishment;
4. **Hierarchical Multi-Tier Routing**: Balances cost and availability by serving traffic on primary free tiers while keeping escalation and emergency tiers ready for automated failover.
