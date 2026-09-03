# 踩坑复盘：LiteLLM Payload 异步落盘与 MinIO 边缘路由的双重排障实录

在将 LiteLLM 网关的长文本（Prompt / Response）冷数据外部化归档至本地 MinIO 对象存储，并通过 MySQL 视图生成公网超链接直达的过程中，系统联调阶段遇到了两个典型且隐蔽的工程问题：
1. **异步写入静默失败**：MySQL 正常落库指标，但 MinIO 存储桶中始终不见新 Request ID 目录生成；
2. **免密链接遭遇强制认证**：通过浏览器公网访问公开下载链接时，页面未返回裸 JSON 文本，而是被拦截弹出了 MinIO 控制台登录框。

本文记录这两个问题的完整排查链路、根因分析与最终落地的解决方案。

---

## 一、故障一：JSON 序列化断点引发的静默丢包

### 1. 现象复现
在通过网关发起多次真实的大模型调用后，检查 OCI MySQL 的 `llm_request_logs` 表，指标（Token 数量、USD/CNY 扣费、耗时）均已正常写入。
然而进入 MinIO Web 控制台或通过 `mc ls` 命令行查看存储桶 `litellm-payloads/2026-09-03/` 目录，发现除了最初冒烟测试的人工假数据目录外，**没有任何真实的 Request ID 文件夹被创建**。

### 2. 日志排查与定位
由于我们在 `payload_uploader.py` 中设计了严格的异常隔离机制（`try...except` 吞掉异常并记录 warning，防止存储故障阻塞用户请求），错误并没有向调用端暴露。

通过 SSH 进入 K3s 集群查看 `litellm-svc` 容器的标准错误日志：

```bash
$ sudo -n k3s kubectl logs -n llm-system deploy/litellm-svc --tail 50
```

捕获到了大量连续的 Warning 输出：
```text
Failed to async upload payload for fZeZavf2INO0g8UPi-_R8Ao to S3: 
Object of type datetime is not JSON serializable
Failed to async upload payload for VJeZatHiForGg8UPmITgcQ to S3: 
Object of type datetime is not JSON serializable
```

### 3. 根因分析
LiteLLM 的 CustomLogger Hook 传递的 `kwargs` 字典包含了极深的多层嵌套结构（如 `litellm_params.metadata.arrival_time`、`hidden_params._response_ms` 等字段）。在某些特定 Provider 插件执行时，底层直接塞入了 Python 原生的 `datetime.datetime` 实例。

在初版序列化代码中，虽然对顶层字段做了递归判断，但在执行 `json.dumps(prompt_dict)` 时，深层嵌套字典中的 `datetime` 触发了 Python 标准库的类型报错，导致整个 S3 `put_object` 协程在组装 Payload 阶段崩溃中断。

### 4. 修复方案
在 `app/core/payload_uploader.py` 中引入两层防御体系：
1. **递归类型清洗**：对 `dict`、`list`、`set`、`bytes` 以及 `datetime` 进行深层预遍历；
2. **Fallback 容错兜底器**：在 `json.dumps()` 中显式配置 `default=_json_default`，一旦遇到遗漏的不可序列化对象（如 `datetime`、`Decimal`、`UUID`、自定义 Class），统一转为 ISO 格式时间字符串或 UTF-8 安全文本。

```python
# app/core/payload_uploader.py


def _to_serializable(obj: Any) -> Any:
    """递归转换不可序列化结构"""
    if hasattr(obj, "model_dump"):
        try:
            return _to_serializable(obj.model_dump())
        except Exception:
            pass
    if hasattr(obj, "dict"):
        try:
            return _to_serializable(obj.dict())
        except Exception:
            pass
    if isinstance(obj, dict):
        return {str(k): _to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_to_serializable(item) for item in obj]
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    if isinstance(obj, Exception):
        return {"error_type": type(obj).__name__, "message": str(obj)}
    if hasattr(obj, "__str__") and not isinstance(obj, (int, float, bool, type(None))):
        return str(obj)
    return obj


def _json_default(obj: Any) -> Any:
    """json.dumps 最终保底序列化器"""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray)):
        return obj.decode("utf-8", errors="replace")
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if isinstance(obj, Exception):
        return {"error_type": type(obj).__name__, "message": str(obj)}
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    return str(obj)
```

并在组装 JSON 字节流时强制注入：
```python
prompt_bytes = json.dumps(
    prompt_dict,
    ensure_ascii=False,
    indent=2,
    default=_json_default,
).encode("utf-8")

response_bytes = json.dumps(
    response_dict,
    ensure_ascii=False,
    indent=2,
    default=_json_default,
).encode("utf-8")
```

---

## 二、故障二：MinIO 双端口流量分流失控导致的登录拦截

### 1. 现象复现
修复序列化问题并重新发版后，MinIO 桶内已能正常按 Request ID 生成对应的文件夹。
但在浏览器中直接点击生成的超链接：
`https://minio.jppwl.asia/litellm-payloads/2026-09-03/tJiZasjhJaC3g8UPrJmp2Qw/prompt.json`

页面并没有直接展示 JSON 内容，而是被重定向加载了 MinIO Console 单页面应用（SPA），并弹出了用户名/密码登录表单。

### 2. 根因分析
MinIO 架构内部存在两个职责截然不同的端口：
- **Port 9000（S3 API 传输端口）**：处理标准 S3 协议调用、数据上传与公开对象的匿名 GET 下载。对于设置了 `download` 策略的桶，访问该端口直接返回文件原始内容；
- **Port 9001（Web Console 管理后台）**：前端 SPA 管理面板，所有操作依赖 Session 会话 Cookie，未登录状态下访问任何路径均会重定向到登录页。

在初版 Gateway API HTTPRoute 配置中，我们将 `minio.jppwl.asia` 的所有流量（`path: /`）全量映射到了服务后端的 **9001 端口**。
因此，当浏览器访问 `/litellm-payloads/...` 时，请求落在了 9001 控制台端口上，控制台找不到 Session 凭证，自然触发了登录拦截。

### 3. 修复方案：基于 Gateway API 的精细化路径路由

无需新增额外的 Service 实例，直接利用 Kubernetes Gateway API 的最长路径前缀优先匹配（Longest Prefix Match）特性，在网关层完成智能分流：

1. **公开数据下载直通（高优先级）**：
   凡是以 `/litellm-payloads` 开头的请求，无缝路由至 **Port 9000**，直通 S3 数据引擎，实现免登录秒出原始 JSON；
2. **管理后台兜底（低优先级）**：
   根路径 `/` 的常规访问请求，继续路由至 **Port 9001**，享受 Logto SSO 单点登录与控制台保护。

修改 `infrastructure/minio/minio.yaml` 清单：

```yaml
---
# 规则 A：数据开放下载直通路由 (Port 9000)
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: minio-payloads-route
  namespace: minio
  labels:
    app: minio
spec:
  parentRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: kong-main-gateway
      namespace: default
  hostnames:
    - "payloads.jppwl.asia"
    - "minio.jppwl.asia"
  rules:
    - backendRefs:
        - group: ""
          kind: Service
          name: minio
          port: 9000
          weight: 1
      matches:
        - path:
            type: PathPrefix
            value: /litellm-payloads
---
# 规则 B：Web 控制台管理后台路由 (Port 9001)
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: minio-console-route
  namespace: minio
  labels:
    app: minio
spec:
  parentRefs:
    - group: gateway.networking.k8s.io
      kind: Gateway
      name: kong-main-gateway
      namespace: default
  hostnames:
    - "minio.jppwl.asia"
  rules:
    - backendRefs:
        - group: ""
          kind: Service
          name: minio
          port: 9001
          weight: 1
      matches:
        - path:
            type: PathPrefix
            value: /
```

---

## 三、最终验收与链路闭环

通过 ArgoCD 将上述配置同步至生产环境后，进行端到端验证：

1. **API 网关调用**：发起真实大模型请求，获取请求 ID `tJiZasjhJaC3g8UPrJmp2Qw`；
2. **存储落盘检查**：
   ```bash
   $ mc ls local/litellm-payloads/2026-09-03/tJiZasjhJaC3g8UPrJmp2Qw/
   [2026-09-03 15:56:37 UTC] 18KiB STANDARD prompt.json
   [2026-09-03 15:56:37 UTC]  956B STANDARD response.json
   ```
3. **公网无鉴权直达测试**：
   ```bash
   $ curl -sI https://minio.jppwl.asia/litellm-payloads/2026-09-03/tJiZasjhJaC3g8UPrJmp2Qw/prompt.json
   HTTP/2 200 
   content-type: application/json
   server: cloudflare
   ```
4. **数据库视图联动**：
   在 DBeaver / DbGate 中执行 `SELECT prompt_url FROM v_llm_request_details LIMIT 1;`，点击链接在浏览器中秒级渲染出格式化工整的 JSON 原始报文。

至此，冷热数据分离的异步写入稳定性与公网免密直达体验全面达标。
