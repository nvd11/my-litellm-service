# K3s + ArgoCD 中的密码管理

Kubernetes 中的密码管理，不只是创建一个 `Secret` 对象，还要明确密码的真实来源、读取权限、注入方式、轮换流程，以及集群重建后如何恢复。

本文只讨论当前 LiteLLM 项目的密码管理方案。

## 1. 当前需要管理的凭证

Phase 1 只有三类运行时敏感信息：

| 凭证 | Kubernetes 环境变量 | 用途 |
|---|---|---|
| Gemini API Key | `OPENAI_API_KEY_FREE_1` | LiteLLM 访问上游 Gemini API |
| LiteLLM Master Key | `LITELLM_MASTER_KEY` | 客户端访问 LiteLLM Proxy |
| Redis 密码 | `REDIS_PASSWORD` | LiteLLM 访问 Redis |

三者属于不同的身份体系：

- Gemini API Key 只能用于上游模型 API；
- LiteLLM Master Key 用于客户端访问 LiteLLM；
- Redis 密码只用于 Redis 认证；
- OCI API Signing Key 用于 ESO 访问 OCI Vault。

这些凭证不能互相替代。比如，Gemini API Key 不能访问 OCI Vault，OpenAI API Key 也不能访问 GCP Secret Manager。

Phase 1 暂不管理 OCI MySQL、Prisma、LiteLLM Virtual Key 和用户级预算凭证。

## 2. ConfigMap 与 Kubernetes Secret

### 2.1 ConfigMap 保存非敏感配置

可以放入 ConfigMap 的内容包括：

```text
LITELLM_PORT=4000
REDIS_HOST=redis.redis.svc.cluster.local
REDIS_PORT=6379
NO_PROXY=localhost,127.0.0.1,10.0.0.0/8,.svc,.cluster.local
```

LiteLLM 的 `config.yaml` 也可以挂载为 ConfigMap，但只能引用环境变量：

```yaml
model_list:
  - model_name: gemini-3.6-flash-freelayer
    litellm_params:
      model: gemini/gemini-3.6-flash
      api_key: os.environ/OPENAI_API_KEY_FREE_1
```

ConfigMap 不能保存 Gemini API Key、LiteLLM Master Key、Redis 密码、OCI 私钥、TLS 私钥或数据库密码。

### 2.2 Kubernetes Secret 不是自动加密

Kubernetes Secret 的 `data` 通常只是 Base64 编码：

```yaml
data:
  REDIS_PASSWORD: <base64-value>
```

Base64 不是加密。Secret 的实际安全性依赖于 Kubernetes RBAC、API Server、etcd 加密、节点权限和备份权限。因此，不能把 Base64 后的 Secret YAML 当作安全文件提交到 Git。

## 3. 密码保存方案

### 3.1 `.env` 文件

本地测试可以使用：

```bash
uv run --env-file .env litellm --config config.yaml
```

`.env` 适合开发机，不适合集群长期运行。它没有集中权限、自动轮换和灾备能力，也容易误提交到 Git。`.env` 必须被 `.gitignore` 排除。

### 3.2 手工创建 Kubernetes Secret

可以在集群外手工创建：

```bash
kubectl -n llm-system create secret generic litellm-secrets \
  --from-literal=LITELLM_MASTER_KEY='...' \
  --from-literal=OPENAI_API_KEY_FREE_1='...' \
  --from-literal=REDIS_PASSWORD='...'
```

优点是密码不进入 Git，缺点是：

- 集群重建时需要人工重新创建；
- 密码轮换需要人工执行；
- 创建过程不在 Git 历史中；
- 密码可能进入 shell history 或进程信息；
- ArgoCD 不知道这个 Secret 的声明状态。

它适合临时测试或 Bootstrap，不适合作为长期密钥来源。

### 3.3 把原生 Secret YAML 提交到 Git

不应该把明文或 Base64 编码后的原生 Secret 提交到 Git：

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: litellm-secrets
stringData:
  LITELLM_MASTER_KEY: real-secret
```

Git 会永久保留旧版本。即使删除当前文件，旧 commit、fork、CI 日志和备份中仍可能存在密码。

### 3.4 Sealed Secrets

Sealed Secrets 在集群外使用公钥加密密码，把加密后的 `SealedSecret` 提交到 Git；集群内 controller 使用私钥解密并生成普通 Kubernetes Secret。

```text
明文 Secret
    ↓ 公钥加密
SealedSecret 密文
    ↓ Git / ArgoCD
Sealed Secrets Controller
    ↓ 私钥解密
Kubernetes Secret
```

优点：密文可以进入 Git，ArgoCD 可以恢复 `SealedSecret` 对象，不依赖云厂商。

风险：controller 私钥丢失后旧密文无法解密；私钥泄露后密文可能被解密。因此 controller 私钥需要独立备份和保护。

Sealed Secrets 解决的是“如何把密文放进 Git”，不等于外部 Secret Manager。

### 3.5 External Secrets Operator

External Secrets Operator，简称 ESO，是运行在 Kubernetes 中的 controller。它从外部 Secret Manager 读取密码，再生成 Kubernetes Secret。

```text
外部 Secret Manager
    ↓ ESO provider
ExternalSecret
    ↓ ESO
Kubernetes Secret
    ↓
LiteLLM Pod
```

`ExternalSecret` 只保存引用和字段映射，不保存真实密码：

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

ESO 的优势是密码保存在外部系统，Git 只保存引用，密码轮换后可以自动同步；但 ESO 自己必须先拥有访问外部系统的身份。

## 4. 当前方案：OCI Vault + ESO

当前项目使用 OCI Secret Management Service，也就是 OCI Vault，作为运行时密码的真实来源。

计划的资源结构：

```text
Tenancy
└── litellm-prod
    └── LiteLLM Vault
        ├── litellm/openai-api-key-free-1
        ├── litellm/master-key
        └── litellm/redis-password
```

映射关系：

| OCI Secret | Kubernetes Secret 字段 | Pod 环境变量 |
|---|---|---|
| `litellm/openai-api-key-free-1` | `OPENAI_API_KEY_FREE_1` | `OPENAI_API_KEY_FREE_1` |
| `litellm/master-key` | `LITELLM_MASTER_KEY` | `LITELLM_MASTER_KEY` |
| `litellm/redis-password` | `REDIS_PASSWORD` | `REDIS_PASSWORD` |

Git 中只保存名称和映射，真实值保存在 OCI Vault 和运行时 Kubernetes Secret 中。

### 4.1 OCI Compartment 与最小权限

LiteLLM 的 OCI 资源计划放在独立 Compartment：

```text
Compartment: litellm-prod
Region: ap-singapore-1
```

ESO 使用专用读取身份：

```text
User:  litellm-vault-reader
Group: litellm-vault-readers
```

Policy 只允许读取目标 Compartment 中的 Secret：

```text
Allow group litellm-vault-readers
to read secret-bundles
in compartment litellm-prod
```

该身份不应拥有创建、删除或修改 Vault、Secret、加密密钥或其他 OCI 资源的权限。

### 4.2 OCI API Signing Key

当前集群运行在 Tencent K3s，不是 OCI OKE，因此不能直接假设 OCI Workload Identity 可用。Phase 1 使用 OCI User Principal 和 API Signing Key。

ESO 官方 OCI provider 支持 `UserPrincipal`、`InstancePrincipal` 和 `Workload`，当前固定使用 `UserPrincipal`。

准确的 `SecretStore` 格式为：

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

Bootstrap Secret 格式为：

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: oci-litellm-vault-reader
  namespace: llm-system
type: Opaque
stringData:
  privateKey: |
    -----BEGIN PRIVATE KEY-----
    <OCI_API_SIGNING_PRIVATE_KEY>
    -----END PRIVATE KEY-----
  fingerprint: "<OCI_API_KEY_FINGERPRINT>"
```

`privateKey` 和 `fingerprint` 放在 Kubernetes Bootstrap Secret 中；`user`、`tenancy`、`region` 和 `vault` 放在 `SecretStore` provider 配置中。

## 5. Bootstrap Secret 与循环依赖

ESO 必须先拥有 OCI API Signing Key，才能访问 OCI Vault。如果 API Signing Key 也放在 OCI Vault，就会形成循环依赖：

```text
ESO 需要 API Signing Key
    ↓
API Signing Key 位于 OCI Vault
    ↓
读取 OCI Vault 又需要 API Signing Key
```

所以 API Signing Key 是 Bootstrap Secret，必须由集群外安全流程提前写入 Kubernetes：

```text
OCI API Signing Key
    ↓ 集群外安全创建
llm-system/oci-litellm-vault-reader
    ↓
ESO
    ↓
OCI Vault
    ↓
llm-system/litellm-secrets
```

Bootstrap Secret：

- 不进入 Git、ConfigMap、镜像或 CI 日志；
- 不由 ExternalSecret 创建；
- 需要通过独立安全流程备份和重新创建。

## 6. Namespace 设计

计划使用：

```text
external-secrets
└── ESO Controller

llm-system
├── oci-litellm-vault-reader
├── oci-litellm-vault-store
├── litellm-secrets
└── LiteLLM Pod
```

当前使用命名空间级 `SecretStore`，因此 Bootstrap Secret 必须和 `SecretStore` 位于同一个 Namespace：

```text
SecretStore:      llm-system/oci-litellm-vault-store
Bootstrap Secret: llm-system/oci-litellm-vault-reader
```

不能把 Bootstrap Secret 只放在 `external-secrets`，再让 `llm-system` 中的 `SecretStore` 直接引用它。

`ClusterSecretStore` 是集群级对象，可以被多个 Namespace 使用，但权限范围更大。当前 Phase 1 使用 `SecretStore`，不增加跨 Namespace 共享身份的复杂度。

## 7. ArgoCD 的恢复边界

ArgoCD 能恢复 Git 中声明的 Kubernetes 对象，例如：

- `SecretStore`；
- `ExternalSecret`；
- `SealedSecret`；
- 普通 Kubernetes Secret（前提是它确实声明在 Git 中）。

例如，`ExternalSecret` 对象被删除后，ArgoCD 可以根据 Git 重新创建这个对象。ESO 看到对象后，会重新从 OCI Vault 读取数据并生成目标 Kubernetes Secret。

这里的“对象”指 Kubernetes API 中的资源对象，不是 OCI Vault 中的 Secret，也不是 Secret 明文。

ArgoCD 不能凭空恢复：

- 没有提交到 Git 的原生 Kubernetes Secret；
- 集群外手工创建的 Bootstrap Secret；
- OCI Vault 中已经删除且没有备份的 Secret；
- Sealed Secrets controller 的解密私钥；
- OCI API Signing Key 私钥。

恢复边界是：

```text
Git 中的 ExternalSecret
    → ArgoCD 可以恢复

OCI Vault 中的真实密码
    → OCI Vault 负责保存和恢复

Bootstrap Secret
    → 外部安全流程负责备份和重新创建
```

`selfHeal` 只表示 ArgoCD 可以把被手工修改或删除的 Git 管理对象恢复为 Git 中的状态，它不等于能够恢复 Git 之外的密码，也不能替代 OCI Vault 备份。

## 8. 为什么不选择 GCP Secret Manager

ESO 支持 GCP provider，但 GCP 的典型配置需要 GCP Service Account JSON：

```yaml
auth:
  secretRef:
    secretAccessKey:
      name: gcp-auth
      key: credentials.json
```

这个 JSON 是访问 GCP Secret Manager 的 IAM 身份，不是 Gemini API Key。当前 K3s 不运行在 GKE 上，不能直接假设 GCP Workload Identity 可用；项目已经选择 OCI Vault，因此不额外引入 GCP Service Account。

## 9. Master Key 与 Virtual Key

`LITELLM_MASTER_KEY` 是 LiteLLM Proxy 的主访问凭证，用于受控的管理和联调请求。它不是每位同事的独立账号，不适合长期多人共享。

Phase 1 暂不接入 MySQL、Prisma 和 LiteLLM Virtual Key，因此先使用受控的 Master Key。后续具备数据库和 Key 管理能力后，再为每位用户创建独立 Virtual Key，并设置预算、权限、过期时间和轮换策略。

## 10. 最终职责边界

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

| 组件 | 负责内容 |
|---|---|
| OCI Vault | 保存真实密码、版本和轮换后的值 |
| OCI IAM | 限制谁能读取 OCI Secret |
| Bootstrap Secret | 让 ESO 获得第一次访问 OCI Vault 的能力 |
| ESO | 从 OCI Vault 同步 Kubernetes Secret |
| ExternalSecret | 声明外部 Secret 到 Kubernetes Secret 的映射 |
| ArgoCD | 恢复 Git 中的 `ExternalSecret` 和 `SecretStore` 对象 |
| LiteLLM Pod | 使用环境变量，不负责保存密码来源 |

最终原则是：Git 管理引用和映射，OCI Vault 管理真实敏感值，ESO 负责同步，Kubernetes Secret 只作为 Pod 的运行时对象存在。
