# xw-modex-setup

在**任意机器**上配置 Modex-MH-Agent 的完整工作环境：DNS/CA 接管 + 多上游池 + 实测验证。

## 它解决什么

Modex 的模型端点**在 UI 里改不了**（域名锁死）。要把它指向自家中转站，
只能从网络层接管。这套流程把接管需要的所有知识、坑、验证方法固化下来，
换机时一次做对。

## 快速开始

```bash
# 1. 装依赖（校验纪律是前置）
npx skills add Kvxw1105/kv-verify-discipline
npx skills add Kvxw1105/xw-modex-setup

# 2. 体检（随时可跑，可复跑）
python scripts/setup_check.py --report report.json
```

体检覆盖 **6 组 25 项**：环境 / hosts / 443 代理 / 证书 / 端到端 / 上游实测。

## 五步流程

1. **装 skill** —— 含前置的 `kv-verify-discipline`
2. **装 Modex** —— 记录安装路径与后端端口
3. **建接管链路** —— 自签 CA + hosts + 443 代理
4. **配上游池** —— 凭据库 + 模型映射 + 故障转移
5. **验证** —— `setup_check.py`，用实测结论而非观察

## 八条实战坑（都踩过）

| # | 坑 | 后果 |
|---|---|---|
| K1 | 系统 HTTP 代理绕过 hosts | 验证脚本 401，误判接管失败 |
| K2 | 上游额度是滚动窗口 | 429，多站一起打满 |
| K3 | 某些站需特定请求头 | 400 / 403 |
| K4 | 「列表里有」≠「能用」 | 池里放死模型 |
| K5 | 多站同时异常先怀疑本地 | 误判"站方故障" |
| K6 | 内存取凭据有多个候选 | 取到不能通过校验的 |
| K7 | **接管中但服务停了** | 最坏状态，表现同"没接管" |
| K8 | 改 hosts 需管理员 | 其余步骤都不需要 |

详见 `SKILL.md` 的「必须避开的坑」章节。

## 核心原理

```
Modex → hosts(劫持到 127.0.0.1) → 443 代理(自签CA) → 协议转换 → 上游池
```

**不碰 Modex 任何字节**，所以对它的版本升级免疫。
2026-09 新版把后端编译成 `.pyd`（反逆向加固），**接管完全不受影响**。

详见 `references/architecture.md`。

## 目录

```
SKILL.md                        五步流程 + 八条坑 + 验收标准 + 汇报格式
scripts/setup_check.py          六组 25 项体检（可复跑）
references/architecture.md      接管链路完整技术说明（含协议转换细节）
references/troubleshooting.md   故障速查表（按症状查）
```

## 许可

MIT
