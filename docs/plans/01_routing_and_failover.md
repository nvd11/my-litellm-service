# 模块一实施计划：多模型路由与自动降级 (Routing & Failover)

> **目标**：在 LiteLLM Proxy 中配置多 Vendor 模型路由，实现一致的 OpenAI 兼容 API 暴露，并在主模型不可用时进行无感 Fallback 自动降级。

---

## 1. 架构与设计说明

### 1.1 路由层级
对客户端统一暴露虚拟模型别名（如 `smart-router` 或直接使用真实模型名），内部配置下发优先级：
1. **Primary Model**: `openai/gpt-4o` / `gemini/gemini-1.5-pro`
2. **Fallback Level 1**: `gemini/gemini-1.5-flash` / `openai/gpt-4o-mini`
3. **Fallback Level 2**: `anthropic/claude-3-5-sonnet`

### 1.2 Vertex AI 鉴权策略
在 K3s Pod 中通过 Kubernetes Secret 或 Workload Identity/外部 Secret 注入 Vertex AI 所需身份信息，连接 Vertex AI Gemini 模型无需在配置文件中硬编码 GCP Key。具体采用哪种注入方式由集群身份方案确定。

---

## 2. 详细实施步骤 (Step-by-Step)

### Step 1: 创建 `config.yaml` 配置文件
定义 `model_list` 映射与 `router_settings` 降级规则。

```yaml
model_list:
  # 虚拟多模型组：gpt-4o 主选，Gemini 降级
  - model_name: gpt-4o
    litellm_params:
      model: openai/gpt-4o
      api_key: os.environ/OPENAI_API_KEY

  - model_name: gpt-4o-mini
    litellm_params:
      model: openai/gpt-4o-mini
      api_key: os.environ/OPENAI_API_KEY
  
  - model_name: gemini-1.5-pro
    litellm_params:
      model: vertex_ai/gemini-1.5-pro
      vertex_project: os.environ/VERTEXAI_PROJECT
      vertex_location: os.environ/VERTEXAI_LOCATION

  - model_name: gemini-1.5-flash
    litellm_params:
      model: vertex_ai/gemini-1.5-flash
      vertex_project: os.environ/VERTEXAI_PROJECT
      vertex_location: os.environ/VERTEXAI_LOCATION

  - model_name: claude-3-5-sonnet
    litellm_params:
      model: anthropic/claude-3-5-sonnet-20240620
      api_key: os.environ/ANTHROPIC_API_KEY

router_settings:
  fallbacks:
    - gpt-4o: ["gemini-1.5-pro", "gemini-1.5-flash", "gpt-4o-mini", "claude-3-5-sonnet"]
    - gemini-1.5-pro: ["gemini-1.5-flash", "gpt-4o-mini", "claude-3-5-sonnet"]
  allowed_fails: 2
  cooldown_time: 30
  num_retries: 3
```

### Step 2: 配置环境变量模板 (`.env.example`)
```env
OPENAI_API_KEY=sk-proj-xxx
VERTEXAI_PROJECT=your-gcp-project-id
VERTEXAI_LOCATION=us-central1
ANTHROPIC_API_KEY=sk-ant-xxx
LITELLM_MASTER_KEY=sk-master-key-admin
LITELLM_PORT=4000
```

### Step 3: 启动 LiteLLM 代理进程验证
```bash
litellm --config config.yaml --port 4000 --debug
```

---

## 3. 验收与测试方案 (Verification & Acceptance)

1. **正常请求测试**：
   ```bash
   curl -X POST http://localhost:4000/v1/chat/completions \
     -H "Authorization: Bearer sk-master-key-admin" \
     -H "Content-Type: application/json" \
     -d '{
       "model": "gpt-4o",
       "messages": [{"role": "user", "content": "Hello!"}]
     }'
   ```
2. **Fallback 降级触发测试**：
   * 故意填入无效的 `OPENAI_API_KEY` 模拟 401/429 故障。
   * 再次向 `gpt-4o` 发起请求，观察 Response 及日志，确认系统是否自动降级并返回来自 `gemini-1.5-pro` 的结果。
3. **健康检查节点验证**：
   * `GET http://localhost:4000/health` 返回 `200 OK` 及各模型连通状态。

---

## 4. 风险控制与红线 (Risk Control)

* ⚠️ **凭证暴露红线**：严禁提交包含真实 API Key 的文件至 Git 仓库，确保使用 `.env` 与环境变量引用。
* ⚠️ **高成本预防**：给所有模型配置默认的 `max_tokens` 参数，防止异常的长 Prompt 消费过量预算。
