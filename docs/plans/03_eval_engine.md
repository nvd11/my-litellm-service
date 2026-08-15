# 模块三实施计划：本地客观评测引擎 (Eval Engine: Option A + Option B)

> **目标**：在 FastAPI (Service B Deployment) 中构建低延迟、客观、零额外 API 成本的大模型评测引擎，支持方案 A (确定性断言) 与 方案 B (黄金数据集比对)。

---

## 1. 架构与设计说明

### 1.1 校验策略对比 (Option A vs Option B)
* **Option A：确定性断言 (Deterministic Validation)**
  * `json_schema`: 校验输出是否符合 Pydantic / JSON Schema 语法。
  * `code_exec`: 在沙盒环境中执行 Python 代码并运行断言。
  * `contains`: 正则或关键字包含断言。
* **Option B：黄金数据集比对 (Golden Dataset Matching)**
  * `exact_match`: 与标准答案（`golden_output`）进行完全文本匹配。
  * `similarity`: 基于 `difflib` 或字符重合率计算相似度得分（0.00 ~ 1.00）。

---

## 2. 详细实施步骤 (Step-by-Step)

### Step 1: MySQL DDL 校验结果表 (`eval_benchmarks`)
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

### Step 2: 评测核心函数编写 (`app/eval/evaluators.py`)
```python
import json
import jsonschema
import difflib

def evaluate_option_a(response_text: str, eval_type: str, rule_config: dict) -> float:
    """方案 A：确定性断言校验"""
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
    """方案 B：黄金数据集比对"""
    if eval_type == "exact_match":
        return 100.0 if response_text.strip() == golden_output.strip() else 0.0
    elif eval_type == "similarity":
        ratio = difflib.SequenceMatcher(None, response_text.strip(), golden_output.strip()).ratio()
        return round(ratio * 100.0, 2)
    return 0.0
```

### Step 3: 并发评测 API 端点 (`POST /v1/eval/run`)
借助 `httpx.AsyncClient` 并发向 LiteLLM Proxy 发起测试，捕获耗时并计算准确率得分：
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
    async with httpx.AsyncClient(base_url="http://litellm-proxy.llm-system.svc.cluster.local:4000") as client:
        # 并发向多模型发送请求并评估...
        pass
        
    return {"eval_run_id": eval_run_id, "results": results}
```

---

## 3. 验收与测试方案 (Verification & Acceptance)

1. **测试 JSON Schema 评测断言**：
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
2. **测试 Golden Match 评测比对**：
   * 传入标准 `golden_output` 文本，验证返回的 `accuracy_score` 是否与比对算法（`difflib` / 完全匹配）预期相符。

---

## 4. 风险控制与红线 (Risk Control)

* ⚠️ **沙盒隔离**：若启用 `code_exec` 执行生成代码，必须限定在隔离环境或受限 `exec()` 环境下运行，防止高危系统命令越权。
* ⚠️ **并发限流**：评测并发数需受限（如 `asyncio.Semaphore(10)`），避免突发大量请求直接打满后端 LLM 限流额度。
