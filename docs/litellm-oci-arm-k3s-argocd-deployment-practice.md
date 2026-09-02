# Engineering Guide: Deploying LiteLLM to OCI ARM Nodes via GitHub Actions, ArgoCD, and Kong Gateway

## 1. Architecture Objectives & Deployment Context

In building an enterprise multi-model gateway, our goal was to deploy **LiteLLM Proxy** as the underlying LLM unified access layer (Service A) into our hybrid multi-cloud Kubernetes cluster (Tencent Cloud K3s control plane + OCI `free-arm-vm` ARM64 worker node).

The CI/CD and runtime infrastructure had to meet production-grade engineering standards:

1. **Immutable GitOps Delivery**: Multi-architecture container builds via GitHub Actions with Digest Pinning (content-addressable hashes) triggering ArgoCD deployments, eliminating mutable tag drifts;
2. **Zero Secrets in Git**: Authoritative API keys and Redis passwords reside in OCI Vault, synchronized into Kubernetes Secrets via External Secrets Operator (ESO);
3. **12-Factor Decoupled Configuration**: Non-sensitive configs (model list, Redis endpoints, proxy whitelists) are stored in `ConfigMaps`, sensitive secrets in `Secrets`, consumed via `envFrom` and volume mounts;
4. **Unified Ingress Gateway (Kong + Gateway API)**: No secondary Ingress controller; reuses existing Kong Gateway for public ingress, utilizing Gateway API `HTTPRoute` for cross-namespace routing and path prefix stripping.

Data flow and deployment topology:

```
[ External Clients / Services ]
         │ (HTTP :31850 /litellm/v1/...)
         ▼
[ OCI free-arm-vm: Kong Gateway (NodePort) ]
         │ (Gateway API HTTPRoute strips /litellm prefix)
         ▼
[ LiteLLM Pod (:4000) on free-arm-vm ] ──(DNS)──► [ K3s Redis (:6379) on free-arm-vm ]
         │ (Decoupled environment injection)
         ├─► [ ConfigMap: litellm-config (/app/config/config.yaml) ]
         └─► [ Secret: litellm-secrets (From OCI Vault: Gemini Key / Master Key / Redis Password) ]
         │
         ▼ (HTTPS Outbound)
[ Google Gemini API (gemini-3.6-flash / gemini-3.7-flash) ]
```

---

## 2. CI & Container Builds: Multi-Arch & Tag Drift Mitigation

### 2.1 Multi-Arch Builds & Digest Pinning

The `free-arm-vm` is an ARM64 (4 OCPU / 24GB RAM) instance, while developer workstations and CI runners are AMD64. The GitHub Actions workflow produces a multi-arch Manifest Index:

```yaml
# .github/workflows/build-and-push-image.yaml
- name: Build and push image
  id: build
  uses: docker/build-push-action@v6
  with:
    context: .
    platforms: linux/amd64,linux/arm64
    push: true
    tags: ${{ steps.meta.outputs.tags }}

- name: Record manifest digest
  run: |
    echo "Digest: ${{ steps.build.outputs.digest }}"
```

### 2.2 Why Digest Pinning Is Mandatory Over Commit Tags

During early validation, we observed: **Re-running a build for the exact same Git Commit SHA produces the same tag name in GHCR, but base image changes or BuildKit metadata produce a newly generated Manifest Index Digest**.

If ArgoCD tracks `sha-<commit>`, re-running CI causes the deployed container content to mutate even though no Git changes occurred in the deployment repo, breaking GitOps reproducibility and auditability.

We updated our shared Helm Chart (`generic-web-service-v2` v2.1.0) to prioritize digests in the Deployment template:

```yaml
# templates/deployment.yaml
image: {{- if .Values.image.digest }} "{{ .Values.image.repository }}@{{ .Values.image.digest }}"{{- else }} "{{ .Values.image.repository }}:{{ .Values.image.tag }}"{{- end }}
```

Upon successful CI builds, `repository_dispatch` updates the exact `image.digest: sha256:...` in the GitOps repo (`my-argocd-manifests`), achieving immutable content locking.

---

## 3. Secret Management: OCI Vault & External Secrets Operator (ESO)

To achieve "Zero Secrets in Git," we integrated OCI Vault with Kubernetes External Secrets Operator (ESO).

### 3.1 Architecture & Bootstrap Credentials

ESO fetches secrets from OCI Vault into native Kubernetes Secrets. To authenticate ESO with OCI, a dedicated User Principal was provisioned:

1. **OCI IAM User**: `litellm-vault-reader`;
2. **OCI IAM Group**: `litellm-vault-readers`;
3. **Local Private Key**: `/home/gateman/keys/litellm-vault-reader.pem` (public key fingerprint uploaded to OCI user);
4. **Bootstrap Secret**: Imperatively created once in the target `llm-system` namespace:

```bash
kubectl create secret generic oci-litellm-vault-reader -n llm-system \
  --from-file=privateKey=/home/gateman/keys/litellm-vault-reader.pem \
  --from-literal=fingerprint="b3:f3:cc:2b:b0:9d:88:c9:75:08:0f:82:e5:b6:e1:a1"
```

### 3.2 Troubleshooting OCI IAM Permissions & KMS Decryption Locks

After creating `SecretStore` and `ExternalSecret`, secrets failed to sync with the following error:

```text
Secrets service failed to GetSecretBundleByName, HTTP status code 404: Authorization failed or requested resource not found.
```

#### Root Cause Analysis:
1. Replicating the request via OCI CLI using `litellm-vault-reader` credentials reproduced `404 NotAuthorizedOrNotFound`;
2. Checking user attributes revealed `litellm-vault-reader` **had not been added to the `litellm-vault-readers` group**;
3. OCI Vault payloads are encrypted by KMS Master Encryption Keys. Calling `GetSecretBundleByName` requires KMS decryption permissions. The original IAM policy only granted `read secret-bundles` without KMS key permissions.

#### Resolution:
1. Add user to the IAM group:
   ```bash
   oci iam group add-user \
     --group-id "ocid1.group.oc1..aaaaaaaajvxytvkmyupwguyzza27dsx2ovbtv26sg2dyppdsnuluxdpe2zja" \
     --user-id "ocid1.user.oc1..aaaaaaaa3fcoxuuzcdtwav4mcsqwxznzsarmj6ctugtnravseu5i5hst7neq"
   ```
2. Update IAM Policy with KMS permissions (`use keys` and `use key-delegate`):
   ```json
   [
     "Allow group litellm-vault-readers to read secret-family in compartment litellm-prod",
     "Allow group litellm-vault-readers to read vaults in compartment litellm-prod",
     "Allow group litellm-vault-readers to read secrets in compartment litellm-prod",
     "Allow group litellm-vault-readers to use keys in compartment litellm-prod",
     "Allow group litellm-vault-readers to use key-delegate in compartment litellm-prod",
     "Allow group litellm-vault-readers to inspect compartments in tenancy"
   ]
   ```

Once updated, ESO completed synchronization immediately. `llm-system/litellm-secrets` transitioned to `SecretSynced / Ready: True`, generating Secrets containing `OPENAI_API_KEY_FREE_1`, `LITELLM_MASTER_KEY`, and `REDIS_PASSWORD`.

### 3.3 Multi-Cloud DNS Optimization: `internalTrafficPolicy: Local`

Because nodes span Tencent Cloud and OCI, ESO initially experienced UDP timeouts querying CoreDNS (`10.43.0.10:53`) across Tailscale.

Optimization:
1. Scaled CoreDNS to 3 replicas, ensuring a local CoreDNS Pod runs on `free-arm-vm`;
2. Patched `kube-dns` service internal traffic policy to `Local`:
   ```bash
   kubectl patch svc kube-dns -n kube-system --type merge -p '{"spec":{"internalTrafficPolicy":"Local"}}'
   ```
This reduced node-local DNS lookups to 0ms, eliminating network timeout warnings.

---

## 4. ArgoCD Control Plane Debugging & GitOps Reconciliation

### 4.1 Resolving Control Plane Starvation (SQLite I/O Wait & Cross-Node Latency)

After committing the Application manifest, the ArgoCD Web UI became unresponsive and application sync status hung in `Unknown`.

#### Debugging Steps:
1. Inspected K3s logs on the master control node (`journalctl -u k3s`), finding repeated warnings: `vxlan_network.go: external interface not found`;
2. Running `top` showed CPU `100% wa` (I/O Wait) and memory usage > 95%;
3. Root cause: The 2C2G Master node's Kine (SQLite) backend had accumulated 4.2 million revisions over 78 days, causing severe disk I/O during compaction checkpoints while hosting all ArgoCD components on the same node.

#### Fix & Topology Tuning:
1. Restarted K3s on Master (`systemctl restart k3s`) to reinitialize the virtual bridge;
2. Re-scheduled ArgoCD workloads:
   - Kept latency-sensitive communication backends (`application-controller`, `repo-server`, `redis`) on the Master node to avoid cross-cloud gRPC overhead;
   - Scheduled the heavy Web UI (`argocd-server`) to the OCI node;
3. Master I/O Wait dropped to 0%, CPU idle reached 99%, and all ArgoCD applications recovered to `Synced` & `Healthy`.

---

## 5. Container Runtime Debugging & 12-Factor Refactoring

### 5.1 Missing Redis Configuration Leading to CrashLoopBackOff

When ArgoCD scheduled the Pod to `free-arm-vm`, startup crashed with:

```text
Setting Cache on Proxy
File "/app/.venv/lib/python3.12/site-packages/litellm/_redis.py", line 475, in _get_redis_client_logic
    raise ValueError("Either 'host' or 'url' must be specified for redis.")
ValueError: Either 'host' or 'url' must be specified for redis.
```

**Root Cause**:
In `config.yaml`, Redis host was configured as `host: os.environ/REDIS_HOST`. In the Deployment manifest, only sensitive secrets were injected, omitting `REDIS_HOST`. Reading `None` crashed LiteLLM during cache initialization.

### 5.2 12-Factor Refactoring: Eliminating Hardcoded Environment Variables

We refactored configuration across three decoupled layers:

1. **Config File (ConfigMap Volume)**: Non-sensitive cluster networking properties are declared directly in `config.yaml`:
   ```yaml
   litellm_settings:
     cache: true
     cache_params:
       type: redis
       host: redis.redis.svc.cluster.local # Internal cluster endpoint
       port: 6379
       password: os.environ/REDIS_PASSWORD  # References env var for secret
       supported_call_types: [chat_completion]
       ttl: 3600
   ```
2. **Non-Sensitive Variables (ConfigMap envFrom)**: Network parameters like `NO_PROXY` are injected via ConfigMap;
3. **Sensitive Secrets (Secret envFrom)**: Credentials (`OPENAI_API_KEY_FREE_1`, `LITELLM_MASTER_KEY`, `REDIS_PASSWORD`) are injected via Secret.

The refactored Deployment values became completely clean:

```yaml
envFrom:
  - configMapRef:
      name: litellm-config
  - secretRef:
      name: litellm-secrets
```

### 5.3 Probe Tuning for Cold Starts

On ARM64 nodes, LiteLLM requires 30–40 seconds to parse models and initialize Redis pools. An initial `initialDelaySeconds: 15` caused Kubelet to kill the container prematurely.

Adjusted probe settings:
```yaml
livenessProbe:
  initialDelaySeconds: 45
  periodSeconds: 15
  failureThreshold: 5
readinessProbe:
  initialDelaySeconds: 45
  periodSeconds: 15
  failureThreshold: 5
```
Pods now transition cleanly to `1/1 Running` with zero restarts.

---

## 6. Gateway Integration: Kong Gateway API Routing & Timeout Hardening

### 6.1 Cross-Namespace Gateway Binding (`NotAllowedByListeners`)

LiteLLM is deployed in `llm-system`, and its `HTTPRoute` binds to the shared gateway `kong-main-gateway` in `default`.

Running `kubectl describe httproute litellm-svc-route -n llm-system` revealed route rejection:
```text
Reason: NotAllowedByListeners, Status: False, Type: Accepted
```

**Root Cause**: `kong-main-gateway` defaulted to `allowedRoutes.namespaces.from: Same`, restricting route bindings to the `default` namespace.

**Resolution**: Updated `infrastructure/kong-gateway/Gateway.yaml` to permit cross-namespace attachment:
```yaml
listeners:
  - name: http
    port: 80
    protocol: HTTP
    allowedRoutes:
      namespaces:
        from: All # Permits cross-namespace route bindings
```

### 6.2 URL Prefix Stripping (`/litellm` Strip Path)

External requests hit `http://134.185.90.98:31850/litellm/v1/models`, but LiteLLM listens on `/v1/models`.

Standard Gateway API `URLRewrite` filter:
```yaml
filters:
  - type: URLRewrite
    urlRewrite:
      path:
        type: ReplacePrefixMatch
        replacePrefixMatch: /
```
Emitted a warning from Kong Gateway Controller: `HTTPRoute can't be routed: httpFilter URLRewrite unsupported`.

**Resolution**:
Added route annotations in the Helm chart attaching Kong-specific strip-path directives:
```yaml
annotations:
  konghq.com/strip-path: "true"
```
Kong automatically strips `/litellm` and forwards the clean path to the LiteLLM Pod.

### 6.3 Extended Timeouts for Reasoning Models

When invoking `gemini-3.7-flash-freelayer` with Thinking mode enabled, inference took 49.6 seconds, hitting Kong's default 60s upstream read timeout and returning HTTP 504.

**Resolution**:
Added Kong timeout annotations on the Kubernetes `Service`, extending timeouts to 180 seconds:
```yaml
service:
  annotations:
    konghq.com/read-timeout: "180000"
    konghq.com/write-timeout: "180000"
    konghq.com/connect-timeout: "60000"
```

---

## 7. End-to-End Verification & Validation

With deployment complete, we validated public endpoints (`134.185.90.98:31850`):

### 7.1 Health Checks & Model Catalog

```bash
# 1. Liveness probe
curl -s http://134.185.90.98:31850/litellm/health/liveliness
# Output: "I'm alive!"

# 2. Readiness probe
curl -s http://134.185.90.98:31850/litellm/health/readiness
# Output: {"status":"healthy","db":"Not connected"}

# 3. Model catalog (Authenticated)
curl -s http://134.185.90.98:31850/litellm/v1/models \
  -H "Authorization: Bearer $LITEL...KEY"
```

Response payload:
```json
{
  "data": [
    { "id": "gemini-3.6-flash-freelayer", "object": "model", "owned_by": "openai" },
    { "id": "gemini-3.7-flash-freelayer", "object": "model", "owned_by": "openai" }
  ],
  "object": "list"
}
```

### 7.2 Chat Completions & Token Usage

```bash
curl -s -X POST "http://134.185.90.98:31850/litellm/v1/chat/completions" \
  -H "Authorization: Bearer $LITEL...KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.6-flash-freelayer",
    "messages": [{"role": "user", "content": "What is 10*10?"}],
    "max_tokens": 512
  }'
```

Returns granular token usage:

```json
{
  "choices": [
    {
      "finish_reason": "stop",
      "index": 0,
      "message": {
        "content": "10 * 10 = 100",
        "role": "assistant"
      }
    }
  ],
  "usage": {
    "completion_tokens": 88,
    "prompt_tokens": 10,
    "total_tokens": 98,
    "completion_tokens_details": {
      "reasoning_tokens": 77,
      "text_tokens": 11
    }
  }
}
```

### 7.3 SSE Streaming Output (`stream: true`)

```bash
curl -s -N -X POST "http://134.185.90.98:31850/litellm/v1/chat/completions" \
  -H "Authorization: Bearer $LITEL...KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.6-flash-freelayer",
    "messages": [{"role": "user", "content": "Count from 1 to 5."}],
    "stream": true
  }'
```

Stream payload:
```text
data: {"choices":[{"delta":{"role":"assistant","content":"1, 2, 3,"}}]}
data: {"choices":[{"delta":{"content":" 4, 5"}}]}
data: [DONE]
```

### 7.4 Redis Rate Limiting & Routing Keys

Inspecting Redis keys:

```bash
kubectl exec -n redis deploy/redis -- redis-cli -a "$REDIS_PASSWORD" keys "*"
```

Redis stores active routing counters and token buckets:
```text
global_router:...:gemini/gemini-3.6-flash:rpm:16-21
global_router:...:gemini/gemini-3.6-flash:tpm:16-21
{model_per_key:litellm_proxy_master_key:gemini-3.6-flash-freelayer}:tokens
{api_key:litellm_proxy_master_key}:tokens
```

---

## 8. Summary of Key Lessons

1. **Immutable Container Builds**: In GitOps pipelines, images must be pinned using **Manifest Digest Pinning** to eliminate unexpected runtime mutation;
2. **Zero-Secret Repositories**: **ESO + OCI Vault** keeps Git completely free of plaintext secrets while maintaining declarative resource bindings;
3. **12-Factor Configuration**: Non-sensitive configs belong in ConfigMaps, secrets in Secrets, avoiding brittle hardcoded variables in Deployment manifests;
4. **Gateway API Evolution**: Cross-namespace routing requires explicit `from: All` policies, coupled with route annotations for path stripping and long upstream timeouts (180s).

Phase 1 infrastructure integration and gateway deployment are fully verified and complete.
