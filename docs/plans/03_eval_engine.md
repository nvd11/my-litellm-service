# Module 3 Implementation Plan: Local Objective Evaluation Engine (Option A + Option B)

> **Goal**: Construct a low-latency, objective, zero-API-cost LLM evaluation engine within FastAPI (Service B Deployment), supporting Option A (Deterministic Validation) and Option B (Golden Dataset Matching).

---

## 1. Architecture & Design Specification

### 1.1 Validation Strategies (Option A vs. Option B)
* **Option A: Deterministic Validation**
  * `json_schema`: Validates output conformance against Pydantic / JSON Schema definitions.
  * `code_exec`: Executes Python code against unit test assertions in a secured sandbox.
  * `contains`: Regex pattern or keyword inclusion assertions.
* **Option B: Golden Dataset Matching**
  * `exact_match`: Performs strict string equality matching against `golden_output`.
  * `similarity`: Computes similarity scores (0.00 ~ 1.00) based on `difflib` or character n-gram coverage.

---

## 2. Step-by-Step Implementation

### Step 1: MySQL Schema for Evaluation Results (`eval_benchmarks`)
```sql
CREATE TABLE IF NOT EXISTS eval_benchmarks (
    id VARCHAR(36) PRIMARY KEY,
    eval_run_id VARCHAR(64) NOT NULL,
    prompt_name VARCHAR(128) NOT NULL,
    model_name VARCHAR(64) NOT NULL,
    eval_type VARCHAR(32) NOT NULL,
    response_content TEXT,
    latency_ms INT NOT NULL,
    cost_usd DECIMAL(10, 6) NOT NULL,
    accuracy_score DECIMAL(5, 2),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_eval_run_id (eval_run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### Step 2: Evaluation Logic Implementation (`app/eval/evaluators.py`)
```python
import json
import jsonschema
import difflib


def evaluate_option_a(response_text: str, eval_type: str, rule_config: dict) -> float:
    """Option A: Deterministic assertion evaluation"""
    if eval_type == "json_schema":
        try:
            data = json.loads(response_text)
            jsonschema.validate(instance=data, schema=rule_config.get("schema", {}))
            return 100.0
        except Exception:
            return 0.0
    elif eval_type == "contains":
        keywords = rule_config.get("keywords", [])
        matched = sum(1 for kw in keywords if kw in response_text)
        return (matched / len(keywords)) * 100.0 if keywords else 0.0
    return 0.0


def evaluate_option_b(response_text: str, eval_type: str, golden_output: str) -> float:
    """Option B: Golden dataset matching"""
    if eval_type == "exact_match":
        return 100.0 if response_text.strip() == golden_output.strip() else 0.0
    elif eval_type == "similarity":
        ratio = difflib.SequenceMatcher(None, response_text.strip(), golden_output.strip()).ratio()
        return round(ratio * 100.0, 2)
    return 0.0
```

### Step 3: Concurrent Benchmark API Endpoint (`POST /v1/eval/run`)
Leverages `httpx.AsyncClient` to dispatch concurrent queries to LiteLLM Proxy, recording latencies and accuracy scores:
```python
# app/routers/eval.py
from fastapi import APIRouter
import time, asyncio, httpx

router = APIRouter(prefix="/v1/eval", tags=["Evaluation"])


@router.post("/run")
async def run_evaluation(eval_payload: dict):
    eval_run_id = f"eval_{int(time.time())}"
    prompts = eval_payload.get("prompts", [])
    models = eval_payload.get("models", ["gpt-4o", "gemini-1.5-pro"])

    results = []
    async with httpx.AsyncClient(
        base_url="http://litellm-proxy.llm-system.svc.cluster.local:4000"
    ) as client:
        # Dispatch concurrent evaluation requests across candidate models...
        pass

    return {"eval_run_id": eval_run_id, "results": results}
```

---

## 3. Verification & Acceptance Testing

1. **JSON Schema Assertion Verification**:
   ```bash
   curl -X POST http://<kong-host>/v1/eval/run \
     -H "Content-Type: application/json" \
     -d '{
       "prompts": [{
         "name": "json_test",
         "prompt": "Return JSON with name and age fields",
         "eval_type": "json_schema",
         "schema": {"type": "object", "properties": {"name": {"type": "string"}, "age": {"type": "number"}}, "required": ["name", "age"]}
       }],
       "models": ["gpt-4o"]
     }'
   ```
2. **Golden Match Verification**:
   * Supply expected `golden_output` strings and verify that returned `accuracy_score` metrics align with matching algorithms (`difflib` / strict match).

---

## 4. Risk Control & Operational Constraints

* ⚠️ **Sandbox Isolation**: If enabling `code_exec` to run model-generated code, execute within restricted environments to prevent unauthorized system command execution.
* ⚠️ **Concurrency Throttling**: Cap benchmark concurrency (e.g., `asyncio.Semaphore(10)`) to avoid overwhelming upstream provider rate limits.
