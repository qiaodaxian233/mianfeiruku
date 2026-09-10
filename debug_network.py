#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam 免费入库雷达 · 网络诊断工具
==================================
逐项检查 Python 进程为什么连不上 Steam 商店，而浏览器可以：
  1. 代理设置（环境变量 / Windows 注册表 / PAC 脚本 / WinHTTP）
  2. DNS 解析（系统解析 vs 阿里 DoH 参考）与 hosts 文件
  3. 对每个 IP 做 TCP 连接 + TLS 握手
  4. requests 直连 / 各种代理 的 HTTP 请求
  5. 本机监听的代理端口探测（含加速器进程占用的端口）
  6. 其他进程对比：系统自带 curl、无头 Edge/Chrome
  7. 加速器痕迹：进程、虚拟网卡、默认路由
最后给出结论与建议，报告保存到同目录 debug_report.txt。

    python debug_network.py            # 图形窗口
    python debug_network.py --console  # 纯命令行
"""
from __future__ import annotations

import json
import locale
import os
import platform
import re
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
REPORT_FILE = os.path.join(HERE, "debug_report.txt")
HOST = "store.steampowered.com"
TEST_URL = f"https://{HOST}/api/appdetails?appids=10&cc=cn&l=schinese"  # AppID 10 = 半条命，任何地区都存在
IS_WIN = sys.platform.startswith("win")
ENC = locale.getpreferredencoding(False) or "utf-8"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
COMMON_PROXY_PORTS = [7890, 7897, 7891, 10809, 10808, 1080, 1081, 8080, 8118, 8888,
                      9910, 2080, 20171, 20172, 33210, 33211, 7070, 3128, 8899, 10800]
SUSPECT_PROC = re.compile(r"(leigod|^nn|xunyou|uu|clash|v2ray|xray|sing-?box|mihomo|verge|shadowsocks|ssr|"
                          r"trojan|naive|hysteria|proxifier|privoxy|netease|qiyou|biubiu|accel|jiasu|speed)", re.I)

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None


# ─────────────────────────────── 小工具 ────────────────────────────────

def run_cmd(args: list[str], timeout: int = 10) -> str:
    kw = {}
    if IS_WIN:
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        cp = subprocess.run(args, capture_output=True, timeout=timeout, **kw)
    except FileNotFoundError:
        return f"<找不到命令 {args[0]}>"
    except subprocess.TimeoutExpired:
        return "<命令超时>"
    except OSError as e:
        return f"<执行失败: {e}>"
    raw = cp.stdout + (b"\n" + cp.stderr if cp.stderr.strip() else b"")
    for enc in (ENC, "utf-8", "gbk"):
        try:
            return raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace").strip()


def short_exc(e: Exception) -> str:
    if requests is not None:
        rx = requests.exceptions
        if isinstance(e, rx.ProxyError):
            base = "无法连接代理服务器"
        elif isinstance(e, rx.ConnectTimeout):
            base = "连接超时"
        elif isinstance(e, rx.ReadTimeout):
            base = "读取超时（连上了但没响应）"
        elif isinstance(e, rx.SSLError):
            base = "SSL 握手失败"
        elif isinstance(e, rx.ConnectionError):
            base = "连接失败"
        else:
            base = type(e).__name__
    else:
        base = type(e).__name__
    s = str(e)
    m = re.search(r"Caused by (.*)\)\s*$", s, re.S)
    inner = m.group(1) if m else s
    qs = [a or b for a, b in re.findall(r'"([^"]{6,})"|\'([^\']{6,})\'', inner)]
    detail = max(qs, key=len) if qs else inner
    detail = re.sub(r"^(HTTPS?Connection\([^)]*\)|<[^>]+>):\s*", "", detail)
    detail = detail.replace(HOST, "…").strip()[:140]
    return f"{base}（{detail}）"


def http_test(url: str, proxies: dict | None = None, timeout: float = 10) -> tuple[bool, str]:
    if requests is None:
        return False, "未安装 requests"
    t0 = time.time()
    try:
        s = requests.Session()
        s.trust_env = False
        r = s.get(url, proxies=proxies or {}, timeout=timeout, headers={"User-Agent": UA})
        dt = time.time() - t0
        ok = r.status_code == 200 and (TEST_URL not in url or '"success"' in r.text[:300])
        return ok, f"HTTP {r.status_code}，{dt:.1f}s，{len(r.content)} 字节"
    except Exception as e:  # noqa: BLE001
        return False, short_exc(e)


def tcp_tls_test(ip: str, port: int = 443, timeout: float = 4) -> tuple[str, str]:
    t0 = time.time()
    try:
        sock = socket.create_connection((ip, port), timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return "TCP 失败", f"{type(e).__name__}: {e}"
    tcp_ms = (time.time() - t0) * 1000
    try:
        ctx = ssl.create_default_context()
        with ctx.wrap_socket(sock, server_hostname=HOST) as ss:
            cert = ss.getpeercert() or {}
            cn = next((v for part in cert.get("subject", ()) for k, v in part if k == "commonName"), "?")
        return "TCP+TLS 正常", f"TCP {tcp_ms:.0f}ms，证书 CN={cn}"
    except Exception as e:  # noqa: BLE001
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass
        return "TCP 通但 TLS 失败", f"{type(e).__name__}: {str(e)[:120]}"


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def parse_proxy_hosts(text: str) -> list[str]:
    """从 'http=127.0.0.1:7890;https=...' / 'PROXY 127.0.0.1:7890' 等文本里提取 host:port。"""
    found = []
    for m in re.finditer(r"(?:(socks5h?|socks4|https?)://)?([\w.\-]+):(\d{2,5})", text, re.I):
        scheme, host, port = m.group(1), m.group(2), m.group(3)
        if host.lower() in ("0.0.0.0",):
            continue
        val = f"{(scheme or 'http').lower()}://{host}:{port}"
        if val not in found:
            found.append(val)
    return found


# ─────────────────────────────── 诊断 ────────────────────────────────

class Diagnostics:
    def __init__(self, log):
        self.log = log
        self.lines: list[str] = []
        self.res: dict = {}

    def out(self, s: str = "") -> None:
        self.lines.append(s)
        self.log(s)

    def head(self, s: str) -> None:
        self.out("")
        self.out(f"━━━ {s} ━━━")

    # 0 环境
    def env(self) -> None:
        self.head("环境")
        self.out(f"时间：{datetime.now():%Y-%m-%d %H:%M:%S}")
        self.out(f"系统：{platform.platform()}")
        self.out(f"Python：{sys.version.split()[0]}  {sys.executable}")
        ver = "?"
        try:
            with open(os.path.join(HERE, "steam_free_finder.py"), encoding="utf-8") as f:
                m = re.search(r"版本：([\d.]+)", f.read())
                ver = m.group(1) if m else "?"
        except OSError:
            pass
        self.out(f"主程序版本：{ver}")
        mods = []
        for name in ("requests", "urllib3", "bs4", "PIL", "socks"):
            try:
                mod = __import__(name)
                mods.append(f"{name} {getattr(mod, '__version__', 'ok')}")
            except ImportError:
                mods.append(f"{name} 未安装")
        self.out("依赖：" + "，".join(mods))

    # 1 代理设置
    def proxy_settings(self) -> None:
        self.head("1. 代理设置")
        cands: list[str] = []
        env = {k: v for k, v in os.environ.items() if k.upper() in
               ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")}
        self.out(f"环境变量：{env if env else '无'}")
        for v in env.values():
            cands += parse_proxy_hosts(v)
        try:
            import urllib.request
            gp = urllib.request.getproxies()
            self.out(f"urllib.getproxies()：{gp if gp else '无'}")
            for v in gp.values():
                cands += parse_proxy_hosts(v)
        except Exception as e:  # noqa: BLE001
            self.out(f"urllib.getproxies() 出错：{e}")
        if IS_WIN:
            try:
                import winreg  # type: ignore
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                     r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
                vals = {}
                for name in ("ProxyEnable", "ProxyServer", "ProxyOverride", "AutoConfigURL", "AutoDetect"):
                    try:
                        vals[name] = winreg.QueryValueEx(key, name)[0]
                    except OSError:
                        pass
                self.out(f"注册表 Internet Settings：{vals}")
                if vals.get("ProxyServer"):
                    cands += parse_proxy_hosts(str(vals["ProxyServer"]))
                pac = vals.get("AutoConfigURL")
                if pac:
                    self.out(f"⚠ 发现 PAC 自动配置脚本：{pac}（浏览器会用它选代理，Python 不会！）")
                    cands += self._read_pac(str(pac))
            except Exception as e:  # noqa: BLE001
                self.out(f"读取注册表失败：{e}")
            self.out("netsh winhttp show proxy：")
            for line in run_cmd(["netsh", "winhttp", "show", "proxy"]).splitlines():
                if line.strip():
                    self.out("    " + line.strip())
        self.res["proxy_candidates"] = list(dict.fromkeys(cands))
        self.out(f"从设置里提取到的代理候选：{self.res['proxy_candidates'] or '无'}")

    def _read_pac(self, url: str) -> list[str]:
        text = ""
        try:
            if url.lower().startswith("file:"):
                path = url[5:].lstrip("/")
                with open(path, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            elif requests is not None:
                s = requests.Session()
                s.trust_env = False
                text = s.get(url, timeout=5).text
        except Exception as e:  # noqa: BLE001
            self.out(f"    读取 PAC 失败：{short_exc(e)}")
            return []
        found = []
        for m in re.finditer(r"\b(PROXY|HTTPS|SOCKS5?|SOCKS)\s+([\w.\-]+:\d+)", text):
            scheme = {"PROXY": "http", "HTTPS": "http", "SOCKS": "socks5h", "SOCKS5": "socks5h"}[m.group(1)]
            val = f"{scheme}://{m.group(2)}"
            if val not in found:
                found.append(val)
        self.out(f"    PAC 中的代理：{found or '未找到 PROXY 指令'}")
        return found

    # 2 DNS
    def dns(self) -> None:
        self.head("2. DNS 解析")
        ips: list[str] = []
        try:
            t0 = time.time()
            infos = socket.getaddrinfo(HOST, 443, proto=socket.IPPROTO_TCP)
            for fam, *_rest, sa in infos:
                if sa[0] not in ips:
                    ips.append(sa[0])
            self.out(f"系统解析 {HOST} → {ips}（{(time.time() - t0) * 1000:.0f}ms）")
        except Exception as e:  # noqa: BLE001
            self.out(f"系统解析失败：{type(e).__name__}: {e}")
        self.res["ips"] = ips
        # 参考：阿里 DoH（国内可直连）
        try:
            if requests is None:
                raise RuntimeError("未安装 requests")
            s = requests.Session()
            s.trust_env = False
            r = s.get("https://dns.alidns.com/resolve", params={"name": HOST, "type": "A"}, timeout=5)
            doh = [a["data"] for a in r.json().get("Answer", []) if a.get("type") == 1]
            self.out(f"参考：阿里 DoH 解析 → {doh}（CDN 按地区给不同 IP，与上面不同不一定是问题）")
        except Exception as e:  # noqa: BLE001
            self.out(f"参考 DoH 解析失败：{short_exc(e)}")
        hosts_path = r"C:\Windows\System32\drivers\etc\hosts" if IS_WIN else "/etc/hosts"
        try:
            with open(hosts_path, encoding="utf-8", errors="replace") as f:
                hits = [ln.strip() for ln in f if "steam" in ln.lower() and not ln.strip().startswith("#")]
            self.out(f"hosts 文件中与 steam 相关的行：{hits or '无'}")
        except OSError as e:
            self.out(f"读取 hosts 失败：{e}")
        if IS_WIN:
            ns = run_cmd(["nslookup", HOST], 8)
            self.out("nslookup：" + " | ".join(ln.strip() for ln in ns.splitlines() if ln.strip())[:400])

    # 3 TCP / TLS
    def tcp_tls(self) -> None:
        self.head("3. TCP 连接 + TLS 握手（每个 IP）")
        results = {}
        for ip in self.res.get("ips", [])[:4]:
            status, detail = tcp_tls_test(ip)
            results[ip] = status
            fam = "IPv6" if ":" in ip else "IPv4"
            self.out(f"  {fam} {ip:<40} {status}  {detail}")
        if not results:
            self.out("  没有可测试的 IP")
        self.res["tcp"] = results

    # 4 HTTP
    def http(self) -> None:
        self.head("4. HTTP 请求（requests）")
        ok, msg = http_test(TEST_URL)
        self.out(f"  直连：{'✔' if ok else '✘'} {msg}")
        self.res["direct_ok"] = ok
        self.res["direct_msg"] = msg
        working = []
        for p in self.res.get("proxy_candidates", []):
            ok, msg = http_test(TEST_URL, {"http": p, "https": p}, timeout=8)
            self.out(f"  代理 {p}：{'✔' if ok else '✘'} {msg}")
            if ok:
                working.append(p)
        self.res["working_proxies"] = working

    # 5 本地端口
    def local_ports(self) -> None:
        self.head("5. 本机监听端口 / 本地代理探测")
        listening = self._listening()  # [(port, pid, name, addr)]
        suspects = [x for x in listening if SUSPECT_PROC.search(x[2] or "")]
        if listening:
            self.out(f"  本机共有 {len(listening)} 个 TCP 监听端口")
            for port, pid, name, addr in suspects:
                self.out(f"  ⚠ 疑似代理/加速器进程在监听：{addr}:{port}  {name} (PID {pid})")
            local = sorted({(x[0], x[2]) for x in listening if x[3].startswith("127.")})
            self.out("  仅本机可见(127.0.0.1)的监听端口：" +
                     ("，".join(f"{port}({name})" for port, name in local[:40]) or "无") +
                     ("  …" if len(local) > 40 else ""))
        else:
            self.out("  无法列出监听端口（非 Windows 或 netstat 不可用）")
        cand_ports = [x[0] for x in suspects]
        for p in self.res.get("proxy_candidates", []):
            m = re.search(r":(\d+)$", p)
            if m and ("127.0.0.1" in p or "localhost" in p):
                cand_ports.append(int(m.group(1)))
        cand_ports += COMMON_PROXY_PORTS
        seen, opened = set(), []
        for port in cand_ports:
            if port in seen:
                continue
            seen.add(port)
            if port_open(port):
                opened.append(port)
        self.out(f"  127.0.0.1 上开放的候选端口：{opened or '无'}")
        try:
            import socks  # noqa: F401
            has_socks = True
        except ImportError:
            has_socks = False
        working = list(self.res.get("working_proxies", []))
        for port in opened[:8]:
            proxy = f"http://127.0.0.1:{port}"
            ok, msg = http_test(TEST_URL, {"http": proxy, "https": proxy}, timeout=6)
            self.out(f"  当 HTTP 代理试 {proxy}：{'✔' if ok else '✘'} {msg}")
            if ok:
                working.append(proxy)
                continue
            if has_socks:
                proxy = f"socks5h://127.0.0.1:{port}"
                ok, msg = http_test(TEST_URL, {"http": proxy, "https": proxy}, timeout=6)
                self.out(f"  当 SOCKS5 代理试 {proxy}：{'✔' if ok else '✘'} {msg}")
                if ok:
                    working.append(proxy)
        if opened and not has_socks:
            self.out("  （未安装 PySocks，未测试 SOCKS 代理；pip install pysocks 后可测）")
        self.res["working_proxies"] = list(dict.fromkeys(working))

    def _listening(self) -> list[tuple[int, int, str, str]]:
        if not IS_WIN:
            out = run_cmd(["ss", "-ltnp"], 8)
            res = []
            for ln in out.splitlines():
                m = re.search(r"\s(\S+):(\d+)\s.*users:\(\(\"([^\"]+)\",pid=(\d+)", ln)
                if m:
                    res.append((int(m.group(2)), int(m.group(4)), m.group(3), m.group(1)))
            return res
        pid_name: dict[int, str] = {}
        for ln in run_cmd(["tasklist", "/FO", "CSV", "/NH"], 15).splitlines():
            parts = [p.strip().strip('"') for p in ln.split('","')]
            if len(parts) >= 2 and parts[1].isdigit():
                pid_name[int(parts[1])] = parts[0]
        self.res["procs"] = sorted(set(pid_name.values()))
        res = []
        for ln in run_cmd(["netstat", "-ano", "-p", "TCP"], 15).splitlines():
            m = re.match(r"\s*TCP\s+(\S+):(\d+)\s+\S+\s+LISTENING\s+(\d+)", ln)
            if m:
                addr, port, pid = m.group(1), int(m.group(2)), int(m.group(3))
                res.append((port, pid, pid_name.get(pid, "?"), addr))
        return res

    # 6 其他进程对比
    def compare(self) -> None:
        self.head("6. 其他进程对比（判断是否只有浏览器被加速）")
        curl = shutil.which("curl")
        if curl:
            null = "NUL" if IS_WIN else "/dev/null"
            out = run_cmd([curl, "-sS", "-m", "15", "--noproxy", "*", "-o", null,
                           "-w", "%{http_code} %{time_total}s", TEST_URL], 20)
            ok = out.strip().startswith("200")
            self.out(f"  curl（普通进程，直连）：{'✔' if ok else '✘'} {out.strip()[:200]}")
            self.res["curl_ok"] = ok
        else:
            self.out("  未找到 curl，跳过")
        try:
            sys.path.insert(0, HERE)
            from steam_free_finder import BrowserFetcher  # type: ignore
        except Exception as e:  # noqa: BLE001
            self.out(f"  无法导入 steam_free_finder（{e}），跳过浏览器测试")
            return
        browsers = BrowserFetcher.find_browsers()
        self.res["browsers_found"] = bool(browsers)
        self.out(f"  找到的浏览器：{browsers or '无'}")
        self.res["browser_ok"] = False
        for exe in browsers[:2]:
            bf = BrowserFetcher(browsers=[exe])
            t0 = time.time()
            try:
                dom = bf.fetch_dom(TEST_URL, timeout=30)
                data = BrowserFetcher.extract_json(dom)
                ok = bool(data.get("10", {}).get("success"))
                self.out(f"  无头 {os.path.basename(exe)}：{'✔' if ok else '✘'} "
                         f"{time.time() - t0:.1f}s，取到 JSON={ok}，DOM {len(dom)} 字符")
                self.res["browser_ok"] = self.res["browser_ok"] or ok
                if ok:
                    self.res["browser_name"] = os.path.basename(exe)
            except Exception as e:  # noqa: BLE001
                self.out(f"  无头 {os.path.basename(exe)}：✘ {time.time() - t0:.1f}s，{e}")

    # 7 加速器痕迹
    def accelerator(self) -> None:
        self.head("7. 加速器 / VPN 痕迹")
        procs = [p for p in self.res.get("procs", []) if SUSPECT_PROC.search(p)]
        self.out(f"  疑似加速器/代理进程：{procs or '未发现'}")
        if IS_WIN:
            ipcfg = run_cmd(["ipconfig", "/all"], 10)
            descs = re.findall(r"(?:描述|Description)[ .]*:\s*(.+)", ipcfg)
            vnics = [d.strip() for d in descs if re.search(r"tap|tun|leigod|雷神|wireguard|vpn|virtual|虚拟", d, re.I)]
            self.out(f"  虚拟网卡：{vnics or '未发现'}")
            rp = run_cmd(["route", "print", "-4"], 10)
            defaults = [ln.strip() for ln in rp.splitlines() if ln.strip().startswith("0.0.0.0")]
            self.out("  默认路由：" + ("; ".join(defaults) if defaults else "未读到"))

    # 结论
    def conclude(self) -> None:
        self.head("结论与建议")
        r = self.res
        tcp = r.get("tcp", {})
        any_tcp_ok = any(v == "TCP+TLS 正常" for v in tcp.values())
        tls_blocked = any(v == "TCP 通但 TLS 失败" for v in tcp.values())
        v4 = {ip: v for ip, v in tcp.items() if ":" not in ip}
        v6 = {ip: v for ip, v in tcp.items() if ":" in ip}
        tips = []
        if any_tcp_ok and not r.get("direct_ok") and "SSL" in r.get("direct_msg", ""):
            tips.append("⚠ 用系统证书库做 TLS 握手成功，但 requests 报 SSL 错误 → 很可能有加速器/安全软件在做 HTTPS 中间人，\n"
                        "   其根证书只装在系统里而 requests 用的是自带证书包。解法：pip install truststore （主程序已支持自动使用）。")
        if r.get("direct_ok"):
            tips.append("✔ Python 直连其实是通的。主程序若仍超时，多半是偶发/超时太短：再扫一次；"
                        "或把「网络」选「直连」避免先去试别的线路。")
        if r.get("working_proxies"):
            p = r["working_proxies"][0]
            tips.append(f"✔ 找到能通 Steam 的本地代理：{p}\n   → 主程序「代理地址」填 {p.split('://', 1)[1]}，「网络」选「自定义」。")
        if not r.get("direct_ok") and not r.get("working_proxies"):
            if r.get("browser_ok"):
                tips.append(f"✔ 无头 {r.get('browser_name', '浏览器')} 能拿到数据，而 Python/curl 不行 → 加速器是「进程模式」，只放行浏览器和 Steam。\n"
                            "   → 主程序「网络」选「浏览器」即可；或在加速器里把加速模式改成「路由模式」后用直连。")
            elif r.get("curl_ok"):
                tips.append("⚠ curl 能通但 Python 不行 → 问题在 Python 侧（代理/SSL 库）。把本报告发给 Claude。")
            elif not r.get("browsers_found"):
                tips.append("✘ Python 和 curl 都连不上 Steam，而且没找到 Edge/Chrome 可供「浏览器」线路使用。\n"
                            "   → 在加速器里把 Steam 商店的加速模式改成「路由模式」（雷神：「启动游戏」旁 ⚙ → 模式3），或安装 Edge/Chrome。")
            else:
                tips.append("✘ 本机所有普通进程（Python、curl、无头浏览器）都连不上 Steam。\n"
                            "   → 加速器只放行了它认定的浏览器窗口进程。最可靠的解法：在加速器里把 Steam 商店的加速模式改成「路由模式」\n"
                            "     （雷神：「启动游戏」旁边 ⚙ → 加速模式 → 模式3 路由模式），重新加速后主程序选「直连」。")
        if tls_blocked and not any_tcp_ok:
            tips.append("⚠ TCP 能连上但 TLS 握手失败 → 典型的 SNI 干扰/阻断，必须走加速器或代理。")
        if v6 and v4 and all(v != "TCP+TLS 正常" for v in v6.values()) and any(v == "TCP+TLS 正常" for v in v4.values()):
            tips.append("⚠ IPv6 地址全部失败、IPv4 正常 → 建议在网络适配器里关闭 IPv6，或告诉 Claude 在主程序里加「仅 IPv4」开关。")
        if not r.get("ips"):
            tips.append("✘ DNS 解析失败 → 检查 DNS 设置，或 hosts 被修改。")
        for t in tips or ["未能自动判断，请把上面的完整报告发给 Claude。"]:
            self.out(t)
        self.out("")
        self.out(f"报告已保存：{REPORT_FILE}")

    def run_all(self) -> str:
        steps = [self.env, self.proxy_settings, self.dns, self.tcp_tls, self.http,
                 self.local_ports, self.compare, self.accelerator, self.conclude]
        self.out("Steam 免费入库雷达 · 网络诊断报告")
        for step in steps:
            try:
                step()
            except Exception as e:  # noqa: BLE001
                self.out(f"  [{step.__name__} 出错：{type(e).__name__}: {e}]")
        report = "\n".join(self.lines)
        try:
            with open(REPORT_FILE, "w", encoding="utf-8") as f:
                f.write(report)
        except OSError:
            pass
        return report


# ─────────────────────────────── 界面 ────────────────────────────────

def run_gui() -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title("网络诊断 — Steam 免费入库雷达")
    root.geometry("960x680")
    font = ("Consolas", 10) if IS_WIN else ("Menlo", 11) if sys.platform == "darwin" else ("DejaVu Sans Mono", 10)
    ui_font = ("Microsoft YaHei UI", 10) if IS_WIN else None
    if ui_font:
        root.option_add("*Font", ui_font)

    top = ttk.Frame(root, padding=(10, 8))
    top.pack(fill="x")
    status = tk.StringVar(value="正在诊断，大约需要 1～3 分钟……")
    ttk.Label(top, textvariable=status).pack(side="left")
    pb = ttk.Progressbar(top, mode="indeterminate", length=180)
    pb.pack(side="right")
    pb.start(12)

    body = ttk.Frame(root, padding=(10, 0))
    body.pack(fill="both", expand=True)
    text = tk.Text(body, wrap="word", font=font, state="disabled", padx=8, pady=6)
    sb = ttk.Scrollbar(body, command=text.yview)
    text.configure(yscrollcommand=sb.set)
    text.pack(side="left", fill="both", expand=True)
    sb.pack(side="right", fill="y")

    btns = ttk.Frame(root, padding=(10, 8))
    btns.pack(fill="x")
    state = {"report": ""}

    def copy_report() -> None:
        root.clipboard_clear()
        root.clipboard_append(state["report"] or text.get("1.0", "end"))
        status.set("已复制到剪贴板，直接粘贴给 Claude 即可")

    def open_folder() -> None:
        try:
            if IS_WIN:
                os.startfile(HERE)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", HERE])
            else:
                subprocess.Popen(["xdg-open", HERE])
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("打开失败", str(e))

    ttk.Button(btns, text="复制报告", command=copy_report).pack(side="left")
    ttk.Button(btns, text="打开报告所在文件夹", command=open_folder).pack(side="left", padx=6)
    ttk.Button(btns, text="关闭", command=root.destroy).pack(side="right")

    def append(line: str) -> None:
        text.configure(state="normal")
        text.insert("end", line + "\n")
        text.see("end")
        text.configure(state="disabled")

    def worker() -> None:
        diag = Diagnostics(lambda s: root.after(0, append, s))
        report = diag.run_all()

        def done() -> None:
            state["report"] = report
            pb.stop()
            pb.pack_forget()
            status.set("诊断完成。点「复制报告」把全文发给 Claude。")

        root.after(0, done)

    threading.Thread(target=worker, daemon=True).start()
    root.mainloop()


def main() -> None:
    if "--console" in sys.argv or os.environ.get("STEAM_DEBUG_CONSOLE"):
        Diagnostics(print).run_all()
        return
    try:
        run_gui()
    except Exception as e:  # noqa: BLE001  图形界面不可用时退回命令行
        print(f"图形界面不可用（{e}），改用命令行输出：\n")
        Diagnostics(print).run_all()


if __name__ == "__main__":
    main()
