# LiteLLM Proxy 启动方式与 Python 环境管理

本文记录 `my-litellm-service` 当前项目中启动 LiteLLM Proxy 的几种方式，并解释相关的 Python 虚拟环境、`uv`、`pip` 和项目安装概念。

文中的命令默认在项目根目录执行：

```bash
cd /home/gateman/projects/github/my-litellm-service
```

当前项目采用以下结构：

```text
my-litellm-service/
├── app/                    # 后续 FastAPI Service B 的 Python 代码
├── config.yaml             # LiteLLM Proxy 模型与缓存配置
├── pyproject.toml          # 项目元数据和依赖声明
├── .env                    # 本地私密配置，不提交 Git
├── .env.example            # 环境变量模板
└── .venv/                  # 项目虚拟环境
```

## 1. LiteLLM Proxy 和 `main.py` 不是一回事

LiteLLM Proxy 是第三方 Python 包提供的网关程序。它不是当前项目里的 `app/main.py`，因此启动 LiteLLM 使用的是它安装后提供的命令行入口：

```bash
litellm --config config.yaml --port 4000
```

当前项目未来还会有另一个 FastAPI 服务。两个服务的启动方式不同：

```text
Service A：LiteLLM Proxy
启动命令：litellm --config config.yaml --port 4000
职责：模型路由、API 兼容、缓存和故障切换

Service B：FastAPI
启动命令：uvicorn app.main:app --port 8000
职责：评测接口、费用统计和业务 API
```

因此，直接执行下面的命令并不适用于当前 LiteLLM Proxy：

```bash
python main.py
```

除非项目确实存在 `main.py`，并且该文件实现了自己的启动逻辑。

## 2. 为什么优先使用 `uv run`

当前项目使用 `uv` 管理虚拟环境、依赖解析和锁文件。推荐的启动方式是：

```bash
uv run litellm --config config.yaml --port 4000
```

`uv run` 会使用项目对应的虚拟环境执行命令。它的好处是：

- 使用项目自己的 `.venv`
- 使用 `pyproject.toml` 中声明的依赖
- 不需要手动执行 `source .venv/bin/activate`
- 避免误用系统 Python
- 依赖已经由 `uv sync` 管理时，命令更容易复现

如果端口放在 `.env` 中，可以这样启动：

```bash
set -a
source .env
set +a

uv run litellm --config config.yaml --port "$LITELLM_PORT"
```

当前项目的端口配置为：

```env
LITELLM_PORT=4000
```

不建议同时在 `.env`、`config.yaml` 和启动参数中维护不同的端口值。当前项目采用 `.env` 保存端口，再由启动命令通过 `--port` 传给 LiteLLM。

## 3. 不使用 `uv` 时的启动方式

`uv` 不是 LiteLLM 的运行时要求。只要使用普通 Python 虚拟环境并安装依赖，也可以启动 LiteLLM。

### 3.1 激活项目虚拟环境后启动

```bash
source .venv/bin/activate

litellm --config config.yaml --port 4000
```

激活后，当前 shell 中的 `python`、`pip` 和 `litellm` 通常都会优先指向：

```text
/home/gateman/projects/github/my-litellm-service/.venv/bin/
```

可以使用下面的命令确认：

```bash
which python
which pip
which litellm
```

正确情况下，它们应当指向项目的 `.venv/bin` 目录，而不是 `/usr/bin` 或系统其他 Python 路径。

### 3.2 不激活虚拟环境，直接使用完整路径

也可以不修改当前 shell 的 PATH，直接执行虚拟环境里的命令：

```bash
.venv/bin/litellm --config config.yaml --port 4000
```

这种方式适合脚本、服务管理器和自动化任务。

### 3.3 创建虚拟环境并使用 pip

如果还没有 `.venv`，可以使用系统中的 Python 3.12 创建：

```bash
python3.12 -m venv .venv
source .venv/bin/activate
```

然后安装项目运行依赖：

```bash
python -m pip install -e .
```

安装完成后启动：

```bash
litellm --config config.yaml --port 4000
```

这里使用 `python -m pip` 比直接使用 `pip` 更稳妥，因为它明确表示：使用当前这个 `python` 对应的 pip。

## 4. `pip install` 到底安装了什么

很多人把 `pip install` 只理解成“从远程下载 wheel”。这只是其中一种情况。

例如：

```bash
pip install litellm
```

这会从包索引下载 LiteLLM 及其依赖，通常优先使用 wheel，安装到当前 Python 环境。

而下面的命令：

```bash
pip install .
```

其中的 `.` 表示当前目录。pip 会读取当前项目的 `pyproject.toml`，然后：

1. 安装当前项目
2. 读取项目的运行依赖
3. 安装包括 LiteLLM 在内的第三方依赖
4. 安装依赖包提供的命令行入口

当前项目的运行依赖中包含：

```toml
dependencies = [
    "aiomysql>=0.2.0,<1.0.0",
    "fastapi>=0.115.0,<1.0.0",
    "httpx>=0.27.0,<1.0.0",
    "litellm>=1.74.0,<2.0.0",
    "pydantic-settings>=2.6.0,<3.0.0",
    "redis[hiredis]>=5.2.0,<7.0.0",
    "uvicorn[standard]>=0.32.0,<1.0.0",
]
```

所以执行 `pip install .` 后，可以使用：

```bash
litellm --config config.yaml --port 4000
```

并不是因为当前项目有一个叫 `litellm` 的 Python 文件，而是因为 LiteLLM 包安装时注册了一个命令行入口。pip 会把这个入口生成到虚拟环境的 `bin` 目录中：

```text
.venv/bin/litellm
```

## 5. `pip install -e .` 中的 `-e` 是什么

`-e` 是 `--editable` 的缩写，表示“可编辑安装”：

```bash
pip install -e .
```

它仍然会安装当前项目声明的第三方依赖，但当前项目本身不会被当作一份固定副本复制到虚拟环境中。虚拟环境会记录当前项目源代码的位置。

可以简单理解为：

```text
pip install .    = 安装当前项目的普通版本
pip install -e . = 登记当前项目的源代码位置
```

开发时使用 editable 安装的好处是，修改源代码后立即生效：

```python
from app.core.config import Settings
```

不需要每次修改 `app/core/config.py` 后重新执行安装命令。

普通安装和可编辑安装的区别：

| 命令 | 当前项目代码 | 修改代码后 | 典型用途 |
|---|---|---|---|
| `pip install .` | 安装普通构建版本 | 通常需要重新安装 | 更接近发布或生产安装 |
| `pip install -e .` | 直接使用源代码目录 | 立即生效 | 本地开发 |

需要注意，`pip install -e .` 不会自动选择虚拟环境。执行前仍然必须先激活 `.venv`，或者直接使用：

```bash
.venv/bin/python -m pip install -e .
```

否则，直接使用系统的 `pip` 可能会把依赖安装到系统 Python 环境。

## 6. 不安装项目也能 import，为什么还要安装项目

在项目根目录执行：

```bash
python -c "from app.core.config import Settings; print(Settings)"
```

通常不需要 `pip install -e .` 也能找到 `app/core/config.py`，因为 Python 会把当前工作目录加入模块搜索路径。

但这只说明 Python 找到了项目源码，不代表项目依赖已经安装，也不代表从其他目录运行时仍然能找到该项目。

可以区分为三件事：

```text
源代码存在             -> Python 可能找到 app.core.config
第三方依赖已安装       -> 文件内部的 import 才能正常执行
pip install -e .        -> 项目被注册到虚拟环境，便于从其他目录使用
```

例如，如果从 `/tmp` 运行：

```bash
cd /tmp
python -c "from app.core.config import Settings"
```

没有安装项目，也没有设置 `PYTHONPATH` 时，Python 通常找不到 `app`。

不过，项目安装和 Python 环境选择仍是两个独立问题。即使执行过 `pip install -e .`，从其他目录直接输入：

```bash
python script.py
```

也不代表会自动使用项目 `.venv`。如果当前 shell 没有激活虚拟环境，这个 `python` 仍可能是系统 Python。

从其他目录安全运行项目，可以使用：

```bash
source /home/gateman/projects/github/my-litellm-service/.venv/bin/activate
python script.py
```

或者直接指定解释器：

```bash
/home/gateman/projects/github/my-litellm-service/.venv/bin/python script.py
```

也可以使用 `uv` 明确指定项目：

```bash
uv run --project /home/gateman/projects/github/my-litellm-service python script.py
```

## 7. 开发依赖：`dependency-groups` 和 `.[dev]`

当前项目的开发依赖定义为：

```toml
[dependency-groups]
dev = [
    "pytest>=8.3.0,<9.0.0",
    "pytest-asyncio>=0.24.0,<2.0.0",
    "pyyaml>=6.0.0,<7.0.0",
    "ruff>=0.8.0,<1.0.0",
]
```

使用 uv 时，可以直接安装运行依赖和开发依赖：

```bash
uv sync --dev
```

而下面这条命令：

```bash
pip install -e ".[dev]"
```

使用的是 pip 的 extras 语法。它要求项目在 `pyproject.toml` 中定义：

```toml
[project.optional-dependencies]
dev = [
    "pytest",
    "pytest-asyncio",
    "pyyaml",
    "ruff",
]
```

当前项目使用的是 `[dependency-groups]`，不是 `[project.optional-dependencies]`。因此，当前项目不应把：

```bash
pip install -e ".[dev]"
```

当作开发依赖安装命令。

不用 uv 时，当前项目可以这样安装：

```bash
python -m pip install -e .
python -m pip install pytest pytest-asyncio pyyaml ruff
```

概念上，两者都表示“安装开发依赖”，但配置格式和执行工具不同：

| 配置位置 | 推荐命令 |
|---|---|
| `[dependency-groups].dev` | `uv sync --dev` |
| `[project.optional-dependencies].dev` | `pip install -e ".[dev]"` |

## 8. 本项目的 Gemini 配置和代理

当前 LiteLLM 使用 Google Gemini API，而不是 Vertex AI：

```yaml
model_list:
  - model_name: gemini-3.7-flash
    litellm_params:
      model: gemini/gemini-3.7-flash
      api_key: os.environ/OPENAI_API_KEY_FREE_1
```

API Key 变量名虽然叫 `OPENAI_API_KEY_FREE_1`，但变量值应当是 Gemini API Key。变量名只是项目内部约定，不会改变 LiteLLM 使用的 provider；真正决定 provider 的是：

```yaml
model: gemini/gemini-3.7-flash
```

本地通过 Moon 代理访问 Gemini 时，`.env` 中配置：

```env
HTTP_PROXY=http://10.0.1.105:7890
HTTPS_PROXY=http://10.0.1.105:7890
NO_PROXY=localhost,127.0.0.1,10.0.0.0/8,100.105.130.0
```

`10.0.0.0/8` 表示所有 `10.*.*.*` 地址不走代理。LiteLLM 不需要配置 `api_base`，因为这里使用的是普通 HTTP/HTTPS 网络代理，不是 Gemini API 中转站。

启动前可以把 `.env` 导出到当前 shell：

```bash
set -a
source .env
set +a
```

## 9. 启动 LiteLLM Proxy

### 方案 A：使用 uv，推荐本地开发方式

```bash
cd /home/gateman/projects/github/my-litellm-service

set -a
source .env
set +a

uv run litellm \
  --config config.yaml \
  --port "$LITELLM_PORT"
```

### 方案 B：激活虚拟环境后直接运行

```bash
source .venv/bin/activate
set -a
source .env
set +a

litellm --config config.yaml --port "$LITELLM_PORT"
```

### 方案 C：不激活虚拟环境，直接调用入口文件

```bash
set -a
source .env
set +a

.venv/bin/litellm \
  --config config.yaml \
  --port "$LITELLM_PORT"
```

### 方案 D：从其他目录使用 uv

```bash
uv run \
  --project /home/gateman/projects/github/my-litellm-service \
  litellm \
  --config /home/gateman/projects/github/my-litellm-service/config.yaml \
  --port 4000
```

如果从其他目录启动，配置文件路径也应使用绝对路径，或者先切换到项目根目录。

## 10. 验证 OpenAI 兼容 API

LiteLLM 启动后，先验证不调用上游模型的接口。

### 10.1 健康检查

```bash
curl http://127.0.0.1:4000/health
```

### 10.2 查看模型列表

```bash
curl http://127.0.0.1:4000/v1/models \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY"
```

这两个请求不会发送 Gemini 推理请求，通常不会产生模型调用费用。

### 10.3 使用项目烟囱脚本

```bash
uv run python -m scripts.smoke_proxy \
  --base-url http://127.0.0.1:4000
```

如果要真实调用一次 Gemini：

```bash
uv run python -m scripts.smoke_proxy \
  --base-url http://127.0.0.1:4000 \
  --model gemini-3.7-flash \
  --send-chat
```

`--send-chat` 会产生真实的上游模型请求，应当显式执行。

### 10.4 直接使用 curl 调用 OpenAI 格式接口

```bash
curl http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.7-flash",
    "messages": [
      {
        "role": "user",
        "content": "Reply with exactly: OK"
      }
    ],
    "max_tokens": 128
  }'
```

这里使用的是 LiteLLM 对外暴露的模型别名：

```text
gemini-3.7-flash
```

不是底层 provider 名称：

```text
gemini/gemini-3.7-flash
```

后者只写在 LiteLLM 的 `litellm_params.model` 中。

## 11. 真实 Gemini pytest

项目中的真实连接测试为：

```text
tests/test_real_gemini_conn.py
```

为了防止普通测试意外产生费用，测试默认跳过。显式执行：

```bash
set -a
source .env
set +a

RUN_REAL_GEMINI_TESTS=1 \
uv run pytest -q -s \
  tests/test_real_gemini_conn.py::test_real_gemini_chat_completion
```

测试会：

- 从 `OPENAI_API_KEY_FREE_1` 读取 Gemini API Key
- 使用 `gemini/gemini-3.7-flash`
- 继承当前 shell 中的 HTTP/HTTPS 代理
- 验证响应中存在 choices、model 和 usage
- 打印响应内容、模型名和 usage
- 捕捉 HTTP 429 和 5xx 错误并转换为清晰的 pytest 失败信息

测试中的 `max_tokens` 是输出 Token 上限，不是输入上下文上限。Gemini 模型的上下文窗口和单次输出上限是不同概念；测试中是否设置 `max_tokens`，只影响本次请求允许生成的输出额度。

测试中的 `timeout` 是客户端等待上限，不是代理地址。即使不设置，LiteLLM 也可能使用自己的默认超时时间；但保留一个明确的超时值可以防止代理不可达时测试长时间等待。

## 12. 常见问题

### 12.1 `litellm: command not found`

通常是虚拟环境没有激活，或者 LiteLLM 没有安装到当前环境。检查：

```bash
which python
python -m pip show litellm
```

也可以直接使用：

```bash
.venv/bin/litellm --config config.yaml --port 4000
```

### 12.2 代码能 import，但 LiteLLM 不能启动

能够执行：

```python
from app.core.config import Settings
```

只说明 Python 找到了项目源码。LiteLLM 能否启动还取决于 LiteLLM 包及其依赖是否安装。

### 12.3 测试长时间没有结果

常见原因包括：

- 当前进程没有继承 `.env` 中的代理变量
- 代理地址或端口不可达
- 代理不支持 HTTPS CONNECT
- 上游 Gemini API 暂时没有响应
- 测试没有设置客户端超时

终端执行测试时，先加载 `.env`：

```bash
set -a
source .env
set +a
```

IDE 的 pytest runner 不一定自动加载 `.env`，需要在 IDE 的测试环境配置中显式指定环境文件，或者直接使用终端运行。

### 12.4 为什么 `max_tokens=64` 可能没有正文

Gemini 3 系列可能使用内部 thinking。`max_tokens` 过小时，输出预算可能被 thinking 消耗，最终出现：

```text
finish_reason=length
content=None
```

对于只验证连通性的测试，应重点检查响应结构；对于验证正文的测试，应提供足够的输出额度，并根据模型的 thinking 行为设计断言。

## 13. 推荐选择

当前项目的本地开发推荐使用：

```bash
cd /home/gateman/projects/github/my-litellm-service

set -a
source .env
set +a

uv run litellm --config config.yaml --port "$LITELLM_PORT"
```

不使用 uv 时，推荐使用：

```bash
source .venv/bin/activate
litellm --config config.yaml --port "$LITELLM_PORT"
```

如果需要脚本化或部署到服务管理器，则使用虚拟环境的绝对路径：

```bash
.venv/bin/litellm --config config.yaml --port 4000
```

无论采用哪种方式，都应确保：

1. LiteLLM 使用的是项目虚拟环境。
2. `.env` 中的敏感配置没有提交到 Git。
3. Gemini 请求需要代理时，当前进程确实继承了代理变量。
4. `/health` 和 `/v1/models` 先通过，再发送真实聊天请求。
5. 真实 API 调用和真实 pytest 都是显式执行的，以控制费用。
