# 新增 LLM API Key 标准接入与配置指南 (New API Key Onboarding Guide)

本文档为 `my-litellm-service` 统一网关在引入新的上游大模型 API Key（例如新增第二个 Gemini 免费 Key、Anthropic Claude Key、OpenAI 官方 Key 等）时的**标准规范化接入操作指引**。

整个接入链路严格遵循 **Zero Secrets in Git（代码仓库零机密）**、**12-Factor 配置解耦** 与 **GitOps 声明式发布** 原则。

---

## 1. 跨层架构与改动全景图

当引入一个新 API Key 时，各层级组件的职责与修改范围如下：

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ 1. 云端机密托管层 (OCI Vault: litellm-prod)                                   │
│    新建 Secret 对象 (例如: litellm-openai-api-key-free-2)                     │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ (ESO 自动化按需拉取)
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 2. GitOps 清单仓库 (my-argocd-manifests)                                    │
│    修改文件: argocd-apps/litellm-svc-app.yaml                                │
│    - a. externalSecret.data: 声明新 Key 到 Kubernetes Secret 的映射关系      │
│    - b. configMap.data["config.yaml"]: 在 model_list 中挂载新 Key 并配置路由  │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ (ArgoCD 自动同步并热更新 Pod)
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ 3. 应用代码仓库 (my-litellm-service)                                         │
│    修改文件:                                                                 │
│    - config.yaml: 本地模型路由配置同步                                       │
│    - .env.example: 补充新环境变量占位符                                      │
│    - app/core/config.py: 在 Settings 类中追加 Pydantic 类型校验支持          │
│    - .env (本地未跟踪私密文件): 本地开发与单元测试注入                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. 详细执行步骤 (Step-by-Step)

### 第一步：在 OCI Vault 创建云端机密 (Layer 1)

> ⚠️ **安全红线**：真实密钥绝不进入 Git 仓库，必须通过 OCI 托管 Vault 加密存储。

1. **登录 OCI 控制台或使用 OCI CLI**；
2. **定位目标 Vault**：
   - **Compartment**：`litellm-prod`
   - **Vault**：`litellm-vault`
   - **Master Encryption Key**：`litellm-secrets-key`
3. **创建新 Secret**：
   - **Secret 名称规范**：`litellm-<provider>-api-key-<alias>`（例如 `litellm-openai-api-key-free-2`）
   - **Secret 格式**：纯文本（Plaintext）或 Base64 编码的 API Key 字符串。
4. **CLI 快速创建示例**：
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

### 第二步：修改 GitOps 清单仓库 (Layer 2: `my-argocd-manifests`)

* **目标代码仓库**：`nvd11/my-argocd-manifests`
* **目标文件路径**：`argocd-apps/litellm-svc-app.yaml`

#### 2.1 修改 `externalSecret.data` (声明机密同步映射)
在 Helm values 的 `externalSecret.data` 中追加新字段映射：

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
    # 🎯 新增下行：将 OCI 的 litellm-openai-api-key-free-2 映射为容器环境变量 OPENAI_API_KEY_FREE_2
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

#### 2.2 修改 `configMap.data["config.yaml"]` (配置模型负载均衡与路由)
在 `model_list` 中增加使用新环境变量的条目，并在 `router_settings` 开启负载均衡与重试：

```yaml
configMap:
  enabled: true
  name: litellm-config
  data:
    NO_PROXY: "localhost,127.0.0.1,10.0.0.0/8,.svc,.cluster.local"
    config.yaml: |
      model_list:
        # Key 1 实例
        - model_name: gemini-3.6-flash-freelayer
          litellm_params:
            model: gemini/gemini-3.6-flash
            api_key: os.environ/OPENAI_API_KEY_FREE_1
            rpm: 15
        # 🎯 新增：Key 2 实例（同名模型组，自动负载均衡）
        - model_name: gemini-3.6-flash-freelayer
          litellm_params:
            model: gemini/gemini-3.6-flash
            api_key: os.environ/OPENAI_API_KEY_FREE_2
            rpm: 15

      router_settings:
        routing_strategy: "least-busy" # 自动选择最空闲的 Key
        num_retries: 3                # 遇到 429 自动换 Key 重试
        retry_after: 5
        cooldown_time: 30             # 429 熔断冷却 30 秒

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

### 第三步：修改应用源码仓库 (Layer 3: `my-litellm-service`)

* **目标代码仓库**：`nvd11/my-litellm-service`

#### 3.1 同步本地 `config.yaml`
保持本地 `config.yaml` 与 GitOps 中的配置一致，便于本地开发与冒烟测试。

#### 3.2 更新 `.env.example`
向环境变量模板补充无害占位符：
```env
# Google Gemini API
OPENAI_API_KEY_FREE_1=replace-with-gemini-api-key-1
OPENAI_API_KEY_FREE_2=replace-with-gemini-api-key-2
```

#### 3.3 更新 `app/core/config.py` (Pydantic 配置校验类)
在 `Settings` 类中添加可选或必填字段支持：
```python
class Settings(BaseSettings):
    # ...
    openai_api_key_free_1: SecretStr
    openai_api_key_free_2: SecretStr | None = None  # 👈 新增
    # ...
```

#### 3.4 更新本地 `.env` (本地生效)
在本地未提交的 `.env` 中填写真实密钥，用于本地运行与测试：
```env
OPENAI_API_KEY_FREE_2=AIzaSy...
```

---

## 3. 验收与验证流程 (Verification Checklist)

完成上述配置并提交 Git 后，执行以下标准验证步骤：

### 1. 验证 OCI Secret 同步到 Kubernetes
```bash
# 检查 ExternalSecret 是否同步成功
kubectl get externalsecret litellm-secrets -n llm-system

# 验证生成的 Kubernetes Secret 包含新 Key 字段（输出不含明文）
kubectl get secret litellm-secrets -n llm-system -o jsonpath='{.data}' | jq 'keys'
# 预期包含: ["LITELLM_MASTER_KEY", "OPENAI_API_KEY_FREE_1", "OPENAI_API_KEY_FREE_2", "REDIS_PASSWORD"]
```

### 2. 验证 ArgoCD 自动发布
```bash
# 检查 ArgoCD 应用状态
kubectl get application litellm-svc -n argocd
# 预期状态: Synced / Healthy

# 检查目标集群 Pod 滚动更新状态
kubectl get pods -n llm-system -o wide
# 预期状态: 1/1 Running, 0 重启
```

### 3. 验证公网 API 负载均衡与连通性
```bash
MASTER_KEY=$(kubectl get secret -n llm-system litellm-secrets -o jsonpath='{.data.LITELLM_MASTER_KEY}' | base64 -d)

# 发送连续多次测试请求
for i in {1..5}; do
  curl -s -X POST "https://gw.jpgcp.cloud/litellm/v1/chat/completions" \
    -H "Authorization: Bearer ${MASTER_KEY}" \
    -H "Content-Type: application/json" \
    -d '{
      "model": "gemini-3.6-flash-freelayer",
      "messages": [{"role": "user", "content": "Ping"}],
      "max_tokens": 64
    }' | jq -c '{content: .choices[0].message.content, model: .model}'
done
```

---

## 4. 常见问题排查速查表 (Troubleshooting)

| 异常现象 | 可能原因 | 排查命令与解决方案 |
| :--- | :--- | :--- |
| `SecretSyncedError / 404 NotAuthorized` | OCI 上的 Secret 名称与 `remoteRef.key` 不匹配，或 IAM 缺少解密权限 | 检查 `oci vault secret list`，确保名称完全一致；确认 Policy 包含 `use keys`。 |
| Pod 报 `ValueError: Either 'host' or 'url' must be specified` | `config.yaml` 引用了未注入的环境变量 | 检查 `ConfigMap` 中的 `config.yaml`，非敏感地址（如 Redis 域名）建议直接内聚在 YAML 中。 |
| 频繁报 429 且未自动切换 | `model_name` 未保持完全一致，未能聚合成同一个模型负载均衡组 | 检查 `model_list` 中所有条目的 `model_name` 必须完全相同。 |
| 客户端报 `504 Gateway Timeout` | 复杂推理/思考模型耗时较长，超过网关默认超时 | 在 `Service` 或 `HTTPRoute` 注解中配置 `konghq.com/read-timeout: "180000"`。 |
