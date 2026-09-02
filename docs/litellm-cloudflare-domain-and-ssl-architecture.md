# Custom Domain and Cloudflare Architecture for LiteLLM Gateway: Pitfalls, Troubleshooting, and Trade-offs

## 1. Background & Core Requirements

After deploying the LiteLLM gateway within a multi-cloud K3s environment, the default access point was exposed via the public IP of the OCI `free-arm-vm` node and Kong Gateway's NodePort:

```text
http://134.185.90.98:31850/litellm/v1/...
```

While this endpoint successfully serviced OpenAI-compatible API calls, relying on bare IPs and high-range NodePorts introduced several operational challenges for cross-team collaboration and client integration (such as Codex, OpenCode, and downstream services):

1. **Credential Security & Compliance**: Plaintext HTTP transmission exposes traffic to man-in-the-middle sniffing, making it unsafe to transmit production-grade Master API Keys;
2. **Endpoint Maintainability**: If the underlying VM migrates or its public IP changes, client configurations across all developer environments require manual updates;
3. **URL Standardization**: Non-standard ports (`:31850`) are frequently blocked by restrictive corporate firewalls, enterprise proxies, or strict SDK connection policies.

To address this, we delegated the domain `jpgcp.cloud` to Cloudflare for authoritative DNS management, intending to assign the subdomain `gw.jpgcp.cloud` to the gateway. However, during onboarding, Cloudflare's edge proxy mechanisms collided with Kubernetes NodePorts and the long-duration inference characteristics of modern reasoning models. This document reviews the debugging process, compares two mainstream architecture patterns, and outlines our engineering decisions.

---

## 2. Core Pitfalls & Conflict Analysis: Cloudflare Proxy vs. LLM Gateways

When initially configuring Cloudflare DNS, we enabled the default **Proxied (Orange Cloud ☁️)** mode, which immediately caused two severe networking issues.

### 2.1 Pitfall 1: Cloudflare Edge Proxy Drops Non-Standard High Ports

With Orange Cloud proxying enabled, we attempted to send requests directly to the subdomain with the NodePort attached:

```bash
curl http://gw.jpgcp.cloud:31850/litellm/health/liveliness
```

**Symptom**: The request hung indefinitely and eventually failed with a Connection Timeout.

#### Root Cause Analysis:
Cloudflare's Free Tier reverse proxy (CDN Edge) **only listens on specific standard web ports**:
- HTTP: `80`, `8080`, `8880`, `2052`, `2082`, `2086`, `2095`
- HTTPS: `443`, `2053`, `2083`, `2087`, `2096`, `8443`

When external traffic hits `gw.jpgcp.cloud:31850`, the TCP SYN packet arrives at Cloudflare's Anycast edge. Because port `31850` is not in Cloudflare's open listening port table, the edge firewall silently drops the packet. Traffic never reaches the origin OCI node.

---

### 2.2 Pitfall 2: Origin Port Mismatch & HTTP 522 Errors

Because high ports could not be targeted directly, we switched to standard HTTPS port 443:

```bash
curl -i https://gw.jpgcp.cloud/litellm/v1/models
```

**Symptom**: Occasional 200 responses, but under sustained requests or complex queries, it frequently returned `HTTP/2 522` or `error code: 522`.

```text
HTTP/2 522
server: cloudflare
error code: 522 (Connection timed out)
```

#### Root Cause Analysis:
1. The client sends a request to `https://gw.jpgcp.cloud` (port 443), and the Cloudflare edge terminates the client TLS connection;
2. Cloudflare attempts an upstream origin connection to `134.185.90.98`, defaulting to ports **443** or **80** at the origin;
3. In our K3s cluster, Kong Gateway is exposed externally via **NodePort `31850`**. Although the OCI Security List permitted ports 80 and 443, no native high-availability web service was listening on port 80/443 directly on the host OS;
4. When port 80 failed to complete the TCP three-way handshake within Cloudflare's timeout window, the edge marked the origin unreachable and returned `Error 522`.

---

### 2.3 Pitfall 3: Reasoning Models (Thinking Models) and 100-Second Edge Timeouts

When benchmarking reasoning models such as `gemini-3.7-flash` (thinking enabled), the server spends significant time generating internal reasoning tokens before emitting output. For complex prompts, time-to-first-token (TTFT) can easily exceed 40–50 seconds.

Cloudflare Free Tier proxies enforce strict hardcoded constraints:
- **HTTP Read Timeout (HTTP 524 Timeout)**: Hardcoded at **100 seconds**, non-configurable in the Free Tier dashboard;
- If a request is non-streaming (`stream: false`) and upstream inference exceeds 100 seconds, Cloudflare unilaterally drops the connection and returns `HTTP 524 (A timeout occurred)` to the client.

---

## 3. Deep Dive into Architectural Options & Trade-offs

To resolve these networking and port challenges, we evaluated two distinct architectural designs:

---

### Option 1: DNS-Only Mode (Grey Cloud Direct Connection)

```
Client ──(DNS Lookup)──► [ Cloudflare DNS ]
   │                           │
   │ ◄──(Returns Origin IP: 134.185.90.98)
   │
   ▼ (Client connects directly to origin port)
[ OCI free-arm-vm: 134.185.90.98:31850 ] ──► [ Kong Gateway ] ──► [ LiteLLM Pod ]
```

#### Mechanism:
Set DNS records in Cloudflare to **DNS Only (`proxied: false`, Grey Cloud)**. Cloudflare serves purely as an authoritative DNS provider and does not proxy HTTP/TCP traffic.

Clients access the gateway using the custom hostname and explicit port:
```text
http://gw.jpgcp.cloud:31850/litellm/v1
```

#### Pros:
1. **Transparent Port Access**: Completely bypasses Cloudflare edge port whitelists; `31850`, `22`, and any custom ports communicate unobstructed;
2. **Zero Middleware Timeout Clamping**: Eliminates Cloudflare's 100s timeout constraint; inference duration is controlled entirely by Kong Gateway settings (e.g., `read-timeout: 180000`);
3. **Lowest Latency**: Establishes direct TCP/TLS connections to the OCI Singapore datacenter without multi-hop CDN routing overhead;
4. **Simple Debugging Path**: Isolates connectivity issues strictly between client and OCI host without third-party CDN policy interference.

#### Cons:
1. **Port Visible in URL**: Clients must explicitly specify `:31850`;
2. **Exposed Origin IP**: DNS resolves directly to the host VM public IP, lacking CDN DDoS scrubbing and edge scanning protection;
3. **Plaintext HTTP**: Enabling HTTPS requires mounting certificates manually at the Kong or host level rather than utilizing Cloudflare automated edge certificates.

---

### Option 2: Standard Port 443 Ingress (Clean URLs & Edge Acceleration)

Option 2 delivers a port-free, standard HTTPS entrypoint: `https://gw.jpgcp.cloud/litellm/v1`. This can be achieved via two paths:

#### Path 2A: Cloudflare Proxied + Origin Rules (Port Rewriting)

```
Client ──(HTTPS :443)──► [ Cloudflare Edge (Auto SSL) ] ──(HTTP :31850)──► [ OCI 134.185.90.98:31850 ]
```

- **Mechanism**: Keep Orange Cloud proxying enabled, and create an Origin Rule in **Rules ➔ Origin Rules**:
  ```text
  When hostname equals "gw.jpgcp.cloud" -> Override destination port to "31850"
  ```
- **Pros**:
  - Clean URL (`https://...` without ports);
  - Zero added cloud costs by leveraging Cloudflare routing rules without provisioning extra OCI resources;
  - Conceals origin IP behind Cloudflare's Anycast edge and DDoS protection;
  - Automated SSL certificate management and rotation.
- **Cons & Constraints**:
  - **100-Second Hard Timeout**: Non-streaming requests exceeding 100s are abruptly terminated by Cloudflare (requiring clients to enforce `stream: true`);
  - Introduces 20–50ms edge handshake and forwarding latency.

---

#### Path 2B: OCI Native Load Balancer (Native Port 443 Listener)

```
Client ──(HTTPS :443)──► [ OCI Load Balancer (:443) ] ──(HTTP :31850)──► [ K3s free-arm-vm:31850 ]
```

- **Mechanism**: Provision an OCI Flexible Load Balancer (utilizing the Always Free 10Mbps quota), configure a port 443 HTTPS Listener targeting backend `free-arm-vm:31850`, and attach a free OCI certificate or a 15-year Cloudflare Origin Certificate.
- **Pros**:
  - Standard HTTPS without custom ports;
  - Upstream timeouts configurable up to 1800 seconds (30 minutes), supporting heavy reasoning workloads;
  - Native multi-node backend load balancing (HA);
  - Direct routing to OCI Singapore datacenter for predictable latency.
- **Cons**:
  - Consumes the single Always Free Load Balancer instance quota per tenancy;
  - Requires maintaining VCN subnets, listeners, backend sets, and certificate chains in the OCI Console.

---

### Architectural Trade-off Matrix

| Evaluation Dimension | Option 1: DNS-Only Direct (`:31850`) | Option 2A: Cloudflare Origin Rules | Option 2B: OCI Load Balancer |
| :--- | :--- | :--- | :--- |
| **URL Format** | `http://gw.jpgcp.cloud:31850` | `https://gw.jpgcp.cloud` | `https://gw.jpgcp.cloud` |
| **Port Requirement** | Requires explicit `:31850` | **Standard 443 (No port)** | **Standard 443 (No port)** |
| **HTTPS Support** | Requires manual origin cert | **Automated via Cloudflare** | **OCI Cert / 15-yr Origin Cert** |
| **Reasoning Timeout Limit** | **No CDN limit** (Kong governs 180s+) | **100s Hard Cutoff** (HTTP 524 risk) | **No CDN limit** (Supports 1800s+) |
| **Origin IP Protection** | Exposes OCI Host IP | **Fully Masked** (Anycast edge VIP) | Exposes OCI LB VIP |
| **Network Latency** | Direct handshake, lowest latency | +20~50ms edge proxy overhead | Direct datacenter connection |
| **Cloud Resource Usage** | Zero | Zero | Consumes 1 OCI Free LB instance |

---

## 4. Current Implementation & Roadmap

Considering our primary operational goal (delivering rock-solid, low-latency, uninterrupted LLM access for local development, Codex assistants, and internal tools), we established the following roadmap:

### 4.1 Current Choice: Option 1 (DNS-Only Direct Connection)

For Phase 1, we adopted **Option 1 (DNS-Only Grey Cloud)**:

1. **Configuration**:
   - In Cloudflare, set the `gw.jpgcp.cloud` A record to `proxied: false`;
   - Point directly to the OCI host IP `134.185.90.98`;
2. **Client Configuration** (e.g., Codex `~/.codex/config.toml`):
   ```toml
   [model_providers.litellm]
   name = "my-litellm-gateway"
   base_url = "http://gw.jpgcp.cloud:31850/litellm/v1"
   env_key = "LITELLM_MASTER_KEY"
   wire_api = "responses"
   ```
3. **Decision Rationale**:
   - Eliminates all Cloudflare edge `522 / 524` timeout hazards;
   - Guarantees seamless execution for Gemini 3.7 Thinking mode long-duration deep reasoning;
   - Completely circumvents edge CDN high-port packet drops.

---

### 4.2 Future Evolution Roadmap

When expanding access to multi-team collaboration or public SaaS exposure, the gateway will transition smoothly to **Option 2B (OCI Load Balancer)**:

```text
[ Client ]
    │ (Standard HTTPS :443)
    ▼
[ OCI Always Free Load Balancer (:443) ] ──(Mounted with 15-year Cloudflare Origin Cert)
    │ (Internal HTTP forward)
    ▼
[ free-arm-vm:31850 (Kong Gateway) ]
    │
    ▼
[ LiteLLM Pod (:4000) ]
```

This evolution delivers:
- Clean `https://gw.jpgcp.cloud/litellm/v1` entrypoint;
- Zero-maintenance 15-year SSL certificate offloading;
- 1800-second extended inference timeouts;
- High-availability traffic distribution across multiple K3s worker nodes.

---

## 5. End-to-End Verification & Benchmark Results

Under Option 1 (DNS-Only mode), end-to-end functionality was validated:

### 5.1 Global DNS Resolution Verification
```bash
dig A gw.jpgcp.cloud @8.8.8.8 +short
# Output: 134.185.90.98 (Resolves directly to OCI origin)
```

### 5.2 Liveness Probe & Model Catalog
```bash
# 1. Liveness check
curl -s "http://gw.jpgcp.cloud:31850/litellm/health/liveliness"
# Output: "I'm alive!"

# 2. Authentication and model list
curl -s "http://gw.jpgcp.cloud:31850/litellm/v1/models" \
  -H "Authorization: Bearer $LITEL...KEY" | jq -c '.data[].id'
# Output: "gemini-3.6-flash-freelayer", "gemini-3.7-flash-freelayer"
```

### 5.3 Chat Inference & Token Accounting
```bash
curl -s -X POST "http://gw.jpgcp.cloud:31850/litellm/v1/chat/completions" \
  -H "Authorization: Bearer $LITEL...KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.6-flash-freelayer",
    "messages": [{"role": "user", "content": "What is 100 + 200? Answer with only number."}],
    "max_tokens": 64
  }' | jq -c '{content: .choices[0].message.content, tokens: .usage.total_tokens}'
```

Response payload:
```json
{"content":"300","tokens":72}
```

### 5.4 Codex Client Integration
Inside the Codex CLI, prompts sent to `http://gw.jpgcp.cloud:31850/litellm/v1` executed flawlessly for codebase indexing and code generation. Total response latency remained consistently within 1–2 seconds with zero disconnections or retry loops.

---

## 6. Summary

When mapping a private LLM gateway to a public domain, standard CDN edge proxying cannot simply be applied to non-standard NodePorts. Managing port whitelists, origin timeouts (100s), and TLS termination boundaries is critical for gateway resilience:

1. **Initial NodePort Exposure**: Use **DNS-Only (Grey Cloud)** to avoid CDN-induced port packet drops and 522/524 connection aborts;
2. **LLM Inference Timeouts**: Modern reasoning models require gateway upstream read timeouts configured well above 120 seconds;
3. **Production HTTPS Ingress**: Deploying an OCI managed Load Balancer on port 443 with TLS offloading is the optimal enterprise pattern combining clean URLs with unlimited reasoning connection timeouts.
