# New API Key Onboarding Standard Operating Guide

This document defines the **standardized operating procedure for onboarding new upstream LLM API keys** (e.g., secondary Gemini keys, Anthropic Claude keys, OpenAI keys) to the `my-litellm-service` unified gateway.

The workflow adheres strictly to **Zero Secrets in Git**, **12-Factor Configuration Decoupling**, and **Declarative GitOps Delivery**.

---

## 1. Cross-Layer Architecture & Mutation Scope

When provisioning a new API Key, layer responsibilities and required modifications are structured as follows:

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. Cloud Secret Management Tier (OCI Vault: litellm-prod)                   │
│    Create Secret resource (e.g., litellm-openai-api-key-free-2)             │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ (Automated sync via ESO)
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 2. GitOps Manifest Repository (my-argocd-manifests)                         │
│    Target File: argocd-apps/litellm-svc-app.yaml                            │
│    - a. externalSecret.data: Map OCI Secret key to Kubernetes Secret key    │
│    - b. configMap.data["config.yaml"]: Mount key in model_list & router     │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ (ArgoCD reconciliation & rolling update)
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 3. Application Codebase (my-litellm-service)                                │
│    Target Files:                                                            │
│    - config.yaml: Mirror model routing configuration locally                │
│    - .env.example: Add environment variable placeholder                    │
│    - app/core/config.py: Update Pydantic Settings validation schema         │
│    - .env (local untracked secret file): Inject secret for local testing    │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Step-by-Step Execution Guide

### Step 1: Create Cloud Secret in OCI Vault (Layer 1)

> ⚠️ **Security Policy**: Real API keys must never be committed to Git; they must reside encrypted within OCI Vault.

1. **Log in to OCI Console or initialize OCI CLI**;
2. **Locate target Vault**:
   - **Compartment**: `litellm-prod`
   - **Vault**: `litellm-vault`
   - **Master Encryption Key**: `litellm-secrets-key`
3. **Create Secret**:
   - **Naming Convention**: `litellm-<provider>-api-key-<alias>` (e.g., `litellm-openai-api-key-free-2`)
   - **Format**: Plaintext or Base64-encoded secret string.
4. **CLI Creation Example**:
   ```bash
   VAULT_OCID=$(oci kms management vault list --compartment-id "<COMPARTMENT_OCID>" --query "data[?\"display-name\"=='litellm-vault'].id | [0]" --raw-output)
   KEY_OCID=$(oci kms management key list --compartment-id "<COMPARTMENT_OCID>" --endpoint "<VAULT_MANAGEMENT_ENDPOINT>" --query "data[?\"display-name\"=='litellm-secrets-key'].id | [0]" --raw-output)

   oci vault secret create-base64 \
     --compartment-id "<COMPARTMENT_OCID>" \
     --vault-id "$VAULT_OCID" \
     --key-id "$KEY_OCID" \
     --secret-name "litellm-openai-api-key-free-2" \
     --secret-content-content "$(echo -n '<YOUR_API_KEY>' | base64)"
   ```

---

### Step 2: Update GitOps Manifests (Layer 2: `my-argocd-manifests`)

* **Repository**: `nvd11/my-argocd-manifests`
* **File Path**: `argocd-apps/litellm-svc-app.yaml`

#### 2.1 Update `externalSecret.data` (Declare Secret Synchronization Mapping)
Append the new secret mapping under `externalSecret.data`:

```yaml
externalSecret:
  enabled: true
  refreshInterval: 1h
  secretStoreRef:
    name: oci-litellm-vault-store
    kind: SecretStore
  target:
    name: litellm-secrets
    creationPolicy: Owner
  data:
    - secretKey: OPENAI_API_KEY_FREE_1
      remoteRef:
        key: litellm-openai-api-key-free-1
    # Maps OCI secret litellm-openai-api-key-free-2 to Pod environment variable OPENAI_API_KEY_FREE_2
    - secretKey: OPENAI_API_KEY_FREE_2
      remoteRef:
        key: litellm-openai-api-key-free-2
    - secretKey: LITELLM_MASTER_KEY
      remoteRef:
        key: litellm-master-key
    - secretKey: REDIS_PASSWORD
      remoteRef:
        key: litellm-redis-password
```

#### 2.2 Update `configMap.data["config.yaml"]` (Configure Routing & Load Balancing)
Add the deployment item using the new environment variable in `model_list`, and configure `router_settings`:

```yaml
configMap:
  enabled: true
  name: litellm-config
  data:
    NO_PROXY: "localhost,127.0.0.1,10.0.0.0/8,.svc,.cluster.local"
    config.yaml: |
      model_list:
        # Key 1 Instance
        - model_name: gemini-3.6-flash-freelayer
          litellm_params:
            model: gemini/gemini-3.6-flash
            api_key: os.environ/OPENAI_API_KEY_FREE_1
            rpm: 15
        # Key 2 Instance (Same logical model alias for automated load balancing)
        - model_name: gemini-3.6-flash-freelayer
          litellm_params:
            model: gemini/gemini-3.6-flash
            api_key: os.environ/OPENAI_API_KEY_FREE_2
            rpm: 15

      router_settings:
        routing_strategy: "least-busy" # Dispatches to least loaded key
        num_retries: 3                # Switches keys upon encountering 429
        retry_after: 5
        cooldown_time: 30             # Quarantines throttled key for 30 seconds

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

### Step 3: Update Application Source Code (Layer 3: `my-litellm-service`)

* **Repository**: `nvd11/my-litellm-service`

#### 3.1 Synchronize Local `config.yaml`
Keep local `config.yaml` aligned with GitOps configurations for local smoke testing.

#### 3.2 Update `.env.example`
Append placeholder keys to the template:
```env
# Google Gemini API
OPENAI_API_KEY_FREE_1=replace-with-gemini-api-key-1
OPENAI_API_KEY_FREE_2=replace-with-gemini-api-key-2
```

#### 3.3 Update `app/core/config.py` (Pydantic Settings Validation)
Add the field to the `Settings` class:
```python
class Settings(BaseSettings):
    # ...
    openai_api_key_free_1: SecretStr
    openai_api_key_free_2: SecretStr | None = None  # Optional secondary key
    # ...
```

#### 3.4 Update Local `.env`
Add the actual API key to the untracked local `.env` for development:
```env
OPENAI_API_KEY_FREE_2=AIzaSy...
```

---

## 3. Verification Checklist

After committing and pushing changes, verify end-to-end functionality:

### 1. Verify OCI Secret Synchronization to Kubernetes
```bash
# Check ExternalSecret sync status
kubectl get externalsecret litellm-secrets -n llm-system

# Verify generated Secret keys (output masks plaintext)
kubectl get secret litellm-secrets -n llm-system -o jsonpath='{.data}' | jq 'keys'
# Expected: ["LITELLM_MASTER_KEY", "OPENAI_API_KEY_FREE_1", "OPENAI_API_KEY_FREE_2", "REDIS_PASSWORD"]
```

### 2. Verify ArgoCD Deployment Reconciliation
```bash
# Check ArgoCD Application status
kubectl get application litellm-svc -n argocd
# Expected: Synced / Healthy

# Check Pod rolling update status
kubectl get pods -n llm-system -o wide
# Expected: 1/1 Running, 0 restarts
```

### 3. Verify Public API Load Balancing & Invocations
```bash
MASTER_KEY=$(kubectl get secret -n llm-system litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)

# Send sequential verification requests
for i in {1..5}; do
  curl -s -X POST "https://gw.jpgcp.cloud/litellm/v1/chat/completions" \
    -H "Authorization: Bearer *** \
    -H "Content-Type: application/json" \
    -d '{
      "model": "gemini-3.6-flash-freelayer",
      "messages": [{"role": "user", "content": "Ping"}],
      "max_tokens": 64
    }' | jq -c '{content: .choices[0].message.content, model: .model}'
done
```

---

## 4. Troubleshooting Reference Matrix

| Symptom | Probable Cause | Diagnostic Command & Resolution |
| :--- | :--- | :--- |
| `SecretSyncedError / 404 NotAuthorized` | OCI Secret name mismatches `remoteRef.key`, or IAM lacks KMS decryption privileges | Check `oci vault secret list`; verify policy includes `use keys` and `use key-delegate`. |
| Pod crashes with `ValueError: Either 'host' or 'url' must be specified` | `config.yaml` references an unbound environment variable | Check `config.yaml` in `ConfigMap`; embed non-sensitive endpoints (like Redis DNS) directly in YAML. |
| Repeated 429 without automatic key failover | `model_name` strings do not match across deployment items | Verify all deployment entries in `model_list` share the identical `model_name` string to form a unified routing group. |
| Client receives `504 Gateway Timeout` | Complex reasoning models exceed gateway default timeout | Add `konghq.com/read-timeout: "180000"` to `Service` or `HTTPRoute` annotations. |
