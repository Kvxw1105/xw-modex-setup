# 故障速查表

按症状查。每条给出**首要怀疑方向**，避免从错的方向排查。

| 症状 | 首要怀疑 | 排查动作 |
|---|---|---|
| Modex 报模型连接失败 / 超时 | **接管中但服务停了**（最坏状态） | 查 443 是否监听：`Get-NetTCPConnection -LocalPort 443 -State Listen` |
| 验证脚本 401 `not belong to MHcoding` | **系统代理绕过 hosts** | 检查脚本是否用了 `ProxyHandler({})` |
| 全部上游 429 | 额度耗尽（滚动窗口） | 看日志 `failover_cooldown` 的 reason；等重置或多站备援 |
| 上游返回 400 `MissingSessionID` | **缺必需请求头** | 该站需要 `x-opencode-session` 之类，配进 `inject_headers` |
| 上游返回 403 + Cloudflare HTML | UA 被拦 | `inject_headers` 加浏览器级 User-Agent |
| 上游返回 400 tool_calls 配对错误 | **协议转换丢配对** | 检查 tool_use ↔ tool_calls 的 id 映射 |
| 请求被原样透传后失败 | **model_map 缺自映射** | 目标站真实模型名要映射到自身 |
| 证书装不上 | 权限或被安全软件拦 | `certutil -user -addstore Root`；或手动双击导入 |
| 授权/更新失败 | **授权域名被劫持** | 检查 hosts 是否误加 `license./up.mingheng.xin` |
| 单次请求卡 1-2 分钟 | **可能是深推理，不是卡死** | 交叉看 TCP 连接 + 进程 CPU + 子进程存活 |
| 多个站同时不可用 | **先怀疑本地** | 换网络节点试；查出口路由 |
| 凭据取了但校验不过 | 取到了错误候选 | 逐个回测，只采纳通过校验的那个 |

## 深推理 vs 卡死 的判别法

**只看 `updated_at` 会误判。** 三个证据交叉：

```powershell
# 1. TCP 连接是否还挂着（有 Established = 请求在飞行中）
Get-NetTCPConnection -OwningProcess <代理pid> | Select State,RemoteAddress

# 2. 进程 CPU 增量（接近 0 = 阻塞等待，正常；持续高 = 死循环）
$a=(Get-Process -Id <pid>).CPU; Start-Sleep 6; $b=(Get-Process -Id <pid>).CPU; $b-$a

# 3. 执行子进程是否存活
Get-Process claude -ErrorAction SilentlyContinue
```

**实测记录**：单次请求最长 **140.57 秒**（正常深推理）。
若仅以"1 分钟没动静"判定失败，会误杀正常任务。

## 错误信息含义对照

| 原文 | 真实含义 |
|---|---|
| `API Error: 502 所有上游都不可用` | 池内全部失败。查各站实测状态 |
| `AccountQuotaExceeded ... reset at <时间>` | 滚动额度耗尽，给的是重置时刻 |
| `invalid desktop control token` | 后端 API 凭据不对（换机需重扫） |
| `Prompt is too long` | 请求体超限。需上下文裁剪 |
| `The requested model does not support the coding plan` | 该站不认这个模型（换模型） |
| `not belong to MHcoding` | **打到了真站** —— 系统代理绕过 hosts |
| `上游相关文件发生变化，需核对受影响部分` | **不是失败**，是依赖指纹变更的陈旧标记 |

## 排查顺序（推荐）

```
1. 跑 setup_check.py            ← 一条命令覆盖 25 项
2. 看它报的异常项，按上表定位
3. 改一处 → 立刻复跑验证
4. 不要一次改多处（无法归因）
```
