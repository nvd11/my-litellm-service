# Secret Management in K3s + ArgoCD

Secret management in Kubernetes involves more than simply creating a `Secret` object. It requires clearly defining the single source of truth for secrets, read access permissions, injection mechanisms, rotation lifecycles, and disaster recovery procedures following cluster rebuilds.

This document focuses specifically on the secret management architecture for the LiteLLM project.

## 1. Credentials Requiring Management

Phase 1 introduces three runtime secrets:

| Credential | Kubernetes Environment Variable | Purpose |
|---|---|---|
| Gemini API Key | `OPENAI_API_KEY_FREE_1` | LiteLLM upstream access to Google Gemini API |
| LiteLLM Master Key | `LITELLM_MASTER_KEY` | Client authentication to LiteLLM Proxy |
| Redis Password | `REDIS_PASSWORD` | LiteLLM authentication to Redis |

These three credentials belong to distinct identity domains:

- The Gemini API Key is scoped exclusively to upstream model APIs;
- The LiteLLM Master Key is scoped to client-to-gateway access;
- The Redis password is scoped solely to Redis authentication;
- The OCI API Signing Key is used by External Secrets Operator (ESO) to authenticate with OCI Vault.

These credentials are not interchangeable. For instance, a Gemini API Key cannot access OCI Vault, nor can an OpenAI API Key authenticate against GCP Secret Manager.

Phase 1 does not yet manage OCI MySQL, Prisma, LiteLLM Virtual Keys, or user-level budget credentials.

## 2. ConfigMaps vs. Kubernetes Secrets

### 2.1 ConfigMaps Store Non-Sensitive Configuration

Values suitable for ConfigMaps include:

```text
LITELLM_PORT=4000
REDIS_HOST=redis.redis.svc.cluster.local
REDIS_PORT=6379
NO_PROXY=localhost,127.0.0.1,10.0.0.0/8,.svc,.cluster.local
```

LiteLLM's `config.yaml` can also be mounted via a ConfigMap, provided it references environment variables rather than embedding raw credentials:

```yaml
model_list:
  - model_name: gemini-3.6-flash-freelayer
    litellm_params:
      model: gemini/gemini-3.6-flash
      api_key: os.environ/OPENAI_API_KEY_FREE_1
```

ConfigMaps must never contain Gemini API keys, LiteLLM master keys, Redis passwords, OCI private keys, TLS private keys, or database credentials.

### 2.2 Kubernetes Secrets Are Not Encrypted by Default

The `data` field in a standard Kubernetes Secret is merely Base64-encoded plaintext:

```yaml
data:
  REDIS_PASSWORD: <base64-value>
```

Base64 is an encoding, not encryption. Actual Secret security relies on Kubernetes RBAC, API Server security, etcd encryption at rest, node access permissions, and backup controls. Consequently, Base64-encoded Secret YAMLs must never be committed to Git.

## 3. Secret Storage Approaches

### 3.1 Local `.env` Files

For local testing:

```bash
uv run --env-file .env litellm --config config.yaml
```

`.env` files are suitable for developer workstations but unfit for long-running cluster workloads. They lack centralized access control, automated rotation, and disaster recovery mechanisms, and present a continuous risk of accidental Git commits. `.env` files must be excluded via `.gitignore`.

### 3.2 Manually Created Kubernetes Secrets

Secrets can be created imperatively outside Git:

```bash
kubectl -n llm-system create secret generic litellm-secrets \
  --from-literal=LITELLM_MASTER_KEY='...' \
  --from-literal=OPENAI_API_KEY_FREE_1='...' \
  --from-literal=REDIS_PASSWORD='...'
```

While this prevents secrets from leaking into Git, it introduces significant operational drawbacks:

- Cluster rebuilds require manual intervention;
- Secret rotations must be executed manually;
- Creation workflows are absent from Git audit trails;
- Secrets can leak into shell histories or process tables;
- ArgoCD cannot manage or track the declarative state of the Secret.

This approach is acceptable for temporary testing or bootstrapping, but unsuitable as a long-term key management solution.

### 3.3 Committing Raw Secret YAMLs to Git

Raw Secrets (whether plaintext `stringData` or Base64 `data`) must never be committed to Git:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: litellm-secrets
stringData:
  LITELLM_MASTER_KEY: real-secret
```

Git histories are immutable. Even if deleted in subsequent commits, secrets persist in commit trees, forks, CI logs, and repository backups.

### 3.4 Sealed Secrets

Bitnami Sealed Secrets encrypts secrets client-side with an asymmetric public key. The encrypted `SealedSecret` custom resource is committed to Git, and an in-cluster controller decrypts it using its private key to generate a native Kubernetes Secret.

```text
Plaintext Secret
    ↓ Public key encryption
SealedSecret ciphertext
    ↓ Git / ArgoCD
Sealed Secrets Controller
    ↓ Private key decryption
Kubernetes Secret
```

**Pros**: Ciphertext can safely reside in Git; ArgoCD can declaratively reconcile `SealedSecret` resources without cloud vendor lock-in.

**Risks**: If the controller's private key is lost, existing ciphertexts cannot be decrypted. If the private key is compromised, all ciphertexts can be decrypted. The controller's private key requires external backup and protection.

Sealed Secrets addresses "how to safely commit ciphertext to Git," but is not an external secret manager.

### 3.5 External Secrets Operator (ESO)

External Secrets Operator (ESO) is a Kubernetes controller that fetches secrets from external Secret Management Services and synchronizes them into native Kubernetes Secrets.

```text
External Secret Manager
    ↓ ESO provider
ExternalSecret
    ↓ ESO
Kubernetes Secret
    ↓
LiteLLM Pod
```

The `ExternalSecret` manifest declares only remote references and key mappings, never the secret values:

```yaml
apiVersion: external-secrets.io/v1
kind: ExternalSecret
metadata:
  name: litellm-secrets
  namespace: llm-system
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: oci-litellm-vault-store
    kind: SecretStore
  target:
    name: litellm-secrets
    creationPolicy: Owner
  data:
    - secretKey: LITELLM_MASTER_KEY
      remoteRef:
        key: litellm/master-key
```

ESO's advantage is that real secrets remain centralized in external vaults while Git holds only declarative references. Secret rotations synchronize automatically. However, ESO itself must be bootstrapped with credentials to access the external provider.

## 4. Current Architecture: OCI Vault + ESO

This project utilizes OCI Secret Management Service (OCI Vault) as the authoritative source of truth for runtime credentials.

Target resource hierarchy:

```text
Tenancy
└── litellm-prod
    └── LiteLLM Vault
        ├── litellm/openai-api-key-free-1
        ├── litellm/master-key
        └── litellm/redis-password
```

Mapping:

| OCI Secret | Kubernetes Secret Key | Pod Environment Variable |
|---|---|---|
| `litellm/openai-api-key-free-1` | `OPENAI_API_KEY_FREE_1` | `OPENAI_API_KEY_FREE_1` |
| `litellm/master-key` | `LITELLM_MASTER_KEY` | `LITELLM_MASTER_KEY` |
| `litellm/redis-password` | `REDIS_PASSWORD` | `REDIS_PASSWORD` |

Git stores only resource names and mappings; actual values reside in OCI Vault and runtime Kubernetes Secrets.

### 4.1 OCI Compartment & Least Privilege

LiteLLM's OCI resources reside in a dedicated Compartment:

```text
Compartment: litellm-prod
Region: ap-singapore-1
```

ESO authenticates via a dedicated read-only identity:

```text
User:  litellm-vault-reader
Group: litellm-vault-readers
```

The IAM Policy strictly restricts access to reading secret bundles in the specified compartment:

```text
Allow group litellm-vault-readers
to read secret-bundles
in compartment litellm-prod
```

This identity possesses zero permissions to create, delete, or update vaults, secrets, cryptographic keys, or other OCI resources.

### 4.2 OCI API Signing Key

Because the cluster runs on Tencent Cloud K3s rather than OCI OKE, OCI Workload Identity is unavailable. Phase 1 employs an OCI User Principal authenticated with an API Signing Key.

The ESO OCI provider supports `UserPrincipal`, `InstancePrincipal`, and `Workload`. We use `UserPrincipal`.

The canonical `SecretStore` manifest is:

```yaml
apiVersion: external-secrets.io/v1
kind: SecretStore
metadata:
  name: oci-litellm-vault-store
  namespace: llm-system
spec:
  provider:
    oracle:
      vault: "<VAULT_OCID>"
      region: "ap-singapore-1"
      principalType: UserPrincipal
      auth:
        user: "<USER_OCID>"
        tenancy: "<TENANCY_OCID>"
        secretRef:
          privatekey:
            name: oci-litellm-vault-reader
            key: privateKey
          fingerprint:
            name: oci-litellm-vault-reader
            key: fingerprint
```

The Bootstrap Secret structure is:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: oci-litellm-vault-reader
  namespace: llm-system
type: Opaque
stringData:
  privateKey: |
    -----BEGIN RSA PRIVATE KEY-----
    [REDACTED PRIVATE KEY]
    -----END RSA PRIVATE KEY-----
  fingerprint: "<OCI_API_KEY_FINGERPRINT>"
```

`privateKey` and `fingerprint` are injected via the Kubernetes Bootstrap Secret; `user`, `tenancy`, `region`, and `vault` are declared in the `SecretStore` provider spec.

## 5. Bootstrap Secrets and Circular Dependencies

ESO must possess an OCI API Signing Key before it can query OCI Vault. If the API Signing Key itself were stored in OCI Vault, a circular dependency would occur:

```text
ESO requires API Signing Key
    ↓
API Signing Key stored in OCI Vault
    ↓
Accessing OCI Vault requires API Signing Key
```

Therefore, the API Signing Key serves as a Bootstrap Secret and must be provisioned out-of-band:

```text
OCI API Signing Key
    ↓ Out-of-band secure provisioning
llm-system/oci-litellm-vault-reader
    ↓
ESO
    ↓
OCI Vault
    ↓
llm-system/litellm-secrets
```

The Bootstrap Secret:

- Never enters Git, ConfigMaps, container images, or CI logs;
- Is never managed by ExternalSecret;
- Must be backed up and recreated via independent secure automation or runbooks.

## 6. Namespace Architecture

The namespace hierarchy:

```text
external-secrets
└── ESO Controller

llm-system
├── oci-litellm-vault-reader
├── oci-litellm-vault-store
├── litellm-secrets
└── LiteLLM Pod
```

Because a namespace-scoped `SecretStore` is used, the Bootstrap Secret must reside in the exact same namespace as the `SecretStore`:

```text
SecretStore:      llm-system/oci-litellm-vault-store
Bootstrap Secret: llm-system/oci-litellm-vault-reader
```

A Bootstrap Secret located exclusively in `external-secrets` cannot be referenced directly by a `SecretStore` inside `llm-system`.

While `ClusterSecretStore` is a cluster-wide resource that can be shared across multiple namespaces, it grants wider permissions. Phase 1 sticks to namespaced `SecretStore` resources to avoid unnecessary cross-namespace privilege exposure.

## 7. ArgoCD Disaster Recovery Boundaries

ArgoCD reconciles Kubernetes objects declared in Git, including:

- `SecretStore`;
- `ExternalSecret`;
- `SealedSecret`;
- Native Kubernetes Secrets (provided they are explicitly defined in Git).

For example, if an `ExternalSecret` resource is accidentally deleted, ArgoCD reconciles and recreates it from Git. ESO then detects the resource, pulls the latest secret bundles from OCI Vault, and regenerates the target Kubernetes Secret.

Here, "objects" refers to Kubernetes API resources, not the remote secrets in OCI Vault, nor plaintext secret values.

ArgoCD cannot recover:

- Native Kubernetes Secrets not committed to Git;
- Manually created out-of-band Bootstrap Secrets;
- Secrets deleted from OCI Vault without external backups;
- The Sealed Secrets controller private decryption key;
- The OCI API Signing Key private key.

The recovery boundary is:

```text
ExternalSecret in Git
    → Recoverable by ArgoCD

Authoritative Secrets in OCI Vault
    → Backed up and recovered by OCI Vault

Bootstrap Secrets
    → Backed up and recreated via out-of-band runbooks
```

`selfHeal` guarantees that ArgoCD will restore Git-managed objects that are manually altered or deleted back to their Git-declared state. It does not magically restore secrets outside Git or replace external vault backups.

## 8. Rationale for Not Using GCP Secret Manager

While ESO supports GCP Secret Manager, authenticating from non-GCP infrastructure requires a GCP Service Account JSON key:

```yaml
auth:
  secretRef:
    secretAccessKey:
      name: gcp-auth
      key: credentials.json
```

This JSON key represents IAM authentication to GCP Secret Manager, not the upstream Gemini API Key. Because K3s is hosted outside GKE, GCP Workload Identity is unavailable. Since the infrastructure already relies on OCI Vault, avoiding redundant GCP Service Account key management simplifies the operational surface.

## 9. Master Key vs. Virtual Keys

`LITELLM_MASTER_KEY` is LiteLLM Proxy's root administrative credential, reserved for restricted operational and integration calls. It is not intended as individual user credentials and must not be shared broadly.

Because Phase 1 does not integrate MySQL, Prisma, or LiteLLM Virtual Keys, the controlled Master Key is used initially. Once database and key management capabilities are introduced, individual Virtual Keys will be provisioned per user with fine-grained budgets, rate limits, expiration dates, and rotation policies.

## 10. Summary of Architectural Responsibilities

```text
OCI Vault
  ├── Gemini API Key
  ├── LiteLLM Master Key
  └── Redis Password
        ↓ OCI IAM Policy
External Secrets Operator
        ↑
llm-system/oci-litellm-vault-reader
        ↓
llm-system/oci-litellm-vault-store
        ↓
llm-system/litellm-secrets
        ↓
LiteLLM Pod
```

| Component | Responsibility |
|---|---|
| OCI Vault | Authoritative source for secret values, version history, and rotations |
| OCI IAM | Enforces least-privilege read access to OCI Secrets |
| Bootstrap Secret | Provides initial authentication for ESO to access OCI Vault |
| ESO | Synchronizes secret bundles from OCI Vault into Kubernetes Secrets |
| ExternalSecret | Declares remote-to-local key mappings and sync policies |
| ArgoCD | Reconciles Git-declared `ExternalSecret` and `SecretStore` resources |
| LiteLLM Pod | Consumes environment variables; completely decoupled from vault internals |

The core guiding principle: Git manages declarative mappings and references, OCI Vault manages raw sensitive values, ESO handles synchronization, and Kubernetes Secrets exist purely as transient runtime artifacts for application Pods.
