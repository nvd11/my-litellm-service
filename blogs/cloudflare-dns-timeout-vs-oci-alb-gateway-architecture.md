# 攻坚实录：大模型网关的域名、端口与超时难题——从 Cloudflare 100秒限制与端口折中，到 OCI 免费 L7 云负载均衡器的终极破局

在构建和运营企业级大模型 API 网关（LiteLLM Gateway）的过程中，我们不仅需要关注模型路由策略、异步财务审计和缓存优化，**公网接入层的网络拓扑设计同样充满陷阱**。

在实际生产环境中，大模型长上下文推理（尤其是思维链模型如 Gemini 3.7 / 3.8 Flash Thinking 模式）与传统 Web API 存在本质差异：**单个请求的思考时间很容易突破数十秒甚至数分钟**。

当我们尝试将部署在自建 K3s 集群上的 LiteLLM 网关通过 **Cloudflare 域名** 暴露给开发助手（Codex、OpenCode、Claude Code）及团队成员时，先后遭遇了 **Cloudflare 边缘端口拦截、100 秒强制超时断流（HTTP 524）、灰云与黄云的端口映射困境，以及明文传输安全妥协** 等一系列深水区难题。

本文完整记录这一过程中的故障溯源、架构权衡演进，以及最终利用 **甲骨文云（OCI）Always Free Layer 7 弹性应用负载均衡器（ALB）** 实现免端口号、超长超时支持与安全直连的终极破局方案。

---

## 一、架构背景与初始暴露方式

我们的 LiteLLM 网关部署在甲骨文云（OCI 新加坡机房）的 ARM64 虚拟机（`free-arm-vm`）上，依托轻量级多云 Kubernetes（K3s）集群运行。

初始的网络暴露拓扑如下图所示：

```mermaid
flowchart TD
    Client["开发者终端 / Coding Agent<br/>(Codex / OpenCode / Claude Code)"]
    
    subgraph Host_OCI["OCI 宿主机: free-arm-vm (公网 IP: 134.185.90.98)"]
        NodePort["Linux 内核 NodePort 监听<br/>TCP :31850"]
        
        subgraph K3s_Cluster["K3s Kubernetes 集群 (llm-system)"]
            Kong["Kong Ingress Gateway<br/>(内部端口 :8000)"]
            LiteLLM["LiteLLM Gateway Pod<br/>(Port :4000)"]
        end
    end
    
    Upstream["上游大模型官方 API<br/>(Google Gemini / A6 / 元衡)"]

    Client -->|"1. 显式指定高位端口<br/>HTTP :31850"| NodePort
    NodePort -->|"2. kube-proxy 转发"| Kong
    Kong -->|"3. strip-path 去前缀"| LiteLLM
    LiteLLM -->|"4. 转发推理请求"| Upstream
```

在这个阶段，外部客户端必须显式携带高位端口访问：
```text
http://134.185.90.98:31850/litellm/v1/chat/completions
```

为了提供更易记、标准化的团队访问入口，我们把域名 `jpgcp.cloud` 托管到了 Cloudflare，并规划分配子域名 `gw.jpgcp.cloud` 作为统一网关入口。

然而，在接入 Cloudflare 的第一时间，系统便遭遇了连续的网络风暴。

---

## 二、遭遇的 Cloudflare 核心暗坑与深度排障

在将域名接入 Cloudflare 后，我们在不同配置模式下遭遇了两种截然不同的典型故障，其故障流向与阻断节点如下所示：

```mermaid
flowchart TD
    Client["客户端发起请求"]
    
    subgraph Mode1["尝试 A：开启小黄云 ☁️ (Proxied: true)"]
        CF_Edge["Cloudflare 边缘 CDN 节点"]
        DropPort["❌ 故障 1：非标端口 31850<br/>边缘白名单直接丢包 (Timeout)"]
        Timeout524["❌ 故障 2：长文本深度思考 > 100s<br/>边缘单方面强行切断 (HTTP 524)"]
    end
    
    subgraph Mode2["尝试 B：开启灰云 🔘 (DNS-Only) 尝试连 443"]
        CF_DNS["Cloudflare 纯 DNS 查号台"]
        Refused["❌ 故障 3：直连宿主机 443 端口<br/>宿主机无监听+无证书 (ERR_CONNECTION_REFUSED)"]
    end

    Client -->|"gw.jpgcp.cloud:31850"| CF_Edge
    CF_Edge -.-> DropPort
    
    Client -->|"https://gw.jpgcp.cloud:443"| CF_Edge
    CF_Edge -.-> Timeout524
    
    Client -->|"https://gw.jpgcp.cloud:443"| CF_DNS
    CF_DNS -->|"返回 IP 134.185.90.98"| Refused
```

### 1. 暗坑一：小黄云（Proxied）下非标准高位端口直接被静默丢包

在 Cloudflare 控制台添加 `gw.jpgcp.cloud` 的 A 记录时，按照常规习惯默认开启了 **Proxied（小黄云 ☁️ 模式）**。

随后我们尝试通过子域名与原有 NodePort 建立连接：
```bash
curl http://gw.jpgcp.cloud:31850/litellm/health/liveliness
```

**故障现象**：请求陷入漫长无响应的挂起，最终以 `Connection Timeout` 失败，数据包连源站的网卡都没能触达。

#### 🔍 根因分析：Cloudflare 边缘端口白名单机制
Cloudflare 免费层 CDN/反向代理**仅在特定的标准 Web 端口上监听入站流量**：
- **HTTP 白名单端口**：`80`, `8080`, `8880`, `2052`, `2082`, `2086`, `2095`
- **HTTPS 白名单端口**：`443`, `2053`, `2083`, `2087`, `2096`, `8443`

当外部客户端带着 `:31850` 端口向 Cloudflare Anycast IP 发起 TCP 握手时，Cloudflare 边缘防火墙因该端口未开放监听，直接在边缘丢弃了 TCP SYN 报文，流量根本无法回源到我们的 OCI 节点。

---

### 2. 暗坑二：小黄云下长推理模型遭遇 100 秒强制限时杀手（HTTP 524）

既然高端口被拦截，我们尝试将前端请求转为标准 443 端口，让 Cloudflare 终结 SSL 并回源到 OCI 虚拟机。

在日常短文本对话时工作正常，但在进行大型工程扫描、Prompt 重构，或者调用 **Gemini 3.7 / 3.8 Flash 的 Thinking（深度思考）模式** 时，灾难再次发生：

```text
HTTP/2 524
server: cloudflare
error code: 524 (A timeout occurred)
```

Coding Agent 终端（如 Codex / OpenCode）收到 524 后，判定连接非正常断开，立刻触发阶梯式重试退避（`Reconnecting... 1/5`），陷入无限重连循环。

#### 🔍 根因分析：Cloudflare 免费版 100 秒硬编码读超时
- Cloudflare 免费套餐的反向代理拥有硬编码的 **100 秒 HTTP 读超时（HTTP 524 Timeout）**，且无法在免费版控制台修改或延长；
- 大模型推理时，服务器必须在将全部思维链或首批 Token 生成完成后才能回传数据流。当提示词达到数万 Token 或逻辑极其复杂时，**首字延迟（TTFT）很容易逼近或超过 100 秒**；
- 一旦回源耗时超过 100 秒，Cloudflare 边缘代理会单方面主动切断客户端 TCP 连接，并给客户端吐出 524 错误页。

---

### 3. 暗坑三：灰云模式下无法直接做“443 到 31850”端口映射

既然小黄云存在 100 秒的致命硬限制，我们能否把域名切换为 **DNS-Only（灰云 🔘 模式）**，并在 443 端口上访问？

答案是：**不能！灰云模式下直连 443 会瞬间报错 `ERR_CONNECTION_REFUSED`**。

#### 🔍 根因分析：DNS 协议层与应用层反向代理的物理隔离
很多开发者容易产生直觉误区：“为什么灰云不能帮我把公网 443 转到虚拟机的 31850？”

因为 **DNS 协议（Layer 3/4 寻址）** 仅仅是一个“查号台”：
1. 灰云模式下，Cloudflare 仅向客户端返回 IP `134.185.90.98`，随后彻底退出连接；
2. 客户端发起 `https://gw.jpgcp.cloud` 时，操作系统网络栈会硬编码向 `134.185.90.98:443` 发起连接；
3. **在源站虚拟机上，Kong 网关是以 NodePort `31850` 运行的，宿主机的 443 端口根本没有进程监听，也没有证书服务**。内核收到 SYN 包后因端口无监听直接回复 `TCP RST`，连接立即被拒。

**结论**：灰云是客户端端到端直连，**DNS 协议绝对不可能在半路修改客户端的目标端口号**。

---

## 三、早期的架构权衡与折中方案（灰云 + 31850）

为了优先保证大模型长推理**绝对不断流、不被 CDN 截断**，我们在早期阶段做出了明确的折中取舍：**全面启用「灰云直连（DNS-Only）+ 显式带 31850 端口」**。

早期折中方案的数据流向与权衡拓扑如下：

```mermaid
flowchart LR
    Client["开发者客户端 / Codex"]
    CF_DNS["Cloudflare DNS (灰云 🔘)<br/>仅返回源站真实 IP: 134.185.90.98"]
    
    subgraph Host_OCI["OCI 宿主机 (134.185.90.98)"]
        NodePort["NodePort 监听 :31850<br/>(Kong Gateway)"]
        LiteLLM["LiteLLM Pod (:4000)"]
    end

    Client -. "1. 仅查 IP 耗时 5ms" .-> CF_DNS
    Client ===>|"2. 端到端纯 TCP 直连 (明文 HTTP)<br/>http://gw.jpgcp.cloud:31850/litellm/v1"| NodePort
    NodePort -->|"3. 转发流量"| LiteLLM
    
    style Client fill:#f9f,stroke:#333,stroke-width:1px
    style NodePort fill:#bbf,stroke:#333,stroke-width:1px
    style LiteLLM fill:#dfd,stroke:#333,stroke-width:1px
```

### 早期折中方案的收益与代价：

| 评估维度 | 灰云 + 31850 早期折中模式 |
| :--- | :--- |
| **推理超时掌控权** | **100% 由源站掌控**（Kong Ingress 设定 `konghq.com/read-timeout: 600000` 整整 10 分钟） |
| **CDN 100s 截断风险** | **🛡️ 彻底消除（零 524 报错）** |
| **高端口通行性** | **畅行无阻**（直连 OCI 宿主机，不经过 CF 端口过滤） |
| **遗留代价 1：URL 冗余** | 客户端必须显式输入 `:31850`，不符合标准 Web 体验 |
| **遗留代价 2：明文传输** | 走的是纯 `http://` 协议，HTTP Header 中的 Virtual Key 在公网上处于明文状态 |

虽然这一方案保障了 Agent 工具长达数周的高可用运行，但明文传输与非标端口始终是系统架构中亟待填平的安全与体验短板。

---

## 四、终极破局：引入 OCI Always Free Layer 7 弹性负载均衡器（ALB）

如何既能保持**“灰云直连的超长不超时（无 100s 限制）”**，又能兼具**“标准端口免带 `:31850` 后缀”**与**“全链路安全”**？

答案是：**在源站前置引入一台真正的独立 Layer 7 应用负载均衡器**。

经过资产盘点，甲骨文云（OCI）在每个租户的 **Always Free（永久免费）** 额度中，包含了 **1 个 10 Mbps Flexible Application Load Balancer（应用型 LB）**，且此前处于完全闲置状态。

---

### 1. 终极架构拓扑设计
                                                                                                                        
```mermaid
flowchart TD
    Client["开发者 / Agent / 同事客户端<br/>(Jayden / Codex / OpenCode)"]
    
    subgraph DNS_Layer["Cloudflare DNS 解析层"]
        CF["Cloudflare DNS (灰云 🔘 DNS-Only)<br/>gw.jpgcp.cloud ➔ A 记录 161.118.240.179"]
    end
    
    subgraph OCI_Always_Free["OCI 新加坡机房 (Always Free 永久免费云资源池)"]
        subgraph ALB_Entity["OCI 弹性应用负载均衡器: litellm-alb"]
            LB_VIP["独立公网 IPv4: 161.118.240.179<br/>10 Mbps 永久免费弹性带宽"]
            Listener80["前端监听器: HTTP :80<br/>(idle-timeout: 1800s / 30分钟超长超时!)"]
            BackendSet["后端服务器组: litellm-backend-set<br/>- 轮询策略: ROUND_ROBIN<br/>- 健康探测: GET /litellm/health/liveliness (🟢 OK)"]
        end
        
        subgraph Host_Worker["K3s ARM64 宿主机: free-arm-vm (内网 IP: 10.0.0.234)"]
            Kong["Kong Ingress Gateway<br/>NodePort :31850 (strip-path: true)"]
            LiteLLM["LiteLLM Gateway Pod (:4000)<br/>- 多模型路由 & 容灾降级<br/>- 异步财务审计 Hook"]
        end
    end

    Upstream["上游大模型官方提供商<br/>(Google Gemini / A6 API / 元衡 API)"]

    Client -. "1. 仅解析 DNS，无 100s 限制" .-> CF
    Client ===>|"2. 标准免端口直连 HTTP :80<br/>http://gw.jpgcp.cloud/litellm/v1"| LB_VIP
    LB_VIP --> Listener80
    Listener80 --> BackendSet
    BackendSet -->|"3. 云机房内网极速转发"| Kong
    Kong --> LiteLLM
    LiteLLM -->|"4. 外部 HTTPS 调用"| Upstream

    style Client fill:#f9f,stroke:#333,stroke-width:1px
    style ALB_Entity fill:#bbf,stroke:#333,stroke-width:1px
    style Host_Worker fill:#dfd,stroke:#333,stroke-width:1px
```

---

### 2. 实地落地上云与配置命令

#### 步骤一：创建 OCI 10 Mbps Always Free 弹性负载均衡器
通过 OCI CLI 创建应用型负载均衡器，严格锁定最小与最大带宽均为 `10 Mbps`，打上 Always Free 免费徽章：

```bash
oci lb load-balancer create \
  --compartment-id "<YOUR_TENANCY_OCID>" \
  --display-name "litellm-alb" \
  --shape-name "flexible" \
  --shape-details '{"maximumBandwidthInMbps": 10, "minimumBandwidthInMbps": 10}' \
  --is-private false \
  --subnet-ids '["<YOUR_REGIONAL_PUBLIC_SUBNET_OCID>"]' \
  --wait-for-state SUCCEEDED
```
*执行完成后，OCI 为其分配了独立的公网 IPv4：`161.118.240.179`。*

#### 步骤二：创建后端服务器组与智能健康检查
将后端指向 `free-arm-vm` 的内网 IP（`10.0.0.234:31850`）。由于 Kong Gateway 配置了路径剥离规则，健康探测必须指向 `/litellm/health/liveliness`：

```bash
# 1. 创建后端服务器组
oci lb backend-set create \
  --load-balancer-id "<ALB_OCID>" \
  --name "litellm-backend-set" \
  --policy "ROUND_ROBIN" \
  --health-checker-protocol "HTTP" \
  --health-checker-port 31850 \
  --health-checker-url-path "/litellm/health/liveliness" \
  --health-checker-return-code 200 \
  --wait-for-state SUCCEEDED

# 2. 挂载真实后端虚拟机节点
oci lb backend create \
  --load-balancer-id "<ALB_OCID>" \
  --backend-set-name "litellm-backend-set" \
  --ip-address "10.0.0.234" \
  --port 31850 \
  --wait-for-state SUCCEEDED
```
*检查后端健康状态：`status: OK`，内网健康探针 100% 绿灯。*

#### 步骤三：配置前端监听器与 1800 秒超长超时
创建 HTTP 标准 80 端口监听器，并将连接空闲超时（`idle-timeout`）拉满至 **1800 秒（整整 30 分钟）**：

```bash
oci lb listener create \
  --load-balancer-id "<ALB_OCID>" \
  --name "http-listener" \
  --default-backend-set-name "litellm-backend-set" \
  --port 80 \
  --protocol "HTTP" \
  --connection-configuration-idle-timeout 1800 \
  --wait-for-state SUCCEEDED
```

#### 步骤四：Cloudflare 灰云 DNS 平滑切换
通过 Cloudflare API，将 `gw.jpgcp.cloud` 的 A 记录从原虚拟机 IP 平滑切换至新 LB IP `161.118.240.179`，并保持 `proxied: false`（灰云直连）：

```bash
curl -s -X PUT "https://api.cloudflare.com/client/v4/zones/<ZONE_ID>/dns_records/<RECORD_ID>" \
  -H "Authorization: Bearer <CF_API_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "A",
    "name": "gw.jpgcp.cloud",
    "content": "161.118.240.179",
    "ttl": 1,
    "proxied": false
  }'
```

---

## 五、全链路验收与成效对比

### 1. 免端口调用验证
直接向 `gw.jpgcp.cloud` 发送标准 HTTP 请求，无需再携带 `:31850` 后缀：

```bash
# 1. 探针验证
curl -s "http://gw.jpgcp.cloud/litellm/health/liveliness"
# 回显: "I'm alive!"

# 2. 真实模型推理验证
curl -X POST "http://gw.jpgcp.cloud/litellm/v1/chat/completions" \
  -H "Authorization: Bearer sk-zwazQSl2YTCNXkagAH6mpg" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemini-3.8-flash",
    "messages": [{"role": "user", "content": "Reply with exactly: 200 OK"}],
    "max_tokens": 16
  }' | jq -r .choices[0].message.content
# 回显: "200 OK"
```

### 2. 全链路请求处理与超时控制时序图 (Sequence Diagram)

从客户端发起请求，到 OCI 负载均衡器转发、Kong 路由、LiteLLM 推理及异步财务落库的完整交互时序如下：

```mermaid
sequenceDiagram
    autonumber
    actor Client as 客户端 (Codex / Jayden)
    participant DNS as Cloudflare DNS (灰云)
    participant ALB as OCI ALB (161.118.240.179:80)
    participant Kong as Kong Ingress (NodePort :31850)
    participant LiteLLM as LiteLLM Pod (:4000)
    participant Upstream as Google Gemini 3.8 Flash
    participant MySQL as OCI MySQL HeatWave
    participant VLogs as VictoriaLogs 报文库

    Client->>DNS: 1. 解析 gw.jpgcp.cloud A 记录
    DNS-->>Client: 返回 ALB 独立 IP (161.118.240.179)
    
    Client->>ALB: 2. POST http://gw.jpgcp.cloud/litellm/v1/chat/completions (免带端口!)
    ALB->>Kong: 3. 内网转发至 free-arm-vm:31850 (TCP 长连接已就绪)
    Kong->>LiteLLM: 4. strip-path 去前缀转发至 Pod :4000
    
    Note over ALB,Upstream: 🌟 1800 秒超长超时全链路生效，深度推理永不断流
    LiteLLM->>Upstream: 5. 转发模型推理请求
    Upstream-->>LiteLLM: 6. 返回生成的 Token 响应流
    
    LiteLLM-->>Kong: 7. 返回 OpenAI 格式标准 JSON
    Kong-->>ALB: 8. 响应回传
    ALB-->>Client: 9. 客户端秒级收到 200 OK
    
    par 异步非阻塞落库
        LiteLLM-)MySQL: 10. 写入 llm_request_logs (Token/费用/耗时)
    and 异步报文时序归档
        LiteLLM-)VLogs: 11. Gzip 流式推入 Prompt/Response (ZSTD 10:1 压缩)
    end
```

### 3. 演进前后全景对比矩阵

| 对比维度 | 阶段一：小黄云 CDN 模式 | 阶段二：灰云 + 31850 模式 | 阶段三：灰云 + OCI ALB 终极模式 (当前) |
| :--- | :--- | :--- | :--- |
| **接入 URL** | `https://gw.jpgcp.cloud/litellm/v1` | `http://gw.jpgcp.cloud:31850/litellm/v1` | **`http://gw.jpgcp.cloud/litellm/v1`** |
| **端口形式** | 443 (免带端口) | **非标高位端口 (`:31850`)** | **标准 Web 端口 (完全免带端口！)** |
| **超时上限** | ⚠️ **100 秒强制限时 (HTTP 524)** | 600 秒 (Kong 控制) | **🏆 1800 秒 (30 分钟超长深度推理！)** |
| **CDN 端口丢包** | 🔴 丢包拦截 | 🟢 直连放行 | **🟢 直连放行 (无 CDN 干扰)** |
| **源站真实 IP 保护**| 🟢 完全隐藏 | 🔴 暴露主机 IP | **🟢 仅暴露独立 LB IP，宿主机完全隔离** |
| **云资源开销** | 零 | 零 | **零（100% 运行在 OCI Always Free 配额内）**|

---

## 六、总结与后续演进

在大模型基础设施工程化过程中，网络拓扑绝不能生搬硬套传统 Web 站点的 CDN 缓存逻辑：

1. **大模型流式推理的核心是长连接与低延迟**：通用 CDN 边缘的强制超时（如 100 秒）是深度思考大模型的天敌，API 流量优先推荐走**直连或可控超时的四/七层云网关**；
2. **DNS 无法解决端口映射问题**：灰云（DNS-Only）是纯 Layer 3/4 寻址，要实现公网免端口访问，必须在源站前置放置真实的负载均衡器或端口代理；
3. **充分榨干云厂商免费额度**：OCI 提供的 Always Free 10 Mbps 弹性负载均衡器（ALB）具备独立的公网 IP、健康检查与可自由配置至 30 分钟的空闲超时，是大模型自建网关的绝配底座。

### 后续加固建议（HTTPS 443 终极闭环）

目前 LB 上的 HTTP 80 端口与 1800 秒超时已平稳支撑生产流量。后续只需在 Cloudflare 控制台申请一张 15 年免费的 Origin CA 证书挂载至 OCI ALB，即可零成本一键开启 **443 HTTPS 监听器**，彻底达成“免端口 + 全链路 TLS 强加密 + 30分钟超长思考”的终极形态。

终极 HTTPS 443 目标架构如下图所示：

```mermaid
flowchart TD
    Client["开发者 / Agent / 同事客户端"]
    CF["Cloudflare DNS (灰云 🔘 DNS-Only)"]
    
    subgraph OCI_Cloud["OCI Always Free 资源池"]
        subgraph ALB_HTTPS["OCI 弹性负载均衡器: litellm-alb"]
            LB_VIP["公网 VIP: 161.118.240.179"]
            Cert["Cloudflare 15年 Origin CA 证书<br/>(挂载至 ALB 终结 SSL 绿锁)"]
            Listener443["前端监听器: HTTPS :443<br/>(idle-timeout: 1800s / 30分钟超时)"]
            BackendSet["后端服务器组<br/>(HTTP 探针健康检查 🟢 OK)"]
        end
        
        subgraph K3s_Host["K3s 宿主机: free-arm-vm (10.0.0.234)"]
            Kong["Kong Ingress 网关 (NodePort :31850)"]
            LiteLLM["LiteLLM Pod (:4000)"]
        end
    end

    Client -. "1. 仅解析 DNS" .-> CF
    Client ===>|"2. 端到端强加密连接<br/>https://gw.jpgcp.cloud/litellm/v1"| LB_VIP
    LB_VIP --- Cert
    LB_VIP --> Listener443
    Listener443 --> BackendSet
    BackendSet -->|"3. 机房内网明文解密转发"| Kong
    Kong --> LiteLLM

    style Client fill:#f9f,stroke:#333,stroke-width:1px
    style ALB_HTTPS fill:#bbf,stroke:#333,stroke-width:1px
    style K3s_Host fill:#dfd,stroke:#333,stroke-width:1px
```
