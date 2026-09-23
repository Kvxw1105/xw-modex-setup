---
name: xw-modex-setup
description: 在任意机器上配置 Modex-MH-Agent 的完整工作环境（DNS/CA 接管 + 多上游池 + 验证）。当需要在新的比赛电脑上部署 Modex、迁移中转站配置、排查接管失效、或验证「模型请求是否真的走了自家中转站」时使用。触发词：Modex 配置、比赛电脑配置、换机部署、接管失效、443 没监听、hosts 没生效、Modex 用不了、中转站配置、上游池、model_map、证书装不上、Modex 环境体检。
---

# Modex 环境配置（换机部署）

> 目标：在一台新机器上，把 Modex 的模型请求从官方端点**透明接管到自家中转站**，
> 并配好多站故障转移，最后用**实测**确认链路成立。

## 核心原理（先理解，再动手）

Modex 的 Base URL 在 UI 里改不了（锁死官方域名）。接管**不碰 Modex 任何字节**，
而是在它下面垫两层：

```
Modex 发起请求（以为在跟官方说话）
    ↓
① hosts：把官方域名指到 127.0.0.1
    ↓
② 本地 443：自签 CA 的 TLS 服务，收下请求
    ↓
③ 协议转换：Anthropic Messages ↔ OpenAI Chat Completions
    ↓
④ 上游池：按序转发，失败自动切换
```

**接管是版本无关的** —— 它作用在 hosts + 443 这一层。Modex 装在哪、什么版本都不影响。

## 五个步骤

### 步骤 1 · 装本 skill 与校验纪律

```bash
npx skills add Kvxw1105/kv-verify-discipline
npx skills add Kvxw1105/xw-modex-setup
```

`kv-verify-discipline` 是**前置依赖**：本流程所有验证结论都必须遵守它的三条铁律
（结论来自脚本 / 先证伪自己 / 只注入检查方法）。

### 步骤 2 · 装 Modex 并定位

1. 从官方渠道安装 Modex-MH-Agent
2. 记录安装根目录（记为 `MODEX_ROOT`）
3. 启动一次，确认 GUI 能打开
4. 记录**后端端口**（通常 18088）

```powershell
# 找安装路径
Get-Process Modex-MH-Agent | Select-Object -First 1 -ExpandProperty Path
# 找后端端口
Get-NetTCPConnection -State Listen | Where-Object LocalPort -in 18088,18089
```

### 步骤 3 · 建接管链路

**3.1 生成自签 CA**（无需管理员）

```powershell
# 生成 CA + 服务器证书，CN 用要接管的域名
# 私钥与证书放在一个固定目录（记为 CERT_DIR）
```

**3.2 装进受信任根**（用户级，无需管理员）

```powershell
certutil -user -addstore Root <CERT_DIR>\ca.cert.der
```

**3.3 写 hosts**（**需要管理员**，只用这一次提权）

```
127.0.0.1 www.mhcoding.ai
127.0.0.1 mhcoding.ai          ← 裸域也要写，别漏
```

**必须放行、绝不劫持的域名**：
```
license.mingheng.xin    授权 / skill 胶囊分发（有真 SPKI pin，劫持会导致授权失败）
up.mingheng.xin         软件更新
```

**3.4 起 443 拦截代理**

- 监听 `127.0.0.1:443`（Windows 上低端口可非管理员绑定）
- 用 3.1 的证书
- 把 Anthropic Messages 转成 OpenAI Chat Completions（若上游是 openai 协议）
- 按配置转发到上游池

### 步骤 4 · 配上游池 + 凭据库 + **Modex 侧模型配置**

**4.1 建凭据库**（推荐 JSON，一个文件管所有站）：

```json
{
  "stations": [
    {
      "name": "例子站",
      "base_url": "https://api.example.com/v1",
      "api_key": "sk-...",
      "kind": "anthropic",
      "inject_headers": {"User-Agent": "claude-cli/2.1.132 (external, cli)"},
      "models": ["model-a", "model-b"],
      "observed": {"at": "2026-01-01T00:00", "usable": ["model-a"], "latency_s": 1.2}
    }
  ]
}
```

**4.2 四条池配置原则**：

1. **池按实测延迟升序排** —— 主用最快的，不是最贵的
2. **只把「实测可用」的模型放进映射表** —— 列表里有 ≠ 能用
3. **model_map 要覆盖两类名字**：
   - 软件实际发出的名字（如 `claude-fable-5`）
   - 目标站的真实模型名（**自映射**，如 `claude-opus-5 -> claude-opus-5`）
   - **漏了自映射会导致：请求被原样透传 → 上游不认 → 池子跳站到别处**
   - **注意实现陷阱**：用 `m[k]=k` 覆盖，不要用 `setdefault` ——
     否则自映射被 default 占位符挡住，导致"要 A 却给了 B"的静默错误
4. **开故障转移**

**4.3 配 Modex 侧（关键，见 K9）**

Modex 有五套独立的模型端点，**分两种配置形态**：

| 形态 | 接口 | 字段 |
|---|---|---|
| **preset（推荐）** | `POST /api/settings/presets` | `{name, model_id, api_key}` |
| 扁平字段 | `PUT /api/settings` | `executor_api_key` / `_model_id` 等 |

**硬规则（否则必 401）**：
- Modex 侧填 **官方 key + 官方 base_url**
- **`*_base_url` 后端不接收**（写了会静默丢弃），所以只能靠 hosts 接管
- 第三方站的 key **不要填进 Modex**，交给代理层替换

```python
# 正确示例（照做）
PUT /api/settings
{"settings": {
  "executor_base_url": "https://www.mhcoding.ai/",   # 官方域名（本地校验用）
  "executor_api_key":  "<官方 key>",                  # 官方 key（本地校验用）
  "executor_model_id": "claude-fable-5"
}}
# 代理层负责把上面这个 key 换成中转站的
```

### 步骤 5 · 验证（用脚本，不靠观察）

```bash
python scripts/setup_check.py --report setup-report.json
```

脚本跑 6 组 25 项检查，输出可读报告与退出码（0=全过）。

## 必须避开的坑（全部实战踩过）

### K1 · 系统 HTTP 代理绕过 hosts —— **最容易犯**

本机常有 Clash 之类的系统级代理（`127.0.0.1:7897`）。Python `urllib` **默认读系统代理**，
于是 `www.mhcoding.ai` 的请求**不经 hosts**，被代理按真实 DNS 打出去，
命中真站返回 `401 This API key does not belong to MHcoding`。

**修法**：验证脚本必须显式禁用代理。

```python
OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),          # 关键
    urllib.request.HTTPSHandler(context=ctx),
)
```

**注意**：Modex 自身（Electron/Node）不走系统代理，它确实经 hosts 打到本机。
**只有你的验证脚本会中招** —— 所以这个坑表现为"Modex 能用但我的脚本报 401"。

### K2 · 上游额度是滚动窗口

部分站（如火山 coding plan）是 **5 小时滚动额度**，耗尽返回 429
`AccountQuotaExceeded`。多个站可能同账号共享额度，一起打满。

**修法**：多站备援；或等重置。日志里要看 `failover_cooldown` 事件。

### K3 · 某些站必须带特定请求头

```
opencode-go    需要 x-opencode-session，否则 400 MissingSessionID
Cloudflare 站点 需要非默认 User-Agent，否则 403（error 1010）
```

**修法**：`inject_headers` 里配好。

### K4 · 「模型列表里有」≠「能用」

同一站的模型可用性差异极大。实测见过：某站 19 个模型只有 2 个能通；
另一站 7 个只有 1 个能通；同站不同模型延迟差 10 倍（1.9s vs 29.8s）。

**修法**：**逐个实测**，只把通过的放进池。

### K5 · 多对象同时异常 → 先怀疑本地

两家不同的站在同一时间都不可用，**第一反应应是"我这边的问题"**（出口路由、
本地网络、系统代理），而不是"两家都挂了"。

**实测**：换网络节点后同一批模型全部恢复。

### K6 · 从进程内存取凭据时有多个候选

扫内存常得到同一凭据的多种编码视图（utf-16 / ascii），长度不同，
**只有一种能通过校验**。

**修法**：**逐个回测，只采用验证通过的那个**，不要按"最长/最后"猜。

### K7 · 「接管中但服务已停」是最坏状态

hosts 指着 127.0.0.1，但 443 没人监听 → 每条模型请求都连接失败，
而**从 Modex 内部看跟"没接管"一模一样**，极易误判。

**修法**：启动时检查这一状态并自动拉起服务；或直接跑 `setup_check.py`。

### K8 · 改 hosts 需要管理员

除 hosts 外，其余（装证书、绑 443）都**不需要**管理员。
把提权集中在 `写 hosts` 这一个动作上。

### K9 · Modex 有本地 key↔base_url 归属校验 —— **最容易致命的一条**

**症状**：`POST /api/settings/test/{executor,reviewer,editor_ai}` 全返回
```
HTTP 401 This API key does not belong to MHcoding.
Check that your key and Base URL come from the same site
```

**误判风险**：看起来像"网络问题/上游拒绝"，实则是 **Modex 本地就拒了，请求根本没发出去**。

**判别法（三步，缺一不可）**：
```powershell
# 1. 抓后端进程的外部连接（测试期间）
Get-NetTCPConnection -OwningProcess <后端pid> -State Established |
  Where-Object { $_.RemoteAddress -ne '127.0.0.1' }
# 2. 查代理日志有没有新记录
Get-Content proxy\logs\proxy.jsonl -Tail 5
# 3. 关掉系统代理重测，看是否变化
```
**若三步都是"无外部连接 + 代理日志无新增 + 关代理无变化" → 就是本地校验拒绝，不是网络问题。**

**正确配置范式（关键）**：
```
Modex 侧填：官方 key + 官方 base_url（如 https://www.mhcoding.ai/）→ 本地校验通过
代理侧：    把官方 key 替换成中转站 key（intercept_server 有替换逻辑）
真实上游：  收到中转站 key，正常服务
```

**禁止**：直接在 Modex 里填第三方站点的 key —— 会被本地校验拒绝。
**不要**听从"把 key 换掉试试"的直觉，那正是错的。

**验证通过的样子**：
```json
{"ok": true, "message": "Hello.", "agent": "executor"}
```
注意那个 "Hello" 来自**你的中转站**（官方 key 通常已无额度），
这同时证明了 key 替换链路成立。

### K10 · 能原生就原生：先探 `/v1/messages`

**动手前先探**：上游若支持 Anthropic 原生接口，就设 `upstream_kind: anthropic`，
**零转换**。只有不支持时才用 `openai`（需协议转换）。

```python
# 探法
POST {base_url}/v1/messages
  headers: {"x-api-key": key, "anthropic-version": "2023-06-01"}
  body: {"model": "...", "max_tokens": 64, "tools": [...], "messages": [...]}
# 200 且 content 里出现 tool_use → 原生可用
```

**转换的实际代价**（实测差异）：
| 环节 | 原生 | 转 OpenAI 后 |
|---|---|---|
| prompt cache | `cache_creation_input_tokens` 可见可透传 | **字段不存在，直接丢失** |
| 工具配对 | `tool_use` block 原样 | 要映射 `tool_calls`，**id 配对有 400 风险** |
| 响应字段 | `service_tier` / `inference_geo` 等保留 | 丢失 |

**判别响应是不是原生**：看 usage 里有没有
`cache_creation_input_tokens`、`ephemeral_5m_input_tokens`、`service_tier`。
**有 = 原生直连；没有 = 走了转换。**

### K11 · 代理的 header 注入是必需项，不是可选项

**实测现象**：同一个站、同一个 key，**直连 403，经代理 200**。

原因：代理注入的 `User-Agent: claude-cli/2.1.132 (external, cli)` 是关键伪装，
代表"合法 Claude 客户端"。直连用默认 UA 会被上游拒绝。

**推论**：上游的准入判断**不只看 key，还看客户端指纹**。
所以 `inject_headers` 必须配，且**要用像真客户端的 UA**。

### K12 · 部分上游按 credit 计费，不是按 token

某些站的响应里带非标准字段，暴露了它的底层通道：
```
"kiro_credits": 0.027, "kiro_total_ms": 2077, "kiro_actual_input_tokens": 6062
```
→ 底层是 **Kiro（AWS AI IDE）** 通道，**按 credit 计费**。

**含义**：官方标注的"按 token"单价不适用，**成本要按 credit 估算**。
**排查建议**：跑一个阶段后，去各站后台对余额消耗，倒推单篇成本。

### K13 · Claude CLI 继承宿主代理变量 —— **最隐蔽、最难定位**

**症状**：Modex 的「测试连接」**通过**（三角色 `ok:true "Hello"`），
但开会话 / 跑任务时 **503 + 一个陌生分组名**：
```
503 No available channel for model <X> under group <某分组名> (distributor)
```

**根因**：新版 Modex 用 **Claude CLI 子进程**执行任务。该子进程
**继承宿主进程的 `HTTP_PROXY` / `HTTPS_PROXY`**。若这两个变量指向本地
代理（Clash/V2Ray 的 `127.0.0.1:7897`），CLI 请求**优先走代理** →
**绕过 hosts 劫持** → 打到真实站点。

**为什么难查**（实测绕了几小时）：
- 443 代理日志**零记录** → 看起来"请求没发出去"
- 全机抓包 90 秒**零外部连接** → 更深地误判
- 错误里的"分组名"来自真站 → 误以为是自己 key/分组配错

**三步定位法**：
```
1. 读后端进程环境块（PEB），确认 HTTP_PROXY/HTTPS_PROXY 是否存在
2. 对比实验：直接用真 claude.exe 跑两次
     A 带 HTTPS_PROXY → 复现 503
     B 清掉 HTTPS_PROXY → 应成功
3. 读父进程环境 —— 变量会一路继承到桌面/终端
```

**修法**：**从「清掉代理变量」的环境启动 Modex**
```powershell
$env:HTTP_PROXY=''; $env:HTTPS_PROXY=''; $env:ALL_PROXY=''
Start-Process "D:\App\Modex-MH-Agent-2\Modex-MH-Agent.exe"
```
**不要**改系统级环境变量（会影响其他软件）。
**替代方案**：把接管域名加进系统代理的 `ProxyOverride` 例外列表。

**实测证据**（同一 exe、同一 key）：
```
A 带 HTTPS_PROXY   → 222.3s，503 "Grok Build to claude"
B 清掉 HTTPS_PROXY → 19.3s，退出码 0，输出 "PONG"
```

### K14 · 协同模式的接口是 `PATCH /mode`，不是 `chat/config`

**症状**：`/api/workflows/{id}/chat/config` 返回
`409 这个工作流不是协同模式，请先在工作流详情页切换模式`。

**误判**：以为只能通过 GUI 切换。

**真相**（在 `workflowStore.setWorkflowMode` 里）：
```
PATCH /api/workflows/{id}/mode
body: {"chat_mode": true}          ← 布尔值，不是字符串
→ 200 {"status": "updated", "chat_mode": true}
```

**三条关键认知**：
- `chat/config` 是**只读**的，不能用它切模式
- 建卡接口 **不接收 `chat_mode`** —— 传了会被静默丢弃
  （params 里只留 `_execution_identity`）
- 协同模式必须**建卡后单独 PATCH /mode**

**绑模型**也要带 `interaction_mode`（否则 422）：
```
PATCH /api/workflows/{id}/chat/config
body: {"model_preset_id": "<id>", "interaction_mode": "auto"}
```

### K15 · Modex 侧的模型名必须是「兼容名」

**症状**：开会话 503，错误里出现**你没配过的模型名**（如 `claude-opus-4-7`）。

**根因**：Claude CLI 只认**它自己的兼容模型名**（如 `claude-fable-5`），
再按内部映射表转成真实模型。直接填真实名（`claude-opus-5`）它不认。

**修法（两全其美）**：
```
Modex preset 填「兼容名」  claude-fable-5                    ← CLI 认
代理 model_map 做映射      claude-fable-5 → claude-opus-5     ← 上游收到真名
```

**判别**：读 CLI 的 `.claude.json`，看
`tengu_auto_mode_config.modelByMainModel` —— 它列出了 CLI 认识的主模型名。

### K16 · 消息状态机与驱动顺序

**症状**：`say` 返回 200 但任务不执行；`confirm` 报
`该提议状态为 pending，不能确认（只有 proposed 可以）`。

**状态流转**：
```
pending    用户消息落库
   ↓ say 触发 CLI 处理
proposed   CLI 生成任务卡，等确认
   ↓ confirm
queued     入队 → running → done / failed
```

**完整驱动顺序（缺一不可）**：
```python
POST /chat/session                                        # 开会话（60~220s）
POST /chat/say  {"text":..., "interaction_mode":"auto"}    # 发指令
# 等 CLI 处理，直到出现 proposed
POST /chat/messages/{mid}/confirm                         # 确认任务卡
```

**字段名注意**：`say` 用 **`text`**（不是 `message`），
且**必须带 `interaction_mode`**（否则 422）。

## 验收标准

- [ ] `setup_check.py` 退出码为 0（全部检查通过）
- [ ] 经 443 的端到端请求返回 200，且**工具调用正常**（tool_use 出现）
- [ ] 至少 2 个上游可用，且故障转移实测生效（手动让主用失败，观察切站）
- [ ] 换上游不需要重启代理（热切换）
- [ ] 授权域名未被劫持
- [ ] 所有验证结论都有脚本可复跑

## 汇报格式

```text
Modex 接管配置报告:
- 环境: MODEX_ROOT=<路径>  后端端口=<端口>  后端形态=pyd|pyc
- 接管: hosts=<已接管域名列表>  443=<监听 pid>  证书=<CN>
- 上游: <各站名> → <实测状态> <延迟>
- 端到端: HTTP <状态>  模型=<实际模型>  工具调用=<有无>  延迟=<秒>
- 未通过: <列出该项与原因，无则写"无">
- 待人工: <需要提权的动作，如补 hosts 裸域>
```

## 参考

- `scripts/setup_check.py` —— 六组 25 项环境体检（可复跑）
- `references/architecture.md` —— 接管链路的完整技术说明
- `references/troubleshooting.md` —— 故障速查表
