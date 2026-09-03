# 实战：基于 K3s + ArgoCD GitOps 在本地 NUC 落地 MinIO 对象存储并通过公网 SSO 安全访问

## 1. 背景与架构需求

在构建企业级大模型网关与中间件（如 LiteLLM Proxy）的过程中，面临着一个核心矛盾：
- **热数据与指标分析**：调用耗时、Token 计量、USD/CNY 扣费、模型路由轨迹等结构化数据，适合存放在托管关系型数据库（如 OCI MySQL / PostgreSQL）中，保证报表查询秒级响应；
- **冷数据与长文本 Payload**：Prompt 输入和 Response 输出动辄数万甚至数十万 Token，如果一股脑塞进 MySQL 的 `LONGTEXT`/`JSON` 字段，会迅速挤占 InnoDB Buffer Pool，导致主键与索引命中率断崖式下跌，且数据库快照备份与迁移成本极高。

为了实现冷热数据物理隔离，需要一套容量大、成本为零、隐私绝对可控的对象存储方案。

### 核心资产与拓扑
- **物理硬件**：本地 Intel NUC 迷你主机（接入 Tailscale 异地组网，作为多云 K3s 业务集群的 Worker 节点）；
- **存储空间**：NUC 物理机上 800GB+ 极速 NVMe SSD 空间；
- **GitOps 控制面**：阿里云 Master 节点托管的 ArgoCD，全声明式同步多云集群工作负载；
- **公网入口与安全**：Cloudflare 边缘 CDN（免费 Universal SSL） + Kong Gateway API 网关；
- **统一身份认证**：Logto Cloud + GitHub OAuth 实现单点登录（SSO）。

整体数据与流量流向如下：

```text
[公网浏览器 / 终端]
       │
       ▼ (HTTPS 443 / Cloudflare Edge SSL)
[Cloudflare CDN Proxy (*.jppwl.asia)]
       │
       ▼ (公网流量汇聚)
[Kong Gateway (K3s tencent-dp1-cluster)]
       │
       ▼ (K3s Flannel VXLAN 内部路由)
[MinIO Pod (固定调度在 NUC Worker 节点)] ◄─── OIDC 认证 ───► [Logto Cloud (sodaxw.logto.app)]
       │
       ▼ (hostPath 挂载)
[NUC 本地物理路径: /home/data/litellm_payloads (800GB NVMe)]
```

---

## 2. 第一步：NUC 物理宿主机存储准备

在 Kubernetes 中使用本地存储时，最容易踩的坑是**挂载错系统分区**。

通过 SSH 进入 NUC 查看磁盘挂载情况：
```bash
$ df -h
Filesystem      Size  Used Avail Use% Mounted on
/dev/nvme0n1p2   39G  9.5G   27G  27% /       <-- 根目录仅剩 27G，极易跑满
/dev/nvme0n1p4  845G  2.1M  802G   1% /home   <-- 800G NVMe 空闲存储
```

为了避免大文本对象把根目录写满导致 K3s 触发 `DiskPressure` 驱逐 Pod，真实数据必须落在 `/home` 分区下。

### 目录创建与权限规范
MinIO 官方容器默认运行在非 root 用户（UID 1000）。需要在宿主机提前建好目录并赋权：

```bash
# 1. 在大容量分区创建专用存储目录
sudo mkdir -p /home/data/litellm_payloads

# 2. 修改属主属组为 1000:1000，赋予读写权限
sudo chown -R 1000:1000 /home/data
sudo chmod -R 775 /home/data

# 3. 建立软链接，保持全局 /data 路径一致性
sudo ln -sfn /home/data /data
```

---

## 3. 第二步：ArgoCD GitOps 声明式交付 MinIO

遵循纯正的 GitOps 范式，不使用任何手动 `kubectl run` 或裸 Docker 命令，所有资源均通过 Git 仓库管理。

### 3.1 编写 Kubernetes 清单 (`infrastructure/minio/minio.yaml`)

核心要点：
1. **调度约束**：使用 `nodeSelector: kubernetes.io/hostname: nuc` 将 Pod 牢牢钉死在 NUC 物理机；
2. **本地存储挂载**：使用 `hostPath` 直接映射宿主机 `/data/litellm_payloads`；
3. **服务与网关**：分别暴露 `9000`（S3 API 传输）与 `9001`（Web Console 管理后台）。

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: minio
---
apiVersion: v1
kind: Secret
metadata:
  name: minio-secret
  namespace: minio
type: Opaque
stringData:
  MINIO_ROOT_USER: litellm_admin
  MINIO_ROOT_PASSWORD: CHANGE_ME
  MINIO_IDENTITY_OPENID_CLIENT_SECRET: "YOUR_LOGTO_OIDC_SECRET"
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: minio
  namespace: minio
  labels:
    app: minio
spec:
  replicas: 1
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app: minio
  template:
    metadata:
      labels:
        app: minio
    spec:
      nodeSelector:
        kubernetes.io/hostname: nuc # 锁定 NUC 节点
      containers:
        - name: minio
          image: quay.io/minio/minio:RELEASE.2024-08-29T01-40-52Z
          command:
            - /bin/sh
            - -ce
            - minio server /data --console-address ":9001"
          env:
            - name: MINIO_ROOT_USER
              valueFrom:
                secretKeyRef:
                  name: minio-secret
                  key: MINIO_ROOT_USER
            - name: MINIO_ROOT_PASSWORD
              valueFrom:
                secretKeyRef:
                  name: minio-secret
                  key: MINIO_ROOT_PASSWORD
            - name: MINIO_BROWSER_REDIRECT_URL
              value: "https://minio.jppwl.asia"
          ports:
            - name: s3-api
              containerPort: 9000
            - name: web-console
              containerPort: 9001
          volumeMounts:
            - name: minio-storage
              mountPath: /data
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: "1"
              memory: 1Gi
          livenessProbe:
            httpGet:
              path: /minio/health/live
              port: 9000
            initialDelaySeconds: 15
            periodSeconds: 20
          readinessProbe:
            httpGet:
              path: /minio/health/ready
              port: 9000
            initialDelaySeconds: 10
            periodSeconds: 15
      volumes:
        - name: minio-storage
          hostPath:
            path: /data/litellm_payloads
            type: DirectoryOrCreate
---
apiVersion: v1
kind: Service
metadata:
  name: minio
  namespace: minio
  labels:
    app: minio
spec:
  type: ClusterIP
  selector:
    app: minio
  ports:
    - name: s3-api
      port: 9000
      targetPort: 9000
    - name: web-console
      port: 9001
      targetPort: 9001
```

### 3.2 注册 ArgoCD Application (`argocd-apps/minio-app.yaml`)

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: minio
  namespace: argocd
  annotations:
    argocd.argoproj.io/sync-wave: "2"
spec:
  project: default
  ignoreDifferences:
    - group: ""
      kind: Secret
      jsonPointers:
        - /data
    - group: gateway.networking.k8s.io
      kind: HTTPRoute
      jsonPointers:
        - /spec/parentRefs/0/group
        - /spec/parentRefs/0/kind
        - /status
  source:
    repoURL: 'https://github.com/nvd11/my-argocd-manifests.git'
    path: infrastructure/minio
    targetRevision: HEAD
  destination:
    name: 'tencent-dp1-cluster'
    namespace: minio
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

提交代码至 Git 后，ArgoCD 自动发现并完成状态对齐，MinIO Pod 在 NUC 节点上进入 `1/1 Running` 状态。

---

## 4. 第三步：公网解析与 Kong Gateway API 路由接入

为了让局域网内的 MinIO 具备公网访问能力，同时实现全站 HTTPS 终结与源站 IP 隐藏，采用 **Cloudflare + Kong Gateway** 架构。

### 4.1 Cloudflare DNS 配置
在 Cloudflare 添加一条 A 记录，开启 Proxy（小黄云）模式：
- **Name**：`minio.jppwl.asia`
- **Target IP**：集群公网入口 IP（Kong Ingress 所在节点）
- **Proxied**：`true`（享受 Cloudflare 免费 Universal Edge SSL）

### 4.2 配置 Kubernetes Gateway API HTTPRoute
在 `infrastructure/minio/minio.yaml` 中追加 HTTPRoute 定义，将公网对 `minio.jppwl.asia` 的 HTTP/HTTPS 流量精准转发至 MinIO 的 `9001` Web 控制台端口：

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: minio-console-route
  namespace: minio
  labels:
    app: minio
spec:
  parentRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: kong-main-gateway
      namespace: default
  hostnames:
    - "minio.jppwl.asia"
  rules:
    - backendRefs:
        - group: ""
          kind: Service
          name: minio
          port: 9001
          weight: 1
      matches:
        - path:
            type: PathPrefix
            value: /
```

---

## 5. 第四步：Logto Cloud + GitHub OAuth 原生单点登录 (SSO)

MinIO 原生支持 OpenID Connect (OIDC) 认证协议。通过接入 Logto Cloud，可以彻底告别在网页上手输账户密码的繁琐流程，实现 GitHub 一键授权免密登录。

### 5.1 Logto 端自动化配置 (Management API)
使用 Logto M2M 凭证调用 Management API，将 MinIO 的回调地址加入客户端白名单：
- **Redirect URIs** 增加：`https://minio.jppwl.asia/oauth_callback`
- **Post Sign-out URIs** 增加：`https://minio.jppwl.asia`

```bash
# 获取 Logto M2M 管理 Token 并更新应用重定向配置
TOKEN=$(curl -s -X POST "https://sodaxw.logto.app/oidc/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=client_credentials&client_id=YOUR_M2M_ID&client_secret=YOUR_M2M_SECRET&resource=https://sodaxw.logto.app/api&scope=all" | jq -r '.access_token')

curl -s -X PATCH "https://sodaxw.logto.app/api/applications/YOUR_APP_ID" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "oidcClientMetadata": {
      "redirectUris": ["https://minio.jppwl.asia/oauth_callback"],
      "postLogoutRedirectUris": ["https://minio.jppwl.asia"]
    }
  }'
```

### 5.2 MinIO 环境变量注入 OIDC 配置
在 MinIO Deployment 中注入 OIDC 相关环境变量，并绑定管理员权限角色策略（`role_policy: consoleAdmin`）：

```yaml
env:
  # === Logto OpenID Connect (OIDC) SSO ===
  - name: MINIO_IDENTITY_OPENID_CONFIG_URL
    value: "https://sodaxw.logto.app/oidc/.well-known/openid-configuration"
  - name: MINIO_IDENTITY_OPENID_CLIENT_ID
    value: "inck8s2812o0gzfgzqvug"
  - name: MINIO_IDENTITY_OPENID_CLIENT_SECRET
    valueFrom:
      secretKeyRef:
        name: minio-secret
        key: MINIO_IDENTITY_OPENID_CLIENT_SECRET
  - name: MINIO_IDENTITY_OPENID_SCOPES
    value: "openid,profile,email"
  - name: MINIO_IDENTITY_OPENID_REDIRECT_URI
    value: "https://minio.jppwl.asia/oauth_callback"
  - name: MINIO_IDENTITY_OPENID_DISPLAY_NAME
    value: "Logto / GitHub SSO"
  - name: MINIO_IDENTITY_OPENID_ROLE_POLICY
    value: "consoleAdmin"
```

配置生效后，访问 `https://minio.jppwl.asia` 页面将展示「Log in with Logto / GitHub SSO」按钮，点击后自动通过 GitHub 身份认证并无缝跳转至 MinIO 控制台主界面。

---

## 6. 第五步：存储桶初始化与生命周期策略 (ILM)

服务启动后，通过 `mc` (MinIO Client) 完成基础 Bucket 的创建与策略下发：

```bash
# 1. 配置集群内别名
mc alias set local http://localhost:9000 litellm_admin YOUR_PASSWORD

# 2. 创建用于存储大模型 Payload 的专用桶
mc mb local/litellm-payloads

# 3. 设置下载策略为公开只读 (便于未来在 MySQL 视图中通过超链接直接查看 JSON)
mc anonymous set download local/litellm-payloads

# 4. 配置 90 天自动过期生命周期规则 (防止历史日志长期积累挤占磁盘)
mc ilm rule add local/litellm-payloads --expire-days 90
```

---

## 7. 架构收益与总结

通过本套方案，在无需采购公有云昂贵对象存储服务的前提下，实现了一套高可用、零额外开销的私有化 S3 基础设施：

1. **真正的冷热数据分离**：MySQL 只承载轻量级指标与聚合报表，海量上下文 Payload 异步写入 NUC 本地 MinIO，彻底消除数据库性能瓶颈；
2. **极简运维与自愈**：所有基础设施配置 100% 纳入 ArgoCD GitOps 体系，节点重启或 Pod 异常均由 K3s 自动编排自愈；
3. **公网访问与企业级安全**：Cloudflare Edge SSL 终结 + Kong 网关反向代理 + Logto OIDC 单点登录，兼顾外网随时随地直连与严格的访问控制；
4. **100% 数据主权**：所有调用敏感 Prompts 稳稳落在家庭 Homelab 的 NVMe 硬盘中，杜绝任何外部数据合规风险。
