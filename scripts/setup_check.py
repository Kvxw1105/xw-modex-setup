"""Modex 接管环境体检 —— 一条命令跑完五步，输出可读报告。

用法：
    python setup_check.py                体检当前机器
    python setup_check.py --report <path>  同时写报告文件

设计依据：kv-verify-discipline 的纪律 —— 所有结论必须来自实测，
不允许"看起来应该没问题"。本脚本只做事实采集与判定，不做推测。
"""
import argparse
import io
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 本机常有系统级 HTTP 代理（Clash 等）。urllib 默认读系统代理，
# 会**绕过 hosts** 把请求发到真实站点，导致 401 之类的假故障。
# 所有对 127.0.0.1 的请求必须显式禁用代理。
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE
OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),          # 关键：禁用系统代理
    urllib.request.HTTPSHandler(context=_CTX),
)
# 用于探测外部上游（这些要走真网络，但同样不该被系统代理劫持干扰判断）
OPENER_NET = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Modex 会请求的域名（接管目标）
TARGET_HOSTS = ["www.mhcoding.ai", "mhcoding.ai"]
# 授权/更新域名（必须原样放行，不能被接管）
PASS_HOSTS = ["license.mingheng.xin", "up.mingheng.xin"]

findings = []


def rec(step, item, ok, detail=""):
    findings.append(dict(step=step, item=item, ok=bool(ok), detail=str(detail)[:300]))
    mark = "OK  " if ok else "FAIL"
    print("  [%s] %-34s %s" % (mark, item, str(detail)[:110]))


def sh(cmd):
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                           capture_output=True, text=True, timeout=40)
        return (r.stdout or "").strip()
    except Exception as exc:
        return "ERR:%s" % exc


# ---------------------------------------------------------------- 1 环境
def check_env():
    print("\n=== 1. 环境 ===")
    # Modex 安装
    out = sh("Get-Process -Name 'Modex-MH-Agent' -ErrorAction SilentlyContinue | "
             "Select-Object -First 1 -ExpandProperty Path")
    if out and "Modex" in out:
        rec("env", "Modex 进程", True, out)
        root = os.path.dirname(out)
        rec("env", "Modex 安装目录", os.path.isdir(root), root)
        # 版本线索
        bak = os.path.join(root, "resources", "app", "backend", "_modex-build-inputs.json")
        if os.path.exists(bak):
            try:
                j = json.load(open(bak, encoding="utf-8"))
                rec("env", "构建清单", True,
                    "schema=%s material=%s" % (j.get("schema_version"), j.get("material_set_id")))
            except Exception as exc:
                rec("env", "构建清单", False, exc)
        # backend 是否编译化（新版特征）
        bdir = os.path.join(root, "resources", "app", "backend")
        n_pyd = 0
        n_pyc = 0
        for dp, _dn, fn in os.walk(bdir):
            for f in fn:
                if f.endswith(".pyd"):
                    n_pyd += 1
                elif f.endswith(".pyc"):
                    n_pyc += 1
        rec("env", "后端形态", True, "pyd=%d pyc=%d（pyd 为主=新版编译化）" % (n_pyd, n_pyc))
    else:
        rec("env", "Modex 进程", False, "未检测到 Modex-MH-Agent 在运行")

    # 后端端口
    out = sh("Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | "
             "Where-Object { $_.LocalPort -in 18088,18089,18090 } | "
             "Select-Object -First 3 LocalPort,OwningProcess | ConvertTo-Json -Compress")
    rec("env", "Modex 后端监听", bool(out and "LocalPort" in out), out or "无")

    # python
    rec("env", "python", True, sys.version.split()[0])


# ---------------------------------------------------------------- 2 hosts
def check_hosts():
    print("\n=== 2. hosts 接管 ===")
    hp = r"C:\Windows\System32\drivers\etc\hosts"
    try:
        text = open(hp, encoding="utf-8", errors="replace").read()
    except Exception as exc:
        rec("hosts", "读 hosts", False, exc)
        return
    for h in TARGET_HOSTS:
        hit = re.search(r"^\s*127\.0\.0\.1\s+%s\s*$" % re.escape(h), text, re.M)
        rec("hosts", "接管 %s" % h, bool(hit), "已指向 127.0.0.1" if hit else "未接管")
    for h in PASS_HOSTS:
        hijacked = re.search(r"^\s*127\.0\.0\.1\s+%s\s*$" % re.escape(h), text, re.M)
        rec("hosts", "放行 %s" % h, not hijacked,
            "未劫持（正确）" if not hijacked else "被劫持（会导致授权失败！）")

    # DNS 实际解析
    out = sh("Resolve-DnsName %s -ErrorAction SilentlyContinue | "
             "Select-Object -ExpandProperty IPAddress" % TARGET_HOSTS[0])
    rec("hosts", "DNS 解析", "127.0.0.1" in out, out)


# ---------------------------------------------------------------- 3 代理
def check_proxy():
    print("\n=== 3. 443 拦截代理 ===")
    # 端口监听
    out = sh("(Get-NetTCPConnection -LocalPort 443 -State Listen "
             "-ErrorAction SilentlyContinue).OwningProcess")
    pids = [x for x in out.split() if x.isdigit()]
    if not pids:
        rec("proxy", "443 监听", False, "无监听 —— hosts 若已接管则所有请求会失败")
        return None
    pid = pids[0]
    cmdline = sh("(Get-CimInstance Win32_Process -Filter \"ProcessId=%s\").CommandLine" % pid)
    is_proxy = "intercept_server" in cmdline or "MHProxyClient" in cmdline
    rec("proxy", "443 监听者", is_proxy, "pid=%s %s" % (pid, cmdline[:80]))

    # 池状态
    try:
        with OPENER.open("https://127.0.0.1/__pool", timeout=10) as r:
            data = json.loads(r.read())
        entries = data.get("entries") or []
        active = [e for e in entries if e.get("active") and not e.get("cooling")]
        rec("proxy", "池条目数", len(entries) > 0, "%d 个" % len(entries))
        rec("proxy", "可用上游", len(active) > 0,
            ", ".join(e["name"] for e in active) or "全部冷却中")
    except Exception as exc:
        rec("proxy", "池状态查询", False, exc)
        entries = None

    # 真实上游凭据要从 config.json 读（/__pool 出于安全不返回 key）
    cfg_entries = load_pool_from_config()
    if cfg_entries:
        rec("proxy", "上游凭据", True, "从 config.json 读到 %d 条" % len(cfg_entries))
    else:
        rec("proxy", "上游凭据", False, "未找到 config.json 或其中无 pool")
    return cfg_entries or entries


def load_pool_from_config():
    """从代理的 config.json 读 pool（含 api_key）。按常见路径顺序找。"""
    cands = [
        os.environ.get("MODEX_PROXY_CONFIG"),
        r"D:\reverse-lab\work\modex-mh-agent\proxy\config.json",
        os.path.join(os.path.expanduser("~"), ".modex-proxy", "config.json"),
        r"C:\Users\kvxkf\AppData\Roaming\MHProxyClient\config.json",
    ]
    for p in cands:
        if not p or not os.path.exists(p):
            continue
        try:
            j = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue
        pool = j.get("pool")
        if isinstance(pool, list) and pool:
            for e in pool:
                e.setdefault("probe_model",
                             next(iter((e.get("model_map") or {}).values()), None))
            return pool
    return None


# ---------------------------------------------------------------- 4 证书
def check_cert():
    print("\n=== 4. 证书 ===")
    # 端到端 TLS（用系统信任库验证，不关校验）
    # 注意：socket 不经系统 HTTP 代理，所以这里能真实命中本机 443
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection(("127.0.0.1", 443), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=TARGET_HOSTS[0]) as ss:
                cert = ss.getpeercert()
        cn = dict(x[0] for x in cert.get("subject", ())).get("commonName")
        rec("cert", "系统信任库校验", True, "CN=%s" % cn)
    except ssl.SSLCertVerificationError as exc:
        rec("cert", "系统信任库校验", False, "证书未装进受信任根：%s" % str(exc)[:120])
    except Exception as exc:
        rec("cert", "系统信任库校验", False, exc)


# ---------------------------------------------------------------- 5 端到端
def check_e2e():
    print("\n=== 5. 端到端（经 443，带工具调用）===")
    body = {
        "model": "claude-fable-5", "max_tokens": 128,
        "tools": [{"name": "read_file", "description": "read a file",
                   "input_schema": {"type": "object",
                                    "properties": {"path": {"type": "string"}},
                                    "required": ["path"]}}],
        "messages": [{"role": "user", "content": "Call read_file for 'a.txt'"}],
    }
    req = urllib.request.Request("https://%s/v1/messages" % TARGET_HOSTS[0],
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "x-api-key": "sk-inbound-test",
                                          "anthropic-version": "2023-06-01",
                                          "User-Agent": UA},
                                 method="POST")
    t0 = time.time()
    try:
        with OPENER.open(req, timeout=180) as r:
            raw = r.read().decode("utf-8", "replace")
            status = r.status
            dt = time.time() - t0
        j = json.loads(raw)
        tool_uses = [b for b in (j.get("content") or [])
                     if isinstance(b, dict) and b.get("type") == "tool_use"]
        rec("e2e", "HTTP 状态", status == 200, "%s  %.1fs" % (status, dt))
        rec("e2e", "响应模型", bool(j.get("model")), j.get("model"))
        rec("e2e", "工具调用", len(tool_uses) > 0,
            "tool_use=%s" % (tool_uses[0].get("name") if tool_uses else "无"))
        rec("e2e", "延迟", dt < 60, "%.1fs" % dt)
    except urllib.error.HTTPError as exc:
        detail = exc.read(300).decode("utf-8", "replace")
        hint = ""
        if "not belong to MHcoding" in detail or "不是 MHcoding" in detail:
            hint = " ← 打到了真实站点：检查系统 HTTP 代理是否绕过了 hosts"
        rec("e2e", "HTTP 状态", False, "%s%s" % (detail[:150], hint))
    except Exception as exc:
        rec("e2e", "端到端", False, exc)


# ---------------------------------------------------------------- 6 上游实测
def probe_one(entry):
    base = (entry.get("base_url") or entry.get("upstream_base_url") or "").rstrip("/")
    name = entry.get("name") or base
    key = entry.get("api_key") or entry.get("upstream_api_key") or ""
    model = entry.get("probe_model")
    if not model:
        mm = entry.get("model_map") or {}
        model = next(iter(mm.values()), "gpt-3.5-turbo")
    if not base or not key:
        return (name, "无凭据(base/key缺)", 0)
    url = base + "/chat/completions"
    payload = json.dumps({"model": model, "max_tokens": 8,
                          "messages": [{"role": "user", "content": "hi"}]}).encode()
    req = urllib.request.Request(url, data=payload,
                                 headers={"Authorization": "Bearer " + key,
                                          "Content-Type": "application/json",
                                          "User-Agent": UA}, method="POST")
    t0 = time.time()
    try:
        with OPENER_NET.open(req, timeout=45) as r:
            r.read()
            return (name, "OK %s" % model, time.time() - t0)
    except urllib.error.HTTPError as e:
        return (name, "HTTP %s" % e.code, time.time() - t0)
    except Exception as e:
        return (name, type(e).__name__, time.time() - t0)


def check_upstreams(entries):
    print("\n=== 6. 上游实测（并发）===")
    if not entries:
        rec("upstream", "池内上游", False, "无可测条目")
        return
    with ThreadPoolExecutor(max_workers=6) as ex:
        res = list(ex.map(probe_one, entries))
    for name, st, dt in res:
        # st 形如 "OK <model>" / "HTTP 429" / "TimeoutError"
        rec("upstream", name, st.startswith("OK"), "%s  %.1fs" % (st, dt))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report")
    args = ap.parse_args()

    print("Modex 接管环境体检")
    print("时间:", time.strftime("%Y-%m-%d %H:%M:%S"))

    check_env()
    check_hosts()
    entries = check_proxy()
    check_cert()
    check_e2e()
    check_upstreams(entries)

    # 汇总
    bad = [f for f in findings if not f["ok"]]
    print("\n" + "=" * 62)
    print("汇总：%d 项检查，%d 项通过，%d 项异常" % (len(findings), len(findings) - len(bad), len(bad)))
    if bad:
        print("\n异常项：")
        for f in bad:
            print("  [%s] %s —— %s" % (f["step"], f["item"], f["detail"][:130]))
    else:
        print("全部通过。")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "findings": findings},
                      fh, ensure_ascii=False, indent=2)
        print("\n报告已写:", args.report)

    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
