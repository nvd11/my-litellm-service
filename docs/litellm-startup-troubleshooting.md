# LiteLLM 启动与接口测试排错记录

本文记录 `my-litellm-service` 第一次在本地启动 LiteLLM Proxy，并通过 OpenAI 兼容接口调用 Gemini 时遇到的问题、排查过程和最终解决方式。

这次排错涉及的内容比较多：Python 依赖、`uv` 环境、FastAPI 版本、Redis 网络路径、Tailscale、LiteLLM 网关认证、健康检查、模型输出 Token，以及 Gemini 的 thinking 和 429 限流。

## 1. LiteLLM Proxy 启动方式

当前项目不是通过 `python main.py` 启动 LiteLLM。LiteLLM Proxy 是第三方包提供的命令行程序，入口来自虚拟环境中的：

```text
.venv/bin/litellm
```

推荐启动命令：

```bash
cd /home/gateman/projects/github/my-litellm-service

uv run --env-file .env \
  litellm \
  --config config.yaml \
  2>&1 | tee -a /var/log/my-litellm-service/litellm.log
```

这里的 `--env-file .env` 只负责把环境变量注入 LiteLLM 进程，例如：

```env
OPENAI_API_KEY_FREE_1=...
LITELLM_MASTER_KEY=...
REDIS_HOST=...
REDIS_PASSWORD=...
```

它不会修改当前 shell 的环境变量。之后使用 `curl` 的终端仍需要单独执行：

```bash
set -a
source .env
set +a
```

否则下面的变量可能为空或仍然是旧值：

```bash
$LITELLM_MASTER_KEY
```

这是本次排错中非常关键的一点：

```text
uv --env-file .env → LiteLLM 进程
source .env       → 当前 shell 和 curl
```

## 2. 第一个问题：缺少 LiteLLM Proxy 依赖

最初的依赖声明是：

```toml
"litellm>=1.74.0,<2.0.0"
```

启动 Proxy 时出现：

```text
ModuleNotFoundError: No module named 'backoff'
```

LiteLLM 的基础包和 Proxy 所需依赖不是完全相同的集合。基础包可以用于 SDK 调用，但启动完整 Proxy 还需要额外依赖。

因此将依赖修改为：

```toml
"litellm[proxy]>=1.74.0,<2.0.0"
```

然后重新解析和同步环境：

```bash
uv lock
uv sync --dev
```

`litellm[proxy]` 会额外安装 Proxy 所需的依赖，例如 `backoff`、Proxy 运行组件、Redis 相关组件和 Web 服务组件。

## 3. 第二个问题：LiteLLM 与 FastAPI 版本不兼容

安装 Proxy extra 后，LiteLLM 可以继续启动，但出现了：

```text
ImportError: cannot import name 'get_flat_dependant'
from fastapi.dependencies.utils
```

检查实际版本：

```text
LiteLLM 1.97.0
FastAPI 0.141.1
```

LiteLLM Proxy 代码仍然导入 `get_flat_dependant`，而较新的 FastAPI 已经移除了这个接口。问题不是缺少 Python 文件，而是两个包的版本接口不兼容。

最后将 FastAPI 固定到仍然提供该接口的版本：

```toml
"fastapi>=0.136.3,<0.137.0"
```

然后重新执行：

```bash
uv lock
uv sync --dev
```

验证：

```bash
.venv/bin/python -c \
  'from fastapi.dependencies.utils import get_flat_dependant; print("compatible")'
```

LiteLLM 随后可以正常进入：

```text
Application startup complete.
Uvicorn running on http://0.0.0.0:4000
```

这里得到的经验是：使用 LiteLLM Proxy 时，不能只看 LiteLLM 自己的版本，还要检查它的 Proxy extra 对 FastAPI、Starlette 和 Uvicorn 的兼容约束。

## 4. 日志输出到指定文件

直接启动 LiteLLM 时，日志默认输出到终端。为了保存日志，使用：

```bash
2>&1 | tee -a /var/log/my-litellm-service/litellm.log
```

第一次执行时出现：

```text
/var/log/my-litellm-service/litellm.log: No such file or directory
```

原因是目标目录还不存在。先创建并授权：

```bash
sudo mkdir -p /var/log/my-litellm-service
sudo chown gateman:gateman /var/log/my-litellm-service
sudo chmod 750 /var/log/my-litellm-service
```

之后重新启动即可：

```bash
uv run --env-file .env \
  litellm --config config.yaml \
  2>&1 | tee -a /var/log/my-litellm-service/litellm.log
```

LiteLLM 是前台服务，启动命令不返回 shell 是正常现象，不是卡死。看到下面的日志就说明服务已经启动：

```text
Application startup complete.
Uvicorn running on http://0.0.0.0:4000
```

## 5. Redis 缓存配置与连接问题

当前 `config.yaml` 启用了 LiteLLM 原生 Redis Response Cache：

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

LiteLLM 会自动创建 Redis 客户端、查询缓存、写入响应和处理 TTL，不需要我们再编写一套缓存读写代码。

### 5.1 Redis 的部署位置

Redis 实际部署在 Tencent K3s 集群中的 OCI `free-arm-vm` 节点：

```text
free-arm-vm
└── Redis Pod
```

通过集群检查确认：

```text
free-arm-vm        Ready
Redis Pod          Running
Redis Service      6379
```

Redis 的实际 Tailscale 地址是：

```text
100.105.130.0
```

### 5.2 一开始使用了错误的地址

曾经把 Redis 配置成：

```env
REDIS_HOST=100.104.150.19
```

这个地址实际上是 NUC 节点，不是 Redis 所在的 OCI 节点。后来改回：

```env
REDIS_HOST=100.105.130.0
REDIS_PORT=6379
```

### 5.3 为什么本地连接一开始超时

从 Main PC 测试：

```text
100.105.130.0:6379 → timeout
```

检查路由发现，Main PC 当时没有 Tailscale 路由，把 `100.105.130.0` 当成普通局域网地址发送到家庭网关。

后来在 Main PC 安装并启用 Tailscale：

```text
tailscaled：active
开机启动：enabled
Tailscale IP：100.121.12.126
```

现在本地 LiteLLM 才具备访问 OCI Redis Tailscale 地址的网络条件。

### 5.4 Kong/KIC 与 Redis 的关系

KIC 负责将 Kubernetes 配置同步到 Kong，Kong Proxy Service 才负责实际网络转发。但部署记录中的低延迟方案不是绕经 Tencent 节点，而是：

```text
LiteLLM
  → Tailscale
  → 100.105.130.0:6379
  → Redis Pod on free-arm-vm
```

如果 LiteLLM 也部署在 K3s 集群内部，则应该使用 Redis Service DNS；如果 LiteLLM 在集群外且已加入 Tailscale，则使用 `100.105.130.0`。

Redis 不应直接暴露到公网。公网入口应该给 LiteLLM API 使用，Redis 继续走 K3s 内部网络或 Tailscale。

## 6. `Setting Cache on Proxy` 不等于 Redis 已连接

启动时看到：

```text
Setting Cache on Proxy
```

只表示 LiteLLM 正在初始化缓存功能。

如果 Redis 不可达，日志可能继续出现：

```text
Timeout connecting to server
Error connecting to Sync Redis client
```

这时可能出现：

```text
LiteLLM Proxy：启动成功
Redis 配置：已开启
Redis 连接：失败
缓存：不可用或降级
```

后来 Tailscale 配置完成后，启动日志不一定每次都打印 `Setting Cache on Proxy`，但这不表示缓存被关闭。是否开启应看 `config.yaml`，是否可用则要看 Redis 连接结果或实际缓存命中。

当前 Redis 是精确响应缓存，不是语义缓存。只有请求的模型、Prompt、消息顺序和相关参数完全一致时，才可能复用响应。语义相近但文字不同的请求不会自动命中。

## 7. LiteLLM 的两类 API Key

本项目同时使用两把不同用途的 Key：

```env
OPENAI_API_KEY_FREE_1=Gemini API Key
LITELLM_MASTER_KEY=LiteLLM 网关访问 Key
```

调用链路是：

```text
客户端
  使用 LITELLM_MASTER_KEY
      ↓
LiteLLM Proxy
  使用 OPENAI_API_KEY_FREE_1
      ↓
Gemini API
```

因此，客户端调用 LiteLLM 时必须携带：

```http
Authorization: Bearer $LITELLM_MASTER_KEY
```

不能把 Gemini API Key 直接当作客户端访问 LiteLLM 的 Key。

### 7.1 占位 Master Key 导致的错误

最初 `.env` 中虽然存在 `LITELLM_MASTER_KEY`，但它仍然是占位值：

```text
replace-with-private-master-key
```

这会导致 LiteLLM 报：

```text
Malformed API Key passed in.
```

后来生成真实的 `sk-...` Key 并写入 `.env`。修改后必须重启 LiteLLM，因为 LiteLLM 只在进程启动时读取环境变量。

### 7.2 `curl` 命令末尾多写字符

还遇到过这样的命令：

```bash
-H "Authorization: Bearer $LITELLM_MASTER_KEY"1
```

末尾的 `1` 会被拼接到 Header 值中，导致 Key 失效。正确写法是：

```bash
-H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

## 8. `/health` 和 `/v1/models` 返回 500 的原因

匿名访问：

```bash
curl http://127.0.0.1:4000/health
```

日志首先出现：

```text
No api key passed in.
```

随后 LiteLLM 的异常处理器又尝试导入可选的 Prisma 依赖：

```text
ModuleNotFoundError: No module named 'prisma'
```

最终客户端看到的是：

```json
{"type":"internal_server_error"}
```

这个 500 的首要原因不是 Redis，也不是 MySQL，而是认证失败；Prisma 错误是错误处理路径中的二次异常。

正确的调用方式是：

```bash
set -a
source .env
set +a

curl http://127.0.0.1:4000/v1/models \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

最终成功返回：

```json
{
  "data": [
    {
      "id": "gemini-3.7-flash"
    }
  ],
  "object": "list"
}
```

这里也再次证明：`uv run --env-file .env` 给 LiteLLM 加载环境变量，并不会自动给另一个终端里的 `curl` 加载环境变量。

## 9. 模型别名和真实模型名称

LiteLLM 配置中可以给模型定义别名：

```yaml
model_list:
  - model_name: gemini-3.6-flash-freelayer
    litellm_params:
      model: gemini/gemini-3.6-flash
      api_key: os.environ/OPENAI_API_KEY_FREE_1
```

客户端请求使用的是：

```text
gemini-3.6-flash-freelayer
```

真正交给 Gemini Provider 的模型是：

```text
gemini/gemini-3.6-flash
```

`freelayer` 只是项目自定义别名，不会自动让账号进入 Gemini 免费层。免费额度和限流策略由 Gemini API Key 对应的账号决定。

LiteLLM 可能在模型尚未被真正调用前就成功启动，即使底层模型名称写错，实际请求时仍可能返回模型不存在或 404。因此模型别名加载成功，不代表上游模型调用已经验证成功。

## 10. `max_tokens` 与 Gemini thinking

第一次请求使用：

```json
"max_tokens": 128
```

返回：

```text
finish_reason: length
content: 很短或不完整
```

原因是 Gemini 3.x 的 thinking/reasoning token 也会占用输出额度。后来把额度提高到：

```json
"max_tokens": 1024
```

模型正常返回：

```text
finish_reason: stop
```

实际 Token 统计类似：

```json
{
  "completion_tokens": 553,
  "reasoning_tokens": 526,
  "text_tokens": 27
}
```

这说明 `max_tokens` 不是单纯的“可见文字上限”，而是包含模型推理过程在内的输出预算。对于一句简单回答，128 可能仍然太小；1024 可以让模型有足够空间完成 thinking 和正文。

响应中的：

```json
"thought_signatures": ["..."]
```

是 Gemini Provider 的思考签名元数据，不是乱码。客户端通常只需要读取：

```bash
curl ... | jq -r '.choices[0].message.content'
```

## 11. LiteLLM 的模型成本警告

启动时还出现过：

```text
model=... not in built-in cost map
cache cost fields will default to 0
```

这表示当前 LiteLLM 内置价格表没有识别某个内部模型标识。它影响的是缓存成本统计，不影响：

- Proxy 启动
- Gemini 请求
- Redis 连接
- OpenAI 兼容响应

如果以后需要精确统计缓存成本，可以补充模型价格信息；当前阶段可以先忽略这条警告。

## 12. 最终验证命令

### 12.1 查看模型列表

```bash
set -a
source .env
set +a

curl http://127.0.0.1:4000/v1/models \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

### 12.2 调用 OpenAI 兼容聊天接口

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.6-flash-freelayer",
    "messages": [
      {"role": "user", "content": "你好，请用一句话介绍你自己。"}
    ],
    "max_tokens": 1024
  }'
```

### 12.3 只显示模型正文

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.6-flash-freelayer",
    "messages": [
      {"role": "user", "content": "Reply with exactly: OK"}
    ],
    "max_tokens": 1024
  }' | jq -r '.choices[0].message.content'
```

## 13. 关于 429

`429 Too Many Requests` 与本地 LiteLLM 启动问题不同。它通常来自 Gemini 上游，常见原因包括：

- 免费层请求频率超过限制
- 项目或 API Key 配额耗尽
- 并发请求过多
- 模型本身的配额策略

如果 `gemini-3.7-flash` 经常返回 429，而 `gemini-3.6-flash` 可以成功，说明网络、LiteLLM 和认证链路未必有问题，更可能是特定模型或账号配额问题。

当前配置只有一个模型别名时，LiteLLM 没有备用模型可以切换。后续如果要做容灾，需要在 `model_list` 中声明多个模型，并配置 fallback；否则 429 会直接返回给客户端。

## 14. 当前结论

这次本地验证最终确认了以下链路：

```text
curl
  → LiteLLM Proxy :4000
  → LITELLM_MASTER_KEY 网关认证
  → gemini-3.6-flash-freelayer 模型别名
  → gemini/gemini-3.6-flash Provider
  → Gemini API
```

同时：

- LiteLLM Proxy 可以正常启动。
- `litellm[proxy]` 是运行 Proxy 所需的依赖集合。
- FastAPI 版本必须与 LiteLLM Proxy 兼容。
- Redis 部署在 OCI `free-arm-vm` 节点上，跨集群访问依赖 Tailscale。
- Redis 是精确响应缓存，不是语义缓存。
- Gemini API Key 和 LiteLLM Master Key 是两把不同的 Key。
- `--env-file` 不会自动更新另一个终端的 shell 环境。
- `/v1/models` 和聊天接口需要携带 LiteLLM Master Key。
- `prisma` 报错是认证失败后的二次异常，不是本次最初原因。
- Gemini 3.x 的 thinking 会消耗 `max_tokens` 预算。
- 429 需要单独按上游配额和限流问题处理。
