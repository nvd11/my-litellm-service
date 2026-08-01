# 技术架构与数据流设计 (Technical Architecture & Data Flow)

本文档补充说明 `my-litellm-service` 的底层架构设计、多进程通信流程、GCP 部署架构与容灾切换逻辑。

---

## 1. 系统网络与进程部署拓扑 (Network & Process Topology)

```
+-----------------------------------------------------------------------------------+
| GCP Compute Engine VM (Ubuntu 22.04 LTS / e2-standard-2)                         |
|                                                                                   |
|  +-----------------------------------+     +-----------------------------------+  |
|  | Process B: FastAPI Middleware     |     | Process A: LiteLLM Proxy          |  |
|  | - Port: 8000                      |     | - Port: 4000                      |  |
|  | - Systemd: fastapi.service        | --> | - Systemd: litellm.service        |  |
|  | - Features: Eval Harness & Metrics|     | - Features: LLM Router & Auth     |  |
|  +-----------------------------------+     +-----------------------------------+  |
|                  |                                           |                    |
+------------------|-------------------------------------------|--------------------+
                   |                                           |
                   v                                           v
    +------------------------------+            +------------------------------+
    | PostgreSQL Database (15+)    |            | Redis In-Memory Cache (7+)   |
    | - Port: 5432                 |            | - Port: 6379                 |
    | - Table: llm_request_logs    |            | - Key-value: Rate Limit &    |
    | - Table: eval_benchmarks     |            |   Exact Prompt Cache         |
    +------------------------------+            +------------------------------+
```

---

## 2. 请求处理与数据流 (Request Processing Flow)

当客户端或评估工具向服务发起请求时，整体数据流向如下：

1. **请求接入 (Ingress)**：
   * 客户端向 FastAPI (`:8000/v1/eval/run`) 或直接向 LiteLLM Proxy (`:4000/v1/chat/completions`) 发送符合 OpenAI 规范的 JSON 请求。
2. **限流与缓存拦截 (Rate Limiting & Caching Check)**：
   * LiteLLM Proxy 首先在 **Redis** 中检查该 API Key 的当前分钟调用计数（RPM / TPM）。若超限则直接返回 `429 Too Many Requests`。
   * 若启用了 Response Cache，匹配 Redis 中相同的 Prompt hash；若命中则直接返回缓存结果。
3. **模型路由与自动重试 (Routing & Fallback)**：
   * LiteLLM 根据配置路由到指定的底层 API（如 OpenAI `gpt-4o` 或 Vertex AI `gemini-1.5-pro`）。
   * 若目标 API 发生异常或超时，代理根据 `config.yaml` 自动重试备用模型。
4. **日志与计费落库 (Async Cost Logging)**：
   * 请求完成后，LiteLLM 异步提取响应中的 Token 数量与耗时，计算 USD 费用，并将日志保存至 **PostgreSQL** 数据表 `llm_request_logs`。
5. **响应返回 (Response)**：
   * 将标准的 OpenAI 格式响应数据返回给客户端。

---

## 3. GCP 部署与 Systemd 守护策略 (Systemd Supervision)

使用 Systemd 在 GCP VM 上守护进程：

### `litellm.service` 示例配置
```ini
[Unit]
Description=LiteLLM Proxy Service
After=network.target postgresql.service redis.service

[Service]
Type=simple
User=gateman
WorkingDirectory=/home/gateman/projects/my-litellm-service
ExecStart=/usr/local/bin/litellm --config config.yaml --port 4000
Restart=always
RestartSec=5
EnvironmentFile=/home/gateman/projects/my-litellm-service/.env

[Install]
WantedBy=multi-user.target
```

---
*End of Architectural Specification.*
