# 模块四实施计划：限流与缓存 (Rate Limiting & Caching)

> **目标**：将 LiteLLM Proxy 接入现有 K3s Redis 7+ 缓存与限流中间件，实现低延迟 API RPM/TPM 限流拦截与 Exact Prompt 哈希缓存；不新建 Redis 实例。

---

## 1. 架构与设计说明

### 1.1 Redis 存储职责划分
1. **Rate Limiting Bucket (速率限制桶)**：
   * 基于 API Key 或 Client IP 统计分钟级请求数 (RPM) 与 Token 消耗数 (TPM)。
   * 超限时立即在网关层抛出 `HTTP 429 Too Many Requests`，避免无谓消耗上游 LLM 额度。
2. **Exact Prompt Cache (精确提示词缓存)**：
   * 将 `sha256(model + prompt + temperature)` 作为 Cache Key。
   * 命中的请求直接返回 Redis 缓存的 JSON Response，响应延迟由 >1000ms 降低至 <5ms，且成本为 $0。

---

## 2. 详细实施步骤 (Step-by-Step)

### Step 1: 接入既有 Redis 服务
复用现有 K3s Redis，不启动本机 Docker Redis 或 `redis-server`。Redis Pod 固定运行于 OCI `free-arm-vm`，对外由 Kong L4 TCP 转发；GCE VM 必须加入 Tailscale 后访问 `100.105.130.0:6379`。

在未提交的 `.env` 中配置：
```env
REDIS_HOST=100.105.130.0
REDIS_PORT=6379
REDIS_PASSWORD=load-from-private-env
```

部署前用 `AUTH + PING` 验证连接；不得在文档、日志或 Git 提交中记录 Redis 密码。

### Step 2: 在 `config.yaml` 中配置 Redis 速率限制与缓存
```yaml
router_settings:
  redis_host: os.environ/REDIS_HOST
  redis_port: os.environ/REDIS_PORT
  enable_caching: true

litellm_settings:
  cache_type: "redis"
  redis_host: os.environ/REDIS_HOST
  redis_port: os.environ/REDIS_PORT
  redis_password: os.environ/REDIS_PASSWORD
  cache_params:
    supported_call_types: ["chat_completion"]
    ttl: 3600 # 缓存默认过期时间 1 小时

# 限流设置示例
user_keys:
  - api_key: "sk-test-client"
    max_budget: 10.0 # 最高 10 美元预算
    rpm_limit: 10    # 每分钟最多 10 次请求
    tpm_limit: 10000 # 每分钟最多 10,000 Tokens
```

---

## 3. 验收与测试方案 (Verification & Acceptance)

1. **缓存命中测试 (Cache Hit Verification)**：
   * 第一次发起请求，观察 Response 耗时（如 `1200ms`）。
   * 保持 Prompt 完全相同，立刻发起第二次请求，观察 Response 耗时（应为 `<10ms`），并查看 Header/日志确认命中了 Cache。
2. **速率限制拦截测试 (429 Rate Limit)**：
   * 使用 `ab` 或 `curl` 循环向 `sk-test-client` 连续发送 12 次请求。
   * 前 10 次请求成功返回 `200 OK`，第 11 次及后续请求必须准确被拦截并返回 `HTTP 429`：
     ```json
     {
       "error": {
         "message": "Rate limit exceeded. RPM limit: 10",
         "type": "rate_limit_error"
       }
     }
     ```

---

## 4. 风险控制与红线 (Risk Control)

* ⚠️ **敏感 Prompt 缓存清理**：为避免长久存储敏感数据，确保配置合理的 TTL（如 3600s）。
* ⚠️ **Redis 挂掉容灾 (Degradation)**：配置 Redis 连接超时时间；既有 Redis、Tailscale 或 Kong 任一不可达时，LiteLLM 需降级为“跳过缓存与本地内存限流”，绝不直接崩掉主服务。
