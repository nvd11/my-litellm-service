# 产品需求文档 (PRD) & 技术规格说明书

**项目名称**：`my-litellm-service` (Enterprise Multi-LLM Gateway & Evaluation Middleware)  
**作者**：Jason Pan (Senior Cloud & AI Solutions Architect)  
**版本**：v1.1.0 (Updated: Deterministic & Golden Dataset Evaluation Engine)  
**状态**：Draft for Hands-On Implementation  
**目标平台**：GCP Compute Engine (VM), PostgreSQL, Redis  

---

## 1. 项目背景与目标 (Background & Objectives)

### 1.1 背景 (Context)
随着企业对大语言模型（LLM）需求的爆发式增长，在实际工程落地过程中面临以下四大核心痛点：
1. **厂商锁定与 API 碎片化**：不同 LLM 厂商（OpenAI, Google Vertex AI, Anthropic）的 SDK 和 API 格式不一致，导致业务代码耦合严重。
2. **高可用与限流（Rate Limit / Failover）**：单一模型服务在遭遇 429 (Rate Limit) 或 5xx 服务不可用时缺少无感容灾切换机制。
3. **调用成本与 Token 盲区**：缺乏统一的 Token 消耗统计与 API 费用审计看板，无法实时拦截异常调用或设置预算上限。
4. **大模型评测（Eval Harness）缺乏基础设施**：业务团队在对比不同模型（如 Gemini 1.5 Pro vs GPT-4o）的响应质量、延迟和单次成本时，缺乏客观、零额外开销的评测中间件。

### 1.2 项目目标 (Goals)
`my-litellm-service` 旨在在 **GCP Compute Engine** 上搭建一套轻量级、企业级的高可用大模型统一网关与评测中间件：
* 统一屏蔽底层 LLM 差异，暴露标准的 **OpenAI 兼容 API**。
* 实现 **PostgreSQL 审计日志**，精准记录每次请求的 Prompt/Completion Tokens 及 USD 扣费。
* 基于 **Redis** 提供 sub-millisecond 级别的速率限制（Rate Limiting）与语义/精确缓存（Caching）。
* 基于 **确定性断言 (Option A) 与 黄金数据集比对 (Option B)** 提供客观、微秒级、零额外 API 成本的大模型综合性能（Accuracy, Latency, Cost）评测引擎。

---

## 2. 系统整体架构设计 (System Architecture)

系统基于 **"One VM, Two Processes"**（单虚拟机双进程）架构模式设计，兼顾部署简易性与高性能并发能力。

```
                          +-----------------------------------+
                          |     Clients / Eval Harness        |
                          +-----------------------------------+
                                            |
                                            v (HTTP / Port 8000)
                          +-----------------------------------+
                          |      Process B: FastAPI Service   |
                          | (Eval Engine: Option A & B Checks)|
                          +-----------------------------------+
                                            |
                                            v (HTTP / Port 4000)
                          +-----------------------------------+
                          |     Process A: LiteLLM Proxy      |
                          |    (Unified Router & Middleware)  |
                          +-----------------------------------+
                                   /        |        \
                                  /         |         \
                                 v          v          v
                          +-----------+ +-------+ +----------+
                          | PostgreSQL| | Redis | | LLM APIs |
                          |  (Logs/   | |(Rate- | |(OpenAI/  |
                          |  Budgets) | |Limit) | |Vertex AI)|
                          +-----------+ +-------+ +----------+
```

### 2.1 进程与组件职责划分
1. **Process A: LiteLLM Proxy (Port 4000)**
   * 作为核心 LLM 网关，处理协议转换、模型负载均衡与 Failover 路由。
   * 内置 Admin UI 界面，提供模型 Key 管理与配置查看。
   * 通过内置 Hook 将请求耗时、Token 数及美元开销实时写入 PostgreSQL 与 Redis。
2. **Process B: FastAPI Application (Port 8000)**
   * 提供应用层 API、自定义 Benchmark 触发接口及成本统计报表导出。
   * 集成 **方案 A (JSON Schema / Code / Regex)** 与 **方案 B (Golden Answer Matching)** 本地评估引擎。
   * 对外暴露 `/v1/eval/run`、`/v1/metrics/spend` 及健康检查 `/health` 接口。
3. **Data & Storage Layer**
   * **PostgreSQL (15+)**：持久化存储请求日志（`llm_request_logs`）及评测结果（`eval_benchmarks`）。
   * **Redis (7+)**：处理并发 API 速率限制、Token 临时 Bucket 计数以及常见 Prompt 的缓存响应。

---

## 3. 核心功能需求规格 (Functional Specifications)

### 3.1 统一路由与多模型容灾 (Routing & Failover)
* **API 兼容性**：网关须 100% 兼容 `POST /v1/chat/completions` 标准。
* **支持模型列表**：
  * `openai/gpt-4o` / `openai/gpt-4o-mini`
  * `gemini/gemini-1.5-pro` / `gemini/gemini-1.5-flash` (via GCP Vertex AI / Service Account)
  * `anthropic/claude-3-5-sonnet`
* **Fallback 容灾规则**：
  * 当主模型返回 `429 (Rate Limit)`、`500` 或 `503` 时，自动重试切换至备用模型。

### 3.2 费用审计与 Token 计量 (Cost Audit & Token Metering)
* **精准 Token 统计**：提取 `prompt_tokens`、`completion_tokens` 和 `total_tokens`。
* **美元花费计算 (USD Cost)**：网关根据模型单价实时计算并记录成本（精确到小数点后 6 位）。
* **PostgreSQL 日志落库**：异步写入 `llm_request_logs` 表。

### 3.3 评估引擎设计 (Evaluation Engine: Option A + Option B)
评测引擎放弃昂贵且带偏见的 LLM-as-a-Judge 方案，全面采用 **方案 A + 方案 B** 本地高客观度校验：

1. **方案 A：确定性断言校验 (Deterministic Validation)**
   * **`eval_type="json_schema"`**：使用 Python `pydantic` 或 `jsonschema` 对 LLM 输出的结构化数据进行语法与字段类型校验（100 分 / 0 分）。
   * **`eval_type="code_exec"`**：在安全 Python 沙盒中运行生成的代码并跑单元测试。
   * **`eval_type="contains"`**：正则匹配或关键词包含判断。
2. **方案 B：黄金数据集比对 (Golden Dataset Matching)**
   * **`eval_type="exact_match"`**：与预设标准答案 `golden_output` 进行完全匹配。
   * **`eval_type="similarity"`**：使用 Python 本地 `difflib` 或字符覆盖率计算文本重合相似度（0.00 ~ 1.00）。

### 3.4 FastAPI 中间件与 Eval Harness 接口
* **`POST /v1/eval/run`**：传入测试集，并发向指定的模型发送请求，收集并返回各模型的响应延迟（`latency_ms`）、成本（`cost_usd`）及准确率得分（`accuracy_score`）。
* **`GET /v1/metrics/spend`**：支持查询按天、按模型统计的累计美元开销。

---

## 4. 数据库 Schema 设计 (Database Schema)

```sql
-- 1. 核心请求与费用日志表
CREATE TABLE IF NOT EXISTS llm_request_logs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    request_id VARCHAR(128) NOT NULL,
    api_key_alias VARCHAR(64) DEFAULT 'default',
    model_requested VARCHAR(64) NOT NULL,
    model_used VARCHAR(64) NOT NULL,
    prompt_tokens INT NOT NULL DEFAULT 0,
    completion_tokens INT NOT NULL DEFAULT 0,
    total_tokens INT NOT NULL DEFAULT 0,
    cost_usd NUMERIC(10, 6) NOT NULL DEFAULT 0.000000,
    latency_ms INT NOT NULL,
    status_code INT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_logs_created_at ON llm_request_logs(created_at);
CREATE INDEX idx_logs_model ON llm_request_logs(model_used);

-- 2. 大模型评测记录表 (Option A + Option B 评测结果)
CREATE TABLE IF NOT EXISTS eval_benchmarks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    eval_run_id VARCHAR(64) NOT NULL,
    prompt_name VARCHAR(128) NOT NULL,
    model_name VARCHAR(64) NOT NULL,
    eval_type VARCHAR(32) NOT NULL, -- json_schema, exact_match, code_exec, similarity
    response_content TEXT,
    latency_ms INT NOT NULL,
    cost_usd NUMERIC(10, 6) NOT NULL,
    accuracy_score NUMERIC(5, 2), -- 0.00 ~ 100.00
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_eval_run_id ON eval_benchmarks(eval_run_id);
```

---

## 5. 环境与配置矩阵 (Environment Configuration)

```env
# GCP & System Environment
GCP_PROJECT_ID=your-gcp-project-id
GCP_REGION=us-central1

# Database & Cache Connection
POSTGRES_USER=litellm_user
POSTGRES_PASSWORD=litellm_password
POSTGRES_DB=litellm_db
POSTGRES_HOST=localhost
POSTGRES_PORT=5432

REDIS_HOST=localhost
REDIS_PORT=6379

# LLM Provider API Keys
OPENAI_API_KEY=sk-proj-...
VERTEXAI_PROJECT=your-gcp-project-id
VERTEXAI_LOCATION=us-central1

# LiteLLM Proxy Configuration
LITELLM_MASTER_KEY=sk-master-key-admin
LITELLM_PORT=4000
FASTAPI_PORT=8000
```

---

## 6. 主人实战练习 Roadmap (Hands-On Execution Plan)

### 阶段一：本地基础设施与 LiteLLM 代理启动 (Phase 1)
- [ ] 启动本地 PostgreSQL 与 Redis 服务。
- [ ] 编写 LiteLLM 配置文件 `config.yaml`，声明 OpenAI、Gemini 及 Claude 的模型路由与 Fallback 机制。
- [ ] 启动 LiteLLM Proxy 进程，验证 `http://localhost:4000/health` 及 `/v1/chat/completions`。

### 阶段二：PostgreSQL 落库与数据库 Hook (Phase 2)
- [ ] 执行 SQL DDL 脚本初始化 `llm_request_logs` 与 `eval_benchmarks` 数据表。
- [ ] 配置 LiteLLM Proxy 连接 PostgreSQL，验证每次 API 调用的 Token 数与 USD 花费成功异步写入数据库。

### 阶段三：FastAPI 中间件与 Eval Engine (方案 A+B) 开发 (Phase 3)
- [ ] 编写 FastAPI 主程序 `main.py`。
- [ ] 实现方案 A (JSON Schema / 代码断言) 与 方案 B (Golden Answer 匹配) 本地评估函数。
- [ ] 实现 `/v1/eval/run` 评测接口：使用 Python 协程并发测试多个模型的响应速度、开销与准确率。
- [ ] 实现 `/v1/metrics/spend` 接口：读取 PostgreSQL 统计当日与累计的花费账单。

### 阶段四：GCP Compute Engine 部署与 Systemd 守护 (Phase 4)
- [ ] 在 GCP Compute Engine 上创建 Ubuntu 22.04 VM 实例。
- [ ] 编写 Systemd 服务文件 `litellm.service` 与 `fastapi.service`，实现双进程守护与开机自启。
- [ ] 进行完整联调与压力测试。

---

*文档完结 - 需求与评估引擎完全收敛为方案 A + 方案 B！*
