# my-litellm-service

> Enterprise Multi-LLM Gateway, Cost Tracking & Evaluation Middleware on GCP / OCI / Hybrid Cloud.

`my-litellm-service` is an enterprise-grade LLM unified gateway and evaluation middleware service built on **K3s + ArgoCD + existing Kong Gateway + OCI MySQL HeatWave**. The project adopts a decoupled multi-cloud architecture combining **LiteLLM Proxy + FastAPI + OCI MySQL + existing K3s Redis**, providing multi-model routing failover, granular token/USD cost auditing, Redis caching acceleration, and automated LLM benchmark evaluation support. Persistent data is stored in **OCI Always Free Managed MySQL (`rin-heatwave`)**, while Redis reuses the existing K3s cluster pinned to the OCI `free-arm-vm` node.

## 📚 Documentation

For detailed technical specifications, architecture designs, and operations guides, refer to the `docs/` directory:

* 📄 **[High-Level Implementation Plan (docs/HIGH_LEVEL_IMPLEMENTATION_PLAN.md)](docs/HIGH_LEVEL_IMPLEMENTATION_PLAN.md)** - High-level goals, technical architecture, API definitions, database schemas, and phased roadmap.
* 📐 **[Technical Architecture & Flow Diagrams (docs/ARCHITECTURE.md)](docs/ARCHITECTURE.md)** - Component data flow, Kubernetes Deployment breakdown, and Kong / ArgoCD integration specs.
* 🧰 **[Phase 1 Low-Level Implementation Plan (docs/plans/phase_1_low_level_implementation.md)](docs/plans/phase_1_low_level_implementation.md)** - File-by-file and step-by-step low-level implementation guide for bootstrapping LiteLLM Proxy.

## 🛠️ Tech Stack

* **Infrastructure**: Tencent Cloud K3s cluster, OCI `free-arm-vm` node, ArgoCD, existing Kong Gateway
* **API Gateway**: LiteLLM Proxy (Unified OpenAI-compatible API)
* **Backend Middleware**: Python 3.12, FastAPI, Uvicorn, Pydantic
* **Database & Cache**: OCI MySQL HeatWave Always Free 9.7+ (Cost & Request Logging), existing K3s Redis 7+ via Kong L4 and Tailscale (Rate Limiting & Caching)
* **LLM Providers**: OpenAI, Google Vertex AI (Gemini), Anthropic Claude

---
*Created for Jason Pan's Hands-On Practice & Production Benchmark.*
