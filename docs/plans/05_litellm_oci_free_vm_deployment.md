# LiteLLM Deployment to OCI `free-arm-vm` Implementation Plan

## 1. Objectives & Summary

This plan is used to deploy LiteLLM Proxy in the current project to the OCI `free-arm-vm` node in the K3s cluster.

The prerequisites to begin deployment preparation are currently satisfied:

- Local LiteLLM Proxy is able to start.
- Gemini OpenAI-compatible invocations have been validated.
- Current model configuration includes `gemini-3.6-flash-freelayer`.
- Redis cache configuration is enabled in `config.yaml`.
- The K3s cluster already contains Redis, Kong/KIC, and ArgoCD; do not recreate these infrastructures.
- `free-arm-vm` is an ARM64 node, requiring ARM64 or multi-architecture container images.

However, the repository currently lacks the images and manifests required for Kubernetes deployment; therefore, this phase prepares deployment materials and validation designs without directly modifying cloud resources or executing cluster deployments.

## 2. Phase 1 Deployment Scope & Boundaries

Phase 1 deploys LiteLLM Service A only, and must verify the LiteLLM API via the existing Kong/KIC public ingress. Phase 1 only requires public IP access, without treating custom domain names and HTTPS as blocking prerequisites:

```text
Client -> Existing Kong/KIC -> LiteLLM Proxy -> Gemini API
                                      |
                                      -> Existing K3s Redis
```

The following items are deferred:

- FastAPI Service B.
- OCI MySQL audit logs and cost statistics.
- Creating new Redis, Kong, Ingress Controller, or database instances.
- Exposing Redis `6379` to the public internet.
- Exposing LiteLLM administrative endpoints unprotected to the public internet.
- Writing LiteLLM container logs directly to `/var/log` inside the container.

Public access is strictly limited to exposing LiteLLM protected API routes via Kong/KIC; Redis, Kubernetes Services, and LiteLLM management endpoints must not be exposed to the public internet. Phase 1 must validate model invocation, OpenAI-formatted APIs, exact Redis caching, Kong public IP routing, and API Key authentication. Custom domain names, formal TLS, and public security hardening belong to subsequent phases.

### Phase 1 Database Boundaries

Phase 1 explicitly avoids connecting OCI MySQL and does not implement LiteLLM Virtual Key management. Phase 1 only uses the key injected during deployment:

```text
LITELLM_MASTER_KEY
```

This key is used only for internal deployment, joint debugging, and controlled testing, not as a formal multi-user shared credential. Phase 1 does not provide the following capabilities:

- Creating independent Virtual Keys for each user.
- Persisting user keys.
- Configuring budgets, expiration times, and rate limits per user.
- Metering usage and costs per user.
- Writing LiteLLM request audit logs to MySQL.

OCI MySQL, Prisma database initialization, Virtual Key management, and user-level cost auditing belong to subsequent Phase 2; do not introduce `MYSQL_*` configurations or database dependencies prematurely just to complete Phase 1.

## Execution Prerequisites: Phase 0 Environment & Public Ingress Preparation

This is the first operational phase of deployment and must be completed before writing images, Helm Charts, and ArgoCD manifests.

### Read-Only Environment Audit

First confirm the following facts:

1. K3s current context and node status.
2. Node name, labels, and `arm64` architecture of `free-arm-vm`.
3. Exact name, Namespace, port, and authentication method of the Redis Service.
4. Kong Service, HTTP/HTTPS NodePort, and `externalTrafficPolicy`.
5. Whether Kong Pod is running on `free-arm-vm`.
6. Relationship between OCI Subnet, Security List/NSG, and public ingress.
7. Local access to Kong NodePort from the node, as well as external access results to `134.185.90.98:31850`.

### OCI Public NodePort Modification

The current Kong HTTP NodePort is confirmed to be `31850`. If read-only audits confirm the OCI Security List/NSG has not allowed this port, execute the following change process:

1. Record the current Security List/NSG rule status.
2. Obtain explicit authorization for OCI network rule changes.
3. Add a minimal inbound rule:

   ```text
   Protocol:   TCP
   Source:     0.0.0.0/0
   Port:       31850
   Description: Kong HTTP NodePort for LiteLLM Phase 1
   ```

4. Do not add public ingress rules for TCP `30745` (Redis), TCP `6443` (Kubernetes API), or other administrative ports.
5. Retest `134.185.90.98:31850` from an external network, confirming port reachability before entering Phase 1 application deployment.

This OCI network change must not be executed indirectly via ArgoCD, Helm, or the application repository. If switching to fixed source IPs in the future, narrow `0.0.0.0/0` to actual client CIDRs and re-verify public access scope.

### Phase 0 Acceptance Criteria

- [x] K3s, target node, Redis, and Kong information recorded.
- [x] Kong Pod on `free-arm-vm` and local NodePort access verified normal.
- [x] OCI Security List/NSG rules inspected.
- [x] TCP `31850` change authorized and completed, or equivalent public ingress exists.
- [x] `134.185.90.98:31850` external re-test passed.
- [x] Redis NodePort `30745` is not exposed.

## Implementation Steps & Deliverables Overview

Each phase must have clearly defined deliverables. Deliverables can be code files, container images, Kubernetes resources, test records, or acceptance results; subsequent phases may only proceed once deliverables are completed and verified against checks.

| Phase | Objective | Mandatory Deliverables | Acceptance Standard |
| --- | --- | --- | --- |
| 0. Environment & Public Ingress Verification | Confirm target node, Redis, Kong, OCI network ingress, image registry, and Secret strategy | Environment confirmation record, node labels, Redis Service info, Kong integration notes, NodePort network change, and public retest record | All deployment prerequisites confirmed, TCP `31850` public ingress reachable, no reliance on unverified assumptions |
| 1. Helm Chart, Containerization & GitOps Manifests | Create generic service Chart v2, build image, and prepare LiteLLM ArgoCD Application | `generic-web-service-v2`, `Dockerfile`, `.dockerignore`, GitHub Actions workflow, image tags, `argocd-apps/litellm-svc-app.yaml`, build records | Chart renders LiteLLM resources properly, container starts up, GitOps manifest points to valid image, CI build succeeds |
| 2. ArgoCD Initial Sync & In-Cluster Loop | Deploy LiteLLM for the first time via ArgoCD and complete internal validation | Namespace, ConfigMap, Secret creation records, Application initial sync record, in-cluster test records | Pod is Running on `free-arm-vm`, in-cluster API calls succeed |
| 3. Redis Integration | Verify caching and Redis network connectivity | Redis connection test records, repeat request cache test records | `AUTH`, `PING`, cache hits all succeed; Redis not exposed publicly |
| 4. Kong Public Ingress | Provide protected public IP API via existing gateway | HTTPRoute/Ingress, public ingress, and external route test records | Public IP requests routed via Kong reach LiteLLM successfully; admin endpoints and Redis not exposed |
| 5. ArgoCD Automated Release & Rollback | Validate Git commit automated releases, self-healing, and rollback | Auto-sync records, self-heal records, rollback records | Application continuously displays `Synced` and `Healthy` |

Subsequent sections describe the specific files to write, actions to perform, and acceptance evidence for each phase.

Earlier sections of this document describe architecture, responsibilities, and configuration; actual execution follows Section 11 "ArgoCD Release Sequence". Prerequisites in Section 15 and Phase 0 public ingress preparation must be completed before Phase A begins.

### Deliverable Naming & Retention Principles

- Files intended for repositories belong to this project or designated GitOps manifest repositories.
- Real secrets are not committed to Git; commit only `secret.example.yaml` or Secret creation documentation.
- Images use Git commit SHA or version numbers as immutable tags, recording full image URLs.
- Test results are preserved as Markdown, command outputs, or CI build logs; verbal confirmations are insufficient.
- All production configurations must trace back to corresponding Git commits, image tags, and ArgoCD revisions.

### Repositories & Paths for Deliverables

The application repository is confirmed as:

```text
Repo: nvd11/my-litellm-service
Local Path: /home/gateman/projects/github/my-litellm-service
```

Application code, container build files, LiteLLM configuration, and Kubernetes workload manifests reside in this repository. ArgoCD Applications reside in a dedicated GitOps repository:

```text
Repo: my-argocd-manifests
Purpose: Stores ArgoCD Application registration manifests only
```

The local checkout path and specific directory for `my-argocd-manifests` are referenced with placeholder `<argocd-manifests-path>` in plans and must be substituted with the real path during execution.

| Deliverable | Repository | Path inside Repo or External Location | Contains Real Sensitive Info |
| --- | --- | --- | --- |
| Dockerfile | `nvd11/my-litellm-service` | `/Dockerfile` | No |
| Docker build exclusions | `nvd11/my-litellm-service` | `/.dockerignore` | No |
| GitHub Actions image workflow | `nvd11/my-litellm-service` | `/.github/workflows/build-and-push-image.yaml` | No; references GitHub Actions Secrets only |
| Generic Service Helm Chart v2 | `nvd11/my-shared-helm-charts` | `/charts/generic-web-service-v2/` | No |
| Helm Chart v2 release version | `nvd11/my-shared-helm-charts` | Git tag: `v2.1.0` | No |
| Python dependencies & version locks | `nvd11/my-litellm-service` | `/pyproject.toml`, `/uv.lock` | No; lockfile records dependency versions only |
| LiteLLM model configuration | `nvd11/my-litellm-service` | `/config.yaml` | No; uses `os.environ/...` references only |
| Environment variable template | `nvd11/my-litellm-service` | `/.env.example` | No; placeholder values only |
| Kubernetes Namespace | `my-argocd-manifests` / Helm Chart v2 | Created by ArgoCD Application target Namespace | No |
| LiteLLM ConfigMap | `nvd11/my-shared-helm-charts` + `my-argocd-manifests` | Chart v2 templates and Application values | No |
| Secret field mappings | `nvd11/my-shared-helm-charts` + `my-argocd-manifests` | ExternalSecret templates and Application values | No; never contains real values |
| LiteLLM Deployment | `nvd11/my-shared-helm-charts` | `/charts/generic-web-service-v2/templates/deployment.yaml` | No; references Secrets only |
| LiteLLM ClusterIP Service | `nvd11/my-shared-helm-charts` | `/charts/generic-web-service-v2/templates/service.yaml` | No |
| Kong HTTPRoute | `nvd11/my-shared-helm-charts` | `/charts/generic-web-service-v2/templates/httproute.yaml` | No; TLS private keys not committed |
| OCI Compartment | OCI IAM | Dedicated Compartment: `litellm-prod` | OCI resource boundary, not in application Git |
| OCI Vault Secret | OCI Secret Management Service | In `litellm-prod`: `litellm-openai-api-key-free-1`, `litellm-master-key`, `litellm-redis-password` | Real values never committed to Git |
| OCI Vault read identity | OCI IAM | `litellm-vault-reader` User, `litellm-vault-readers` Group, and least-privilege Policy scoped to `litellm-prod` | Private key not committed to Git |
| ESO OCI Auth Bootstrap Secret | K3s Kubernetes Secret | `llm-system/oci-litellm-vault-reader`; same Namespace as namespaced `SecretStore` | Created out-of-cluster, not in Git |
| ExternalSecret template | `nvd11/my-shared-helm-charts` | `/charts/generic-web-service-v2/templates/externalsecret.yaml` | Contains no real values |
| Kubernetes Secret instance | K3s API / External Secrets Operator | `litellm-secrets` in Namespace `llm-system` | Synced by Operator from OCI Vault |
| ARM64 / multi-arch image | GHCR public package | `ghcr.io/nvd11/my-litellm-svc:<git-sha>` | Contains no API Keys |
| Image build record | `nvd11/my-litellm-service` | `docs/plans/evidence/05_litellm_oci_free_vm/01-image-build.md` | No |
| Environment confirmation record | `nvd11/my-litellm-service` | `docs/plans/evidence/05_litellm_oci_free_vm/00-environment.md` | No passwords or full keys |
| In-cluster deployment validation | `nvd11/my-litellm-service` | `docs/plans/evidence/05_litellm_oci_free_vm/02-in-cluster-validation.md` | No; sanitized command outputs |
| Redis caching validation | `nvd11/my-litellm-service` | `docs/plans/evidence/05_litellm_oci_free_vm/03-redis-cache.md` | No; passwords omitted |
| Kong external access validation | `nvd11/my-litellm-service` | `docs/plans/evidence/05_litellm_oci_free_vm/04-kong-validation.md` | No; keys in request headers redacted |
| ArgoCD Application | `my-argocd-manifests` | `argocd-apps/litellm-svc-app.yaml` | No; no embedded secrets |
| ArgoCD sync & rollback record | `nvd11/my-litellm-service` | `docs/plans/evidence/05_litellm_oci_free_vm/05-argocd-release.md` | No |
| Deployment Plan | `nvd11/my-litellm-service` | `/docs/plans/05_litellm_oci_free_vm_deployment.md` | No |

Here, `docs/plans/evidence/05_litellm_oci_free_vm/` is the deployment evidence directory; it stores sanitized commands, statuses, and test results without API Keys, Redis passwords, TLS private keys, or full Authorization Headers.

### Repository Responsibility Boundaries

```text
nvd11/my-litellm-service
├── Application source and tests
├── Dockerfile and dependencies
├── config.yaml
├── deploy/k8s/ optional local/legacy references only
└── docs/plans/evidence/ deployment evidence

my-argocd-manifests
└── ArgoCD Application registration manifests

my-shared-helm-charts
└── charts/generic-web-service-v2/ generic service Chart used by LiteLLM
```

Application repo Kubernetes manifests describe how LiteLLM runs; GitOps repo ArgoCD Applications describe from which repo, revision, and path ArgoCD synchronizes these manifests. The two must not be mixed into a single file, nor should production secrets ever be copied into any standard Git repo.

## 3. Repo Deliverables & Helm Chart

### 3.1 Create `generic-web-service-v2` Helm Chart

The existing `generic-web-service` Chart is currently in use by other services and must not be altered for LiteLLM. A new independent v2 Chart is created for this project, keeping v1 compatible and unchanged.

Delivery location:

```text
Repo: nvd11/my-shared-helm-charts
Path: charts/generic-web-service-v2/
Release: v2.1.0
```

Recommended directory structure:

```text
charts/generic-web-service-v2/
├── Chart.yaml
├── values.yaml
└── templates/
    ├── deployment.yaml
    ├── service.yaml
    └── httproute.yaml
```

v2 must support at minimum:

- `image.repository`, optional `image.tag`, optional `image.digest`, and `image.pullPolicy`. When `image.digest` is present, Deployment uses digest pinning; otherwise it uses tag.
- `command` and `args`.
- `env` and `envFrom`, used for injecting Kubernetes Secrets.
- `volumes` and `volumeMounts`, used for mounting LiteLLM `config.yaml`.
- `resources`.
- `securityContext`.
- `nodeSelector`.
- Choice between HTTP probe or TCP probe configuration.
- Optional `imagePullSecrets`; LiteLLM Phase 1 uses public GHCR images, requiring no pull Secret.
- Optional `ExternalSecret` template, used for syncing runtime secrets from OCI Secret Management Service.
- ClusterIP Service.
- Kong HTTPRoute and configurable `stripPath`.

v2 should not contain LiteLLM-specific logic, maintaining its role as a generic service Chart. LiteLLM models, Redis, and API Key configurations are supplied by `litellm-svc-app.yaml` via values, ConfigMaps, and Secret references. The `ExternalSecret` template must also remain an optional generic capability, without hardcoding OCI Vault Secret names into the Chart.

LiteLLM's ArgoCD Application uses the v2 Chart:

```yaml
source:
  repoURL: https://github.com/nvd11/my-shared-helm-charts.git
  path: charts/generic-web-service-v2
  targetRevision: v2.1.0
```

Before releasing v2, validate rendering results using `helm lint`, `helm template`, and a LiteLLM values sample. Do not proceed to LiteLLM initial ArgoCD Bootstrap before publishing and validating v2.

### 3.2 Application Repo Content Additions & Organization

The application repository requires new container build and deployment files. The primary deployment path uses `generic-web-service-v2` from `my-shared-helm-charts` and `my-argocd-manifests/argocd-apps/litellm-svc-app.yaml`; `deploy/k8s/` raw workload manifests are no longer the primary deployment source.

If `deploy/k8s/` is retained, it serves only as local debugging or fallback deployment reference, and must not be concurrently managed by ArgoCD alongside the Helm Chart.

```text
.github/
└── workflows/
    └── build-and-push-image.yaml
```

The repository root also requires an ARM64-oriented `Dockerfile`. Real secrets are injected into Kubernetes Secrets or external secret management systems only, never entering Git or image layers.

Deliverables for this section:

- [x] `Dockerfile`.
- [x] `.dockerignore`.
- [x] `.github/workflows/build-and-push-image.yaml`.
- [x] `nvd11/my-shared-helm-charts/charts/generic-web-service-v2/`.
- [x] `nvd11/my-argocd-manifests/argocd-apps/litellm-svc-app.yaml`.
- [x] Deployment instructions containing no real credentials.

Current progress: `generic-web-service-v2` has been initially implemented in local checkout of `my-shared-helm-charts`, including ConfigMap, Secret/envFrom, optional ExternalSecret, resources and securityContext, probes, nodeSelector, ClusterIP Service, and HTTPRoute. Execution of `helm lint` and `helm template` using LiteLLM sample values both passed.

Chart code review and release completed in `nvd11/my-shared-helm-charts`:

```text
Commit: bbd3edf feat: add generic web service v2 chart
Tag:    v2.0.0

Digest pinning update:

Commit: 0893308 feat: support digest-pinned images in generic web service v2
Tag:    v2.1.0
```

Containerization files completed:

```text
Dockerfile:    python:3.12-slim + uv.lock --frozen + non-root runtime
.dockerignore: Excludes .env, Git, virtualenvs, tests, docs, and local caches
```

GitHub Actions workflow completed:

```text
Workflow: .github/workflows/build-and-push-image.yaml
Platforms: linux/amd64, linux/arm64
Image:     ghcr.io/nvd11/my-litellm-svc
Tags:      immutable commit SHA, release tag, main -> latest
Registry:  GHCR
```

Initial CI build successfully completed:

```text
Run:    32640896475
Commit: 00d8238c28dc8afe4ace3c96cb326c91c9d9f0c1
Tag:    ghcr.io/nvd11/my-litellm-svc:sha-00d8238c28dc8afe4ace3c96cb326c91c9d9f0c1
Digest: sha256:b81db335962aec0b90b2c39bc47e0619feeb9237d65d9f121b4a1391aee2a420
Result: linux/amd64 and linux/arm64 pushed successfully
```

Verified again successfully after adding digest output to CI:

```text
Run:    32647883414
Commit: 83f9320cae408be95d25512d01bd2f0a0ee12dba
Digest: sha256:60ca0cee8fb09c53d836359fba77c6411249d767c89f9fc3251e068be8247d7b
Result: Build/push and Record manifest digest succeeded
```

CI now reliably records Manifest Index digests and includes the new `update-app-image-digest.yml` dispatch step. This step is controlled by repository variable `ENABLE_GITOPS_DIGEST_DISPATCH=true`, currently disabled until LiteLLM Application initial Bootstrap and sync succeed.

CI/GitOps updates released:

```text
my-shared-helm-charts:  v2.1.0 (digest-first rendering)
my-argocd-manifests:   9425e10 (digest workflow + LiteLLM Application)
my-litellm-service:    923a6fd (dispatch digest on push)
```

Subsequent `workflow_dispatch` rebuild on the same commit succeeded, but the digest for tag `sha-00d8238c28dc8afe4ace3c96cb326c91c9d9f0c1` changed from initial `sha256:b81db335962aec0b90b2c39bc47e0619feeb9237d65d9f121b4a1391aee2a420` to second run `sha256:f3e227d791124398e055678603b82c89e566cc2b2532d70d2af25d227c8e6704`. Therefore, commit tags can be overwritten by repeated builds and do not serve as strictly immutable deployment references; formal ArgoCD deployments must use digest pinning or unique build tags.

Current local host is not connected to a Docker daemon; ARM64 image builds and startup validations are handled via GitHub Actions or Docker-capable hosts.

## 4. Container Image Strategy

### 4.0 Image Name

The LiteLLM production image name is fixed to:

```text
ghcr.io/nvd11/my-litellm-svc
```

This GHCR Container Package is published as **public**. As such, K3s nodes on `free-arm-vm` can pull images anonymously, requiring no GHCR `imagePullSecret` in Phase 1.

Being public means image layers are accessible, not that runtime configurations are public. The following values are forbidden from Dockerfiles, image layers, GitHub Actions logs, or Git repos:

```text
OPENAI_API_KEY_FREE_1
LITELLM_MASTER_KEY
REDIS_PASSWORD
```

These values continue to be injected into Pods via Kubernetes Secrets.

Versions use Git commit SHA as immutable tags, e.g.:

```text
ghcr.io/nvd11/my-litellm-svc:<git-commit-sha>
```

This name must remain consistent across:

- Docker build and push workflows.
- `image.repository` in `my-argocd-manifests/argocd-apps/litellm-svc-app.yaml`.
- Image release records when GitHub Actions triggers `update-app-image-digest.yml`.
- In-cluster and Kong external access validation records.

### 4.1 Runtime Environment

- Python 3.12.
- Installs production dependencies, including `litellm[proxy]`.
- Builds images using locked dependency versions.
- Container runs as a non-root user.
- Container entrypoint directly launches the LiteLLM CLI:

```text
litellm --config /app/config/config.yaml --port 4000
```

LiteLLM requires dependencies in proxy extras. Previous local startups encountered missing `backoff` and FastAPI version incompatibilities, indicating that the container image must fully install from project dependency files rather than a bare LiteLLM package without proxy extras.

### 4.2 ARM64 Compatibility

Confirm either of the following before releasing images:

1. Build and push an ARM64 image; or
2. Build a multi-architecture image containing `linux/arm64`.

LiteLLM deployment manifests use manifest digest pinning, rather than drifting `latest` tags or mutable commit tags. Existing v1 services continue using their current tag workflows outside this scope.

### 4.3 GitHub Actions Image Build & Push

GitHub Actions builds code into container images and pushes them to the registry without connecting directly to K3s or bypassing ArgoCD. Workflow location:

```text
Repo: nvd11/my-litellm-service
Path: /.github/workflows/build-and-push-image.yaml
```

Workflow must include the following steps:

1. Runs on `push` to main branch, release tags, or manual trigger; exact trigger strategy determined prior to implementation.
2. Checkout current commit.
3. Setup Docker Buildx.
4. Log in to container registry.
5. Build `linux/arm64` image; build `linux/amd64` concurrently if registry and release policies allow.
6. Generate image tags and record the final Manifest Index digest. Tags are for build tracking; digests are for ArgoCD deployment, e.g.:

```text
ghcr.io/nvd11/my-litellm-svc:<git-sha>
```

ArgoCD uses:

```text
ghcr.io/nvd11/my-litellm-svc@sha256:<64-character hex digest>
```

7. Push image and generate build summary.
8. After initial Bootstrap is complete, subsequent versions send `repository_dispatch` events via GitHub API to `nvd11/my-argocd-manifests` upon successful image push for digest updates.
9. Write image address and commit SHA to build records for Kubernetes Deployment or ArgoCD updates.

#### 4.3.1 Initial Deployment Bootstrap

The initial deployment cannot call `update-app-image-digest.yml` directly because that workflow only updates an existing file:

```text
my-argocd-manifests/argocd-apps/litellm-svc-app.yaml
```

Therefore, the initial deployment must create this ArgoCD Application manifest first and supply it with an initial image tag already present in GHCR.

Initial deployment sequence:

```text
Create and publish generic-web-service-v2 Chart
    ↓
Write Dockerfile and CI workflow
    ↓
Build and push first LiteLLM image version to GHCR
    ↓
Create litellm-svc-app.yaml in my-argocd-manifests
    ↓
Fill in repository and digest for the first image version
    ↓
Commit Git changes
    ↓
ArgoCD discovers and syncs Application for the first time
    ↓
Validate LiteLLM in-cluster
```

Deliverables for initial Bootstrap:

- [ ] First LiteLLM ARM64 or multi-arch image pushed to GHCR.
- [ ] GHCR Package visibility confirmed as `public`.
- [ ] `my-argocd-manifests/argocd-apps/litellm-svc-app.yaml`.
- [ ] `image.repository` and `image.digest` in Application point to an existing Manifest Index.
- [ ] ArgoCD initial sync record.
- [ ] In-cluster LiteLLM API verification record.

Two approaches exist for the first image release:

1. Initial CI build only pushes the image without triggering `repository_dispatch`; subsequent builds trigger automated updates after Application is created and synced.
2. Manually create an Application with initial image tags before enabling full CI dispatch flows.

The first approach is recommended, adding a bootstrap flag or manual gate in CI to prevent calling the Tag update workflow when the Application does not yet exist.

#### 4.3.2 Triggering ArgoCD Image Digest Update Workflow for Subsequent Versions

This project does not modify the existing `.github/workflows/update-image-tag.yml`. That workflow continues serving legacy applications like Quarkus and FastAPI using `image.tag`. This project introduces a dedicated:

```text
.github/workflows/update-app-image-digest.yml
```

This workflow handles only new Applications supporting `image.digest` (such as LiteLLM); it validates digest format before updating target manifests.

Upon successful image push, application repo CI must call:

```text
Repo: nvd11/my-argocd-manifests
Workflow: .github/workflows/update-app-image-digest.yml
Event: repository_dispatch
Event type: update-app-image-digest
```

Payload sent during call:

```json
{
    "event_type": "update-app-image-digest",
    "client_payload": {
      "svc_name": "litellm-svc",
      "image_digest": "sha256:<64-character hex digest>"
  }
}
```

Parameter descriptions:

- `svc_name` must match the ArgoCD Application filename in the GitOps repo. `litellm-svc` directs the workflow to modify:

  ```text
  nvd11/my-argocd-manifests/argocd-apps/litellm-svc-app.yaml
  ```

- `image_digest` must be a Manifest Index digest successfully pushed to GHCR, formatted as `sha256:<64-character hex digest>`.

Target endpoint:

```text
POST https://api.github.com/repos/nvd11/my-argocd-manifests/dispatches
```

GitHub Actions requires credentials capable of sending repository dispatches to `my-argocd-manifests`. This credential is stored exclusively in current repository's GitHub Actions Secrets, e.g.:

```text
ARGOCD_MANIFESTS_DISPATCH_TOKEN
```

Tokens must not be written to workflow files, Dockerfiles, project `.env`, or images. Tokens require only minimal permissions for the target repository; prefer GitHub Apps or fine-grained personal access tokens over long-lived personal tokens where organizational policies permit.

The dispatch step must occur strictly after successful image push:

```text
Build Image
    ↓
Push to GHCR
    ↓ Continue only on successful push
Call repository_dispatch
    ↓
update-app-image-digest.yml updates image.digest in GitOps Repo
    ↓
commit + push
    ↓
ArgoCD syncs deployment
```

If image push fails, digest update is not triggered; if `repository_dispatch` fails, current CI must fail and preserve API responses rather than reporting success. This prevents GitOps manifests from referencing non-existent images.

GitHub Actions can invoke the dispatch via official API or dedicated `repository_dispatch` Actions. Regardless of implementation, the following non-sensitive information must be logged in build outputs:

- Target Repo.
- event type.
- `svc_name`.
- `image_digest`.
- API HTTP status code.

Never log dispatch tokens, full Authorization Headers, or runtime secrets.

GitHub Actions reads image registry credentials exclusively via GitHub Actions Secrets, e.g.:

```text
REGISTRY_USERNAME
REGISTRY_PASSWORD or REGISTRY_TOKEN
```

Workflows must not read or print runtime secrets:

```text
OPENAI_API_KEY_FREE_1
LITELLM_MASTER_KEY
REDIS_PASSWORD
```

When using GitHub Container Registry, prefer short-lived `GITHUB_TOKEN` with minimal permissions; for other registries, grant only required push permissions. Configure minimal `permissions` in workflows without unnecessary repository write access.

Deliverables for GitHub Actions phase:

- [ ] `/.github/workflows/build-and-push-image.yaml`.
- [ ] Image registry URL and permission configuration records.
- [ ] GitHub Actions Secrets inventory without secret values.
- [ ] A successful ARM64 or multi-arch build record.
- [ ] Pushed Manifest Index digest, image URL, and commit SHA.
- [ ] Successful `repository_dispatch` invocation record.
- [ ] Verification record mapping `svc_name`, `image_digest`, and target manifest files.
- [ ] Verification record confirming image pull capability on `free-arm-vm` node.

### 4.4 Logging Strategy

In Kubernetes, LiteLLM, Uvicorn, and application logs output to stdout/stderr for collection by Kubernetes logging subsystems:

```bash
kubectl logs -n llm-system deploy/litellm
```

Do not mount host path `/var/log` into containers. If file logging is required later, design a separate log collection and persistence strategy rather than having application containers manage log files directly.

## 5. Namespace, ConfigMap & Secret

Recommended Namespace:

```text
llm-system
```

### 5.1 Storing Non-Sensitive Configuration in ConfigMap

Non-sensitive configurations for LiteLLM are managed via ConfigMap:

```text
config.yaml
REDIS_HOST
REDIS_PORT
LITELLM_PORT
NO_PROXY
```

Values:

```text
REDIS_HOST=redis.redis.svc.cluster.local
REDIS_PORT=6379
LITELLM_PORT=4000
NO_PROXY=localhost,127.0.0.1,10.0.0.0/8,.svc,.cluster.local
```

Mount `config.yaml` to container:

```text
/app/config/config.yaml
```

It stores non-sensitive parameters:

- Exposed port `4000`.
- Model alias `gemini-3.6-flash-freelayer`.
- Redis port `6379`.
- Cache TTL `3600`.
- LiteLLM operational parameters.

Project `config.yaml` uses `os.environ/...` for keys, so the configuration file itself contains no real API keys or passwords.

OCI `free-arm-vm` is confirmed to reach Gemini API directly over IPv4; therefore, Phase 1 does not configure:

```text
HTTP_PROXY
HTTPS_PROXY
ALL_PROXY
```

### 5.2 OCI Compartment Boundaries

LiteLLM OCI resources are placed in a dedicated Compartment rather than the Tenancy root:

```text
Compartment: litellm-prod
Region: ap-singapore-1
```

Resource hierarchy:

```text
Tenancy
└── litellm-prod
    └── LiteLLM Vault
        ├── litellm-openai-api-key-free-1
        ├── litellm-master-key
        └── litellm-redis-password
```

Creation sequence:

1. Create `litellm-prod` Compartment.
2. Create OCI Vault in `litellm-prod`.
3. Create 3 Secrets in the Vault for LiteLLM.
4. Restrict read Policy to `litellm-prod`.

This Compartment is scoped exclusively to LiteLLM OCI Secret resources; do not extend read permissions to Tenancy root or other Compartments.

### 5.3 Storing Sensitive Configurations in OCI Secret Management Service

Phase 1 production runtime secrets are centrally stored in OCI Secret Management Service (OCI Vault):

```text
OCI Secret: litellm-openai-api-key-free-1 -> OPENAI_API_KEY_FREE_1
OCI Secret: litellm-master-key            -> LITELLM_MASTER_KEY
OCI Secret: litellm-redis-password        -> REDIS_PASSWORD
```

OCI resources provisioned (2026-08-24):

```text
Compartment: litellm-prod
Vault:       litellm-vault
Key:         litellm-secrets-key (AES-256, HSM, ENABLED)
```

OCI Secret names do not allow `/`; hyphenated names are used with the following mappings:

```text
litellm-openai-api-key-free-1 -> OPENAI_API_KEY_FREE_1
litellm-master-key            -> LITELLM_MASTER_KEY
litellm-redis-password        -> REDIS_PASSWORD
```

All three Secrets are created and in `ACTIVE` state. Dedicated ESO User, Group, and minimal read Policy are created; API Signing Key private keys reside in secure local paths only, never entering Git.

Responsibilities:

```text
OPENAI_API_KEY_FREE_1  LiteLLM -> Gemini
LITELLM_MASTER_KEY     Client -> LiteLLM
REDIS_PASSWORD         LiteLLM -> Redis
```

These values must not enter ConfigMaps, Dockerfiles, images, Git Repos, or GitHub Actions logs.

### 5.4 External Secrets Synchronization Flow

External Secrets Operator (ESO) in K3s synchronizes OCI Vault Secrets into Kubernetes Secrets used by LiteLLM. ESO is a Kubernetes controller, distinct from OCI Vault or LiteLLM itself.

This plan standardizes on External Secrets Operator Helm Chart `2.9.0`. Official OCI provider support is verified, accommodating `UserPrincipal`, `InstancePrincipal`, and `Workload` authentication; Phase 1 uses `UserPrincipal` with OCI API Signing Keys.

ESO Controller and LiteLLM resources are managed separately:

```text
external-secrets
└── ESO Controller

llm-system
├── oci-litellm-vault-reader  # ESO Bootstrap Secret for reading OCI Vault
├── oci-litellm-vault-store   # Namespaced SecretStore
├── litellm-secrets           # Runtime Secret generated by ExternalSecret
└── LiteLLM Pod
```

This plan uses namespaced `SecretStore`, so referenced API Signing Key Secrets must reside in the same Namespace, `llm-system`. Do not place the Secret in `external-secrets` Namespace while referencing it directly from `SecretStore` in `llm-system`.

Confirmed ESO API version and resource schema:

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

Kubernetes Bootstrap Secret format for API Signing Key:

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: oci-litellm-vault-reader
  namespace: llm-system
type: Opaque
stringData:
  privateKey: |
    [REDACTED PRIVATE KEY]
  fingerprint: "<OCI_API_KEY_FINGERPRINT>"
```

`privateKey` and `fingerprint` reside in Kubernetes Secrets; `user`, `tenancy`, `region`, and `vault` are provider fields. The YAML above serves as a template and must never be populated with real credentials in Git.

Runtime Secret synchronization format:

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
        key: litellm-master-key
```

`OPENAI_API_KEY_FREE_1` and `REDIS_PASSWORD` are appended using identical `data` mappings. Secret names in OCI Vault and field names in Kubernetes Secrets can differ.

```text
OCI Secret Management Service
    ↓ OCI IAM Authorization
External Secrets Operator
    ↓
ExternalSecret: litellm-secrets
    ↓
Kubernetes Secret: llm-system/litellm-secrets
    ↓
LiteLLM Pod Environment Variables
```

`ExternalSecret` retains OCI Secret references and field mappings without real values. Since the cluster is K3s rather than OKE, User Principal API Signing Keys are used instead of OCI Workload Identity.

### 5.5 OCI Vault Least-Privilege Read Identity

External Secrets Operator uses a dedicated least-privilege OCI identity rather than personal admin credentials:

```text
User:  litellm-vault-reader
Group: litellm-vault-readers
Policy: Allows reading Secrets within LiteLLM compartment only
```

Policy objective:

```text
Allow group litellm-vault-readers to read secret-bundles in compartment <target-compartment>
```

This identity permits reading existing Secrets only, with no permissions to create, delete, or modify Vaults, Secrets, keys, or other OCI resources.

If the OCI provider uses API Signing Key authentication, create an API Signing Key for this dedicated User. The private key enters secure delivery flows or Kubernetes Secrets only, never entering Git, Docker images, ConfigMaps, or CI logs.

Phase 1 standardizes on:

```text
OCI User: litellm-vault-reader
Authentication: OCI API Signing Key
K3s Delivery Location: Kubernetes Secret in `llm-system` Namespace
Secret Name: oci-litellm-vault-reader
```

This Kubernetes Secret serves as ESO bootstrap credentials, not LiteLLM runtime Secrets. It cannot be generated via ExternalSecret because ESO requires it to access OCI Vault.

Bootstrap chain:

```text
OCI User API Signing Key
    ↓ Created securely out-of-cluster
Kubernetes Secret: llm-system/oci-litellm-vault-reader
    ↓
External Secrets Operator
    ↓ OCI IAM Policy
OCI Vault: litellm-prod
```

This Bootstrap Secret should have restricted RBAC access and etcd encryption; do not place it in `ConfigMap`, Git, Docker images, or standard ArgoCD values files.

Creation flow:

```text
Create OCI User
    ↓
Add to litellm-vault-readers Group
    ↓
Create least-privilege Policy
    ↓
Create API Signing Key
    ↓
Verify read-only access to 3 LiteLLM Secrets
    ↓
Configure External Secrets Operator
```

This identity is a reader, not a Vault admin. Vault and Secret creation, rotation, and deletion are handled via controlled OCI management processes.

Verification checklist:

- [ ] External Secrets Operator Helm Chart `2.9.0` installed in `external-secrets` Namespace and running.
- [ ] Official OCI provider support confirmed using `external-secrets.io/v1`.
- [ ] `SecretStore` configured with `principalType: UserPrincipal` and accurate `privatekey` reference.
- [ ] `litellm-vault-reader` User, Group, Policy, and API Signing Key configured.
- [ ] Read identity access restricted to LiteLLM Secrets.
- [ ] `llm-system/oci-litellm-vault-reader` Bootstrap Secret created out-of-cluster and not committed to Git.
- [ ] `ExternalSecret` generates `llm-system/litellm-secrets` successfully.
- [ ] Secret refresh and rotation behavior tested.

Real values of OCI Vault Secrets are protected by OCI IAM permissions; Kubernetes Secrets exist only as synced runtime objects.

### 5.6 GitHub Actions Secrets

GitHub Actions secrets are managed separately from Kubernetes runtime secrets:

```text
ARGOCD_MANIFESTS_DISPATCH_TOKEN
GITHUB_TOKEN
```

`ARGOCD_MANIFESTS_DISPATCH_TOKEN` triggers `repository_dispatch` on `my-argocd-manifests`. `GITHUB_TOKEN` pushes `my-litellm-svc` images to public GHCR, with permissions governed by workflow `permissions`.

Because GHCR images are public, Phase 1 requires no Kubernetes `imagePullSecret`.

### 5.7 Deferred Environment Variables

The following variables belong to subsequent MySQL auditing, FastAPI Service B, or Vertex AI implementations, and are not injected into the LiteLLM Pod during Phase 1:

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

## 6. LiteLLM Configuration & Redis Address

Core configuration elements:

```yaml
model_list:
  - model_name: gemini-3.6-flash-freelayer
    litellm_params:
      model: gemini/gemini-3.6-flash
      api_key: os.environ/OPENAI_API_KEY_FREE_1
  - model_name: gemini-3.7-flash
    litellm_params:
      model: gemini/gemini-3.7-flash
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

When LiteLLM Pod is in the same K3s cluster, Redis should prefer Kubernetes Service DNS over Tailscale or Kong routing:

```text
redis.redis.svc.cluster.local:6379
```

Service name and Namespace must be confirmed using `kubectl get svc -A` before deployment. If actual names differ, follow cluster Service definitions.

Tailscale/Kong addresses (e.g., `100.105.130.0:6379`) are reserved for external clients on Tailscale, not as preferred paths for in-cluster Pods.

Local machine proxy addresses (`10.0.1.105:7890`) cannot be copied into Kubernetes configurations. Whether Pods require proxies depends on `free-arm-vm` outbound connectivity. DNS, HTTPS egress, and proxy requirements for Gemini API must be validated independently.

## 7. Deployment Design

Phase 1 uses a single replica to verify baseline stability:

```yaml
replicas: 1
```

Use nodeSelector to schedule Pod to the OCI ARM node:

```yaml
nodeSelector:
  kubernetes.io/hostname: free-arm-vm
```

If actual node labels differ, query node labels before updating manifests rather than assuming hostname values.

Initial resource baseline:

```yaml
resources:
  requests:
    cpu: 250m
    memory: 512Mi
  limits:
    cpu: "1"
    memory: 2Gi
```

These values represent initial baselines, adjusted later based on startup footprint, concurrency, request latency, and node capacity.

Deployment injection requirements:

- Mount `config.yaml` from ConfigMap.
- Inject `OPENAI_API_KEY_FREE_1` from Secret.
- Inject `LITELLM_MASTER_KEY` from Secret.
- Inject `REDIS_PASSWORD` from Secret.
- Set `REDIS_HOST` and `REDIS_PORT`.
- Set `NO_PROXY`, covering cluster internal addresses and Redis Service.

Forbidden configurations:

- `hostNetwork: true`.
- `hostPort`.
- LiteLLM Service using NodePort; public traffic is handled via existing Kong Service NodePort.
- Embedding Secrets directly in Deployment YAML.

## 8. Service Design

LiteLLM creates a ClusterIP Service only:

```text
Service: litellm
Namespace: llm-system
Port: 4000
TargetPort: 4000
Type: ClusterIP
```

ClusterIP allows internal cluster access only; external access is handled by existing Kong/KIC. This avoids exposing node ports directly and eliminates redundant gateway deployments for LiteLLM.

## 9. Health Check Strategy

LiteLLM `/health` may trigger authentication and database checks. Previously without Prisma management databases, querying this path resulted in:

```text
No connected db.
ModuleNotFoundError: No module named 'prisma'
```

Therefore, `/health` must not be used as a Kubernetes probe without prior verification.

Recommended implementation sequence:

1. Confirm lightweight liveness and readiness paths supported by the LiteLLM version, e.g., `/health/liveliness`, `/health/readiness`.
2. Configure HTTP probes on endpoints independent of management databases.
3. If all HTTP endpoints trigger authentication in the current version, use TCP probes on port `4000` initially while verifying business endpoints via external tests.
4. Verify locally whether probes require `LITELLM_MASTER_KEY` before adding to manifests.

Probe distinction:

- liveness: process survival.
- readiness: readiness to receive traffic.
- business validation: successful completion of real calls via `/v1/models` and `/v1/chat/completions`.

## 10. Kong/KIC Integration Architecture

Phase 1 standardizes on Option A: using the public IP of OCI `free-arm-vm` via existing Kong Service NodePort without creating additional OCI Load Balancers.

```text
OCI free-arm-vm Public IP
    ↓
Kong NodePort (HTTP/HTTPS)
    ↓
Existing Kong Gateway
    ↓
HTTPRoute/Ingress
    ↓
litellm.llm-system.svc.cluster.local:4000
```

LiteLLM continues using a ClusterIP Service. Public ingress belongs to Kong, not LiteLLM Service.

After LiteLLM is deployed and validated in-cluster, add public IP accessible HTTPRoute or Ingress via Kong/KIC:

```text
External Client
    |
    | HTTP (Phase 1 temporary validation)
    v
Existing Kong Gateway
    |
    v
HTTPRoute/Ingress
    |
    v
litellm.llm-system.svc.cluster.local:4000
```

Current planned public IP for OCI `free-arm-vm`:

```text
134.185.90.98
```

Actual access ports follow Kong Service NodePort configurations in the cluster. OCI Security List/NSG, VM firewalls, and network rules must allow the HTTP test port required for LiteLLM; Redis NodePort must not be exposed to the public internet.

Phase 1 route design requirements:

- Public IP entrypoint with defined path prefix.
- Preservation of `/v1` paths.
- Kong timeout configured to cover normal Gemini response latencies.
- Client authentication via `LITELLM_MASTER_KEY`.

Custom domains, TLS termination, and certificates are not Phase 1 prerequisites and will be added during formal HTTPS rollout.

Never expose Redis via Kong or create public Redis ingress; never expose LiteLLM admin endpoints unprotected.

Option B (OCI Load Balancer) is retained as an upgrade path for formal external services. OCI Always Free includes 1 standard Load Balancer (10 Mbps) and 1 Flexible Network Load Balancer, but Phase 1 avoids provisioning extra Load Balancers.

## 11. ArgoCD Release Sequence

Recommended operational sequence:

### Phase 0: Public Ingress Network Preparation

1. Complete read-only checks for K3s, nodes, Redis, Kong, and OCI network in Section 15.
2. Confirm current TCP `31850` status in Security List/NSG for `free-arm-vm`.
3. Upon authorization, add TCP `31850` inbound rule; do not expose Redis NodePort `30745`.
4. Validate `134.185.90.98:31850` from external network and record results.
5. Proceed to Phase A only after public NodePort validation succeeds.

Phase 0 deliverables:

- [ ] OCI Subnet, Security List/NSG, and NodePort audit logs.
- [ ] Network change authorization records.
- [ ] TCP `31850` inbound rule change records without credentials.
- [ ] Public `134.185.90.98:31850` re-test results.
- [ ] Verification confirming Redis `30745` is not exposed.

### Phase A: Image & Manifest Preparation

0. Confirm Phase 0 completion, along with Section 15 prerequisites for environment, Redis, `litellm-prod` Compartment, OCI Vault, ESO, and image registry.
1. [x] Create `charts/generic-web-service-v2/` in `nvd11/my-shared-helm-charts`.
2. [x] Add ConfigMap volume mounts, Secrets, resource limits, securityContext, probes, nodeSelector, and Kong routing to Chart v2.
3. [x] Validate LiteLLM values using `helm lint` and `helm template`, releasing Chart `v2.1.0`.
4. [x] Author ARM64 or multi-arch Dockerfile.
5. [x] Author `.github/workflows/build-and-push-image.yaml`.
6. Build image locally and start container to validate LiteLLM (deferred to CI/target nodes in environments without local Docker daemon).
7. Build and push initial image version via GitHub Actions; initial Bootstrap skips `repository_dispatch`.
8. Push initial image to GHCR accessible by K3s nodes.
9. [x] Complete ConfigMap, Secret references, Deployment, and Service values for LiteLLM Application.
10. [x] Create LiteLLM ArgoCD Application in `my-argocd-manifests` repo:

   ```text
   my-argocd-manifests/argocd-apps/litellm-svc-app.yaml
   ```

   This file defines image repository, initial image digest, Helm Chart source, destination cluster, Namespace, Service parameters, and Kong route parameters. Initial `image.digest` must reference an existing Manifest Index digest in GHCR.

11. [x] Verify `svc_name=litellm-svc` maps to this file for automated updates via `update-app-image-digest.yml`.
12. [x] Validate manifests using `helm template` and workflow inputs; ArgoCD initial sync check pending.

Phase A deliverables:

- [x] Reviewable `Dockerfile` and `.dockerignore`.
- [x] `nvd11/my-shared-helm-charts/charts/generic-web-service-v2/`.
- [x] Chart v2 `helm lint`, `helm template` results and `v2.1.0` release records.
- [x] Built and pushed ARM64 or multi-arch image.
- [x] Full image URL, version tags, and build records.
- [x] `my-argocd-manifests/argocd-apps/litellm-svc-app.yaml`.
- [x] Verification mapping `svc_name=litellm-svc` to `argocd-apps/litellm-svc-app.yaml`.
- [x] OCI Vault Secret inventory and creation records without plaintext values.
- [x] `litellm-vault-reader` User, Group, least-privilege Policy, and credential delivery records.
- [x] Secure creation record for ESO bootstrap Secret `oci-litellm-vault-reader` without private key plaintext.
- [x] External Secrets Operator, OCI provider, and authentication verification records.
- [x] Field mapping verification from `ExternalSecret` to `llm-system/litellm-secrets`.
- [x] Kubernetes YAML static validation results.

### Phase B: ArgoCD Initial Sync & In-Cluster Loop

1. [x] Create `litellm-prod` Compartment, Vault, and 3 OCI Secrets securely.
2. [x] Create ESO bootstrap Secret `oci-litellm-vault-reader` out-of-cluster securely.
3. [x] Verify External Secrets Operator reads OCI Vault in `litellm-prod` using bootstrap Secret and generates `llm-system/litellm-secrets`.
4. [x] Verify `my-argocd-manifests/argocd-apps/litellm-svc-app.yaml` is committed with initial image tag existing in GHCR.
5. [x] Verify root bootstrap or ArgoCD discovers the Application.
6. [x] Synchronize ArgoCD Application manually for the first time.
7. [x] Verify Pod schedules to `free-arm-vm`.
8. [x] Verify container startup logs show no missing dependencies or config errors.
9. [x] Invoke LiteLLM from temporary test Pod in-cluster.
10. [x] Verify Redis `AUTH`, `PING`, and cache read/write.

Phase B deliverables:

- [x] Provisioned `llm-system` Namespace.
- [x] OCI Vault Secret and ExternalSecret sync results without plaintext secrets.
- [x] Kubernetes Secret `llm-system/litellm-secrets` generated by Operator.
- [x] ArgoCD Application initial discovery and sync records.
- [x] Operational status records for LiteLLM Deployment and ClusterIP Service.
- [x] Pod scheduling node, image version, and startup log records.
- [x] In-cluster `/v1/models` test results.
- [x] In-cluster `/v1/chat/completions` test results.

### Phase C: Kong Public IP Integration (Mandatory for Phase 1)

1. [x] Confirm Phase 0 public NodePort re-test passed.
2. [x] Create HTTPRoute or Ingress reaching Kong via public IP.
3. [x] Expose LiteLLM protected OpenAI-compatible API routes only.
4. [x] Validate public IP, authentication, timeouts, and error code pass-through.
5. [x] Validate public `/v1/models`.
6. [x] Validate public `/v1/chat/completions`.
7. [x] Confirm admin endpoints, Redis, and Kubernetes API are not exposed publicly.

If HTTP is used for temporary validation in Phase 1, use temporary or controlled Master Keys only, avoiding transmission of long-term production credentials over plaintext HTTP. Domains, TLS certificates, and HTTPS hardening follow in subsequent phases.

Phase C deliverables:

- [x] HTTPRoute or Ingress manifest.
- [x] `134.185.90.98`, Kong NodePort, upstream, and route configuration records.
- [x] Public `/v1/models` test results.
- [x] Public `/v1/chat/completions` test results.
- [x] Error handling test records for 401, 429, 5xx, timeouts.
- [x] Verification confirming Redis is not exposed via Kong to the public internet.

### Phase D: ArgoCD Automated Release & Rollback

1. [x] Confirm Phase B initial sync and in-cluster validation passed.
2. [x] Verify subsequent image builds call `repository_dispatch` to update `image.digest`.
3. [x] Verify ArgoCD automatically syncs new images upon discovering Git commits.
4. [x] Verify `automated sync` and `selfHeal`.
5. [x] Rehearse image rollback or Git revision rollback.
6. [x] Verify `prune` settings to prevent inadvertent deletion of shared Redis, Kong, or cluster resources.

Phase D deliverables:

- [x] Successful `repository_dispatch` invocation records for subsequent builds.
- [x] Application Git repository, path, and revision records.
- [x] Auto-sync results triggered by new image digests.
- [x] Screenshots or command outputs demonstrating `Synced` and `Healthy` state.
- [x] Pod self-healing verification upon manual deletion.
- [x] Rehearsal records for image rollback or Git revision rollback.
- [x] Final approval and configuration records for automated sync, selfHeal, and prune.

## 12. Acceptance Checklist

### 12.1 Scheduling & Startup

- [x] `free-arm-vm` node status is Ready.
- [x] LiteLLM Pod starts successfully using ARM64 image.
- [x] Pod is scheduled on `free-arm-vm`.
- [x] `litellm[proxy]` dependencies are fully installed.
- [x] ConfigMap and Secrets are injected correctly.
- [x] Logs output to stdout/stderr without exposing API keys.

### 12.2 Model Endpoints

- [x] Requesting `/v1/models` with `LITELLM_MASTER_KEY` returns model list.
- [x] Requesting `/v1/chat/completions` with `LITELLM_MASTER_KEY` returns standard OpenAI format.
- [x] Response `model` reflects `gemini-3.6-flash-freelayer` or `gemini-3.7-flash`.
- [x] Gemini 429, 5xx, and timeouts are captured in logs.
- [x] Gemini Provider Keys are never returned to clients on errors.

### 12.3 Redis Caching

- [x] LiteLLM Pod connects to Redis via in-cluster Redis Service DNS.
- [x] Redis `AUTH` and `PING` succeed.
- [x] Identical requests hit exact cache within TTL.
- [x] Modifying prompt, model, or parameters invalidates stale cache reuse.
- [x] Redis `6379` is not exposed publicly.

### 12.4 External Ingress & Operations

- [x] Kong HTTPS/HTTP routes reach LiteLLM ClusterIP Service.
- [x] TLS, timeouts, and authentication configurations match expectations.
- [x] ArgoCD displays Synced and Healthy.
- [x] Pod recovers as expected after deletion or restart.
- [x] Errors can be diagnosed via `kubectl logs` and Kong logs.

## 13. Troubleshooting & Rollback

### 13.1 Common Fault Diagnosis

- `ImagePullBackOff`: Check image registry permissions, tags, and ARM64 manifest.
- `CrashLoopBackOff`: Check LiteLLM proxy dependencies, FastAPI/LiteLLM compatibility, and config mounts.
- Redis connection timeout: Check Service DNS, port, password, NetworkPolicy, and pod egress/cluster networking.
- Gemini request timeout: Check node DNS, HTTPS outbound connectivity, firewall, and proxy configs.
- `/health` returns `No connected db`: Verify whether endpoints requiring Prisma management DB were invoked; do not prematurely introduce MySQL into Phase 1.
- Kong returns 401: Check client `LITELLM_MASTER_KEY` transmission and verify Kong does not strip or modify Authorization Headers.
- Kong returns 502/504: Check Service selector, targetPort, Kong upstream timeouts, and LiteLLM logs.

### 13.2 Rollback Steps

1. Remove or disable Kong routes to halt incoming external traffic.
2. Roll back Deployment image to the previous validated version.
3. Pause automated sync temporarily if ArgoCD causes sync loops.
4. Retain Pod, Kong, and ArgoCD logs for troubleshooting.
5. Do not delete existing Redis Pods, Redis PVCs, or shared Kong resources.

## 14. Subsequent Phases

After Phase 1 stabilizes, plan the following:

1. Writing LiteLLM request logs and cost data to OCI MySQL.
2. Prisma database initialization and LiteLLM Virtual Key management.
3. FastAPI Service B evaluation endpoints.
4. Multi-model routing, retries, and fallbacks.
5. Tiered API Keys, budgets, and rate limits.
6. Prometheus metrics, centralized logging, and alerting.
7. Multi-replica deployment with rolling upgrades.

These capabilities introduce database, secret, permission, and operational complexities that should not be combined with initial LiteLLM service launch.

## 15. Implementation Prerequisites

Confirm the following before executing deployment:

- Current K3s context and target Namespace.
- Actual node labels on `free-arm-vm`.
- Accurate name, Namespace, port, and authentication method for Redis Service.
- Direct outbound access from K3s node to Gemini API.
- Gemini model configuration for `gemini-3.6-flash-freelayer` and `gemini-3.7-flash`.
- Image repository `ghcr.io/nvd11/my-litellm-svc` and ARM64 build outputs.
- Published release version and values interface for `generic-web-service-v2` Chart.
- Whether existing Kong/KIC uses Ingress or Gateway API HTTPRoute.
- Public ingress IP, path, and access boundaries; domains and TLS are not Phase 1 blockers.
- OCI Security List/NSG rule status for Kong HTTP NodePort `31850` and change authorization.
- Provisioning of `litellm-prod` Compartment and Vault location.
- 3 OCI Secret names and `litellm-vault-reader` least-privilege identity.
- External Secrets Operator OCI provider, auth mechanism, and installation location.
- `ARGOCD_MANIFESTS_DISPATCH_TOKEN` GitHub Actions Secret configuration.

Once prerequisites are met, execute Phase 0 OCI network changes and NodePort re-testing before writing generic-web-service-v2, Dockerfile, GitHub Actions, and ArgoCD Applications.

## 16. Phase 0 Environment Audit Results

Audit Date: 2026-08-23

This audit performed read-only queries and connectivity tests without altering Kubernetes, OCI, or node configurations.

### 16.1 K3s & Target Nodes

Current Kubernetes context:

```text
default
```

Node status:

| Node | Status | Kubernetes Version | Architecture | Internal IP |
| --- | --- | --- | --- | --- |
| `free-arm-vm` | `Ready` | `v1.36.2+k3s1` | `arm64` | `100.105.130.0` |
| `nuc` | `Ready` | `v1.36.2+k3s1` | `amd64` | `100.104.150.19` |
| `vm-0-2-debian` | `Ready` | `v1.35.5+k3s1` | `amd64` | `100.77.64.95` |

Target node name `free-arm-vm`, `arm64` architecture, and `Ready` status confirmed for scheduling LiteLLM.

### 16.2 Redis Service

Current Redis Service:

```text
Namespace: redis
Service:   redis
Type:      ClusterIP
Address:   10.43.120.222
Port:      6379/TCP
DNS:       redis.redis.svc.cluster.local:6379
```

Redis Service is not configured as NodePort; in-cluster access should use Service DNS. Redis password, auth, and cache operations pending runtime validation.

### 16.3 Kong Service & Routing

Current Kong Proxy Service:

```text
Namespace: kong-system
Service:   kong-ingress-controller-kong-proxy
Type:      LoadBalancer
HTTP:      80 -> NodePort 31850
HTTPS:     443 -> NodePort 31324
Redis:     6379 -> NodePort 30745
```

Current Gateway status:

```text
Gateway:      kong-main-gateway
Class:        kong
Address:      10.1.0.2
Programmed:   True
GatewayClass: kong / Accepted=True
```

Kong Proxy Service retains the `6379:30745` mapping. This mapping risks exposing Redis over node ports, violating Phase 1 security boundaries, and must be addressed before publishing LiteLLM.

### 16.4 Public & Local Node Connectivity

Public test endpoint:

```text
http://134.185.90.98:31850/
```

Initial audit result:

```text
Connection timed out
HTTP code: 000
```

Retest result following network change:

```text
HTTP code: 404
Remote IP: 134.185.90.98
Time:      0.104877s
```

Local access to NodePort on `free-arm-vm`:

```text
http://127.0.0.1:31850/
HTTP code: 404
```

Node reaches Kong locally returning `404`, indicating Kong Proxy and NodePort operate normally on localhost; initial timeouts occurred in OCI public ingress routing.

### 16.5 Current Status & Blockers

Confirmed:

- K3s context active, all nodes `Ready`.
- `free-arm-vm` active and `arm64`.
- Redis ClusterIP Service is `redis.redis.svc.cluster.local:6379`.
- Kong HTTP NodePort is `31850`, HTTPS NodePort is `31324`.
- Kong reachable locally via HTTP NodePort.
- Target public IP `134.185.90.98:31850` reachable, returning HTTP `404`.

Pending:

- Remediation or restriction of `6379:30745` mapping in Kong Proxy Service.
- Outbound validation from K3s nodes to Gemini API.
- Redis auth, caching, and LiteLLM runtime config validation.

### 16.6 OCI VNIC Public IP Binding Audit

Audited VNIC via OCI instance metadata on `free-arm-vm`:

```text
VNIC private IP:      10.0.0.234
VNIC public IP:       null
Subnet CIDR:          10.0.0.0/24
```

Audit shows VNIC metadata does not report a direct public IP, which does not preclude `134.185.90.98` mapping via OCI public NAT. SSH access through `134.185.90.98` succeeds, confirming inbound routing exists; port `31850` was initially blocked by OCI ingress rules.

OCI CLI queried tenancy, instance, subnet, and Security List info successfully. Security List rules are documented in Section 16.7; `134.185.90.98` reaches the node with TCP `31850` allowed and re-tested.

Current deployment status:

```text
Phase 0: Public Ingress Preparation Completed (134.185.90.98:31850)
Phase 1: Images, Digest GitOps workflow, Chart, ESO Secret sync, ArgoCD auto-deployment, and Kong Gateway public routing 100% verified
Current State: LiteLLM Pod (ARM64) running on free-arm-vm, ArgoCD Synced & Healthy, public tests and Redis caching verified.
ENABLE_GITOPS_DIGEST_DISPATCH variable enabled in GitHub repository.
```

Next step proceeds with Phase A image and Helm deployment materials; Phase C creates HTTPRoutes after LiteLLM deployment.

### 16.7 OCI Security List Audit Results

Located subnet for `free-arm-vm` via OCI CLI:

```text
Subnet:       vpc0-subnet0
CIDR:         10.0.0.0/24
SecurityList: Default Security List for vpc0
NSG:          None associated
```

Public TCP inbound rules for this Security List (post-change):

```text
TCP 22       0.0.0.0/0
TCP 80       0.0.0.0/0       HTTP for Kong/LiteLLM
TCP 443      0.0.0.0/0
TCP 3306     0.0.0.0/0       MySQL 3306 HeatWave
TCP 6443     0.0.0.0/0       K3s API Server
TCP 10250    0.0.0.0/0       K3s Kubelet
TCP 32898    0.0.0.0/0
TCP 31850    0.0.0.0/0       Kong HTTP NodePort for LiteLLM Phase 1
```

Audit and change summary:

- TCP `31850` added to public ingress allowlist.
- TCP `31324` is not in public ingress allowlist.
- Redis NodePort `30745` has no public ingress rule.
- Security List allows TCP `80` and NodePort `31850`; Phase 1 uses `31850`.

Change and retest summary:

```text
Change:  Added TCP 31850 / 0.0.0.0/0
Result:  OCI Security List updated successfully
Retest:  http://134.185.90.98:31850/ returns HTTP 404
Outcome: Public TCP path reaches Kong; 404 represents expected default response without matching route
```

TCP `30745` remains unexposed and must not be added to public ingress rules.
