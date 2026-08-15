# my-litellm-service

> Enterprise Multi-LLM Gateway, Cost Tracking & Evaluation Middleware on GCP.

`my-litellm-service` 是一个基于 **K3s + ArgoCD + 现有 Kong Gateway + OCI MySQL HeatWave** 的企业级大模型统一网关与评测中间件服务。项目采用 **LiteLLM Proxy + FastAPI + OCI MySQL + 现有 K3s Redis** 跨云解耦架构，提供多模型路由容灾、精细化 Token/美金费用审计、Redis 缓存加速以及大模型 Benchmark 自动化评测支持。数据持久化存储于 **OCI Always Free 托管 MySQL (`rin-heatwave`)**，Redis 复用现有 K3s 集群并固定在 OCI `free-arm-vm`。

## 📚 项目文档 (Documentation)

详细的技术需求规格说明书与架构设计文档请见 `docs/` 目录：

* 📄 **[High-Level Implementation Plan (docs/HIGH_LEVEL_IMPLEMENTATION_PLAN.md)](docs/HIGH_LEVEL_IMPLEMENTATION_PLAN.md)** - 包含整体目标、技术架构、接口定义、数据库 Schema 及分阶段实施 Roadmap。
* 📐 **[技术架构图与流程说明 (docs/ARCHITECTURE.md)](docs/ARCHITECTURE.md)** - 组件数据流图、Kubernetes Deployment 拆分与 Kong/ArgoCD 部署说明。
* 🧰 **[Phase 1 低层实施计划 (docs/plans/phase_1_low_level_implementation.md)](docs/plans/phase_1_low_level_implementation.md)** - 逐文件、逐方法说明基础设施接入与 LiteLLM Proxy 启动步骤。

## 🛠️ 技术栈 (Tech Stack)

* **Infrastructure**: Tencent Cloud K3s 集群、OCI `free-arm-vm` 节点、ArgoCD、现有 Kong Gateway
* **API Gateway**: LiteLLM Proxy (Unified OpenAI-compatible API)
* **Backend Middleware**: Python 3.12, FastAPI, Uvicorn, Pydantic
* **Database & Cache**: OCI MySQL HeatWave Always Free 9.7+ (Cost & Request Logging), existing K3s Redis 7+ via Kong L4 and Tailscale (Rate Limiting & Caching)
* **LLM Providers**: OpenAI, Google Vertex AI (Gemini), Anthropic Claude

---
*Created for Jason Pan's Hands-On Practice & Production Benchmark.*
