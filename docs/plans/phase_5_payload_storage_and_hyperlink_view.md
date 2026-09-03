# Phase 5 Implementation Plan: Cloud-Native LLM Payload Offloading via ArgoCD & NUC K3s Storage with MySQL Hyperlink View

> **Goal**: Achieve complete hot/cold data separation between LLM request payloads (Prompt inputs and Response outputs) and the MySQL main audit table (`llm_request_logs`); deploy a cloud-native **MinIO S3 Service** via **ArgoCD GitOps** onto the **K3s business cluster (`tencent-dp1-cluster`) pinned to the NUC worker node (`100.104.150.19`)** with `local-path` persistent storage; extend the LiteLLM Proxy asynchronous hook with non-blocking S3 payload uploads (`aioboto3`); construct a database view with direct HTTPS/Kong hyperlinks (`v_llm_request_details`) in MySQL to deliver a streamlined troubleshooting and auditing experience: "second-level metrics analysis + one-click detail exploration".

---

## 1. Architecture Evolution & Motivation

### 1.1 Why Separate Hot and Cold Payload Data?
In the Phase 2/3/4 architecture, the MySQL HeatWave main table `llm_request_logs` records core structured metrics including token counts, CNY/USD settlement, latency, status codes, and model fallback trajectories.
As Agent tasks (such as Hermes, Codex, and OpenClaw) introduce long contexts with tens or hundreds of thousands of tokens, directly storing full Prompt and Response payloads in MySQL (`LONGTEXT` / `JSON`) causes severe issues:
1. **Buffer Pool Pollution**: Frequent reads and writes of multi-megabyte text fields crowd out the InnoDB buffer pool, severely degrading hit rates for primary keys and index lookups;
2. **Uncontrolled Table Size and Backup Difficulties**: Hundreds of thousands of monthly invocations generate tens of gigabytes of unstructured text, making database snapshot backup, restoration, and migration extremely slow;
3. **Network and Query Overhead**: Daily financial reporting and metrics monitoring only require numerical aggregations; large text columns dramatically reduce I/O throughput during table scans.

---

### 1.2 GitOps Topology & Data Flow

```
[Client (Hermes / Codex / User)]
            │
            ▼ (HTTP POST :4000)
[LiteLLM Proxy on K3s (free-arm-vm)]
            │
            ├──────────────────────────────────────────────────────┐
            │ (Async Non-blocking Event)                           │ (Async Non-blocking Event)
            ▼                                                      ▼
[app.core.logging_hook.custom_logger]             [app.core.payload_uploader (aioboto3)]
            │                                                      │
            │ (SQLAlchemy Async INSERT)                            │ (S3 PutObject via K3s Flannel / Kong)
            ▼                                                      ▼
[OCI MySQL HeatWave / Neon PG]                   [K3s Business Cluster (tencent-dp1-cluster)]
  (llm_request_logs Pure Structured Metrics)       - Namespace: `minio`
            │                                     - Node: `nuc` (100.104.150.19, nodeSelector pinned)
            │                                     - Storage: PVC `minio-data` (50Gi local-path on NUC)
            │                                     - S3 Endpoint: `http://minio.minio.svc.cluster.local:9000`
            ▼                                     - Public Ingress: `https://payloads.jppwl.asia` (Kong Edge)
[MySQL View: v_llm_request_details]
  (Dynamic CONCAT generates prompt_url & response_url)
            │
            ▼ (Browser One-Click)
[Browser / IDE / DBeaver] ───► Syntax-highlighted rendering of raw Prompt / Response JSON
```

---

## 2. Cloud-Native MinIO GitOps Deployment via ArgoCD

Following the established infrastructure pattern (identical to the GitOps Redis deployment on `free-arm-vm`), MinIO is managed declaratively through **ArgoCD (`aliyun-k3s`)** and scheduled strictly onto the **NUC worker node (`nuc`)** in the **`tencent-dp1-cluster`**.

### 2.1 ArgoCD Application Definition (`minio-app.yaml`)

Add the Application manifest to repository `nvd11/my-argocd-manifests`:

```yaml
# nvd11/my-argocd-manifests/apps/minio-app.yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: minio
  namespace: argocd
  finalizers:
    - resources-finalizer.argocd.argoproj.io
spec:
  project: default
  source:
    repoURL: https://github.com/nvd11/minio-deployment.git
    targetRevision: HEAD
    path: k8s
  destination:
    name: tencent-dp1-cluster
    namespace: minio
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
```

---

### 2.2 Kubernetes Manifests (`nvd11/minio-deployment`)

#### A. Namespace & PVC (`k8s/01-storage.yaml`)
```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: minio-data
  namespace: minio
spec:
  accessModes:
    - ReadWriteOnce
  storageClassName: local-path
  resources:
    requests:
      storage: 50Gi
```

#### B. Secret & Configuration (`k8s/02-secret.yaml`)
```yaml
apiVersion: v1
kind: Secret
metadata:
  name: minio-secret
  namespace: minio
type: Opaque
stringData:
  MINIO_ROOT_USER: "litellm_admin"
  MINIO_ROOT_PASSWORD: "CHANGE_ME_IN_ETCD_VIA_PATCH"
```

#### C. Deployment (`k8s/03-deployment.yaml`)
```yaml
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
        kubernetes.io/hostname: nuc # 🔒 Pin workload strictly to home NUC with large local disk
      containers:
        - name: minio
          image: quay.io/minio/minio:RELEASE.2024-08-29T01-40-52Z
          command:
            - /bin/sh
            - -ce
            - minio server /data --console-address ":9001"
          envFrom:
            - secretRef:
                name: minio-secret
          env:
            - name: MINIO_BROWSER_REDIRECT_URL
              value: "https://payloads-console.jppwl.asia"
          ports:
            - name: s3-api
              containerPort: 9000
            - name: web-console
              containerPort: 9001
          resources:
            requests:
              cpu: 100m
              memory: 256Mi
            limits:
              cpu: 1000m
              memory: 1024Mi
          volumeMounts:
            - name: storage
              mountPath: /data
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
        - name: storage
          persistentVolumeClaim:
            claimName: minio-data
```

#### D. Service & Kong Ingress (`k8s/04-service-and-ingress.yaml`)
```yaml
apiVersion: v1
kind: Service
metadata:
  name: minio
  namespace: minio
  labels:
    app: minio
spec:
  type: ClusterIP
  ports:
    - name: s3-api
      port: 9000
      targetPort: 9000
    - name: web-console
      port: 9001
      targetPort: 9001
  selector:
    app: minio
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: minio-public-ingress
  namespace: minio
  annotations:
    konghq.com/strip-path: "false"
    konghq.com/protocol: "http"
spec:
  ingressClassName: kong
  rules:
    - host: payloads.jppwl.asia
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: minio
                port:
                  number: 9000
```

---

### 2.3 Post-Deployment: Bucket Creation & Anonymous Read Policy

Once ArgoCD finishes syncing the MinIO application, initialize the `litellm-payloads` bucket via a one-time Kubernetes Job or MinIO Client (`mc`):

```bash
# Exec into MinIO Pod or run via mc container
mc alias set k3s http://minio.minio.svc.cluster.local:9000 litellm_admin $MINIO_ROOT_PASSWORD
mc mb k3s/litellm-payloads
mc anonymous set download k3s/litellm-payloads
mc ilm rule add k3s/litellm-payloads --expire-days 90
```

---

## 3. Storage Hierarchy & Payload Organization

Each API invocation is stored immutably by date and `request_id`:
```
litellm-payloads/
  └── 2026-09-02/
      └── {request_id}/
          ├── prompt.json        # User/System messages, tools definition, model, hyperparameters
          └── response.json      # LLM output choices, tool_calls, usage tokens, finishing reason
```

---

## 4. LiteLLM Proxy Asynchronous S3 Uploader

Introduce the `app.core.payload_uploader` module using `aioboto3` for non-blocking, exception-isolated S3 uploads.

### 4.1 Pydantic Configuration Extension (`app/core/config.py`)

```python
class Settings(BaseSettings):
    # Existing settings ...
    
    # === Payload Offloading & S3 Storage ===
    ENABLE_PAYLOAD_OFFLOAD: bool = True
    # Internal K3s Service DNS when running in cluster, or Tailscale IP when running locally
    PAYLOAD_S3_ENDPOINT: str = "http://minio.minio.svc.cluster.local:9000"
    PAYLOAD_BUCKET_NAME: str = "litellm-payloads"
    PAYLOAD_ACCESS_KEY: str = "litellm_admin"
    PAYLOAD_SECRET_KEY: str = "CHANGE_ME"
    # Public Base URL rendered in MySQL Hyperlinks (Kong Edge Domain)
    PAYLOAD_PUBLIC_BASE_URL: str = "https://payloads.jppwl.asia/litellm-payloads"
```

---

### 4.2 Asynchronous Upload Module (`app/core/payload_uploader.py`)

```python
"""LiteLLM Asynchronous Payload Uploader Module using aioboto3."""

import json
import logging
from datetime import datetime, timezone
from typing import Any
import aioboto3
from botocore.config import Config

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)


async def async_upload_payload(
    request_id: str,
    kwargs: dict[str, Any],
    response_obj: Any,
    settings: Settings | None = None,
) -> None:
    """Asynchronously upload Prompt and Response payloads to MinIO S3 bucket."""
    try:
        settings = settings or get_settings()
        if not settings.ENABLE_PAYLOAD_OFFLOAD:
            return

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        prefix = f"{date_str}/{request_id}"

        # 1. Extract Prompt Payload
        prompt_data = {
            "model": kwargs.get("model"),
            "messages": kwargs.get("messages", []),
            "optional_params": kwargs.get("optional_params", {}),
            "litellm_params": kwargs.get("litellm_params", {}),
        }

        # 2. Extract Response Payload
        if hasattr(response_obj, "model_dump"):
            response_data = response_obj.model_dump()
        elif hasattr(response_obj, "dict"):
            response_data = response_obj.dict()
        elif isinstance(response_obj, dict):
            response_data = response_obj
        else:
            response_data = {"raw": str(response_obj)}

        prompt_bytes = json.dumps(prompt_data, ensure_ascii=False, indent=2).encode("utf-8")
        response_bytes = json.dumps(response_data, ensure_ascii=False, indent=2).encode("utf-8")

        # 3. Asynchronous S3 PutObject via aioboto3
        session = aioboto3.Session()
        boto_config = Config(connect_timeout=2, read_timeout=3, retries={"max_attempts": 2})

        async with session.client(
            "s3",
            endpoint_url=settings.PAYLOAD_S3_ENDPOINT,
            aws_access_key_id=settings.PAYLOAD_ACCESS_KEY,
            aws_secret_access_key=settings.PAYLOAD_SECRET_KEY,
            config=boto_config,
        ) as s3_client:
            await s3_client.put_object(
                Bucket=settings.PAYLOAD_BUCKET_NAME,
                Key=f"{prefix}/prompt.json",
                Body=prompt_bytes,
                ContentType="application/json; charset=utf-8",
            )
            await s3_client.put_object(
                Bucket=settings.PAYLOAD_BUCKET_NAME,
                Key=f"{prefix}/response.json",
                Body=response_bytes,
                ContentType="application/json; charset=utf-8",
            )

        logger.debug("Successfully offloaded payloads for request %s to S3", request_id)
    except Exception as err:
        # Complete failure isolation: errors never block main API traffic or MySQL logging
        logger.warning("Failed to upload LLM payload for %s: %s", request_id, err)
```

---

### 4.3 Integration into `logging_hook.py`

In `app/core/logging_hook.py`, dispatch the upload task concurrently:

```python
# app/core/logging_hook.py
import asyncio
from app.core.payload_uploader import async_upload_payload

# Inside async_log_success_event and async_log_failure_event:
asyncio.create_task(
    async_upload_payload(
        request_id=request_id,
        kwargs=kwargs,
        response_obj=response_obj,
        settings=self.settings,
    )
)
```

---

## 5. MySQL Dynamic Hyperlink View (`v_llm_request_details`)

Zero schema migrations are required on the base `llm_request_logs` table. The view dynamically computes direct URLs.

### 5.1 View DDL Script (`scripts/create_views.sql`)

```sql
USE litellm_db;

-- Dynamic Hyperlink Detail View
CREATE OR REPLACE VIEW v_llm_request_details AS
SELECT 
    l.id,
    l.request_id,
    l.api_key_alias,
    l.model_requested,
    l.model_used,
    l.provider,
    l.provider_key_alias,
    l.prompt_tokens,
    l.completion_tokens,
    l.total_tokens,
    l.cost_usd,
    l.cost_cny,
    l.latency_ms,
    l.status_code,
    l.created_at,
    -- Dynamically construct public HTTPS hyperlink through Kong / Cloudflare proxy
    CONCAT(
        'https://payloads.jppwl.asia/litellm-payloads/',
        DATE_FORMAT(l.created_at, '%Y-%m-%d'), '/',
        l.request_id, '/prompt.json'
    ) AS prompt_url,
    CONCAT(
        'https://payloads.jppwl.asia/litellm-payloads/',
        DATE_FORMAT(l.created_at, '%Y-%m-%d'), '/',
        l.request_id, '/response.json'
    ) AS response_url
FROM llm_request_logs l;
```

---

### 5.2 Query Demonstration

```sql
SELECT 
    created_at,
    api_key_alias,
    model_used,
    total_tokens,
    cost_cny,
    prompt_url,
    response_url
FROM v_llm_request_details
ORDER BY created_at DESC
LIMIT 10;
```

**Query Output Example**:
| created_at | api_key_alias | model_used | total_tokens | cost_cny | prompt_url | response_url |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 2026-09-02 10:15:20 | hebe | gemini-3.7-flash | 152,396 | 0.089120 | `https://payloads.jppwl.asia/litellm-payloads/2026-09-02/req_abc123/prompt.json` | `https://payloads.jppwl.asia/litellm-payloads/2026-09-02/req_abc123/response.json` |

---

## 6. Implementation Milestones & Verification Checklist

- [ ] **Milestone 1: GitOps Repository & ArgoCD App Provisioning**
  - Create Kubernetes manifests in `nvd11/minio-deployment` (PVC 50Gi on NUC `local-path`, Deployment pinned to `nuc`, Service, Kong Ingress);
  - Add `minio-app.yaml` to `nvd11/my-argocd-manifests` and verify automatic sync on ArgoCD.
- [ ] **Milestone 2: Bucket & Access Policy Setup**
  - Initialize `litellm-payloads` bucket with public read-only policy and 90-day ILM retention.
- [ ] **Milestone 3: LiteLLM Payload Uploader Module**
  - Add `aioboto3` to dependencies in `pyproject.toml`;
  - Implement `app/core/payload_uploader.py` and unit tests in `tests/test_payload_uploader.py`.
- [ ] **Milestone 4: Logging Hook Integration**
  - Integrate `async_upload_payload` call in `app/core/logging_hook.py`.
- [ ] **Milestone 5: MySQL View Creation & E2E Validation**
  - Apply `create_views.sql` to OCI MySQL (`litellm_db`);
  - Send long context requests, query `v_llm_request_details`, and click generated URLs in browser.

---

## 7. Architecture Benefits Summary

1. **Zero Database Bloat**: Keeps MySQL InnoDB Buffer Pool 100% focused on indexing and analytical aggregation;
2. **True GitOps Infrastructure**: MinIO lifecycle, scheduling, storage, and networking are fully managed by ArgoCD;
3. **100% Data Sovereignty**: All prompt and agent history remain securely on the home homelab NUC storage;
4. **Sub-second Troubleshooting**: Immediate one-click drill down from DBeaver / DataGrip directly into full JSON payloads.
