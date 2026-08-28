# 实战解析：LiteLLM 路由熔断、多阶重试与高可用降级（Fallback）架构机制

## 1. 背景与并发痛点

在大模型网关（LLM Gateway）的实际工程落地中，依赖单一模型或单一供应商 API Key 往往会面临以下核心问题：

1. **瞬时高频并发与 429 限流（Rate Limit）**：
   - 以 Google AI Studio Gemini 免费层为例，其配额限制为 **15 RPM**（在高峰期或特定模型突发阶段可能降至 **5 RPM**）。
   - 当上层使用 Coding Agent（如 Codex、OpenCode 或 Claude Code）执行“阅读项目全部代码并重构”等重度任务时，Agent 会在 10~20 秒内连续触发 10~15 轮函数调用（Tool Calling Round-trips）。
   - 单一 Key 的令牌桶（Token Bucket）会在 2~3 秒内被迅速抽干，上游直接返回 `429 Too Many Requests`，并强制要求等待 30 秒以上。
2. **流式传输中途截断（Mid-Stream Failure）**：
   - 编码助手通常采用 SSE 流式传输（`stream: true`）。如果网关缺乏快速熔断机制，在数据流吐出几个字符后上游抛出 429，就会导致连接在半路异常关闭（`stream closed before response.completed`），触发客户端长达数分钟的挂起与重连。
3. **多账号与混合付费梯队难以统筹**：
   - 团队通常拥有多个不同来源的 Key（例如：个人免费老号、高信任分的 Google AI Pro 订阅号、即将过期的测试项目号）。
   - 如何在保证 99.9% 流量享受免费的前提下，实现平摊负载、自动升舱与终极兜底，需要精细化的路由策略。

本文基于在生产 K3s 集群中运行 LiteLLM 的真实踩坑经验，系统剖析 LiteLLM 的重试、熔断与降级机制，并给出三级阶梯容灾的工程实现。

---

## 2. 核心路由参数与数学机理解析

在 LiteLLM 的 `router_settings` 中，有 3 个至关重要的参数决定了网关面对故障时的行为：

```yaml
router_settings:
  routing_strategy: "least-busy"
  num_retries: 5
  allowed_fails: 1
  cooldown_time: 60
  fallbacks:
    - gemini-3.7-flash: ["gemini-3.7-pro-plan", "gemini-3.7-backup"]
```

### 2.1 `num_retries`: 重试次数与总尝试预算（Why 5 instead of 3?）

在计算机网络和 LiteLLM 源码中，`num_retries` 表示**在第 1 次初始尝试失败后，额外允许发起的重试次数**。

整个请求允许消耗的最大尝试总数（Total Attempts）为：

$$\text{最大尝试总数} = 1 \text{ (初始调用)} + \text{num\_retries}$$

#### 为什么 4 个 Key 至少需要 `num_retries: 5`？
假设我们拥有 4 个 Key，分布在 3 个梯队（Tier 1: Key 1 + Key 2，Tier 2: Key 4，Tier 3: Key 3）：

* **若配置 `num_retries: 3`**（总尝试次数为 4 次）：
  - 尝试 1：Key 1 失败（429）；
  - 尝试 2（重试 1）：Key 2 失败（429）；
  - 尝试 3（重试 2）：Key 4 (Pro Plan) 失败；
  - 尝试 4（重试 3）：Key 3 (保底号) 尝试。
  - **风险**：重试预算卡在边缘。一旦中间任何一个 Key 发生 1 次网络握手抖动，重试预算在轮到 Key 3 之前就会被提前耗尽，导致 Key 3 连出场机会都没有就被直接向客户端报错。
* **若配置 `num_retries: 5`**（总尝试次数为 6 次）：
  - 即使 Key 1、Key 2、Key 4 相继遇到限流，重试预算依然保有 2 次以上的安全裕度，**100% 保证 Key 3 终极保底号能够稳稳接力执行**。

---

### 2.2 `allowed_fails`: 快速熔断阈值（Why 1 instead of 3?）

`allowed_fails` 定义了一个具体的 Deployment/Key **连续失败多少次后被标记为不可用（Unhealthy）并进入冷却隔离**。

* **默认值通常为 3**：如果设为 3，当 Key 1 遭遇 429 时，LiteLLM 还会尝试在 Key 1 上重试 2 次。在已知 Key 1 已经透支的情况下，这 2 次重试 100% 会继续报 429，白白消耗重试预算并增加客户端延迟；
* **优化为 `allowed_fails: 1`**：一旦 Key 1 返回 429 或 5xx，LiteLLM **在 0.01 秒内立即将 Key 1 打上冷却标记并移出可用池**，后续重试直接转交给健康的 Key 2 或备用梯队，实现真正的秒级无感避震。

---

### 2.3 `cooldown_time`: 冷却隔离周期（Why 60s instead of 30s?）

Google 免费层的限流是基于 **1 分钟（60 秒）滑动窗口** 计算的。

* **若配置 `cooldown_time: 30`（容易引发二次暴毙）**：
  - 第 0 秒：Key 1 突发透支报 429；
  - 第 30 秒：禁闭解除，Key 1 被放回可用池。但此时 Google 的 60 秒窗口才走了一半，Key 1 的令牌桶中只恢复了 2~3 个令牌；
  - 第 31 秒：Agent 发起多文件调用，Key 1 接下前 2 个请求后立即再次暴毙；
  - **后果**：Key 1 在“冷却 ➔ 刚放出 ➔ 秒挂”之间剧烈抖动（Rate Limit Flapping）。
* **优化为 `cooldown_time: 60`（满血复活）**：
  - 将故障 Key 隔离整整 60 秒，确保其完全跨越 Google 的 1 分钟滑动惩罚期；
  - 重新归队时，Key 1 的令牌桶**100% 满血回满（15 次完整额度）**，能够从容承接下一轮大任务；
  - 在 Key 1 隔离期间，多 Key 池中的其余 Key 无缝承接流量，客户端 0 感知。

---

## 3. 三级主备阶梯式容灾架构设计

为了最大化利用各账号的特性，我们设计了 **“双核日常轮换 + Pro Plan 应急升舱 + 历史项目终极保底”** 的三级阶梯架构：

```
[ 客户端请求: model="gemini-3.7-flash" ]
                   │
                   ▼
┌──────────────────────────────────────────────────────────────────┐
│ 🟢 Tier 1: 主力日常轮换池 (gemini-3.7-flash)                      │
│   ├── Key 1 (主人老号 Gmail, RPM: 15)                             │
│   └── Key 2 (师母老号 Gmail, RPM: 15)                             │
│   • 策略: least-busy 最闲优先，平摊 50/50 负载 (合并 30 RPM)      │
│   • 覆盖 99% 的日常编码与问答                                    │
└──────────────────────────────────┬───────────────────────────────┘
                                   │ (若 Key 1 和 Key 2 均报 429)
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│ 🟡 Tier 2: 一级应急升舱池 (gemini-3.7-pro-plan)                  │
│   └── Key 4 (Google AI Pro 订阅账号, 高信用分)                    │
│   • 平时 0 流量消耗                                              │
│   • 主力池被打满时触发，利用高权重账号迅速解围                   │
└──────────────────────────────────┬───────────────────────────────┘
                                   │ (若 Pro Plan 偶发异常)
                                   ▼
┌──────────────────────────────────────────────────────────────────┐
│ 🛡️ Tier 3: 终极应急保底池 (gemini-3.7-backup)                    │
│   └── Key 3 (即将回收的项目账号)                                  │
│   • 额度永久满格，作为最后一道防线                               │
│   • 即使该项目未来被彻底回收，也不影响前两级主力运行             │
└──────────────────────────────────────────────────────────────────┘
```

### 3.1 完整配置文件实现 (`config.yaml`)

```yaml
# ==============================================================================
# LiteLLM Proxy Model & Multi-Tier Routing Configuration
# ==============================================================================
# Key 角色与梯队定义说明：
# - OPENAI_API_KEY_FREE_1  : 主力老号 1 (主人) - Tier 1 日常轮换
# - OPENAI_API_KEY_FREE_2  : 主力老号 2 (师母) - Tier 1 日常轮换
# - OPENAI_API_KEY_PRO_PLAN: 主力 Google AI Pro 旗舰号 - Tier 2 一级应急升舱
# - OPENAI_API_KEY_FREE_3  : 终极应急保底号 - Tier 3 终极避震保底
# ==============================================================================

model_list:
  # === 🟢 梯队一：主力日常轮换组 (Key 1 & Key 2 平摊 50/50 负载) ===
  - model_name: gemini-3.7-flash
    litellm_params:
      model: gemini/gemini-3.6-flash
      api_key: os.environ/OPENAI_API_KEY_FREE_1
      rpm: 15

  - model_name: gemini-3.7-flash
    litellm_params:
      model: gemini/gemini-3.6-flash
      api_key: os.environ/OPENAI_API_KEY_FREE_2
      rpm: 15

  # === 🟡 梯队二：一级应急升舱组 (Pro Plan 专属，主力 429 时触发) ===
  - model_name: gemini-3.7-pro-plan
    litellm_params:
      model: gemini/gemini-3.6-flash
      api_key: os.environ/OPENAI_API_KEY_PRO_PLAN
      rpm: 15

  # === 🛡️ 梯队三：终极应急保底组 (Key 3 专属，平时 0 流量) ===
  - model_name: gemini-3.7-backup
    litellm_params:
      model: gemini/gemini-3.6-flash
      api_key: os.environ/OPENAI_API_KEY_FREE_3
      rpm: 15

router_settings:
  routing_strategy: "least-busy" # 负载均衡：优先选择当前空闲/并发压力最小的 Key 派发请求

  # 🎯 充沛重试预算：最多跨 Key / 跨梯队重试 5 次（总共 6 次尝试机会），确保覆盖所有梯队
  num_retries: 5

  # 🎯 极速熔断触发器：单个 Key 失败 1 次立即关禁闭，绝不在报错 Key 上浪费重试次数
  allowed_fails: 1

  # 🎯 满血禁闭冷却期：故障 Key 隔离 60 秒，完整错开 1 分钟滑动窗口惩罚期
  cooldown_time: 60

  fallbacks:
    # 🎯 梯队级联降级链：主力组 (FREE_1 + FREE_2) -> Pro Plan 升舱 -> Key 3 终极保底
    - gemini-3.7-flash: ["gemini-3.7-pro-plan", "gemini-3.7-backup"]

litellm_settings:
  cache: true
  cache_params:
    type: redis
    host: redis.redis.svc.cluster.local
    port: 6379
    password: os.environ/REDIS_PASSWORD
    supported_call_types: [chat_completion]
    ttl: 3600
```

---

## 4. 流式传输（Streaming）中途异常与客户端重连机制

在使用 Codex、OpenCode 等 CLI 工具时，客户端通常使用流式传输（`stream: true`）。理解网关重试与客户端重试的边界至关重要：

### 4.1 非流式 vs 流式的重试差异

1. **非流式请求（Non-Streaming）**：
   - 客户端发送请求 ➔ 网关在后台请求上游；
   - 若 Key 1 报 429，由于尚未向客户端发送任何数据包，LiteLLM 在内部自动切换到 Key 2 重试；
   - 最终返回正常的 `200 OK` JSON 包，**客户端完全无感知**。
2. **流式打字机请求（Streaming / SSE）**：
   - 连接在第 0.1 秒即已建立（HTTP 200 SSE Stream），部分头部或初始 Chunk 已发送给客户端；
   - 若模型在生成第 10 个 Token 时突然被上游掐断（抛出 `MidStreamFallbackError`），TCP 连接半路关闭；
   - 客户端（Codex）发现流在没有收到 `response.completed` 结束标记前中断，**会由客户端自身触发断线重连**：
     ```text
     • Reconnecting... 2/5 (stream closed before response.completed)
     ```

### 4.2 Codex 客户端挂起保护

当出现 `Reconnecting... waiting for network (esc to interrupt)` 时：
- 这是 Codex 为了防止用户的长输入丢失而开启的挂起保护状态；
- 在终端中按下 **`Esc` 键**（或 `Ctrl + C`）即可主动终止挂起并重新发送请求。

---

## 5. 真实压测与分流验证数据

在 Codex 客户端连续发起 3 次重度 Agent 任务（包含读取全仓库 30+ 文件）后，分析网关实际承载的数据：

1. **总请求统计**：
   - 3 次用户交互在底层共触发了 **9 次独立的 LLM Tool Calling Round-trips**。
2. **分流结果**：
   - **Key 1 (主人自用)**：承接 **5 次** 调用（55.5%）；
   - **Key 2 (师母自用)**：承接 **4 次** 调用（44.5%）；
   - **Key 4 (Pro Plan)**：0 次（主力池负载良好，未触发 Tier 2）；
   - **Key 3 (终极保底)**：0 次（满血待命中）。
3. **性能表现**：
   - 9 次调用全部在 0.8~1.2 秒内响应，成功率为 **100%**；
   - 双 Key 轮询将瞬时流速压制在安全线以下，全程 0 次 429 告警。

---

## 6. 总结

在大模型网关的建设中，高可用不能只寄希望于单点的稳定，而必须通过工程化的**流量分摊、极速熔断与多级容灾**来实现：

1. **`num_retries: 5`**：提供充足的重试预算，确保多级 Fallback 链条能被 100% 完整遍历；
2. **`allowed_fails: 1`**：单次失败即刻隔离，杜绝在已知故障节点上盲目重试；
3. **`cooldown_time: 60`**：匹配供应商的分钟级配额窗口，确保节点冷却后满血归队；
4. **主备分层调度**：日常使用主力免费池，高并发或故障时自动升舱与兜底，兼顾了成本控制与系统可用性。
