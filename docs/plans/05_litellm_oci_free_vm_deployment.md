# LiteLLM 部署到 OCI `free-arm-vm` 的实施计划

## 1. 目标与结论

本计划用于将当前项目中的 LiteLLM Proxy 部署到 Tencent Cloud K3s 集群中的 OCI `free-arm-vm` 节点。

当前已经具备开始部署准备的条件：

- 本地 LiteLLM Proxy 已经能够启动。
- Gemini OpenAI 兼容调用已经验证成功。
- 当前模型配置为 `gemini-3.6-flash-freelayer`。
- Redis 缓存配置已经在 `config.yaml` 中启用。
- K3s 集群中已有 Redis、Kong/KIC 和 ArgoCD，不重复创建这些基础设施。
- `free-arm-vm` 是 ARM64 节点，需要使用 ARM64 或多架构容器镜像。

但仓库目前还缺少 Kubernetes 部署所需的镜像和清单，因此本阶段先完成部署材料和验证设计，不直接修改云资源或执行集群部署。

## 2. 第一阶段的部署边界

第一阶段只部署 LiteLLM Service A：

```text
客户端 -> 现有 Kong/KIC -> LiteLLM Proxy -> Gemini API
                                      |
                                      -> 现有 K3s Redis
```

暂不纳入以下内容：

- FastAPI Service B。
- OCI MySQL 审计日志和费用统计。
- 新建 Redis、Kong、Ingress Controller 或数据库。
- 将 Redis `6379` 暴露到公网。
- 将 LiteLLM 容器日志直接写入容器内的 `/var/log`。

这样可以先验证最小生产链路：模型调用、OpenAI 格式 API、Redis 精确缓存、Kong 路由和 API Key 鉴权。

## 实施步骤与交付物总览

每个阶段都必须有明确的交付物。交付物可以是代码文件、镜像、Kubernetes 资源、测试记录或验收结果；只有交付物完成并通过对应检查，才能进入下一阶段。

| 阶段 | 实施目标 | 必须交付的成果 | 阶段完成标准 |
| --- | --- | --- | --- |
| 0. 环境确认 | 确认目标节点、Redis、Kong、镜像仓库和 Secret 方案 | 环境确认记录、节点标签、Redis Service 信息、Kong 接入方式记录 | 所有部署前置事实已确认，没有依赖猜测值 |
| 1. Helm Chart、容器化与 GitOps 清单准备 | 创建通用服务 Chart v2，构建镜像并准备 LiteLLM 的 ArgoCD Application | `generic-web-service-v2`、`Dockerfile`、`.dockerignore`、GitHub Actions workflow、镜像标签、`argocd-apps/litellm-svc-app.yaml`、构建记录 | Chart 能渲染 LiteLLM 所需资源，容器能够启动，GitOps 清单能指向有效镜像，CI 构建成功 |
| 2. ArgoCD 首次同步与集群内闭环 | 通过 ArgoCD 首次部署 LiteLLM 并完成内部验证 | Namespace、ConfigMap、Secret 创建记录、Application 首次同步记录、集群内测试记录 | Pod 在 `free-arm-vm` Running，集群内 API 调用成功 |
| 3. Redis 联调 | 验证缓存和 Redis 网络连接 | Redis 连接测试记录、重复请求缓存测试记录 | `AUTH`、`PING`、缓存命中均成功，Redis 未暴露公网 |
| 4. Kong 接入 | 通过现有网关提供外部 HTTPS API | HTTPRoute/Ingress、TLS 配置、路由测试记录 | 外部请求经过 Kong 成功到达 LiteLLM |
| 5. ArgoCD 自动发布与回滚 | 验证后续 Git commit 自动发布、自愈和回滚 | 自动同步记录、自愈记录、回滚记录 | Application 持续显示 `Synced` 和 `Healthy` |

后续章节分别说明每个阶段需要编写的文件、执行的动作和验收证据。

### 交付物命名和保存原则

- 应提交到代码仓库的文件进入本项目或约定的 GitOps 清单仓库。
- 真实 Secret 不提交到 Git；只提交 `secret.example.yaml` 或 Secret 创建说明。
- 镜像使用 Git commit SHA 或版本号作为不可变标签，并记录完整镜像地址。
- 测试结果保存为 Markdown、命令输出或 CI 构建记录，不能只依赖口头确认。
- 所有生产配置都要能追溯到对应的 Git commit、镜像标签和 ArgoCD revision。

### 交付物的 Repo 与路径

当前应用仓库已确认是：

```text
Repo: nvd11/my-litellm-service
本地路径: /home/gateman/projects/github/my-litellm-service
```

应用代码、镜像构建文件、LiteLLM 配置和 Kubernetes workload manifest 统一放在这个 Repo。ArgoCD Application 不放在应用 Repo，而放在单独的 GitOps Repo：

```text
Repo: my-argocd-manifests
用途: 只保存 ArgoCD Application 注册文件
```

`my-argocd-manifests` 的本地 checkout 路径和具体目录当前尚未在本机确认，因此计划中统一使用占位路径 `<argocd-manifests-path>`；实际实施前必须替换为真实路径。

| 交付物 | 所属 Repo | 仓库内位置或外部位置 | 是否提交真实敏感信息 |
| --- | --- | --- | --- |
| Dockerfile | `nvd11/my-litellm-service` | `/Dockerfile` | 否 |
| Docker 构建排除规则 | `nvd11/my-litellm-service` | `/.dockerignore` | 否 |
| GitHub Actions 镜像工作流 | `nvd11/my-litellm-service` | `/.github/workflows/build-and-push-image.yaml` | 否；只引用 GitHub Actions Secrets |
| 通用服务 Helm Chart v2 | `nvd11/my-shared-helm-charts` | `/charts/generic-web-service-v2/` | 否 |
| Helm Chart v2 发布版本 | `nvd11/my-shared-helm-charts` | Git tag，例如 `v2.0.0` | 否 |
| Python 依赖和版本约束 | `nvd11/my-litellm-service` | `/pyproject.toml`、`/uv.lock` | 否；锁文件只记录依赖版本 |
| LiteLLM 模型配置 | `nvd11/my-litellm-service` | `/config.yaml` | 否；只使用 `os.environ/...` 引用 |
| 环境变量模板 | `nvd11/my-litellm-service` | `/.env.example` | 否；只使用占位值 |
| Kubernetes Namespace | `nvd11/my-litellm-service` | `/deploy/k8s/namespace.yaml` | 否 |
| LiteLLM ConfigMap | `nvd11/my-litellm-service` | `/deploy/k8s/configmap.yaml` | 否 |
| Secret 字段模板 | `nvd11/my-litellm-service` | `/deploy/k8s/secret.example.yaml` | 否；不能放真实值 |
| LiteLLM Deployment | `nvd11/my-litellm-service` | `/deploy/k8s/litellm-deployment.yaml` | 否；只引用 Secret |
| LiteLLM ClusterIP Service | `nvd11/my-litellm-service` | `/deploy/k8s/litellm-service.yaml` | 否 |
| Kong HTTPRoute/Ingress | `nvd11/my-litellm-service` | `/deploy/k8s/kong-route.yaml` | 否；TLS 私钥不提交 |
| Secret 实例 | 集群 Secret 管理系统 | Namespace `llm-system` 中的 Secret，例如 `litellm-secrets` | 不进入 Git；由安全流程创建 |
| ARM64/多架构容器镜像 | GHCR public package | `ghcr.io/nvd11/my-litellm-svc:<git-sha>` | 不包含 API Key |
| 镜像构建记录 | `nvd11/my-litellm-service` | `docs/plans/evidence/05_litellm_oci_free_vm/01-image-build.md` | 否 |
| 环境确认记录 | `nvd11/my-litellm-service` | `docs/plans/evidence/05_litellm_oci_free_vm/00-environment.md` | 不记录密码和完整密钥 |
| 集群内部署验证 | `nvd11/my-litellm-service` | `docs/plans/evidence/05_litellm_oci_free_vm/02-in-cluster-validation.md` | 否；脱敏命令输出 |
| Redis 缓存验证 | `nvd11/my-litellm-service` | `docs/plans/evidence/05_litellm_oci_free_vm/03-redis-cache.md` | 否；不记录密码 |
| Kong 外部访问验证 | `nvd11/my-litellm-service` | `docs/plans/evidence/05_litellm_oci_free_vm/04-kong-validation.md` | 否；请求头中的 Key 必须脱敏 |
| ArgoCD Application | `my-argocd-manifests` | `argocd-apps/litellm-svc-app.yaml` | 否；不内嵌 Secret |
| ArgoCD 同步与回滚记录 | `nvd11/my-litellm-service` | `docs/plans/evidence/05_litellm_oci_free_vm/05-argocd-release.md` | 否 |
| 本部署计划 | `nvd11/my-litellm-service` | `/docs/plans/05_litellm_oci_free_vm_deployment.md` | 否 |

其中，`docs/plans/evidence/05_litellm_oci_free_vm/` 是部署证据目录；它保存脱敏后的命令、状态和测试结果，不保存 API Key、Redis 密码、TLS 私钥或完整 Authorization Header。

### Repo 职责边界

```text
nvd11/my-litellm-service
├── 应用源码和测试
├── Dockerfile 和依赖
├── config.yaml
├── deploy/k8s/ workload manifests
└── docs/plans/evidence/ 部署证据

my-argocd-manifests
└── ArgoCD Application 注册文件

my-shared-helm-charts
└── charts/generic-web-service-v2/ LiteLLM 使用的通用服务 Chart
```

应用 Repo 的 Kubernetes 清单负责描述 LiteLLM 如何运行；GitOps Repo 的 ArgoCD Application 负责描述 ArgoCD 从哪个 Repo、哪个 revision、哪个路径同步这些清单。两者不能混成一个文件，也不能把生产 Secret 复制到任一普通 Git Repo。

## 3.1 创建 `generic-web-service-v2` Helm Chart

现有 `generic-web-service` Chart 已经被其他服务使用，不能为了 LiteLLM 直接修改其行为。本项目新增独立的 v2 Chart，保持 v1 兼容不变。

交付位置：

```text
Repo: nvd11/my-shared-helm-charts
Path: charts/generic-web-service-v2/
Release: v2.0.0
```

建议目录：

```text
charts/generic-web-service-v2/
├── Chart.yaml
├── values.yaml
└── templates/
    ├── deployment.yaml
    ├── service.yaml
    └── httproute.yaml
```

v2 至少需要支持：

- `image.repository`、`image.tag`、`image.pullPolicy`。
- `command` 和 `args`。
- `env` 和 `envFrom`，用于注入 Kubernetes Secret。
- `volumes` 和 `volumeMounts`，用于挂载 LiteLLM `config.yaml`。
- `resources`。
- `securityContext`。
- `nodeSelector`。
- HTTP 探针和 TCP 探针二选一配置。
- 可选的 `imagePullSecrets`；LiteLLM 第一阶段使用 public GHCR 镜像，不需要配置拉取 Secret。
- ClusterIP Service。
- Kong HTTPRoute 和可配置的 `stripPath`。

v2 不应写入 LiteLLM 专用逻辑，仍然保持通用服务 Chart 的定位。LiteLLM 的模型、Redis 和 API Key 配置由 `litellm-svc-app.yaml` 通过 values、ConfigMap 和 Secret 引用提供。

LiteLLM 的 ArgoCD Application 使用 v2 Chart：

```yaml
source:
  repoURL: https://github.com/nvd11/my-shared-helm-charts.git
  path: charts/generic-web-service-v2
  targetRevision: v2.0.0
```

v2 发布前必须使用 `helm lint`、`helm template` 和一份 LiteLLM values 文件验证渲染结果。未发布并验证 v2 之前，不进入 LiteLLM 的首次 ArgoCD Bootstrap。

## 3. 需要新增或整理的仓库内容

建议新增以下目录和文件。它们是容器化和 Kubernetes 部署阶段的主要代码交付物：

```text
deploy/
└── k8s/
    ├── namespace.yaml
    ├── configmap.yaml
    ├── secret.example.yaml
    ├── litellm-deployment.yaml
    ├── litellm-service.yaml
    └── kong-route.yaml

.github/
└── workflows/
    └── build-and-push-image.yaml
```

根目录还需要增加面向 ARM64 的 `Dockerfile`。真实密钥只进入 Kubernetes Secret 或外部 Secret 管理系统，不进入 Git，也不写入镜像层。

本节的交付物为：

- [ ] `Dockerfile`。
- [ ] `.dockerignore`。
- [ ] `.github/workflows/build-and-push-image.yaml`。
- [ ] `deploy/k8s/namespace.yaml`。
- [ ] `deploy/k8s/configmap.yaml`。
- [ ] `deploy/k8s/secret.example.yaml`。
- [ ] `deploy/k8s/litellm-deployment.yaml`。
- [ ] `deploy/k8s/litellm-service.yaml`。
- [ ] `deploy/k8s/kong-route.yaml`。
- [ ] 一份不包含真实凭证的部署说明。

## 4. 容器镜像方案

### 4.0 镜像名称

LiteLLM 生产镜像名称固定为：

```text
ghcr.io/nvd11/my-litellm-svc
```

该 GHCR Container Package 固定发布为 **public**。因此 `free-arm-vm` 上的 K3s 节点可以匿名拉取镜像，第一阶段不创建 GHCR `imagePullSecret`。

public 只表示镜像层可以被公开拉取，不表示运行时配置公开。以下内容仍然禁止写入 Dockerfile、镜像层、GitHub Actions 日志或任何 Git Repo：

```text
OPENAI_API_KEY_FREE_1
LITELLM_MASTER_KEY
REDIS_PASSWORD
```

这些值继续通过 Kubernetes Secret 注入 Pod。

版本使用 Git commit SHA 作为不可变 tag，例如：

```text
ghcr.io/nvd11/my-litellm-svc:<git-commit-sha>
```

该名称必须在以下位置保持一致：

- Docker build 和 push workflow。
- `my-argocd-manifests/argocd-apps/litellm-svc-app.yaml` 的 `image.repository`。
- GitHub Actions 触发 `update-image-tag.yml` 时的镜像发布记录。
- 集群内和 Kong 外部访问验收记录。

### 4.1 运行环境

- Python 3.12。
- 安装项目生产依赖，包括 `litellm[proxy]`。
- 使用锁定后的依赖版本构建镜像。
- 镜像使用非 root 用户运行。
- 容器入口直接启动 LiteLLM CLI：

```text
litellm --config /app/config/config.yaml --port 4000
```

LiteLLM 需要 proxy extra 中的依赖。之前本地启动时缺少 `backoff`，以及 FastAPI 版本不兼容，说明镜像必须从项目依赖文件完整安装，不能只安装一个不带 proxy extra 的 LiteLLM 基础包。

### 4.2 ARM64 兼容性

镜像发布前需要确认以下任一方案：

1. 构建并推送 ARM64 镜像；或
2. 构建包含 `linux/arm64` 的多架构镜像。

部署清单中的镜像标签必须是不可变版本标签，例如 Git commit SHA，不使用长期漂移的 `latest`。

### 4.3 GitHub Actions 镜像构建与推送

GitHub Actions 负责把代码构建成容器镜像并推送到镜像仓库，但不直接连接 K3s，也不绕过 ArgoCD 修改集群。Workflow 文件位于：

```text
Repo: nvd11/my-litellm-service
Path: /.github/workflows/build-and-push-image.yaml
```

Workflow 至少应包含以下步骤：

1. 在 `push` 到主分支、发布 tag 或手动触发时运行；具体触发策略在正式实施前确定。
2. Checkout 当前 commit。
3. 设置 Docker Buildx。
4. 登录镜像仓库。
5. 构建 `linux/arm64` 镜像；如果镜像仓库和发布策略允许，也可以同时构建 `linux/amd64`。
6. 使用 Git commit SHA 生成不可变镜像标签，例如：

```text
ghcr.io/nvd11/my-litellm-svc:<git-sha>
```

7. 推送镜像并生成构建摘要。
8. 首次 Bootstrap 完成后，后续版本在镜像推送成功时，通过 GitHub API 向 `nvd11/my-argocd-manifests` 发送 `repository_dispatch` 事件。
9. 将镜像地址和 commit SHA 写入构建记录，供 Kubernetes Deployment 或后续 ArgoCD 更新使用。

#### 4.3.1 首次部署 Bootstrap

首次部署不能直接调用 `update-image-tag.yml`，因为该 workflow 只会修改已经存在的：

```text
my-argocd-manifests/argocd-apps/litellm-svc-app.yaml
```

因此第一次部署必须先创建这个 ArgoCD Application 清单，并为它提供一个已经存在于 GHCR 的初始镜像 tag。

首次部署顺序：

```text
创建并发布 generic-web-service-v2 Chart
    ↓
编写 Dockerfile 和 CI workflow
    ↓
构建并推送第一版 LiteLLM 镜像到 GHCR
    ↓
在 my-argocd-manifests 创建 litellm-svc-app.yaml
    ↓
填入第一版镜像的 repository 和 tag
    ↓
提交 Git commit
    ↓
ArgoCD 首次发现并同步 Application
    ↓
集群内验证 LiteLLM
```

首次 Bootstrap 的交付物：

- [ ] 第一版已推送到 GHCR 的 LiteLLM ARM64 或多架构镜像。
- [ ] GHCR Package visibility 已确认是 `public`。
- [ ] `my-argocd-manifests/argocd-apps/litellm-svc-app.yaml`。
- [ ] Application 中的 `image.repository` 和 `image.tag` 指向真实存在的镜像。
- [ ] ArgoCD 首次同步记录。
- [ ] 集群内 LiteLLM API 验证记录。

首次镜像发布时有两种实现方式：

1. 首次 CI 构建只负责 push 镜像，不触发 `repository_dispatch`；Application 创建并首次同步后，后续构建才触发自动更新。
2. 先手工创建一个包含初始镜像 tag 的 Application，再启用完整的 CI dispatch 流程。

推荐使用第一种方式，并在 CI 中增加 Bootstrap 标志或人工审批，避免在 Application 尚不存在时调用 Tag 更新 workflow。

#### 4.3.2 后续版本触发 ArgoCD 镜像 Tag 更新 workflow

镜像推送成功后，当前应用 Repo 的 CI 必须调用：

```text
Repo: nvd11/my-argocd-manifests
Workflow: .github/workflows/update-image-tag.yml
Event: repository_dispatch
Event type: update-image-tag
```

调用时发送以下 payload：

```json
{
  "event_type": "update-image-tag",
  "client_payload": {
    "svc_name": "litellm-svc",
    "image_tag": "<git-commit-sha>"
  }
}
```

参数含义：

- `svc_name` 必须与 GitOps Repo 中的 ArgoCD Application 文件名对应。`litellm-svc` 会让 workflow 修改：

  ```text
  nvd11/my-argocd-manifests/argocd-apps/litellm-svc-app.yaml
  ```

- `image_tag` 必须是已经成功推送到 GHCR 的镜像 tag，推荐使用当前 Git commit SHA。

调用 API 的目标地址为：

```text
POST https://api.github.com/repos/nvd11/my-argocd-manifests/dispatches
```

GitHub Actions 需要使用能够向 `my-argocd-manifests` 发送 repository dispatch 的凭证。该凭证只保存在当前 Repo 的 GitHub Actions Secrets 中，例如：

```text
ARGOCD_MANIFESTS_DISPATCH_TOKEN
```

Token 不得写入 workflow 文件、Dockerfile、项目 `.env` 或镜像。Token 只需要具备目标 Repo 所需的最小权限；如果组织策略支持，优先使用 GitHub App 或细粒度 token，而不是个人长期 token。

触发步骤必须放在镜像 push 成功之后：

```text
构建镜像
    ↓
推送 GHCR
    ↓ 只有 push 成功才继续
调用 repository_dispatch
    ↓
update-image-tag.yml 修改 GitOps Repo 中的 image.tag
    ↓
commit + push
    ↓
ArgoCD 同步部署
```

如果镜像 push 失败，不能触发 Tag 更新；如果 `repository_dispatch` 调用失败，当前 CI 必须失败并保留 API 响应，不能报告为成功。这样可以避免 GitOps 清单指向一个实际不存在的镜像。

GitHub Actions 中可以使用官方 API 或专门的 `repository_dispatch` Action 实现调用。无论采用哪种实现，都必须在构建日志中记录以下非敏感信息：

- 目标 Repo。
- event type。
- `svc_name`。
- `image_tag`。
- API 调用 HTTP 状态。

不得记录 dispatch token、完整 Authorization Header 或其他运行时密钥。

GitHub Actions 只通过 GitHub Actions Secrets 读取镜像仓库凭证，例如：

```text
REGISTRY_USERNAME
REGISTRY_PASSWORD 或 REGISTRY_TOKEN
```

Workflow 不得读取或打印以下运行时 Secret：

```text
OPENAI_API_KEY_FREE_1
LITELLM_MASTER_KEY
REDIS_PASSWORD
```

如果使用 GitHub Container Registry，优先使用短生命周期的 `GITHUB_TOKEN` 和最小权限；如果使用其他镜像仓库，则只授予推送目标仓库所需的权限。Workflow 中应设置最小化的 `permissions`，不使用不必要的仓库写权限。

GitHub Actions 阶段的交付物为：

- [ ] `/.github/workflows/build-and-push-image.yaml`。
- [ ] 镜像仓库地址和仓库权限配置记录。
- [ ] GitHub Actions Secrets 名称清单，不包含 Secret 值。
- [ ] 一次成功的 ARM64 或多架构构建记录。
- [ ] 推送后的不可变镜像地址和 commit SHA。
- [ ] 成功调用 `repository_dispatch` 的记录。
- [ ] `svc_name`、`image_tag` 和目标 manifest 文件对应关系的验证记录。
- [ ] 镜像可以被 `free-arm-vm` 节点拉取的验证记录。

### 4.4 日志策略

Kubernetes 中优先让 LiteLLM、Uvicorn 和应用日志输出到 stdout/stderr，由 Kubernetes 日志系统采集：

```bash
kubectl logs -n llm-system deploy/litellm
```

不把主机路径 `/var/log` 挂载进容器。若后续确实需要文件日志，应单独设计日志采集和持久化方案，而不是让应用容器自行管理日志文件。

## 5. Namespace、ConfigMap 与 Secret

Namespace 建议使用：

```text
llm-system
```

### 5.1 ConfigMap 中保存非敏感配置

LiteLLM 的非敏感配置通过 ConfigMap 管理。建议的 ConfigMap 内容为：

```text
config.yaml
REDIS_HOST
REDIS_PORT
LITELLM_PORT
NO_PROXY
```

配置值为：

```text
REDIS_HOST=redis.redis.svc.cluster.local
REDIS_PORT=6379
LITELLM_PORT=4000
NO_PROXY=localhost,127.0.0.1,10.0.0.0/8,.svc,.cluster.local
```

`config.yaml` 挂载到容器：

```text
/app/config/config.yaml
```

它可以保存以下非敏感内容：

- 暴露端口 `4000`。
- 模型别名 `gemini-3.6-flash-freelayer`。
- Redis 端口 `6379`。
- 缓存 TTL `3600`。
- LiteLLM 的非敏感运行参数。

当前项目的 `config.yaml` 使用 `os.environ/...` 读取密钥，因此配置文件本身不应包含真实 API Key 或密码。

OCI `free-arm-vm` 已确认可以通过 IPv4 直连 Gemini API，因此第一阶段不配置：

```text
HTTP_PROXY
HTTPS_PROXY
ALL_PROXY
```

### 5.2 Secret Manager 中保存敏感配置

以下变量必须通过 Kubernetes Secret、SealedSecret 或 External Secret Manager 注入：

```text
OPENAI_API_KEY_FREE_1
LITELLM_MASTER_KEY
REDIS_PASSWORD
```

职责分别是：

```text
OPENAI_API_KEY_FREE_1  LiteLLM -> Gemini
LITELLM_MASTER_KEY     客户端 -> LiteLLM
REDIS_PASSWORD         LiteLLM -> Redis
```

这些值不能进入 ConfigMap、Dockerfile、镜像、Git Repo 或 GitHub Actions 日志。

### 5.3 GitHub Actions Secrets

GitHub Actions 使用的 Secret 与 Kubernetes 运行时 Secret 分开管理：

```text
ARGOCD_MANIFESTS_DISPATCH_TOKEN
GITHUB_TOKEN
```

`ARGOCD_MANIFESTS_DISPATCH_TOKEN` 用于触发 `my-argocd-manifests` Repo 的 `repository_dispatch`。`GITHUB_TOKEN` 用于向 public GHCR 推送 `my-litellm-svc` 镜像，具体权限由 workflow 的 `permissions` 控制。

由于 GHCR 镜像是 public，第一阶段不创建 Kubernetes `imagePullSecret`。

### 5.4 Phase 1 暂不注入的变量

以下变量属于后续 MySQL 审计、FastAPI Service B 或 Vertex AI 方案，第一阶段不注入 LiteLLM Pod：

```text
MYSQL_HOST
MYSQL_PORT
MYSQL_USER
MYSQL_PASSWORD
MYSQL_DB
FASTAPI_PORT
GCP_PROJECT_ID
GCP_REGION
VERTEXAI_PROJECT
VERTEXAI_LOCATION
```

## 6. LiteLLM 配置与 Redis 地址

当前配置的核心内容是：

```yaml
model_list:
  - model_name: gemini-3.6-flash-freelayer
    litellm_params:
      model: gemini/gemini-3.6-flash
      api_key: os.environ/OPENAI_API_KEY_FREE_1

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

LiteLLM Pod 位于同一个 K3s 集群时，Redis 应优先使用 Kubernetes Service DNS，而不是通过 Tailscale 或 Kong 绕行：

```text
redis.redis.svc.cluster.local:6379
```

这里的 Service 名称和 Namespace 必须在部署前用 `kubectl get svc -A` 确认。如果实际名称不同，以集群中的 Redis Service 为准。

只有集群外、已经加入 Tailscale 的客户端才考虑使用现有的 Tailscale/Kong 地址，例如 `100.105.130.0:6379`。该地址不应作为集群内 Pod 的首选路径。

本地机器上的代理地址 `10.0.1.105:7890` 不能直接复制到 Kubernetes 配置中。Pod 是否需要代理，取决于 `free-arm-vm` 的出网能力和代理是否可从该节点访问。部署前应单独确认 Gemini API 的 DNS、HTTPS 出站连接和代理需求。

## 7. Deployment 设计

第一阶段使用单副本，先验证稳定性：

```yaml
replicas: 1
```

使用节点选择器将 Pod 调度到 OCI ARM 节点：

```yaml
nodeSelector:
  kubernetes.io/hostname: free-arm-vm
```

如果集群实际节点标签不是这个值，应先读取节点标签再调整清单，不能凭主机显示名猜测标签。

建议的初始资源配置：

```yaml
resources:
  requests:
    cpu: 250m
    memory: 512Mi
  limits:
    cpu: "1"
    memory: 2Gi
```

这些数值是第一轮运行基线，后续根据启动占用、并发量、请求延迟和节点余量调整。

Deployment 需要完成以下注入：

- 挂载 ConfigMap 中的 `config.yaml`。
- 从 Secret 注入 `OPENAI_API_KEY_FREE_1`。
- 从 Secret 注入 `LITELLM_MASTER_KEY`。
- 从 Secret 注入 `REDIS_PASSWORD`。
- 设置 `REDIS_HOST` 和 `REDIS_PORT`。
- 设置 `NO_PROXY`，至少包含集群内部地址和 Redis Service 地址。

不使用以下配置：

- `hostNetwork: true`。
- `hostPort`。
- NodePort。
- 将 Secret 直接写进 Deployment YAML。

## 8. Service 设计

LiteLLM 只创建 ClusterIP Service：

```text
Service: litellm
Namespace: llm-system
Port: 4000
TargetPort: 4000
Type: ClusterIP
```

ClusterIP 只允许集群内部访问，外部访问统一交给现有 Kong/KIC。这样可以避免直接暴露节点端口，也避免为 LiteLLM 再部署一套网关。

## 9. 健康检查策略

LiteLLM 的 `/health` 在当前环境中可能触发认证和管理数据库检查。此前没有配置 Prisma 管理数据库时，访问该路径出现过：

```text
No connected db.
ModuleNotFoundError: No module named 'prisma'
```

因此不能未经验证就把 `/health` 当作 Kubernetes 探针。

实施顺序建议如下：

1. 先确认当前 LiteLLM 版本支持的轻量存活和就绪路径，例如 `/health/liveliness`、`/health/readiness`。
2. 使用不依赖管理数据库的路径配置 HTTP 探针。
3. 如果当前版本的 HTTP 路径都会触发鉴权，则第一版使用 TCP 探针检查 `4000` 端口，同时通过外部测试验证业务接口。
4. 探针请求是否需要 `LITELLM_MASTER_KEY`，必须在本地用相同版本实际验证后再写入清单。

探针的目的不同：

- liveness：进程是否仍然存活。
- readiness：Pod 是否可以接收流量。
- 业务验收：是否能通过 `/v1/models` 和 `/v1/chat/completions` 完成真实调用。

## 10. Kong/KIC 接入方案

LiteLLM 部署并在集群内验证成功后，再通过现有 Kong/KIC 增加 HTTPRoute 或 Ingress：

```text
外部客户端
    |
    | HTTPS
    v
现有 Kong Gateway
    |
    v
HTTPRoute/Ingress
    |
    v
litellm.llm-system.svc.cluster.local:4000
```

路由设计需要明确：

- 使用独立域名或明确的路径前缀。
- TLS 终止位置和证书来源。
- 是否保留 `/v1` 路径。
- Kong 的超时设置必须覆盖 Gemini 的正常响应时间。
- 客户端仍然通过 `LITELLM_MASTER_KEY` 鉴权。

不通过 Kong 暴露 Redis，不创建 Redis 公网入口，也不把 LiteLLM 的管理接口无保护地暴露给同事或公网。

## 11. ArgoCD 发布顺序

建议按以下顺序操作：

### 阶段 A：镜像和清单准备

1. 在 `nvd11/my-shared-helm-charts` 中创建 `charts/generic-web-service-v2/`。
2. 为 Chart v2 增加配置挂载、Secret、资源限制、安全上下文、探针、节点选择和 Kong 路由能力。
3. 使用 `helm lint` 和 `helm template` 验证 LiteLLM values，发布 Chart `v2.0.0`。
4. 编写 ARM64 或多架构 Dockerfile。
5. 编写 `.github/workflows/build-and-push-image.yaml`。
6. 本地构建镜像并启动容器验证 LiteLLM。
7. 通过 GitHub Actions 构建并推送第一版镜像；首次 Bootstrap 不调用 `repository_dispatch`。
8. 将第一版镜像推送到可被 K3s 节点访问的 GHCR 仓库。
9. 完成 Namespace、ConfigMap、Secret 引用、Deployment 和 Service values 清单。
10. 在 `my-argocd-manifests` Repo 中创建 LiteLLM 的 ArgoCD Application：

   ```text
   my-argocd-manifests/argocd-apps/litellm-svc-app.yaml
   ```

   该文件至少需要定义镜像 Repository、初始 image tag、Helm Chart 来源、目标集群、Namespace、Service 参数和 Kong 路由参数。初始 `image.tag` 必须使用一个已经存在于 GHCR 的镜像 tag，不能使用尚未推送的值。

11. 确认 `svc_name=litellm-svc` 会映射到该文件，使 `update-image-tag.yml` 能够正确更新它。
12. 用 `helm template`、YAML 校验和 ArgoCD manifest 检查清单。

阶段 A 交付物：

- [ ] 可审查的 `Dockerfile` 和 `.dockerignore`。
- [ ] `nvd11/my-shared-helm-charts/charts/generic-web-service-v2/`。
- [ ] Chart v2 的 `helm lint`、`helm template` 结果和 `v2.0.0` 发布记录。
- [ ] 已构建并推送的 ARM64 或多架构镜像。
- [ ] 镜像完整地址、版本标签和构建记录。
- [ ] `deploy/k8s/` 下的 Namespace、ConfigMap、Deployment、Service 和 Kong 路由清单。
- [ ] `my-argocd-manifests/argocd-apps/litellm-svc-app.yaml`。
- [ ] `svc_name=litellm-svc` 到 `argocd-apps/litellm-svc-app.yaml` 的路径映射验证。
- [ ] Secret 字段清单和安全创建说明。
- [ ] Kubernetes YAML 静态校验结果。

### 阶段 B：ArgoCD 首次同步与集群内最小闭环

1. 通过安全流程创建 `llm-system` Namespace 和 Secret。
2. 确认 `my-argocd-manifests/argocd-apps/litellm-svc-app.yaml` 已提交，并且初始镜像 tag 已存在于 GHCR。
3. 确认 root bootstrap 或现有 ArgoCD 管理机制发现该 Application。
4. 首次以人工确认方式同步 ArgoCD Application。
5. 确认 Pod 调度到 `free-arm-vm`。
6. 确认容器启动日志没有依赖缺失或配置解析错误。
7. 从集群内临时测试 Pod 调用 LiteLLM。
8. 验证 Redis `AUTH`、`PING` 和缓存读写。

阶段 B 交付物：

- [ ] 已创建的 `llm-system` Namespace。
- [ ] Secret 创建结果；输出中不得包含 Secret 明文。
- [ ] ArgoCD Application 首次发现和同步记录。
- [ ] LiteLLM Deployment 和 ClusterIP Service 的运行状态记录。
- [ ] Pod 调度节点、镜像版本和启动日志记录。
- [ ] 集群内 `/v1/models` 测试结果。
- [ ] 集群内 `/v1/chat/completions` 测试结果。

### 阶段 C：Kong 接入

1. 先以内部或受限域名创建路由。
2. 验证 HTTPS、鉴权、超时和错误码透传。
3. 验证 `/v1/models`。
4. 验证 `/v1/chat/completions`。
5. 再决定是否开放给非 Tailscale 同事访问。

阶段 C 交付物：

- [ ] HTTPRoute 或 Ingress 清单。
- [ ] 域名、TLS 证书和 Kong upstream 配置记录。
- [ ] 外部 `/v1/models` 测试结果。
- [ ] 外部 `/v1/chat/completions` 测试结果。
- [ ] 401、429、5xx、超时等错误路径测试记录。
- [ ] Redis 未被 Kong 暴露公网的检查结果。

### 阶段 D：ArgoCD 自动发布与回滚

1. 确认阶段 B 的首次同步和集群内验证已经成功。
2. 验证后续镜像构建能够调用 `repository_dispatch` 并更新 `image.tag`。
3. 验证 ArgoCD 发现 Git commit 后自动同步新镜像。
4. 验证 `automated sync` 和 `selfHeal`。
5. 演练镜像回滚或 Git revision 回滚。
6. `prune` 必须经过确认，避免误删现有 Redis、Kong 或其他共享资源。

阶段 D 交付物：

- [ ] 后续 `repository_dispatch` 调用成功记录。
- [ ] Application 对应的 Git 仓库、路径和 revision 记录。
- [ ] 新 image tag 触发的自动同步结果。
- [ ] `Synced`、`Healthy` 状态截图或命令输出。
- [ ] Pod 删除后由 Deployment 恢复的记录。
- [ ] 镜像回滚或 Git revision 回滚的演练记录。
- [ ] automated sync、selfHeal 和 prune 的最终启用配置及审批记录。

## 12. 验收清单

### 12.1 调度和启动

- [ ] `free-arm-vm` 节点状态为 Ready。
- [ ] LiteLLM Pod 使用 ARM64 镜像并成功启动。
- [ ] Pod 实际调度在 `free-arm-vm`。
- [ ] `litellm[proxy]` 所需依赖完整。
- [ ] ConfigMap 和 Secret 已正确注入。
- [ ] 日志通过 stdout/stderr 输出，没有泄露 API Key。

### 12.2 模型接口

- [ ] 带 `LITELLM_MASTER_KEY` 请求 `/v1/models` 返回模型列表。
- [ ] 带 `LITELLM_MASTER_KEY` 请求 `/v1/chat/completions` 返回标准 OpenAI 格式。
- [ ] 返回中的 `model` 为 `gemini-3.6-flash-freelayer`。
- [ ] Gemini 429、5xx 和超时能够被日志识别。
- [ ] 失败时不会把 Gemini Provider Key 返回给客户端。

### 12.3 Redis 缓存

- [ ] LiteLLM Pod 能通过集群内 Redis Service DNS 连接 Redis。
- [ ] Redis `AUTH` 和 `PING` 成功。
- [ ] 相同请求在 TTL 内可以命中精确缓存。
- [ ] 修改 prompt、模型或影响响应的参数后不会错误复用旧响应。
- [ ] Redis `6379` 没有被新增公网暴露。

### 12.4 外部入口与运维

- [ ] Kong HTTPS 路由能够到达 LiteLLM ClusterIP Service。
- [ ] TLS、超时和鉴权配置符合预期。
- [ ] ArgoCD 显示 Synced 和 Healthy。
- [ ] 删除或重启 Pod 后能够按预期恢复。
- [ ] 发生错误时可以通过 `kubectl logs` 和 Kong 日志定位问题。

## 13. 失败处理与回滚

### 13.1 常见故障判断

- `ImagePullBackOff`：检查镜像仓库权限、标签和 ARM64 manifest。
- `CrashLoopBackOff`：检查 LiteLLM proxy 依赖、FastAPI/LiteLLM 版本兼容性和配置文件挂载。
- Redis 连接超时：检查 Service DNS、端口、密码、NetworkPolicy 和 Pod 出网/集群网络。
- Gemini 请求超时：检查节点 DNS、HTTPS 出站、防火墙和代理配置。
- `/health` 返回 `No connected db`：确认是否误用了需要 Prisma 管理数据库的路径，不要立即把 MySQL 引入第一阶段。
- Kong 返回 401：检查客户端是否发送 `LITELLM_MASTER_KEY`，以及 Kong 是否改写或丢失 Authorization Header。
- Kong 返回 502/504：检查 Service selector、targetPort、Kong upstream timeout 和 LiteLLM 日志。

### 13.2 回滚步骤

1. 先移除或禁用 Kong 路由，停止外部流量进入。
2. 将 Deployment 镜像回滚到上一个已验证版本。
3. 如果 ArgoCD 自动同步造成反复回滚，暂时暂停 automated sync。
4. 保留 Pod、Kong 和 ArgoCD 日志用于排查。
5. 不删除现有 Redis Pod、Redis 数据卷或共享 Kong 资源。

## 14. 后续阶段

第一阶段闭环稳定后，再分别规划：

1. LiteLLM 请求日志和费用数据写入 OCI MySQL。
2. FastAPI Service B 的评测接口。
3. 多模型路由、重试和 fallback。
4. API Key 分级、预算和速率限制。
5. Prometheus 指标、集中式日志和告警。
6. 多副本部署与滚动升级。

这些功能会增加 Secret、数据库、权限和运维复杂度，不应与第一次 LiteLLM 上线绑定实施。

## 15. 本计划的实施前置确认

真正执行部署前，需要确认以下事实：

- K3s 当前上下文和目标 Namespace。
- `free-arm-vm` 的实际节点标签。
- Redis Service 的准确名称、Namespace、端口和认证方式。
- K3s 节点能否直接访问 Gemini API。
- 镜像仓库地址以及 ARM64 拉取权限。
- 现有 Kong/KIC 使用 Ingress 还是 Gateway API HTTPRoute。
- 外部访问使用的域名、TLS 证书和访问范围。
- Secret 的正式交付方式。

以上确认完成后，才进入 generic-web-service-v2、Dockerfile、GitHub Actions 和 ArgoCD Application 的实际编写与部署阶段。
