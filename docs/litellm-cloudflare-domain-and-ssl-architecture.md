# LiteLLM 统一网关接入自定义域名与 Cloudflare 托管架构实践：踩坑复盘与方案权衡

## 1. 背景与核心诉求

在跨云 K3s 环境中部署 LiteLLM 网关后，默认的访问入口是通过 OCI `free-arm-vm` 节点的公网 IP 加 Kong Gateway 的 NodePort：

```text
http://134.185.90.98:31850/litellm/v1/...
```

虽然该地址已经可以跑通 OpenAI 兼容的 API 调用，但在实际团队协作和多客户端接入（如 Codex、OpenCode、业务服务）时，直接使用裸 IP 和高位端口存在几个明显问题：

1. **凭据安全与合规**：明文 HTTP 传输存在中间人窃听风险，无法安全下发生产级的 Master API Key；
2. **端点可维护性**：一旦底层 VM 迁移或公网 IP 变动，所有客户端的配置文件都需要批量修改；
3. **URL 规范性**：非标准端口（`:31850`）在某些受限内网、企业代理或第三方 SDK 中可能被默认策略拦截。

为此，我们将已有的域名 `jpgcp.cloud` 委托给 Cloudflare 进行 DNS 托管，并计划为网关分配二级域名 `gw.jpgcp.cloud`。但在实际接入过程中，Cloudflare 的边缘代理机制与 Kubernetes NodePort、大模型长推理特性产生了若干预期之外的冲突。本文记录完整的踩坑排查过程、两种主流架构方案的深度对比以及当前阶段的工程选择。

---

## 2. 核心踩坑复盘：Cloudflare 代理与 LLM 网关的冲突机制

在初次配置 Cloudflare DNS 时，我们开启了默认的 **Proxied（橙色云朵 ☁️）** 模式，随后在调用时遇到了两个典型的网络阻断问题。

### 2.1 踩坑一：Cloudflare 边缘代理丢弃非标准高位端口

在开启橙色云朵的前提下，我们尝试直接请求带端口的二级域名：

```bash
curl http://gw.jpgcp.cloud:31850/litellm/health/liveliness
```

**现象**：请求无限挂起，最终报连接超时（Connection Timeout）。

#### 原理剖析：
Cloudflare 免费版的反向代理（CDN Proxy）在边缘节点上**只监听特定的标准 Web 端口**：
- HTTP：`80`, `8080`, `8880`, `2052`, `2082`, `2086`, `2095`
- HTTPS：`443`, `2053`, `2083`, `2087`, `2096`, `8443`

当外部流量发送至 `gw.jpgcp.cloud:31850` 时，TCP 握手包到达 Cloudflare Anycast 边缘节点，由于 31850 端口不在其开放监听列表内，边缘防火墙会直接将数据包丢弃（Drop），流量根本无法到达源站 OCI 节点。

---

### 2.2 踩坑二：源站端口不匹配与 HTTP 522 报错

既然无法直接带端口请求，我们转而通过标准的 HTTPS 443 端口发起请求：

```bash
curl -i https://gw.jpgcp.cloud/litellm/v1/models
```

**现象**：偶尔返回 200，但在连续调用或执行复杂请求时，频繁出现 `HTTP/2 522` 或 `error code: 522`。

```text
HTTP/2 522
server: cloudflare
error code: 522 (Connection timed out)
```

#### 原理剖析：
1. 客户端向 Cloudflare 发送 `https://gw.jpgcp.cloud`（端口 443），Cloudflare 边缘节点接受连接；
2. Cloudflare 尝试向源站 IP（`134.185.90.98`）发起回源连接，默认尝试连接源站的 **443** 或 **80** 端口；
3. 而在我们的 K3s 集群中，Kong Gateway 的实际对外承载端口是 **NodePort `31850`**。虽然 OCI 安全组放行了 80 和 443，但节点操作系统本地并没有对应的高可用原生监听进程；
4. 当源站 80 端口因网络抖动未能在规定时间内完成 TCP 三次握手时，Cloudflare 边缘即刻判定源站不可达，抛出 `Error 522`。

---

### 2.3 踩坑三：思考大模型（Thinking Models）的长推理与 100 秒超时

在测试 `gemini-3.7-flash` 或具备深度推理能力的思考模型时，模型会先在服务端生成若干轮思考 Token（Reasoning Tokens）。对于复杂提示词，从请求发送到首个 Token 返回可能耗时 40~50 秒以上。

Cloudflare 免费版在代理模式下具有以下硬性限制：
- **HTTP 连接读取超时（HTTP 524 Timeout）**：硬编码为 **100 秒**，用户无法在控制台自定义调大；
- 如果请求未开启流式（`stream: false`），且底层模型推理超过 100 秒，Cloudflare 会单方面向客户端返回 `HTTP 524 (A timeout occurred)`，直接中断客户端连接。

---

## 3. 两大架构方案深度解析与权衡

为了彻底解决上述网络与端口问题，我们在工程上评估了两种落地架构：

---

### 方案一：DNS-Only 模式（灰色云朵直连）

```
客户端 ──(DNS 查询)──► [ Cloudflare DNS ]
   │                        │
   │ ◄──(返回源站真实 IP: 134.185.90.98)
   │
   ▼ (客户端直连源站端口)
[ OCI free-arm-vm: 134.185.90.98:31850 ] ──► [ Kong Gateway ] ──► [ LiteLLM Pod ]
```

#### 实现机制：
将 Cloudflare 中的 DNS 记录设置为 **DNS Only（`proxied: false`，灰色云朵）**。Cloudflare 仅承担权威 DNS 解析职责，不参与任何 HTTP/TCP 流量转发。

客户端通过统一域名直接访问带端口的网关地址：
```text
http://gw.jpgcp.cloud:31850/litellm/v1
```

#### 优点：
1. **全端口透明直达**：彻底绕过 Cloudflare 边缘代理的端口限制，`31850`、`22` 及任意自定义端口均可正常通信；
2. **0 中间层超时截断**：彻底消除 Cloudflare 的 100 秒超时限制，模型推理时长完全由 Kong 网关配置（如 `read-timeout: 180000`）决定；
3. **极致超低延迟**：客户端与 OCI 新加坡机房直接建立 TCP/TLS 连接，没有跨国 CDN 节点的额外转发损耗；
4. **架构极简、排障链路短**：出现网络异常时，只需排查本地客户端与 OCI 节点之间，排除第三方 CDN 策略干扰。

#### 缺点：
1. **URL 带有端口号**：访问时必须显式携带 `:31850`，未能做到完全纯净的 `https://...`；
2. **暴露源站真实 IP**：DNS 直接解析出 OCI 实例的公网 IP，缺少 CDN 边缘防扫描与 DDoS 流量清洗保护；
3. **明文 HTTP 限制**：若要实现 HTTPS，需要在 Kong 层或节点层独立挂载证书，不能直接使用 Cloudflare 的边缘自动证书。

---

### 方案二：标准 443 端口接入（隐藏端口与边缘加速）

方案二旨在对外提供完全不带端口号的标准 HTTPS 入口：`https://gw.jpgcp.cloud/litellm/v1`。该方案包含两种具体实现路径：

#### 路径 2A：Cloudflare Proxied + Origin Rules（端口重写）

```
客户端 ──(HTTPS :443)──► [ Cloudflare Edge (自动 SSL) ] ──(HTTP :31850)──► [ OCI 134.185.90.98:31850 ]
```

- **实现机制**：在 Cloudflare 中保持开启橙色云朵，并在 **Rules ➔ Origin Rules** 中配置一条重定向规则：
  ```text
  When hostname equals "gw.jpgcp.cloud" -> Override destination port to "31850"
  ```
- **优点**：
  - URL 完全纯净（标准 `https://...` 无端口）；
  - 零新增云成本，利用 Cloudflare 规则实现，无需创建任何 OCI 额外资源；
  - 隐藏源站 IP，享受 Cloudflare 免费防 DDoS 与 CDN 边缘缓存；
  - SSL 证书由 Cloudflare 自动轮换，无需本地维护证书文件。
- **缺点与限制**：
  - **100 秒硬性超时**：非流式长思考请求若超过 100 秒会被 Cloudflare 强制切断（需依赖客户端强制开启 `stream: true`）；
  - 存在 20~50ms 的边缘中转握手开销。

---

#### 路径 2B：部署 OCI 原生 Load Balancer（原生 443 监听）

```
客户端 ──(HTTPS :443)──► [ OCI Load Balancer (:443) ] ──(HTTP :31850)──► [ K3s free-arm-vm:31850 ]
```

- **实现机制**：在 OCI 租户中创建一个弹性 Load Balancer（利用 Always Free 10Mbps 免费额度），在 443 端口配置 Listener，后端挂载 `free-arm-vm:31850`，并在 OCI Certificates 申请免费证书或挂载 Cloudflare 15 年 Origin 证书。
- **优点**：
  - 标准 HTTPS，无端口号；
  - 超时时间可自主配置为 1800 秒（30 分钟），完美支持任何长时间复杂推理；
  - 原生支持多节点后端负载均衡（HA）；
  - 链路直达新加坡 OCI 机房，延迟稳定。
- **缺点**：
  - 占用 OCI 租户唯一的免费 Load Balancer 实例配额；
  - 需要在云控制台维护 VCN、子网、监听器与证书链路，配置相对较重。

---

### 方案对比汇总表

| 评估维度 | 方案一：DNS-Only 直连 (`:31850`) | 方案 2A：Cloudflare Origin Rules | 方案 2B：OCI Load Balancer |
| :--- | :--- | :--- | :--- |
| **URL 形态** | `http://gw.jpgcp.cloud:31850` | `https://gw.jpgcp.cloud` | `https://gw.jpgcp.cloud` |
| **端口要求** | 需显式指定 `:31850` | **标准 443（无需端口）** | **标准 443（无需端口）** |
| **HTTPS 锁头** | 需源站自行处理 | **Cloudflare 全自动提供** | **OCI 证书 / 15年 Origin 证书** |
| **长推理超时限制** | **无限制** (Kong 自主控制 180s+) | **100 秒硬截断** (非流式易报 524) | **无限制** (支持 1800s 超长连接) |
| **源站 IP 保护** | 暴露 OCI 真实 IP | **完全隐藏** (对外仅暴露 Anycast IP) | 暴露 OCI LB 公网 IP |
| **网络开销** | 0 中转，直连最低延迟 | +20~50ms 边缘转译 | 直连机房，低延迟 |
| **云资源占用** | 0 | 0 | 占用 1 个 OCI Free LB 实例 |

---

## 4. 当前阶段的选择与工程落地

结合当前系统的运行目标（为本地开发、Codex 助手及内部工具提供高稳定、低延迟的统一大模型接入），我们在不同阶段做出如下策略选择：

### 4.1 当前选择：方案一（DNS-Only 直连）

当前 Phase 1 阶段，我们优先采用 **方案一（DNS-Only 灰色云朵）**：

1. **配置实施**：
   - 在 Cloudflare 将 `gw.jpgcp.cloud` A 记录设置为 `proxied: false`；
   - 解析直接指向 OCI 实例 IP `134.185.90.98`；
2. **客户端端点配置**（以 Codex `~/.codex/config.toml` 为例）：
   ```toml
   [model_providers.litellm]
   name = "my-litellm-gateway"
   base_url = "http://gw.jpgcp.cloud:31850/litellm/v1"
   env_key = "LITELLM_MASTER_KEY"
   wire_api = "responses"
   ```
3. **选型依据**：
   - 彻底消除了 Cloudflare 边缘代理产生的 `522 / 524` 超时风险；
   - 保证了 Gemini 3.7 Thinking 模式长时间深度推理的稳定性；
   - 规避了非标准端口在 CDN 边缘被丢包的问题。

---

### 4.2 未来演进路线

当系统进入多团队共享或正式对外提供 SaaS 服务时，系统将平滑演进至 **方案 2B（OCI Load Balancer）**：

```text
[ 客户端 ]
    │ (标准 HTTPS :443)
    ▼
[ OCI Always Free Load Balancer (:443) ] ──(挂载 15 年 Cloudflare Origin 证书)
    │ (私网 HTTP 转发)
    ▼
[ free-arm-vm:31850 (Kong Gateway) ]
    │
    ▼
[ LiteLLM Pod (:4000) ]
```

该架构能够同时实现：
- 纯净的 `https://gw.jpgcp.cloud/litellm/v1` 标准访问；
- 15 年免维护的 SSL 证书安全卸载；
- 支持 1800 秒的超长推理超时；
- 多 K3s 工作节点的高可用流量分发。

---

## 5. 验收实测与验证数据

在当前采用的方案一（DNS-Only 模式）下，执行全链路功能验证：

### 5.1 全球 DNS 解析验证
```bash
dig A gw.jpgcp.cloud @8.8.8.8 +short
# 返回: 134.185.90.98 (直接解析到 OCI 源站)
```

### 5.2 存活探针与模型列表
```bash
# 1. 探针检查
curl -s "http://gw.jpgcp.cloud:31850/litellm/health/liveliness"
# 返回: "I'm alive!"

# 2. 鉴权与模型列表
curl -s "http://gw.jpgcp.cloud:31850/litellm/v1/models" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" | jq -c '.data[].id'
# 返回: "gemini-3.6-flash-freelayer", "gemini-3.7-flash-freelayer"
```

### 5.3 真实聊天推理与 Token 统计
```bash
curl -s -X POST "http://gw.jpgcp.cloud:31850/litellm/v1/chat/completions" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.6-flash-freelayer",
    "messages": [{"role": "user", "content": "What is 100 + 200? Answer with only number."}],
    "max_tokens": 64
  }' | jq -c '{content: .choices[0].message.content, tokens: .usage.total_tokens}'
```

返回数据：
```json
{"content":"300","tokens":72}
```

### 5.4 Codex 客户端联调
在 Codex 终端执行指令，模型顺利通过 `http://gw.jpgcp.cloud:31850/litellm/v1` 完成上下文读取与代码生成，全过程耗时稳定在 1~2 秒内，未出现任何断连或重试提示。

---

## 6. 总结

在将私有大模型统一网关接入公网域名时，不能简单将 CDN 代理套用在非标准端口上。理解 CDN 边缘的端口白名单、回源超时时间（100s）以及 TLS 握手层级是保障网关稳定性的前提：

1. **NodePort 临时暴露阶段**：优先使用 **DNS-Only（灰色云朵）** 直连，避免 CDN 代理介入导致的端口阻断和 522/524 超时；
2. **大模型专属超时机制**：推理模型（如 Thinking / o1 类）对长连接要求高，网关层必须配置大于 120 秒的 upstream read timeout；
3. **标准 HTTPS 演进**：通过 OCI 托管 Load Balancer 承载 443 端口与 TLS 卸载，是兼顾纯净 URL 与超长推理连接的企业级最优解。
