# Phase 6 实施计划：LiteLLM 可观测性看板 (Observatory Dashboard) 与 FastAPI 监控接口技术规格

> **目标**：在当前代码仓库（`my-litellm-service`）中构建一套开箱即用、轻量高效的 **大模型可观测性看板（LiteLLM Observatory Dashboard）**。复用已有的 **FastAPI 服务 (`Service B: Port 8000`)**，对外暴露标准分页与统计接口；前端采用 **现代单页面架构 (Tailwind CSS + Vue 3 ESM + Highlight.js)** 嵌入静态资源；与 **OCI MySQL (`v_llm_request_details`)** 及 **NUC MinIO S3** 深度联动；通过 **ArgoCD GitOps** 与 **Kong Gateway (Logto SSO 零信任保护)** 接入统一域名路由（`https://gw.jppwl.asia/dashboard`），实现大模型调用的实时审计、费用监控与报文深度穿透。

---

## 1. 架构定位与设计哲学

### 1.1 为什么自研内嵌轻量看板？
1. **极致轻量（Zero Fat）**：
   - 传统 BI（如 Apache Superset / Databricks）常驻内存需 1GB~2GB，依赖 Redis、Celery、Gunicorn 等复杂中间件矩阵；
   - 自研看板直接运行在现有的 FastAPI 进程中，常驻内存增量 **< 30MB**，首屏秒开，资源开销几乎为零。
2. **大模型专用交互体验（Tailored for LLM Observability）**：
   - 传统数据库表格无法良好展示多轮对话、长上下文与思维链（Reasoning）；
   - 自研看板支持 **双栏抽屉（Sliding Drawer）**，一键拆解 `System Prompt`、`User Prompt`、`Model Reply` 与 `Reasoning Tokens`，支持 Markdown 高亮渲染与一键复制。
3. **单仓库一体化交付（Monorepo Delivery）**：
   - 前后端代码完全在同一仓库内管理，同一次 Git Commit 即可完成 API 变更与 UI 渲染对齐，单次 CI/CD 自动发布上线。

---

## 2. 系统整体架构拓扑 (Mermaid)

```mermaid
flowchart TD
    subgraph ClientLayer["客户端与浏览器访问层"]
        Browser["开发者 / 管理员浏览器"]
    end

    subgraph SecurityLayer["安全网关与 SSO 认证 (Kong + Cloudflare)"]
        CF["Cloudflare Edge CDN (*.jppwl.asia)"]
        Kong["Kong Gateway (oauth2-forward-auth 插件)"]
        Logto["Logto Cloud IdP (GitHub SSO 扫码认证)"]
    end

    subgraph K3sCluster["K3s 业务集群 (tencent-dp1-cluster)"]
        subgraph FastAPI_Pod["FastAPI Observatory Service (:8000)"]
            StaticUI["/dashboard (Tailwind + Vue 3 静态单页面)"]
            LogAPI["/api/v1/logs (分页查询 / 条件过滤)"]
            SummaryAPI["/api/v1/metrics/summary (今日大盘指标)"]
            PayloadProxy["/api/v1/logs/{id}/payload (MinIO S3 报文直读)"]
        end

        subgraph LiteLLM_Pod["LiteLLM Proxy 网关 (:4000)"]
            Router["多 Tier 容灾路由 / 模型负载均衡"]
        end

        MinIO["NUC MinIO S3 存储 (:9000)<br/>/home/data/litellm_payloads (800GB NVMe)"]
    end

    subgraph StorageLayer["持久化数据层"]
        MySQL[("OCI MySQL HeatWave (litellm_db)<br/>- llm_request_logs (主表)<br/>- v_llm_request_details (超链接视图)")]
    end

    Browser -->|HTTPS 443| CF
    CF --> Kong
    Kong -.->|未登录拦截| Logto
    Kong -->|已认证流量放行| StaticUI
    Kong -->|API 数据请求| LogAPI
    Kong -->|统计请求| SummaryAPI
    Kong -->|报文查看| PayloadProxy

    LogAPI -->|SQLAlchemy 异步查询| MySQL
    SummaryAPI -->|聚合 COUNT/SUM| MySQL
    PayloadProxy -->|K3s 内网 S3 直读| MinIO
```

---

## 3. 后端数据接口设计 (`app/api/`)

在 `app/api/` 目录下提供标准化的 FastAPI 路由模块。

### 3.1 核心数据接口规范

#### 1. 审计日志分页与筛选接口 (`GET /api/v1/logs`)
- **请求参数**：
  - `page`: 页码（默认 1）
  - `page_size`: 每页大小（默认 20，最大 100）
  - `start_date`: 开始日期（如 `2026-09-01`）
  - `end_date`: 结束日期（如 `2026-09-03`）
  - `api_key_alias`: 调用方别名（`cindy` / `hebe` / `rin` / `default_user_id`）
  - `model_used`: 模型名称（`gemini-3.7-flash` / `gemini-3.7-backup`）
  - `status_code`: 状态码（200 / 429 / 500）
  - `search`: 模糊匹配关键字（匹配 `request_id` 或 `api_key_alias`）
- **返回结构**：
  ```json
  {
    "code": 0,
    "data": {
      "items": [
        {
          "id": "5de66938-76de-46b2-999e-382259e6f302",
          "request_id": "tJiZasjhJaC3g8UPrJmp2Qw",
          "api_key_alias": "cindy",
          "model_requested": "gemini-3.7-flash",
          "model_used": "gemini-3.7-flash",
          "provider": "google-gemini",
          "provider_key_alias": "OPENAI_API_KEY_FREE_3",
          "prompt_tokens": 18,
          "completion_tokens": 27,
          "total_tokens": 45,
          "cost_cny": 0.000775,
          "cost_usd": 0.000107,
          "latency_ms": 995,
          "status_code": 200,
          "created_at": "2026-09-03 15:56:37",
          "prompt_url": "https://payloads.jppwl.asia/litellm-payloads/2026-09-03/tJiZasjhJaC3g8UPrJmp2Qw/prompt.json",
          "response_url": "https://payloads.jppwl.asia/litellm-payloads/2026-09-03/tJiZasjhJaC3g8UPrJmp2Qw/response.json"
        }
      ],
      "total": 1420,
      "page": 1,
      "page_size": 20
    }
  }
  ```

---

#### 2. 大盘指标汇总卡片接口 (`GET /api/v1/metrics/summary`)
- **请求参数**：`date`（可选，默认当天）
- **返回结构**：
  ```json
  {
    "code": 0,
    "data": {
      "today_requests": 384,
      "today_tokens": 12850400,
      "today_cost_cny": 8.4215,
      "today_cost_usd": 1.1648,
      "avg_latency_ms": 1120,
      "success_rate": 99.48,
      "active_keys": [
        {"alias": "cindy", "count": 210, "cost_cny": 5.12},
        {"alias": "hebe", "count": 98, "cost_cny": 2.10},
        {"alias": "rin", "count": 76, "cost_cny": 1.20}
      ]
    }
  }
  ```

---

#### 3. 报文详情内联获取接口 (`GET /api/v1/logs/{request_id}/payload`)
- **作用**：解决跨域和前端直接渲染问题。FastAPI 后端通过集群内网（`http://minio.minio.svc.cluster.local:9000`）快速读取 MinIO S3 中的 `prompt.json` 与 `response.json`，聚合后返回前端：
  ```json
  {
    "code": 0,
    "data": {
      "request_id": "tJiZasjhJaC3g8UPrJmp2Qw",
      "prompt": {
        "model": "gemini-3.7-flash",
        "system_prompt": "你叫 Cindy，是主人的贴身秘书兼架构副手。",
        "user_prompt": "Cindy，请汇报系统状态！",
        "messages": [ ... ],
        "parameters": { "temperature": 0.7, "max_tokens": 100 }
      },
      "response": {
        "model": "gemini-3.7-flash",
        "reply": "主人，系统各项指标 100% 正常运行～",
        "reasoning_content": null,
        "finish_reason": "stop",
        "usage": { "total_tokens": 45 }
      }
    }
  }
  ```

---

## 4. 前端 UI 看板交互设计 (`app/static/`)

在 `app/static/index.html` 采用 **Vue 3 (ES Modules) + Tailwind CSS CDN + Lucide Icons + Highlight.js** 组织纯静态 SPA 单文件，零构建、零打包、开箱即用。

### 4.1 UI 界面布局规划

```text
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ 🌟 LiteLLM Observatory 看板     [ 2026-09-03 ▼] [ 自动刷新: 5s 🟢 ] [ 搜索: RequestID ] │
├───────────────────┬───────────────────┬───────────────────┬────────────────────────────┤
│ 📊 今日调用量      │ 🪙 今日消耗 Tokens │ 💰 今日折合人民币  │ ⚡ 平均响应延迟            │
│    384 次         │   12.85 M Tokens  │     ¥ 8.4215      │       1,120 ms             │
├───────────────────┴───────────────────┴───────────────────┴────────────────────────────┤
│ 📋 请求审计数据流 (点击行滑出右侧透视抽屉)                                              │
├─────────────────┬──────────┬──────────────────┬──────────────┬──────────┬──────────────┤
│ 触发时间        │ 调用者   │ 实际使用模型     │ 消耗 Tokens  │ 扣费金额 │ 状态         │
├─────────────────┼──────────┼──────────────────┼──────────────┼──────────┼──────────────┤
│ 17:57:16        │ cindy    │ gemini-3.7-flash │ 45           │ ¥0.0007  │ 🟢 200 OK    │
│ 17:54:40        │ hebe     │ gemini-3.7-flash │ 35           │ ¥0.0006  │ 🟢 200 OK    │
│ 15:56:37        │ rin      │ gemini-3.7-flash │ 18           │ ¥0.0003  │ 🟢 200 OK    │
│ 15:51:28        │ default  │ gemini-3.7-flash │ 22           │ ¥0.0003  │ 🔴 429 Limit │
└─────────────────┴──────────┴──────────────────┴──────────────┴──────────┴──────────────┘
```

### 4.2 右侧报文透视抽屉 (Sliding Drawer)

点击任意行后，右侧平滑滑出全屏/半屏抽屉面板：

- **Tab 1: 格式化高亮视图（默认）**：
  - 📌 **System Prompt 卡片**（浅紫底色，带一键复制与收起功能）；
  - 💬 **User Prompt 卡片**（浅蓝底色，展示用户最新提问）；
  - 💡 **Assistant Reply 卡片**（浅绿底色，支持 Markdown 语法高亮）；
  - 🧠 **Thinking & Reasoning 折叠栏**（针对思维链模型展示思考过程）；
  - 📜 **多轮对话上下文折叠列表**（可按时序展开全部历史问答）。
- **Tab 2: 原始 JSON 报文**：
  - 左右并排高亮展示原始 `prompt.json` 与 `response.json`，提供复制 Raw JSON 功能。
- **Tab 3: 元数据与计费明细**：
  - 汇率基准、上游真实 Key 别名（`provider_key_alias`）、毫秒级耗时明细。

---

## 5. 项目代码结构变更规划

```text
my-litellm-service/
├── app/
│   ├── __init__.py
│   ├── main.py                  # 🌟 FastAPI 主入口 (托管路由与静态文件)
│   ├── api/                     # 🌟 新增 API 路由层
│   │   ├── __init__.py
│   │   ├── logs.py              # /api/v1/logs 审计分页与过滤
│   │   ├── metrics.py           # /api/v1/metrics/summary 指标汇总
│   │   └── payload.py           # /api/v1/logs/{id}/payload S3 报文代理
│   ├── core/
│   │   ├── config.py            # 配置扩展
│   │   ├── logging_hook.py      # 日志 Hook
│   │   └── payload_uploader.py  # S3 异步上传
│   ├── db/
│   │   ├── engine.py
│   │   └── tables.py
│   └── static/                  # 🌟 新增前端静态资源
│       ├── index.html           # 现代单页面看板 SPA (Vue 3 + Tailwind)
│       └── favicon.ico
├── tests/
│   ├── test_api_logs.py         # 🌟 接口单测与覆盖
│   └── test_payload_uploader.py
├── scripts/
│   └── create_views.sql
└── docs/plans/
    └── phase_6_observability_dashboard_implementation_plan.md
```

---

## 6. 六大实施里程碑与步骤 (Step-by-Step)

### 【Milestone 1】FastAPI 核心应用骨架与静态托管 (`app/main.py`)
- 创建 FastAPI 实例，配置 CORS 跨域放行；
- 挂载静态文件目录 `app/static` 至路由 `/dashboard`；
- 配置健康检查路由 `/health`。

### 【Milestone 2】MySQL 审计日志与汇总 API 开发 (`app/api/logs.py` & `metrics.py`)
- 基于 SQLAlchemy 异步查询构建器，实现对 `v_llm_request_details` 视图的多条件动态筛选与分页；
- 编写聚合函数，计算今日请求量、Token 总量、CNY 消费总额与调用方消耗排行。

### 【Milestone 3】内网 S3 Payload 报文代理读取 (`app/api/payload.py`)
- 使用 `aioboto3`，通过 `minio.minio.svc.cluster.local:9000` 直接异步拉取指定 `request_id` 的 `prompt.json` 与 `response.json`；
- 增加本地缓存与容错处理，未落盘的报文返回优雅友好提示。

### 【Milestone 4】前端现代单页面看板开发 (`app/static/index.html`)
- 基于 Vue 3 Composition API 构建响应式状态；
- 集成 Tailwind CSS 现代化配色（深色/浅色卡片式设计）；
- 实现自动刷新轮询（5s / 10s 开关）；
- 实现右侧平滑抽屉式查看器，集成 Highlight.js 实现代码与 JSON 高亮。

### 【Milestone 5】全链路接口测试与自动化验证
- 编写 `tests/test_api_logs.py`，模拟分页、筛选、空数据与异常条件；
- 确保测试套件全部 100% 绿灯。

### 【Milestone 6】ArgoCD GitOps 发布与网关 SSO 路由联调
- 在 `my-argocd-manifests` 中配置 Kong HTTPRoute：
  - 路由 `https://gw.jppwl.asia/dashboard` ──► 转发至 FastAPI `:8000`；
  - 挂载 Kong 插件 `oauth2-forward-auth`（保护看板只对主人的 GitHub/Logto 账号开放）。
- 验证生产环境真实访问与联动体验。

---

## 7. 方案核心收益总结

1. **极致开销**：单容器架构，FastAPI 内存常驻仅 ~30MB，彻底告别 Superset 的沉重资源负担；
2. **直观可观测**：结构化指标秒级大盘聚合 + 长文本多轮对话右侧抽屉一键透视；
3. **零信任安全**：外网访问全程受 Cloudflare SSL + Kong Gateway + Logto GitHub SSO 严格保护；
4. **单仓库闭环**：前后端统一代码库、统一自动化 CI/CD 与 GitOps 发布流程。
