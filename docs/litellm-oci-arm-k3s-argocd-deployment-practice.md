# 实战记录：基于 GitHub Actions、ArgoCD 与 Kong 网关将 LiteLLM 部署至 OCI ARM 节点的完整踩坑与落地实践

## 1. 架构目标与部署背景

在多模型网关系统的建设中，我们希望将 **LiteLLM Proxy** 作为底层大模型统一接入层（Service A），部署在已有的跨云 Kubernetes（Tencent Cloud K3s 控制面 + OCI `free-arm-vm` ARM64 工作节点）集群中。

整个发布与运行链路要求满足以下生产级工程标准：

1. **不可变 GitOps 交付**：使用 GitHub Actions 构建多架构容器镜像，通过 Digest Pinning（内容哈希寻址）自动触发 ArgoCD 发布，杜绝依赖会漂移的 tag；
2. **机密全生命周期托管（Zero Secrets in Git）**：真实 API Key 和 Redis 密码统一保存在 OCI Vault 中，通过 External Secrets Operator（ESO）自动同步到集群内 Secret，代码仓库中仅保留结构映射；
3. **12-Factor 配置与机密解耦**：非敏感配置（LiteLLM 模型列表、Redis 连接参数、网络代理白名单）由 `ConfigMap` 承载，敏感机密由 `Secret` 承载，容器通过 `envFrom` 与卷挂载解耦消费；
4. **统一入口网关（Kong + Gateway API）**：不为服务单独部署额外 Ingress 控制器，复用现有 Kong Gateway 暴露公网 IP，通过 Kubernetes Gateway API 的 `HTTPRoute` 实现跨命名空间转发与路径前缀剥离。

最终的系统数据流与部署拓扑如下：

```
[ 外部客户端 / 业务系统 ]
         │ (HTTP :31850 /litellm/v1/...)
         ▼
[ OCI free-arm-vm: Kong Gateway (NodePort) ]
         │ (Gateway API HTTPRoute 剥离 /litellm 前缀)
         ▼
[ LiteLLM Pod (:4000) on free-arm-vm ] ──(DNS)──► [ K3s Redis (:6379) on free-arm-vm ]
         │ (环境变量解耦注入)
         ├─► [ ConfigMap: litellm-config (/app/config/config.yaml) ]
         └─► [ Secret: litellm-secrets (来自 OCI Vault 的 Gemini Key / Master Key / Redis 密码) ]
         │
         ▼ (HTTPS 出站)
[ Google Gemini API (gemini-3.6-flash / gemini-3.7-flash) ]
```

---

## 2. CI 与镜像构建：解决多架构与 Tag 漂移问题

### 2.1 多架构构建与 Digest Pinning

`free-arm-vm` 是 4C24G 的 ARM64 实例，而开发机与 CI Runner 多为 AMD64。因此 GitHub Actions 工作流必须生成多架构 Manifest Index：

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

### 2.2 为什么必须使用 Digest Pinning 而非 Commit Tag？

在初期验证时，我们发现：**相同的 Git Commit SHA 再次触发构建时，GHCR 中生成的镜像 Tag 名称虽然相同，但由于基础镜像更新或 BuildKit 元数据变化，Tag 指向的底层 Manifest Index Digest 发生了改变**。

如果 ArgoCD 直接跟踪 `sha-<commit>`，当 CI 重跑后，Git 仓库中的清单没有任何提交记录，但线上拉取的镜像内容已经发生漂移，这严重破坏了 GitOps 的可复现性与审计能力。

因此，我们升级了共享 Helm Chart（`generic-web-service-v2` v2.1.0），在 Deployment 模板中引入 Digest 优先机制：

```yaml
# templates/deployment.yaml
image: {{- if .Values.image.digest }} "{{ .Values.image.repository }}@{{ .Values.image.digest }}"{{- else }} "{{ .Values.image.repository }}:{{ .Values.image.tag }}"{{- end }}
```

CI 构建成功后，通过 `repository_dispatch` 自动向 GitOps 清单仓库（`my-argocd-manifests`）提交具体的 `image.digest: sha256:...`，实现内容级别的严格锁定。

---

## 3. 机密管理：OCI Vault 与 External Secrets Operator (ESO) 落地

为了实现“零机密进 Git”，我们采用了 OCI 托管的 Vault 与 Kubernetes 内的 External Secrets Operator（ESO）。

### 3.1 架构与 Bootstrap 凭证设计

ESO 负责将 OCI Vault 中的机密拉取为 Kubernetes Secret，但 ESO 访问 OCI 也需要身份证明。为此，我们建立了专用的机器账号（User Principal）：

1. **OCI IAM 用户**：`litellm-vault-reader`；
2. **OCI IAM 组**：`litellm-vault-readers`；
3. **本地私钥**：`/home/gateman/keys/litellm-vault-reader.pem`（公钥指纹已上传至 OCI 用户下）；
4. **Bootstrap Secret**：在集群目标命名空间 `llm-system` 中手工注入一次性凭据：

```bash
kubectl create secret generic oci-litellm-vault-reader -n llm-system \
  --from-file=privateKey=/home/gateman/keys/litellm-vault-reader.pem \
  --from-literal=fingerprint="b3:f3:cc:2b:b0:9d:88:c9:75:08:0f:82:e5:b6:e1:a1"
```

### 3.2 踩坑排查：OCI IAM 权限与密钥解密死锁

在配置好 `SecretStore` 和 `ExternalSecret` 后，发现 Secret 始终无法同步，ESO 控制器抛出错误：

```text
Secrets service failed to GetSecretBundleByName, HTTP status code 404: Authorization failed or requested resource not found.
```

#### 排查与原因定位：
1. 使用 OCI CLI 工具带着 `litellm-vault-reader` 凭据复现请求，同样复现了 `404 NotAuthorizedOrNotFound`；
2. 检查 OCI 用户属性，发现虽然创建了用户 `litellm-vault-reader`，但**该用户未被加入 `litellm-vault-readers` 组**；
3. OCI Vault 的机密内容由 KMS 主密钥（Master Encryption Key）加密存储。调用 `GetSecretBundleByName` 时，OCI 底层需要解密机密载荷。原 IAM Policy 仅配置了 `read secret-bundles`，缺少 KMS 密钥解密权限。

#### 修复方案：
1. 将用户加入专属组：
   ```bash
   oci iam group add-user \
     --group-id "ocid1.group.oc1..aaaaaaaajvxytvkmyupwguyzza27dsx2ovbtv26sg2dyppdsnuluxdpe2zja" \
     --user-id "ocid1.user.oc1..aaaaaaaa3fcoxuuzcdtwav4mcsqwxznzsarmj6ctugtnravseu5i5hst7neq"
   ```
2. 补齐 Policy 权限（加入 `use keys` 和 `use key-delegate`）：
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

策略更新生效后，ESO 立即完成了密钥抓取，`llm-system/litellm-secrets` 状态转为 `SecretSynced / Ready: True`，成功生成包含 `OPENAI_API_KEY_FREE_1`、`LITELLM_MASTER_KEY` 和 `REDIS_PASSWORD` 的 Kubernetes Secret。

### 3.3 跨云 DNS 抖动优化：`internalTrafficPolicy: Local`

由于集群节点分布在腾讯云与 OCI，早期 ESO 控制器在跨节点请求 CoreDNS（`10.43.0.10:53`）时，因 UDP 流量走 Tailscale 叠加网络偶发超时。

优化方案：
1. 将 CoreDNS 扩容至 3 副本，确保 OCI `free-arm-vm` 本地运行一个 CoreDNS Pod；
2. 将 `kube-dns` Service 的内部流量策略由 `Cluster` 切换为 `Local`：
   ```bash
   kubectl patch svc kube-dns -n kube-system --type merge -p '{"spec":{"internalTrafficPolicy":"Local"}}'
   ```
此后本节点所有 Pod 的 DNS 解析请求均直接由本地 CoreDNS 承载，耗时降至 0ms，彻底消除了网络超时告警。

---

## 4. ArgoCD 控制面排错与 GitOps 自动化发布

### 4.1 控制面假死排查（SQLite IO Wait 与跨节点网络）

在配置好 Application 清单后，发现 ArgoCD Web 界面无法打开，应用同步状态卡在 `Unknown`。

#### 排查过程：
1. 登录阿里云 Master 控制节点查看 K3s 日志（`journalctl -u k3s`），发现系统每隔 30 秒打印 `vxlan_network.go: external interface not found`；
2. 检查节点负载（`top`），发现 CPU `100% wa`（I/O Wait），内存使用率超过 95%；
3. 原因为：阿里云 Master 为 2C2G 规格，底层的 Kine (SQLite) 累计了 78 天共 420 万次历史 revision，在执行压缩检查点时产生剧烈磁盘 I/O；同时所有 ArgoCD 组件挤在同一节点加剧了内存竞争。

#### 修复与拓扑优化：
1. 重启 Master 节点的 k3s 进程（`systemctl restart k3s`），重新初始化虚拟网桥；
2. 重新编排 ArgoCD Pod 拓扑：
   - 核心通信后台（`application-controller`、`repo-server`、`redis`）保留在 Master 节点，避免跨云跨节点 gRPC 远程调用；
   - 较重的 Web 界面（`argocd-server`）调度至 OCI 节点；
3. 调整后，Master 节点 I/O Wait 降为 0%，CPU 空闲率达到 99%，ArgoCD 所有 Application 状态恢复为 `Synced` & `Healthy`。

---

## 5. LiteLLM 容器运行时踩坑与 12-Factor 配置重构

### 5.1 踩坑：Redis 配置丢失导致容器 CrashLoopBackOff

ArgoCD 将 Pod 调度到 `free-arm-vm` 后，Pod 启动失败并反复重启。查看日志发现报错：

```text
Setting Cache on Proxy
File "/app/.venv/lib/python3.12/site-packages/litellm/_redis.py", line 475, in _get_redis_client_logic
    raise ValueError("Either 'host' or 'url' must be specified for redis.")
ValueError: Either 'host' or 'url' must be specified for redis.
```

**根本原因**：
在 `config.yaml` 中，Redis 主机配置为 `host: os.environ/REDIS_HOST`。但在 Deployment 的环境变量注入中，我们只注入了来自 Secret 的敏感密码，遗漏了 `REDIS_HOST` 环境变量，导致读取为 `None`，LiteLLM 初始化缓存直接崩溃。

### 5.2 优雅重构：拒绝 Deployment Hardcode，践行标准 12-Factor

为了彻底避免在 Deployment 的 values 中硬编码散乱的环境变量，我们将配置进行了三层解耦重构：

1. **配置文件（ConfigMap Volume）**：Redis 主机和端口属于集群内部非敏感网络属性，直接写在 ConfigMap 挂载的 `config.yaml` 中：
   ```yaml
   litellm_settings:
     cache: true
     cache_params:
       type: redis
       host: redis.redis.svc.cluster.local # 👈 直接内聚在配置文件中
       port: 6379
       password: os.environ/REDIS_PASSWORD  # 👈 仅密码引用环境变量
       supported_call_types: [chat_completion]
       ttl: 3600
   ```
2. **非敏感环境变量（ConfigMap envFrom）**：`NO_PROXY` 等网络参数通过 ConfigMap 注入；
3. **敏感机密（Secret envFrom）**：`OPENAI_API_KEY_FREE_1`、`LITELLM_MASTER_KEY`、`REDIS_PASSWORD` 通过 Secret 注入。

重构后的 Deployment 变得极其干净，移除了所有 `env:` 块：

```yaml
envFrom:
  - configMapRef:
      name: litellm-config
  - secretRef:
      name: litellm-secrets
```

### 5.3 探针冷启动策略调整

LiteLLM 在 ARM64 节点启动时，加载模型定义与初始化 Redis 连接需要约 30-40 秒。初始配置的 `initialDelaySeconds: 15` 会导致 Kubelet 在应用尚未监听端口前判定探针失败并强杀容器。

将探针宽限期调整为符合实际冷启动特性的值：
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
调整后，Pod 启动平滑，**0 重启直接进入 `1/1 Running`**。

---

## 6. 网关层接入：Kong Gateway API 路由与超时加固

### 6.1 踩坑 1：Gateway 跨命名空间拦截 (`NotAllowedByListeners`)

LiteLLM 部署在 `llm-system` 命名空间，其创建的 `HTTPRoute` 尝试绑定部署在 `default` 命名空间的公共网关 `kong-main-gateway`。

执行 `kubectl describe httproute litellm-svc-route -n llm-system` 发现路由被拒绝：
```text
Reason: NotAllowedByListeners, Status: False, Type: Accepted
```

**原因**：`kong-main-gateway` 的监听器默认配置了 `allowedRoutes.namespaces.from: Same`，仅允许同一命名空间（`default`）下的应用绑定。

**解决**：在基础清单 `infrastructure/kong-gateway/Gateway.yaml` 中将网关策略调整为放行所有命名空间：
```yaml
listeners:
  - name: http
    port: 80
    protocol: HTTP
    allowedRoutes:
      namespaces:
        from: All # 👈 允许跨 Namespace 挂载路由
```

### 6.2 踩坑 2：URL 前缀剥离（`/litellm` Strip Path）

外部请求入口为 `http://134.185.90.98:31850/litellm/v1/models`，而后端 LiteLLM 监听的实际路径是 `/v1/models`。

早期尝试在 HTTPRoute 中使用 Gateway API 标准的 `URLRewrite` 过滤器：
```yaml
filters:
  - type: URLRewrite
    urlRewrite:
      path:
        type: ReplacePrefixMatch
        replacePrefixMatch: /
```
但 Kong Gateway Controller 报警：`HTTPRoute can't be routed: httpFilter URLRewrite unsupported`。

**解决**：
通过在 `generic-web-service-v2` Chart 中支持 `route.annotations`，并在 `HTTPRoute` 资源上附加 Kong 专用注解：
```yaml
annotations:
  konghq.com/strip-path: "true"
```
Kong 接收到请求后自动将 `/litellm` 前缀剥离，干净透传给后端 LiteLLM。

### 6.3 踩坑 3：思考大模型（Thinking Models）的长超时处理

在测试 `gemini-3.7-flash-freelayer`（启用 Thinking 推理模式）时，模型深入推演耗时达到了 49.6 秒，触发了 Kong 网关默认的 60 秒上游读取超时，客户端收到 HTTP 504（`The upstream server is timing out`）。

**解决**：
在 Kubernetes `Service` 层面增加 Kong 超时配置注解，将读取超时放宽至 180 秒：
```yaml
service:
  annotations:
    konghq.com/read-timeout: "180000"
    konghq.com/write-timeout: "180000"
    konghq.com/connect-timeout: "60000"
```

---

## 7. 全链路验收与实测验证

全套部署完成后，我们通过公网入口（`134.185.90.98:31850`）执行了多轮全量验证。

### 7.1 健康检查与模型列表

```bash
# 1. 存活探针
curl -s http://134.185.90.98:31850/litellm/health/liveliness
# 返回: "I'm alive!"

# 2. 就绪探针
curl -s http://134.185.90.98:31850/litellm/health/readiness
# 返回: {"status":"healthy","db":"Not connected"}

# 3. 查看挂载的模型列表 (鉴权通过)
curl -s http://134.185.90.98:31850/litellm/v1/models \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

返回数据：
```json
{
  "data": [
    { "id": "gemini-3.6-flash-freelayer", "object": "model", "owned_by": "openai" },
    { "id": "gemini-3.7-flash-freelayer", "object": "model", "owned_by": "openai" }
  ],
  "object": "list"
}
```

### 7.2 标准聊天与 Token 计量响应

执行单次数学问答测试：

```bash
curl -s -X POST "http://134.185.90.98:31850/litellm/v1/chat/completions" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.6-flash-freelayer",
    "messages": [{"role": "user", "content": "What is 10*10?"}],
    "max_tokens": 512
  }'
```

返回包含精细化的 Token 与开销元数据：

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

### 7.3 SSE 流式输出测试 (`stream: true`)

```bash
curl -s -N -X POST "http://134.185.90.98:31850/litellm/v1/chat/completions" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.6-flash-freelayer",
    "messages": [{"role": "user", "content": "Count from 1 to 5."}],
    "stream": true
  }'
```

输出流：
```text
data: {"choices":[{"delta":{"role":"assistant","content":"1, 2, 3,"}}]}
data: {"choices":[{"delta":{"content":" 4, 5"}}]}
data: [DONE]
```
Kong 网关与 LiteLLM 对流式长连接支持稳定，无缓冲阻断。

### 7.4 Redis 缓存与速率限制检查

查询集群内 Redis 实例的状态与 Key 列表：

```bash
kubectl exec -n redis deploy/redis -- redis-cli -a "$REDIS_PASSWORD" keys "*"
```

Redis 成功捕获并维护了网关层的全局路由与配额键值：
```text
global_router:...:gemini/gemini-3.6-flash:rpm:16-21
global_router:...:gemini/gemini-3.6-flash:tpm:16-21
{model_per_key:litellm_proxy_master_key:gemini-3.6-flash-freelayer}:tokens
{api_key:litellm_proxy_master_key}:tokens
```

---

## 8. 总结与经验清单

通过本次实战，我们打通了一条真正企业级的多模型网关跨云 GitOps 部署流水线：

1. **镜像不可变原则**：在 GitOps 场景中，镜像必须通过 **Manifest Digest Pinning** 绑定，防止 Tag 覆盖导致的运行时漂移；
2. **机密解耦原则**：借助 **ESO + 云厂商托管 Vault**，实现了 Git 仓库完全免机密化，结合 Bootstrap 密钥实现优雅闭环；
3. **12-Factor 配置**：非敏感配置（`config.yaml`、服务发现域名）归 ConfigMap，敏感密码归 Secret，严禁在 Deployment 模板中硬编码环境变量；
4. **网关演进**：Kubernetes Gateway API 在跨 Namespace 场景下需显式配置 `from: All`，并结合 Ingress/Route 注解精细化管理前缀剥离与长超时（180s）。

至此，LiteLLM 基础设施与网关接入第一阶段（Phase 1）已 100% 验收收官。
EOF