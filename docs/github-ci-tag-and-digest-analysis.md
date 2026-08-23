# GitHub Actions 构建中重复 Tag 的排查与处理

在给 LiteLLM 服务增加多架构镜像构建时，我们遇到了一个容易被误解的问题：同一个 Git commit 再次运行 GitHub Actions 后，GHCR 中相同的镜像 tag 仍然存在，但它指向的 digest 发生了变化。

表面上看，这像是“GHCR 里出现了两个相同 tag 的镜像”。实际情况更准确：镜像仓库中的包仍然是同一个，tag 也仍然是同一个，只是这个 tag 的指向被后一次构建更新了。真正需要解决的不是 tag 数量，而是部署系统是否依赖了一个会漂移的引用。

## 先看 CI 到底生成了什么

当前工作流使用 Docker Metadata Action 生成三类 tag：

```yaml
tags: |
  type=sha,format=long
  type=ref,event=tag
  type=raw,value=latest,enable=${{ github.ref == 'refs/heads/main' }}
```

这三行不是把三个字符串拼成一个 tag，而是生成一个 tag 列表：

| 配置 | 生成示例 | 用途 |
| --- | --- | --- |
| `type=sha,format=long` | `sha-83f9320cae408be95d25512d01bd2f0a0ee12dba` | 按 commit 追踪构建来源 |
| `type=ref,event=tag` | `v2.0.0` | 发布版本 tag 触发时使用 |
| `type=raw,value=latest` | `latest` | `main` 分支的便捷入口 |

多架构构建只构建一次镜像发布结果：

```text
linux/amd64 manifest
linux/arm64 manifest
          ↓
Manifest Index
```

三个 tag 都可以指向同一个 Manifest Index。它们是同一个发布结果的三个名字，不是三个独立的镜像版本。一个 Manifest Index 再通过平台信息指向 amd64 和 arm64 的具体 manifest。

## 问题是怎样被发现的

最初的假设是：完整 commit SHA 生成的 `sha-*` tag 应该足够稳定，因为同一个 commit 不会改变。于是 ArgoCD 可以使用这个 tag 部署镜像。

为了验证这个假设，我们先让 CI 构建并推送多架构镜像，再记录 `docker/build-push-action` 输出的 digest。之后对同一个 commit 再次执行 `workflow_dispatch`，比较两次构建结果。

第一次构建得到：

```text
Commit: 00d8238c28dc8afe4ace3c96cb326c91c9d9f0c1
Tag:    sha-00d8238c28dc8afe4ace3c96cb326c91c9d9f0c1
Digest: sha256:b81db335962aec0b90b2c39bc47e0619feeb9237d65d9f121b4a1391aee2a420
```

同一个 commit 再次构建后，tag 名称没有变化，但 digest 变成：

```text
Tag:    sha-00d8238c28dc8afe4ace3c96cb326c91c9d9f0c1
Digest: sha256:f3e227d791124398e055678603b82c89e566cc2b2532d70d2af25d227c8e6704
```

这次对比说明了两个事实：

1. commit SHA 只描述源码版本，不描述一次具体的镜像构建结果。
2. `sha-*` tag 并不是严格不可变的引用，重复构建可以让它指向新的 Manifest Index。

构建结果可能因为基础镜像更新、依赖解析、BuildKit provenance、SBOM 或其他构建元数据变化而不同。因此“源码 commit 相同”不等于“镜像 digest 必然相同”。

## Tag 和 Digest 的关系

镜像地址通常有两种写法：

```text
ghcr.io/nvd11/my-litellm-svc:sha-<commit>
ghcr.io/nvd11/my-litellm-svc@sha256:<digest>
```

tag 是仓库中的可读名称，类似一个可以被重新指向的别名。digest 是镜像内容及其 Manifest Index 的内容寻址标识，内容不同，digest 就不同。

因此，两次构建可能产生这样的关系：

```text
sha-<commit> ───────→ digest-A   第一次构建
sha-<commit> ───────→ digest-B   第二次构建
```

这里不是两个 tag 同时拥有相同名字，而是后一次 push 更新了 tag 的指向。旧的 digest 仍可能存在于 registry 中，但不能再通过这个 tag 找到它。

对于 ArgoCD 来说，使用 tag 的问题是：清单内容没有变化，镜像实际内容却可能变化。ArgoCD 的 Git revision 没有改变，部署引用却已经漂移，这不利于审计、回滚和复现。

## 评估过的方案

### 方案一：继续使用 commit SHA tag

例如：

```text
ghcr.io/nvd11/my-litellm-svc:sha-83f9320cae408be95d25512d01bd2f0a0ee12dba
```

优点是改动最少，tag 直观，也能看出源码来源。

问题是它依赖“同一个 commit 只构建一次”这个流程约束。手动重跑、失败重试、构建环境变化都可能覆盖同一个 tag。它适合作为追踪标签，不适合作为严格的部署引用。

### 方案二：给 tag 增加时间戳或 Run ID

例如：

```text
sha-<commit>-run-<run-id>
sha-<commit>-<timestamp>
```

优点是每次构建都有唯一 tag，不会覆盖之前的 tag。

缺点是部署系统必须处理新的 tag 生成规则，ArgoCD 清单更新逻辑也要同步修改。时间戳还会增加格式和时区处理问题，Run ID 则把 GitHub Actions 的实现细节带入镜像版本命名。对于已有使用 `image.tag` 的旧服务，也会增加兼容成本。

这个方案能够解决 tag 覆盖，但没有解决“部署应该锁定镜像内容”这个根本问题：部署仍然依赖 tag 管理。

### 方案三：构建前删除旧 manifest

思路是发现相同 tag 已存在时，先删除旧版本，再推送新版本。

这个方案不采用。删除 registry 内容会增加权限和竞态条件，构建失败时还可能造成原有引用失效。多个 CI 运行同时操作时，删除和推送的顺序也难以保证。它把一个引用管理问题变成了 registry 数据清理问题。

### 方案四：改造所有旧服务，统一改用 digest

从长期看，所有服务使用 digest 是合理方向，但这次不适合作为 LiteLLM 部署的最小变更。现有 Quarkus、FastAPI 等服务依赖旧的 `update-image-tag.yml`，直接修改会扩大影响范围，也会把一次新服务接入变成全仓库迁移。

因此保留旧 workflow，让旧应用继续使用 `image.tag`；为支持 digest 的新应用增加独立流程。

### 方案五：使用 digest pinning

最终采用这个方案。CI 继续生成 tag，方便人查看构建来源；但 ArgoCD 不使用 tag，而是使用构建完成后得到的 Manifest Index digest：

```text
ghcr.io/nvd11/my-litellm-svc@sha256:<digest>
```

优点是部署引用直接绑定镜像内容，不受 `latest`、commit tag 或重复构建影响。回滚时只需要把 GitOps 清单中的 digest 改回已知值，审计记录也能准确对应到实际发布内容。

代价是需要修改通用 Helm Chart，并增加一条专门更新 digest 的 GitOps workflow。这个代价是局部的，而且只影响新接入 digest 的应用。

## 最终实现

### 1. CI 记录 Manifest Index digest

构建步骤增加了 ID：

```yaml
- name: Build and push image
  id: build
  uses: docker/build-push-action@v6
```

之后从 `steps.build.outputs.digest` 读取最终 digest，并写入 GitHub Actions Summary：

```yaml
- name: Record manifest digest
  run: |
    echo "Digest: ${{ steps.build.outputs.digest }}"
```

这里记录的是多架构 Manifest Index digest，不是某一个 amd64 或 arm64 子 manifest 的 digest。ArgoCD 在 ARM 节点拉取这个 index 后，会根据节点架构选择对应镜像。

### 2. Helm Chart 支持 digest 优先

`generic-web-service-v2` 增加了可选的 `image.digest`：

```yaml
image:
  repository: ghcr.io/nvd11/my-litellm-svc
  digest: sha256:<manifest-index-digest>
```

模板的选择规则是：

```text
image.digest 存在
    → repository@digest

image.digest 不存在
    → repository:tag
```

LiteLLM 使用 digest 时最终渲染为：

```text
ghcr.io/nvd11/my-litellm-svc@sha256:<manifest-index-digest>
```

这项能力发布在 `generic-web-service-v2` `v2.1.0`，没有修改旧的 v1 Chart。

### 3. 新建独立的 GitOps digest workflow

旧的：

```text
.github/workflows/update-image-tag.yml
```

继续处理使用 `image.tag` 的旧服务。

新增的：

```text
.github/workflows/update-app-image-digest.yml
```

只处理支持 `image.digest` 的新 Application。它通过 `repository_dispatch` 接收：

```json
{
  "event_type": "update-app-image-digest",
  "client_payload": {
    "svc_name": "litellm-svc",
    "image_digest": "sha256:<64位十六进制 digest>"
  }
}
```

workflow 会检查服务名、digest 格式和目标文件是否存在，然后更新：

```text
argocd-apps/litellm-svc-app.yaml
```

更新使用的是结构明确的 `digest:` 行，而不是继续用旧 workflow 的 tag 正则去猜测字段。这避免了 tag 和 digest 两种格式互相误匹配。

### 4. CI 在 push 成功后触发更新

应用 CI 的完整流程变为：

```text
Checkout
  ↓
Build amd64 + arm64
  ↓
Push GHCR
  ↓
读取 Manifest Index digest
  ↓
repository_dispatch
  ↓
GitOps workflow 更新 image.digest
  ↓
ArgoCD 根据 Git 变化同步
```

dispatch 受到 Repository Variable 控制：

```text
ENABLE_GITOPS_DIGEST_DISPATCH=true
```

首次 Bootstrap 阶段保持关闭，因为 Application 文件必须先存在，并且需要先用一个已经存在的 digest 完成首次部署。首次同步并验证健康后，才启用这个变量。

dispatch 使用的 token 存在 GitHub Actions Secret：

```text
ARGOCD_MANIFESTS_DISPATCH_TOKEN
```

CI 还会检查 digest 是否符合 `sha256:<64 位十六进制>` 格式，并要求 GitHub API 返回 HTTP `204`。镜像 push 失败或 dispatch 失败时，CI 不报告成功，避免 GitOps 清单指向不存在的镜像。

## 结果

这次处理没有试图让所有 tag 都变成不可变，也没有通过删除旧 manifest 来维持表面上的唯一性。tag 继续承担追踪和人工操作的职责，digest 承担部署锁定的职责：

```text
Tag     → 方便查找和追踪
Digest  → 锁定实际镜像内容
```

最终 LiteLLM 的 ArgoCD 清单使用：

```yaml
image:
  repository: ghcr.io/nvd11/my-litellm-svc
  digest: sha256:<manifest-index-digest>
```

这种分工保留了现有 CI 的可读性，也避免了重复构建覆盖 tag 对部署结果造成影响。对当前项目来说，这是修改范围、兼容性和发布可追溯性之间最合适的平衡。

## 相关提交

```text
83f9320  记录 Manifest Index digest
0893308  generic-web-service-v2 支持 digest pinning
9425e10  新增 GitOps digest workflow 和 LiteLLM Application
923a6fd  应用 CI 增加 push 后 digest dispatch
```

