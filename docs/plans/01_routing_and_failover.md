# Module 1 Implementation Plan: Multi-Model Routing & Automated Failover

> **Goal**: Configure multi-vendor model routing within LiteLLM Proxy to expose a unified OpenAI-compatible API, providing seamless automated fallback degradation when primary models fail.

---

## 1. Architecture & Design Specification

### 1.1 Routing Hierarchy
Expose unified virtual model aliases (or standard model names) to clients while configuring upstream prioritization internally:
1. **Primary Model**: `openai/gpt-4o` / `gemini/gemini-1.5-pro`
2. **Fallback Level 1**: `gemini/gemini-1.5-flash` / `openai/gpt-4o-mini`
3. **Fallback Level 2**: `anthropic/claude-3-5-sonnet`

### 1.2 Vertex AI Authentication Strategy
Inject Vertex AI authentication credentials into K3s Pods via Kubernetes Secrets, Workload Identity, or External Secrets without hardcoding GCP keys in configuration files. The specific injection mechanism is determined by the cluster identity architecture.

---

## 2. Step-by-Step Implementation

### Step 1: Create `config.yaml`
Define `model_list` mappings and `router_settings` fallback rules.

```yaml
model_list:
  # Virtual multi-model group: gpt-4o primary, Gemini fallback
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

### Step 2: Configure Environment Template (`.env.example`)
```env
OPENAI_API_KEY=sk-proj-xxx
VERTEXAI_PROJECT=your-gcp-project-id
VERTEXAI_LOCATION=us-central1
ANTHROPIC_API_KEY=sk-ant-xxx
LITELLM_MASTER_KEY=sk-mas...dmin
LITELLM_PORT=4000
```

### Step 3: Launch LiteLLM Proxy Process for Verification
```bash
litellm --config config.yaml --port 4000 --debug
```

---

## 3. Verification & Acceptance Testing

1. **Standard Invocations**:
   ```bash
   curl -X POST http://localhost:4000/v1/chat/completions \
     -H "Authorization: Bearer *** \
     -H "Content-Type: application/json" \
     -d '{
       "model": "gpt-4o",
       "messages": [{"role": "user", "content": "Hello!"}]
     }'
   ```
2. **Fallback Trigger Testing**:
   * Temporarily provide an invalid `OPENAI_API_KEY` to simulate 401/429 upstream errors.
   * Send a request to `gpt-4o` and verify through logs and responses that the system automatically degrades and returns results from `gemini-1.5-pro`.
3. **Health Probe Verification**:
   * `GET http://localhost:4000/health` returns `200 OK` along with upstream connectivity status.

---

## 4. Risk Control & Operational Constraints

* ⚠️ **Zero Credential Leaks**: Never commit files containing raw API keys to Git; always reference environment variables via `.env`.
* ⚠️ **Cost Protection**: Enforce default `max_tokens` across all model configurations to prevent runaway costs from abnormally large prompts.
