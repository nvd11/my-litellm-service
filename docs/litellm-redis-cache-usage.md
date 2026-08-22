# LiteLLM 如何使用 Redis：从部署位置到精确响应缓存

在 LiteLLM Proxy 中接入 Redis，最容易产生误解的地方有两个：

第一，Redis 并不是 LiteLLM 自己部署的，而是一个独立的基础设施服务。

第二，LiteLLM 默认提供的是精确响应缓存，不是能够理解语义的智能缓存。请求内容稍有变化，通常就会生成不同的缓存 Key。

本文记录当前项目中 LiteLLM 与 Redis 的实际使用方式，以及排查过程中确认的网络路径和边界。

## 1. Redis 在整体架构中的位置

当前项目的 Redis 部署在 Tencent K3s 集群中的 OCI `free-arm-vm` 节点上：

```text
Tencent K3s Cluster
└── OCI free-arm-vm
    └── Redis Pod
```

Redis Pod 通过 Kubernetes 的节点亲和性固定在 `free-arm-vm`，并使用 K3s 的本地存储保存数据。Redis 不是部署在 LiteLLM 进程所在的机器上，也不是部署在项目目录中。

集群中确认过的运行状态包括：

```text
Redis Pod：Running
节点：free-arm-vm
Redis Service：6379
```

Redis 的具体部署、密码、持久化、PVC、Pod 健康检查等，属于 Kubernetes 基础设施职责；LiteLLM 只作为 Redis 客户端使用它。

## 2. LiteLLM 的 Redis 配置

当前项目的 `config.yaml` 使用 LiteLLM 原生 Redis Cache：

```yaml
litellm_settings:
  cache: true
  cache_params:
    type: redis
    host: os.environ/REDIS_HOST
    port: os.environ/REDIS_PORT
    password: os.environ/REDIS_PASSWORD
    supported_call_types: [chat_completion]
    ttl: 3600
```

各项配置含义如下：

| 配置 | 含义 |
|---|---|
| `cache: true` | 开启 LiteLLM 响应缓存 |
| `type: redis` | 使用 Redis 保存缓存 |
| `host` | 从环境变量读取 Redis 地址 |
| `port` | 从环境变量读取 Redis 端口 |
| `password` | 从环境变量读取 Redis 密码 |
| `supported_call_types` | 当前只缓存聊天完成请求 |
| `ttl: 3600` | 缓存 3600 秒，即 1 小时 |

项目的私密配置放在未提交的 `.env` 中：

```env
REDIS_HOST=100.105.130.0
REDIS_PORT=6379
REDIS_PASSWORD=...
```

密码不应写进 `config.yaml`、代码、测试文件或 Git 提交记录。

## 3. LiteLLM 会自动管理 Redis 吗？

会。只要 LiteLLM Proxy 的 Redis 配置正确，并且 Redis 能够访问，LiteLLM 会自动完成客户端侧的工作：

1. 建立 Redis 连接。
2. 为请求生成缓存 Key。
3. 查询 Redis 中是否已有响应。
4. 命中时直接返回缓存结果。
5. 未命中时调用 Gemini。
6. 将上游响应写入 Redis。
7. 按 TTL 自动让缓存过期。

我们不需要为 LiteLLM 的基础响应缓存额外编写 Redis 读写代码，也不需要在 `app/` 中再实现一套缓存客户端。

但 LiteLLM 不会替我们管理 Redis 基础设施。以下事项仍由 K3s、Kubernetes Manifest 或 Redis 运维配置负责：

- 部署 Redis Pod
- 创建和挂载 PVC
- 设置 Redis 密码
- 配置 RDB/AOF 持久化
- 设置内存上限和淘汰策略
- 节点调度和故障恢复
- 备份、监控和扩缩容

因此要区分两件事：

```text
LiteLLM：自动使用 Redis
K3s/Redis：负责运行和维护 Redis
```

## 4. 缓存命中是精确匹配，不是语义匹配

当前配置使用的是普通 Redis Response Cache。它不会判断两句话的意思是否相近，也不会自动调用 Embedding 模型做向量搜索。

LiteLLM 会根据请求内容和影响结果的参数生成缓存 Key。通常需要保持一致的内容包括：

- 模型别名或模型名称
- `messages` / prompt
- system prompt
- 消息顺序
- `temperature`
- `max_tokens`
- tools
- `response_format`
- 其他影响模型输出的请求参数

例如，下面两个请求即使看起来只差一个空格，也可能产生不同的缓存 Key：

```text
请求 A：Hello
请求 B：Hello 
```

下面这些变化也通常会导致缓存未命中：

```text
model 不同
system prompt 不同
temperature 不同
max_tokens 不同
messages 顺序不同
```

因此，当前缓存可以概括为：

```text
相同请求参数 → 可能命中
语义相近请求 → 不会自动命中
```

例如：

```text
今天天气怎么样？
今天广州天气如何？
```

这两个问题语义相近，但不会因为“意思差不多”而共用缓存响应。

## 5. 精确缓存到底有什么用

对于开放式聊天，用户的每次问题通常不完全相同，精确缓存的命中率确实可能不高。但它仍然适合以下场景：

### 5.1 重复提交

用户重复点击提交按钮，或者前端因为超时进行重试时，请求内容可能完全一样。缓存可以避免再次调用 Gemini。

### 5.2 网络重试

客户端没有及时收到响应，又重新发送同一请求时，精确缓存可以避免重复消耗 Token。

### 5.3 评测任务

评测系统通常会反复执行固定的 Prompt、固定的模型和固定参数。这是精确缓存最容易获得收益的场景之一。

### 5.4 批处理和固定模板

企业系统中的分类、摘要、字段抽取等任务，往往使用固定的 system prompt 和较稳定的输入格式，也可能出现大量重复请求。

### 5.5 成本和延迟

命中缓存后：

- 不再调用 Gemini
- 不再消耗上游 Token 配额
- 不产生新的模型调用费用
- 不需要等待模型推理

因此，Redis 精确缓存的实际定位不是“理解用户意图”，而是：

```text
消除重复请求
```

## 6. 为什么不直接做语义缓存

语义缓存通常需要额外的处理链路：

```text
Prompt
  ↓
Embedding 模型
  ↓
向量存储
  ↓
相似度搜索
  ↓
超过阈值才复用响应
```

它需要额外引入：

- Embedding 模型
- 向量数据库或 Redis Vector Search
- 相似度阈值
- 向量版本管理
- 缓存失效策略
- 误命中控制

最重要的问题是：

```text
语义相似，不代表答案一定可以复用。
```

例如两个问题都询问天气，但城市、日期或上下文可能不同。相似度阈值设置过低，会返回错误答案；设置过高，又会接近精确匹配，失去语义缓存的意义。

因此当前项目先使用实现简单、行为可预测的精确缓存。等真实请求量和命中率数据稳定后，再决定是否引入语义缓存。

## 7. Redis 的访问路径

### 7.1 集群内部访问

如果 LiteLLM 也部署在 K3s 集群内，应该优先使用 Kubernetes Service DNS，而不是绕到外部 IP：

```env
REDIS_HOST=redis.redis.svc.cluster.local
REDIS_PORT=6379
```

实际 Service 名称和 namespace 以集群中的 Manifest 为准。

集群内部的 ClusterIP 类似：

```text
10.43.120.222:6379
```

这个地址只能在 K3s 集群内部使用。

### 7.2 集群外、但已加入 Tailscale 的客户端

当前部署记录采用的是直连 OCI 节点的方式。Redis 所在节点的 Tailscale 地址为：

```text
100.105.130.0
```

集群外且已加入同一个 Tailscale 网络的 LiteLLM，可以使用：

```env
REDIS_HOST=100.105.130.0
REDIS_PORT=6379
```

访问路径是：

```text
LiteLLM
  → Tailscale
  → OCI free-arm-vm: 100.105.130.0
  → Redis Service / Redis Pod
```

博客部署实测中，直连 OCI 节点是延迟最低的路径。通过 NUC 或 Tencent 控制节点转发，会增加额外的跨节点和 Overlay 网络开销。

### 7.3 Main PC 的情况

Main PC 之前无法访问 `100.105.130.0:6379`，原因不是 Redis Pod 停止，而是 Main PC 当时没有 Tailscale 路由。

后来在 Main PC 安装并启用了 Tailscale：

```text
tailscaled：active
开机启动：enabled
```

安装完成后，Main PC 需要使用同一个 Tailscale 账号完成登录：

```bash
sudo tailscale up
```

登录后才能通过：

```text
100.105.130.0:6379
```

访问 OCI 节点上的 Redis。

## 8. Kong/KIC 和 Redis 的关系

KIC（Kong Ingress Controller）负责把 Kubernetes 资源同步成 Kong 配置；Kong Proxy Service 才是真正承载网络流量的组件。

但对于当前 Redis 访问方案，最优路径并不是一定要经过 KIC/Kong，而是直接通过 Tailscale 访问 OCI `free-arm-vm` 节点。

需要区分三种地址：

```text
Redis ClusterIP
  只适合 K3s 集群内部

Kong LoadBalancer / NodePort
  由 Kong 转发 TCP 流量

OCI free-arm-vm 的 Tailscale IP
  当前跨集群直连 Redis 的推荐路径
```

Kong Service 也可能列出多个节点地址，例如：

```text
100.105.130.0
100.104.150.19
100.77.64.95
```

这些是 Tailscale/节点内部地址，不是公网 IP。通过非 Redis 所在节点访问时，可能发生额外的 Kubernetes 跨节点转发，所以部署文档推荐优先使用 `100.105.130.0`。

## 9. 不要把 Redis 6379 暴露到公网

LiteLLM API 最终可以通过公网 HTTPS 提供给没有 Tailscale 的同事：

```text
同事
  → 公网 HTTPS
  → Kong / 公网 Load Balancer
  → LiteLLM :4000
```

但 Redis 应保持私网访问：

```text
LiteLLM
  → Tailscale 或 K3s Service
  → Redis :6379
```

不建议开放：

```text
公网:6379
```

Redis 即使设置了密码，也不应直接暴露在公网。公网暴露会增加暴力破解、协议探测和配置错误带来的风险。

LiteLLM 的公网入口和 Redis 的内部入口应当分开：

```text
LiteLLM API：公网 HTTPS，供同事调用
Redis Cache：Tailscale/K3s 私网，仅供服务使用
```

## 10. Redis 连接失败时会怎样

LiteLLM 启动时会尝试初始化 Redis Cache。如果 Redis 不可达，常见日志包括：

```text
Error connecting to Sync Redis client
Timeout connecting to server
LiteLLM Redis Caching: async set() ... Timeout connecting to server
```

这通常不会阻止 LiteLLM Proxy 启动。实际状态需要分开判断：

```text
LiteLLM Proxy：可能正常启动
Redis 配置：可能已开启
Redis 连接：可能失败
缓存功能：连接失败时不可用或降级
```

因此看到：

```text
Setting Cache on Proxy
```

只代表 LiteLLM 正在初始化缓存功能，不等于 Redis 已经成功连接。还要确认后续没有连接超时日志，或者执行认证 `PING`：

```bash
set -a
source .env
set +a

uv run python -m scripts.check_phase1
```

预期结果应包含：

```text
redis      | OK       | ... | connected
```

Redis 认证检查的核心是：

```text
AUTH + PING → PONG
```

## 11. Redis 和 MySQL 的职责不同

当前 LiteLLM 配置只启用了 Redis Cache，没有启用 MySQL：

```text
Gemini：模型调用
Redis：响应缓存
MySQL：当前尚未接入 LiteLLM 费用审计
```

项目中的 MySQL 目前只用于 Phase 1 的只读连通性检查：

```sql
SELECT 1
```

后续 Phase 2 才会实现：

- 请求日志表
- Prompt/Completion Token 记录
- USD 成本记录
- LiteLLM Callback 或异步落库

Redis 不适合替代 MySQL 做长期费用审计；缓存数据可以过期或被淘汰，审计数据则需要持久化和可查询性。

## 12. Redis 目前还没有承担限流职责

Redis 很适合做分布式限流，例如：

- RPM（每分钟请求数）
- TPM（每分钟 Token 数）
- API Key 计数器
- 临时配额
- 多个 LiteLLM Pod 共享状态

但当前 `config.yaml` 只配置了 Redis Response Cache：

```yaml
supported_call_types: [chat_completion]
```

这不等于已经启用了完整的 RPM/TPM 限流。限流策略需要后续明确配置和测试，不能仅因为 Redis 已连接就认为限流已经生效。

## 13. 如何判断当前缓存是否工作

可以使用同一个模型、同一个 Prompt 和同一组请求参数连续发送两次请求：

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.7-flash",
    "messages": [
      {"role": "user", "content": "Reply with exactly: OK"}
    ],
    "max_tokens": 128
  }'
```

第二次请求可以观察：

- 延迟是否明显下降
- Gemini 请求日志是否再次出现
- LiteLLM 是否记录 cache hit
- Redis 中是否存在相应 Key

测试时必须保持请求完全一致。否则第二次请求可能只是正常的 cache miss。

## 14. 当前方案的定位

当前 Redis 方案适合：

```text
固定 Prompt
重复请求
评测任务
批处理
网络重试去重
```

它不适合直接解决：

```text
开放式聊天的语义相似问题
```

如果实际统计发现命中率很低，可以考虑关闭缓存：

```yaml
litellm_settings:
  cache: false
```

也可以后续引入语义缓存，但那将是一个独立的设计，需要评估 Embedding 成本、向量存储、阈值和误命中风险。

当前先采用精确缓存的原因很简单：行为可预测、改动小、容易验证，而且在评测和固定模板场景中确实有价值。
