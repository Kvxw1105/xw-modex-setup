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

### 步骤 4 · 配上游池 + 凭据库

**建凭据库**（推荐 JSON，一个文件管所有站）：

```json
{
  "stations": [
    {
      "name": "例子站",
      "base_url": "https://api.example.com/v1",
      "api_key": "sk-...",
      "kind": "openai",
      "inject_headers": {"User-Agent": "claude-cli/2.1.132 (external, cli)"},
      "models": ["model-a", "model-b"],
      "observed": {"at": "2026-01-01T00:00", "usable": ["model-a"], "latency_s": 1.2}
    }
  ]
}
```

**四条配置原则**：

1. **池按实测延迟升序排** —— 主用最快的，不是最贵的
2. **只把「实测可用」的模型放进映射表** —— 列表里有 ≠ 能用
3. **model_map 要覆盖两类名字**：
   - Modex 实际发出的名字（如 `claude-fable-5`）
   - 目标站的真实模型名（**自映射**，如 `claude-opus-5 -> claude-opus-5`）
   - **漏了自映射会导致：请求被原样透传 → 上游不认 → 池子跳站到别处**
4. **开启故障转移** —— 某站限流/失败自动切下一个

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
