# my-litellm-service

> Enterprise Multi-LLM Gateway, Cost Tracking & Evaluation Middleware on GCP.

`my-litellm-service` 是一个基于 **GCP Compute Engine** 的企业级大模型统一网关与评测中间件服务。项目采用 **LiteLLM Proxy + FastAPI + PostgreSQL + Redis** 架构，提供多模型路由容灾、精细化 Token/美金费用审计、Redis 缓存加速以及大模型 Benchmark 自动化评测支持。

## 📚 项目文档 (Documentation)

详细的技术需求规格说明书与架构设计文档请见 `docs/` 目录：

* 📄 **[PRD & 需求规格说明书 (docs/PRD.md)](docs/PRD.md)** - 包含完整的业务需求、技术架构、接口定义、数据库 Schema 及分阶段练手 Roadmap。
* 📐 **[技术架构图与流程说明 (docs/ARCHITECTURE.md)](docs/ARCHITECTURE.md)** - 组件数据流图、GCP VM 双进程模型与容灾机制说明。

## 🛠️ 技术栈 (Tech Stack)

* **Infrastructure**: GCP Compute Engine (Ubuntu 22.04 LTS), Systemd / Docker Compose
* **API Gateway**: LiteLLM Proxy (Unified OpenAI-compatible API)
* **Backend Middleware**: Python 3.11, FastAPI, Uvicorn, Pydantic
* **Database & Cache**: PostgreSQL 15+ (Cost & Request Logging), Redis 7+ (Rate Limiting & Caching)
* **LLM Providers**: OpenAI, Google Vertex AI (Gemini), Anthropic Claude

---
*Created for Jason Pan's Hands-On Practice & Production Benchmark.*
