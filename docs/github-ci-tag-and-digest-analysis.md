# Troubleshooting and Handling Tag Mutation in GitHub Actions CI

When adding multi-architecture image builds for the LiteLLM service, we encountered a commonly misunderstood issue: when GitHub Actions was re-run for the exact same Git commit, the same image tag remained in GHCR, but the digest it pointed to changed.

On the surface, this appeared as if "GHCR had two images with the same tag." The reality is more accurate: the package in the container registry remained the same, and the tag remained the same, but the tag pointer was updated by the subsequent build. What needed solving was not the tag count, but whether the deployment system depended on a mutable, drifting reference.

## What CI Actually Generates

The current workflow uses the Docker Metadata Action to generate three types of tags:

```yaml
tags: |
  type=sha,format=long
  type=ref,event=tag
  type=raw,value=latest,enable=${{ github.ref == 'refs/heads/main' }}
```

These three lines do not concatenate three strings into one tag; rather, they generate a list of tags:

| Configuration | Example Output | Purpose |
| --- | --- | --- |
| `type=sha,format=long` | `sha-83f9320cae408be95d25512d01bd2f0a0ee12dba` | Track build origin by commit SHA |
| `type=ref,event=tag` | `v2.0.0` | Triggered upon release version tags |
| `type=raw,value=latest` | `latest` | Convenience pointer for the `main` branch |

A multi-architecture build produces a single published image result:

```text
linux/amd64 manifest
linux/arm64 manifest
          ↓
Manifest Index
```

All three tags point to the same Manifest Index. They are three distinct names for the same publication artifact, not three independent image versions. The Manifest Index then points to specific amd64 and arm64 manifests based on platform metadata.

## How the Problem Was Discovered

The initial assumption was that the `sha-*` tag generated from the full commit SHA should be sufficiently stable because the commit itself never changes. Therefore, ArgoCD could deploy the image using this tag.

To validate this assumption, we ran CI to build and push the multi-arch image and recorded the digest output by `docker/build-push-action`. Then, we triggered `workflow_dispatch` again for the same commit and compared the two build results.

The first build produced:

```text
Commit: 00d8238c28dc8afe4ace3c96cb326c91c9d9f0c1
Tag:    sha-00d8238c28dc8afe4ace3c96cb326c91c9d9f0c1
Digest: sha256:b81db335962aec0b90b2c39bc47e0619feeb9237d65d9f121b4a1391aee2a420
```

Rebuilding the same commit kept the tag name identical, but the digest changed to:

```text
Tag:    sha-00d8238c28dc8afe4ace3c96cb326c91c9d9f0c1
Digest: sha256:f3e227d791124398e055678603b82c89e566cc2b2532d70d2af25d227c8e6704
```

This comparison highlighted two fundamental facts:

1. A commit SHA only describes the source code version, not a specific container image build artifact.
2. The `sha-*` tag is not a strictly immutable reference; repeated builds can repoint it to a newly generated Manifest Index.

Build artifacts can vary due to base image updates, dependency resolution, BuildKit provenance attestations, SBOM metadata, or build timestamps. Therefore, "identical source commit" does not equal "identical image digest."

## Relationship Between Tag and Digest

Image addresses are typically expressed in two ways:

```text
ghcr.io/nvd11/my-litellm-svc:sha-<commit>
ghcr.io/nvd11/my-litellm-svc@sha256:<digest>
```

A tag is a human-readable alias in the registry that can be repointed at any time. A digest is a content-addressable identifier of the image payload and its Manifest Index; if any bit of content or metadata changes, the digest changes.

Thus, two successive builds produce the following mapping:

```text
sha-<commit> ───────→ digest-A   (Build 1)
sha-<commit> ───────→ digest-B   (Build 2)
```

This is not two tags coexisting with the same name, but rather the subsequent push updating where the tag points. The old digest may still exist in the registry, but it can no longer be retrieved using the tag.

For ArgoCD, relying on tags introduces a drift hazard: the Git manifest remains unchanged, yet the actual deployed container image can drift. When ArgoCD's Git revision does not change but the deployed runtime changes, auditing, rollbacks, and reproducibility are compromised.

## Evaluated Solutions

### Option 1: Continue Using Commit SHA Tags

Example:

```text
ghcr.io/nvd11/my-litellm-svc:sha-83f9320cae408be95d25512d01bd2f0a0ee12dba
```

**Pros**: Minimal code changes, intuitive tags, clear traceability to source code.

**Cons**: Relies on the fragile operational constraint that "each commit is built exactly once." Manual re-runs, retries on failure, or CI environment updates can overwrite the tag. It works well as a tracking label, but not as an immutable deployment target.

### Option 2: Append Timestamp or Run ID to Tags

Examples:

```text
sha-<commit>-run-<run-id>
sha-<commit>-<timestamp>
```

**Pros**: Every build produces a unique tag, avoiding tag overwrites.

**Cons**: The deployment system must parse complex tag naming schemes, and ArgoCD manifest update scripts must be refactored. Timestamps introduce timezone and format parsing issues, while Run IDs leak CI execution details into image version strings. It also creates backward compatibility overhead for legacy services using `image.tag`.

While this avoids tag mutation, it fails to solve the underlying problem: deployment still relies on tag management instead of cryptographic content locking.

### Option 3: Delete Old Manifests Prior to Build

The idea is to query whether a tag exists, delete the old version from the registry, and then push the new version.

This option was rejected. Deleting registry artifacts increases permission requirements and introduces race conditions. If the subsequent build fails, existing deployment references break. When concurrent CI runs execute, deletion and push ordering cannot be guaranteed. It turns a reference management issue into an error-prone registry cleanup problem.

### Option 4: Refactor All Legacy Services to Use Digests

Long-term, pinning all services to digests is best practice, but doing so would violate the principle of minimal blast radius for LiteLLM deployment. Existing Quarkus, FastAPI, and other services rely on the legacy `update-image-tag.yml`. Modifying that pipeline globally would expand scope and turn a single service rollout into a repo-wide migration.

Therefore, the legacy workflow is preserved so existing applications continue using `image.tag`, while an isolated workflow is introduced for new services supporting digests.

### Option 5: Digest Pinning

This was the chosen solution. CI continues generating tags for human traceability, but ArgoCD deploys directly using the Manifest Index digest output upon build completion:

```text
ghcr.io/nvd11/my-litellm-svc@sha256:<digest>
```

**Pros**: Deployment references bind directly to immutable content, immune to `latest` drift, commit tag overwrites, or rebuild variations. Rollbacks simply require reverting the GitOps digest to a known previous value, guaranteeing exact auditability.

**Trade-offs**: Requires minor updates to the shared Helm Chart and a dedicated GitOps workflow to update digests. This overhead is localized and strictly impacts services opted into digest pinning.

## Final Implementation

### 1. CI Captures Manifest Index Digest

The build step was assigned an explicit step ID:

```yaml
- name: Build and push image
  id: build
  uses: docker/build-push-action@v6
```

The resulting digest is read from `steps.build.outputs.digest` and emitted to the GitHub Actions Job Summary:

```yaml
- name: Record manifest digest
  run: |
    echo "Digest: ${{ steps.build.outputs.digest }}"
```

This captures the multi-arch Manifest Index digest, not an isolated amd64 or arm64 sub-manifest digest. When ArgoCD pulls this index on an ARM node, the container runtime automatically selects the matching ARM64 architecture image.

### 2. Helm Chart Supports Digest Prioritization

`generic-web-service-v2` added support for an optional `image.digest`:

```yaml
image:
  repository: ghcr.io/nvd11/my-litellm-svc
  digest: sha256:<manifest-index-digest>
```

The template rendering logic is:

```text
If image.digest is present:
    → repository@digest

If image.digest is absent:
    → repository:tag
```

When LiteLLM uses digests, it renders as:

```text
ghcr.io/nvd11/my-litellm-svc@sha256:<manifest-index-digest>
```

This feature was released in `generic-web-service-v2` `v2.1.0` without modifying the legacy v1 Chart.

### 3. Dedicated GitOps Digest Workflow

The legacy workflow:

```text
.github/workflows/update-image-tag.yml
```

Continues handling legacy services that use `image.tag`.

The new workflow:

```text
.github/workflows/update-app-image-digest.yml
```

Exclusively handles new Applications supporting `image.digest`. It is triggered via `repository_dispatch`:

```json
{
  "event_type": "update-app-image-digest",
  "client_payload": {
    "svc_name": "litellm-svc",
    "image_digest": "sha256:<64-hex-char-digest>"
  }
}
```

The workflow validates the service name, verifies the digest format, ensures the target file exists, and updates:

```text
argocd-apps/litellm-svc-app.yaml
```

Updates target the explicit `digest:` key structure rather than relying on regex heuristic guessing from the old tag workflow, preventing cross-matching errors between tag and digest formats.

### 4. CI Triggers Update After Successful Push

The end-to-end application CI lifecycle is:

```text
Checkout
  ↓
Build amd64 + arm64
  ↓
Push to GHCR
  ↓
Extract Manifest Index digest
  ↓
Trigger repository_dispatch
  ↓
GitOps workflow updates image.digest
  ↓
ArgoCD synchronizes cluster state from Git
```

The dispatch is governed by a Repository Variable:

```text
ENABLE_GITOPS_DIGEST_DISPATCH=true
```

This remains disabled during the initial bootstrap phase, as the Application manifest must first exist and be deployed with an initial digest. Once verified and healthy, the variable is toggled on.

The dispatch authentication uses a GitHub Actions Secret:

```text
ARGOCD_MANIFESTS_DISPATCH_TOKEN
```

CI validates that the digest matches the `sha256:<64-hex-chars>` regex and requires an HTTP `204` response from the GitHub API. If image push or dispatch fails, CI fails fast, preventing GitOps manifests from referencing invalid images.

## Summary

This architecture does not attempt to enforce immutability across all tags, nor does it rely on destructive manifest deletions in the registry. Tags remain for human tracking and inspection, while digests enforce immutable deployment locking:

```text
Tag     → Human discovery and version tracking
Digest  → Immutable deployment locking
```

LiteLLM's ArgoCD manifest uses:

```yaml
image:
  repository: ghcr.io/nvd11/my-litellm-svc
  digest: sha256:<manifest-index-digest>
```

This division preserves CI readability while insulating deployments from rebuild mutations and tag drifts. It provides the optimal balance of scope, backward compatibility, and release traceability.

## Relevant Commits

```text
83f9320  Record Manifest Index digest in CI
0893308  Support digest pinning in generic-web-service-v2
9425e10  Add GitOps digest workflow and LiteLLM Application manifest
923a6fd  Add post-push digest dispatch in application CI
```
