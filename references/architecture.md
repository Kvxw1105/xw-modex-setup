# 接管链路技术说明

## 一、为什么必须走 DNS/CA 接管

Modex 的模型端点**在 UI 里改不了** —— 设置页没有 Base URL 输入框，只让填
模型 ID + API Key，域名写死。所以只能从**网络层**接管。

接管的作用点在 Modex 之外，**不修改它的任何字节**，因此：

- 完整性校验不受影响
- 随时可回滚（还原 hosts + 停服务）
- 对 Modex 的版本升级免疫

## 二、四层结构

```
┌─────────────────────────────────────────────────────┐
│ Modex（Electron + 后端 python）                       │
│   以为自己在请求 https://www.mhcoding.ai/v1/messages   │
└───────────────────┬─────────────────────────────────┘
                    │  ① DNS 解析
                    ▼
┌─────────────────────────────────────────────────────┐
│ hosts：127.0.0.1 www.mhcoding.ai                     │
│   把域名指向本机                                       │
└───────────────────┬─────────────────────────────────┘
                    │  ② TCP/TLS 到 127.0.0.1:443
                    ▼
┌─────────────────────────────────────────────────────┐
│ 本地拦截代理（监听 443，用自签 CA 签的证书）             │
│   - 终止 TLS（Modex 信任我们的 CA，因为已装进系统根）    │
│   - 读请求体                                          │
└───────────────────┬─────────────────────────────────┘
                    │  ③ 协议转换
                    ▼
┌─────────────────────────────────────────────────────┐
│ 协议适配：Anthropic Messages ↔ OpenAI Chat Completions │
│   - 请求：tools / tool_use / tool_result 双向映射      │
│   - 流式：SSE 事件格式转换                             │
│   - 响应：把 OpenAI 的 choices 还原成 content blocks   │
└───────────────────┬─────────────────────────────────┘
                    │  ④ 转发 + 故障转移
                    ▼
┌─────────────────────────────────────────────────────┐
│ 上游池（按延迟排序，失败自动切下一个）                   │
│   deepseek-official / qiniu / codexplus / ...        │
└─────────────────────────────────────────────────────┘
```

## 三、三份凭据/配置

| 件 | 位置 | 换机时 |
|---|---|---|
| 自签 CA + 服务器证书 | `CERT_DIR` | **重新生成并安装**（每机独立） |
| hosts 条目 | `C:\Windows\System32\drivers\etc\hosts` | **重写**（需管理员） |
| 上游池配置 | 代理的 `config.json` | **迁移**（含 key，可复用） |

`CERT_DIR` 里的两件：
```
server.chain.pem     服务器证书链
server.key.pem       私钥
```

## 四、协议转换的关键点

Modex 发的是 **Anthropic Messages** 格式；多数中转站是 **OpenAI** 格式。

**必须正确映射的三处**：

### 1. 工具调用（tool_use）

```
Anthropic 请求:
  "tools": [{"name": "read_file", "input_schema": {...}}]

→ 转为 OpenAI:
  "tools": [{"type": "function", "function": {"name": "read_file", "parameters": {...}}}]
```

### 2. 工具结果的配对（**最容易出错**）

```
Anthropic:
  assistant: [{type: "tool_use", id: "t1", name: "read_file", input: {...}}]
  user:      [{type: "tool_result", tool_use_id: "t1", content: "..."}]

→ OpenAI:
  assistant: {role: "assistant", tool_calls: [{id: "t1", ...}]}
  tool:      {role: "tool", tool_call_id: "t1", content: "..."}
```

**若丢了配对，上游会返回 400**：
```
An assistant message with 'tool_calls' must be followed by tool messages
responding to each 'tool_call_id'.
```
这个错误在实战中真实发生过一次。

### 3. 流式事件

```
OpenAI SSE:   data: {"choices":[{"delta":{"content":"..."}}]}
Anthropic SSE: event: content_block_delta
               data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"..."}}
```

## 五、hosts 的两类条目

```hosts
# 接管（指向本机）
127.0.0.1 www.mhcoding.ai
127.0.0.1 mhcoding.ai            ← 裸域别漏

# 放行（绝不劫持）
# license.mingheng.xin          ← 授权网关，有真 SPKI pin
# up.mingheng.xin               ← 更新服务器
```

**劫持授权域名的后果**：证书 pin 校验失败 → 账号无法续期、skill 胶囊无法下载。

## 六、为什么验证必须禁用系统代理

本机常有 Clash 之类的**系统级 HTTP 代理**。Python `urllib` 默认读系统代理设置：

```
请求 www.mhcoding.ai
  ↓ urllib 查系统代理 → 命中 127.0.0.1:7897
  ↓ 把请求发给代理
  ↓ 代理按真实 DNS 解析，打到真站
→ 401 This API key does not belong to MHcoding
```

**hosts 被完全绕过**。所以验证脚本必须：

```python
OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),      # 关键：空字典 = 不使用任何代理
    urllib.request.HTTPSHandler(context=ctx),
)
```

**注意**：Modex 自身（Electron/Node 网络栈）**不读这个代理设置**，
它的请求确实经 hosts 打到本机。所以这个坑的表现是：
**Modex 能用，但你的验证脚本报 401** —— 极易误判为"接管失败"。

## 七、可回滚性

| 回滚动作 | 命令 |
|---|---|
| 还原 hosts | 删除添加的两行（需管理员） |
| 停服务 | 杀掉监听 443 的进程 |
| 卸证书 | `certutil -user -delstore Root <指纹>` |

**顺序**：先还原 hosts，再停服务。反过来会出现
"hosts 指着本机但没服务"的最坏状态。

## 八、新版变化（2026-09 观测）

新版 Modex 的后端**从 `.pyc` 换成 `.cp311-win_amd64.pyd`**（编译化，反逆向加固），
但**接管机制完全不受影响** —— 因为接管不依赖对 Modex 内部的任何理解。

新增模块中与执行相关的：
```
chat_repair / chat_recovery / chat_rollback    失败自修复
execution_capacity                             执行容量控制
newapi_execution                                多渠道执行
```

**runtime 的数学库新旧两版逐项相同**（`cvxpy`/`highspy`/`ortools`/`pulp`/`sympy`/`scipy`）。
所谓"增强求解器"更准确的描述是"增强执行可靠性"，不是数学能力提升。
