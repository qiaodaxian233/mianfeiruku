#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam 免费入库雷达 · Steam Free-to-Keep Finder
================================================
自动查找 Steam 商店中正在进行「限时 100% 折扣（免费入库）」活动的游戏和 DLC，
拉取好评率与评测数量，按好评排序并自动分类，带图形界面。

功能
- 一键扫描 / 定时自动扫描（默认每 30 分钟）
- 自动补全：好评率、评测数、评价等级、类型（游戏/DLC…）、游戏类别、原价、发行日期
- 三种自动分类：评价等级 / 内容类型 / 游戏类别，点击表头排序
- 过滤：搜索、最低好评率、最少评测数、仅游戏、隐藏已领取
- 发现新的免费入库时高亮 + 响铃 + 弹窗提醒
- 双击打开商店页；右键：Steam 客户端打开 / 复制链接 / 标记已领取
- 导出 CSV；已领取记录、设置自动保存在同目录的 steam_free_finder_data.json
- 网络线路：自动（直连 → 系统代理 → 自定义代理）/ 直连 / 系统代理 / 自定义，
  兼容开着加速器、VPN 的情况；自动修正 Windows 系统代理的 https:// 前缀问题

版本：1.1（2026-09-10）

依赖
    pip install requests beautifulsoup4
    pip install pillow      # 可选：显示游戏封面

运行
    python steam_free_finder.py
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


def _fatal(msg: str) -> None:
    print(msg, file=sys.stderr)
    try:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("缺少依赖", msg)
    except Exception:
        pass
    sys.exit(1)


try:
    import requests
except ImportError:  # pragma: no cover
    _fatal("缺少 requests 库，请先在命令行运行：\n\n    pip install requests beautifulsoup4")

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    _fatal("缺少 beautifulsoup4 库，请先在命令行运行：\n\n    pip install requests beautifulsoup4")

try:
    from PIL import Image, ImageTk

    HAS_PIL = True
except ImportError:
    HAS_PIL = False


# ─────────────────────────────── 常量 ────────────────────────────────

APP_NAME = "Steam 免费入库雷达"
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "steam_free_finder_data.json")
REGIONS = ("cn", "hk", "tw", "us", "jp", "uk", "de", "ru", "ar", "tr")

# Steam appreviews 接口的 review_score → 中文评价等级
REVIEW_SCORE_LABELS = {
    9: "好评如潮", 8: "特别好评", 7: "好评", 6: "多半好评", 5: "褒贬不一",
    4: "多半差评", 3: "差评", 2: "特别差评", 1: "差评如潮", 0: "评测不足",
}
TIER_ORDER = ["好评如潮", "特别好评", "好评", "多半好评", "褒贬不一",
              "多半差评", "差评", "特别差评", "差评如潮", "评测不足", "未知"]

TYPE_LABELS = {
    "game": "游戏", "dlc": "DLC", "demo": "演示版", "software": "软件", "music": "原声音乐",
    "video": "视频", "mod": "模组", "episode": "章节", "series": "系列", "unknown": "未知",
}
TYPE_ORDER = ["游戏", "DLC", "软件", "演示版", "原声音乐", "视频", "模组", "章节", "系列", "未知"]


# ─────────────────────────────── 工具函数 ────────────────────────────────

def default_font() -> tuple:
    if sys.platform.startswith("win"):
        return ("Microsoft YaHei UI", 10)
    if sys.platform == "darwin":
        return ("PingFang SC", 13)
    return ("Noto Sans CJK SC", 10)


def load_state() -> dict:
    state = {"claimed": [], "seen": {}, "settings": {}}
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            saved = json.load(f)
        for key, default in state.items():
            if isinstance(saved.get(key), type(default)):
                state[key] = saved[key]
    except (OSError, ValueError):
        pass
    return state


def save_state(state: dict) -> None:
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"保存数据失败: {e}", file=sys.stderr)


def open_external(url: str) -> bool:
    """打开网页或 steam:// 链接。"""
    if url.startswith("steam://"):
        try:
            if sys.platform.startswith("win"):
                os.startfile(url)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", url])
            else:
                subprocess.Popen(["xdg-open", url])
            return True
        except Exception:
            return False
    return webbrowser.open(url)


def price_number(text: str) -> float:
    """把 '¥ 1,234.00' / 'HK$ 88' / '12,99€' 之类的价格文本转成数字用于排序。"""
    s = re.sub(r"[^\d.,]", "", text or "")
    if not s:
        return 0.0
    if "," in s and "." in s:
        s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) == 2 else s.replace(",", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def normalize_proxy(url: str) -> str:
    """'127.0.0.1:7890' → 'http://127.0.0.1:7890'；'https://…' → 'http://…'（Windows 系统代理常见写法，
    urllib3 会误以为要和代理服务器做 TLS 握手，导致 SSLEOFError）。socks5:// 保持原样。"""
    u = (url or "").strip()
    if not u:
        return ""
    if u.startswith("https://"):
        u = "http://" + u[len("https://"):]
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", u):
        u = "http://" + u
    return u


def system_proxies() -> dict:
    """读取系统代理（Windows 注册表 / 环境变量），并修正 https:// 前缀。"""
    import urllib.request
    raw = urllib.request.getproxies()
    out = {}
    for scheme in ("http", "https"):
        v = raw.get(scheme) or raw.get("http") or raw.get("https")
        if v:
            out[scheme] = normalize_proxy(v)
    return out


def short_error(e: Exception) -> str:
    """把 requests 的长串错误压缩成一句话。"""
    if isinstance(e, requests.exceptions.ProxyError):
        return "无法连接代理服务器"
    if isinstance(e, requests.exceptions.ConnectTimeout):
        return "连接超时"
    if isinstance(e, requests.exceptions.ReadTimeout):
        return "读取超时"
    if isinstance(e, requests.exceptions.SSLError):
        return "SSL 握手失败"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "连接失败（可能被拦截或无网络）"
    if isinstance(e, requests.exceptions.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code}"
    s = str(e)
    return s if len(s) <= 120 else s[:117] + "…"


# ─────────────────────────────── Steam 接口 ────────────────────────────────

class SteamClient:
    """封装几个无需登录的 Steam 商店公开接口。"""

    SEARCH_URL = "https://store.steampowered.com/search/results/"
    APPDETAILS_URL = "https://store.steampowered.com/api/appdetails"
    REVIEWS_URL = "https://store.steampowered.com/appreviews/{appid}"

    PROXY_MODES = ("自动", "直连", "系统代理", "自定义")

    def __init__(self, cc: str = "cn", lang: str = "schinese", proxy_mode: str = "自动",
                 proxy_url: str = "", timeout=(8, 25)):
        self.cc = cc
        self.lang = lang
        self.timeout = timeout
        self.session = requests.Session()
        # 不让 requests 自动读系统代理，由下面的“线路候选”统一控制
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        })
        # 绕过年龄验证 / 成人内容确认页
        self.session.cookies.update({
            "birthtime": "568022401",
            "lastagecheckage": "1-0-1988",
            "mature_content": "1",
            "Steam_Language": lang,
        })
        self._candidates = self._build_routes(proxy_mode, proxy_url)
        self._route_idx = 0
        self._route_locked = False
        self.route_desc = self._candidates[0][0]

    @staticmethod
    def _build_routes(mode: str, custom: str) -> list[tuple[str, dict]]:
        """返回 [(线路名称, proxies字典), …]，按尝试顺序排列。"""
        direct = ("直连", {})
        sys_p = system_proxies()
        sys_route = (f"系统代理 {sys_p.get('https', '')}", sys_p) if sys_p else None
        cu = normalize_proxy(custom)
        custom_route = (f"代理 {cu}", {"http": cu, "https": cu}) if cu else None

        if mode == "直连":
            return [direct]
        if mode == "系统代理":
            return [sys_route] if sys_route else [direct]
        if mode == "自定义":
            return [custom_route] if custom_route else [direct]
        # 自动：直连 → 系统代理 → 自定义代理，去重
        routes = [direct]
        for r in (sys_route, custom_route):
            if r and all(r[1] != x[1] for x in routes):
                routes.append(r)
        return routes

    def _get(self, url: str, params: dict | None = None, retries: int = 2) -> requests.Response:
        """依次尝试各条线路；某条线路一旦成功就固定使用它。"""
        if self._route_locked:
            routes = [(self._route_idx, self._candidates[self._route_idx])]
        else:
            routes = list(enumerate(self._candidates))[self._route_idx:]

        errors: list[str] = []
        for idx, (desc, proxies) in routes:
            last_err: Exception | None = None
            for attempt in range(retries):
                try:
                    r = self.session.get(url, params=params, timeout=self.timeout, proxies=proxies)
                    if r.status_code == 429:
                        last_err = RuntimeError("HTTP 429（请求过于频繁，已自动等待）")
                        time.sleep(5 * (attempt + 1))
                        continue
                    r.raise_for_status()
                    self._route_idx, self._route_locked, self.route_desc = idx, True, desc
                    return r
                except requests.RequestException as e:
                    last_err = e
                    time.sleep(1.0 * (attempt + 1))
            errors.append(f"{desc}：{short_error(last_err) if last_err else '未知错误'}")
        raise RuntimeError("\n".join(errors))

    # ---------- 1) 搜索页：找出「限时免费 / 100% 折扣」的项目 ----------

    def search_free_to_keep(self, progress=None, max_pages: int = 10) -> list[dict]:
        items: list[dict] = []
        start, page_size = 0, 50
        for _ in range(max_pages):
            params = {
                "query": "", "start": start, "count": page_size, "dynamic_data": "",
                "sort_by": "_ASC", "maxprice": "free", "specials": 1, "infinite": 1,
                "cc": self.cc, "l": self.lang,
            }
            data = self._get(self.SEARCH_URL, params).json()
            rows = self.parse_search_rows(data.get("results_html", ""))
            items.extend(rows)
            total = int(data.get("total_count") or 0)
            start += page_size
            if progress:
                progress(f"已扫描 {min(start, total)}/{total} 条搜索结果…")
            if not rows or start >= total:
                break
            time.sleep(0.6)

        seen: set[int] = set()
        result: list[dict] = []
        for it in items:
            if it["appid"] in seen:
                continue
            seen.add(it["appid"])
            is_100_off = it["discount"] == 100
            fallback = it["discount"] is None and it["final_price"] == 0 and bool(it["original_price"])
            if is_100_off or fallback:
                result.append(it)
        return result

    @staticmethod
    def parse_search_rows(html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        rows: list[dict] = []
        for a in soup.select("a.search_result_row"):
            appid_raw = (a.get("data-ds-appid") or "").split(",")[0].strip()
            if not appid_raw.isdigit():
                continue
            appid = int(appid_raw)

            title_el = a.select_one(".title")
            name = title_el.get_text(strip=True) if title_el else f"App {appid}"

            # 折扣百分比：优先 data-discount 属性，其次 "-100%" 文本
            discount = None
            block = a.select_one("[data-discount]")
            if block is not None:
                try:
                    discount = int(block["data-discount"])
                except (ValueError, TypeError, KeyError):
                    pass
            if discount is None:
                pct_el = a.select_one(".discount_pct") or a.select_one(".search_discount")
                if pct_el:
                    m = re.search(r"(\d+)\s*%", pct_el.get_text())
                    if m:
                        discount = int(m.group(1))

            final_price = None
            pf = a.select_one("[data-price-final]")
            if pf is not None:
                try:
                    final_price = int(pf["data-price-final"])
                except (ValueError, TypeError, KeyError):
                    pass

            orig_el = a.select_one(".discount_original_price") or a.select_one(".search_price strike")
            original_price = orig_el.get_text(strip=True) if orig_el else ""
            rel_el = a.select_one(".search_released")
            release = rel_el.get_text(strip=True) if rel_el else ""

            # 搜索页自带的评测摘要（appreviews 接口失败时的兜底）
            percent = total = None
            label = ""
            rs = a.select_one(".search_review_summary")
            if rs is not None:
                tip = rs.get("data-tooltip-html", "") or ""
                label = re.split(r"<br\s*/?>", tip)[0].strip()
                m = re.search(r"(\d{1,3})\s*%", tip)
                if m:
                    percent = int(m.group(1))
                m = (re.search(r"(\d[\d,]*)\s*(?:篇|條|则|user reviews)", tip)
                     or re.search(r"of the\s+(\d[\d,]*)", tip))
                if m:
                    total = int(m.group(1).replace(",", ""))

            plats = [n for cls, n in (("win", "Win"), ("mac", "Mac"), ("linux", "Linux"))
                     if a.select_one(f".platform_img.{cls}")]
            href = (a.get("href") or "").split("?")[0]

            rows.append({
                "appid": appid, "name": name,
                "url": href or f"https://store.steampowered.com/app/{appid}/",
                "discount": discount, "final_price": final_price, "original_price": original_price,
                "release": release, "platforms": "/".join(plats),
                "percent": percent, "total": total, "positive": None, "negative": None,
                "review_score": None, "review_label": label or "未知",
                "type": "unknown", "type_label": "未知", "genres": [], "categories": [],
                "short_desc": "", "header_image": "", "fullgame": "", "error": "",
            })
        return rows

    # ---------- 2) 补全评测统计 + 商店详情 ----------

    def enrich(self, item: dict) -> None:
        appid = item["appid"]
        try:
            q = self._get(self.REVIEWS_URL.format(appid=appid), {
                "json": 1, "language": "all", "purchase_type": "all", "num_per_page": 0,
            }).json().get("query_summary", {})
            pos = int(q.get("total_positive") or 0)
            neg = int(q.get("total_negative") or 0)
            tot = int(q.get("total_reviews") or (pos + neg))
            score = int(q.get("review_score") or 0)
            item.update(
                positive=pos, negative=neg, total=tot,
                percent=round(pos * 100 / tot, 1) if tot else None,
                review_score=score,
            )
            if tot < 10:
                item["review_label"] = "评测不足"
            else:
                item["review_label"] = REVIEW_SCORE_LABELS.get(score) or q.get("review_score_desc") or "未知"
        except Exception as e:
            item["error"] += f"评测数据获取失败：{e}；"

        time.sleep(0.2)

        try:
            payload = self._get(self.APPDETAILS_URL, {"appids": appid, "cc": self.cc, "l": self.lang}).json()
            entry = payload.get(str(appid)) or {}
            data = entry.get("data") if entry.get("success") else None
            if data:
                t = data.get("type") or "unknown"
                item["type"] = t
                item["type_label"] = TYPE_LABELS.get(t, t)
                item["genres"] = [g.get("description", "") for g in data.get("genres", []) if g.get("description")]
                item["categories"] = [c.get("description", "") for c in data.get("categories", []) if c.get("description")]
                item["short_desc"] = re.sub(r"<[^>]+>", "", data.get("short_description") or "").strip()
                item["header_image"] = data.get("header_image") or ""
                item["name"] = data.get("name") or item["name"]
                rd = data.get("release_date") or {}
                if rd.get("date"):
                    item["release"] = rd["date"]
                po = data.get("price_overview")
                if po and not item["original_price"]:
                    item["original_price"] = po.get("initial_formatted") or po.get("final_formatted") or ""
                if not item["platforms"]:
                    p = data.get("platforms") or {}
                    item["platforms"] = "/".join(
                        n for k, n in (("windows", "Win"), ("mac", "Mac"), ("linux", "Linux")) if p.get(k))
                if t == "dlc":
                    item["fullgame"] = (data.get("fullgame") or {}).get("name", "")
        except Exception as e:
            item["error"] += f"商店详情获取失败：{e}；"


# ─────────────────────────────── 图形界面 ────────────────────────────────

class App(tk.Tk):
    COLUMNS = (
        ("#0", "名称", 330, "w"),
        ("percent", "好评率", 75, "center"),
        ("total", "评测数", 85, "e"),
        ("label", "评价", 95, "center"),
        ("type", "类型", 75, "center"),
        ("genres", "类别", 200, "w"),
        ("orig", "原价", 90, "e"),
        ("release", "发行日期", 110, "center"),
        ("status", "状态", 80, "center"),
    )
    GROUP_MODES = ("评价等级", "内容类型", "游戏类别", "不分组")

    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} — 自动查找 Steam 限时免费入库，按好评分类")
        self.geometry("1250x780")
        self.minsize(960, 600)

        self.state_data = load_state()
        self.claimed: set[int] = set(self.state_data["claimed"])
        self.seen: dict[str, str] = self.state_data["seen"]
        settings = self.state_data["settings"]

        self.items: list[dict] = []
        self.new_ids: set[int] = set()
        self._sort: tuple[str, bool] = ("percent", True)
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self._auto_job: str | None = None
        self._manual = True
        self._img_cache: dict[int, bytes] = {}
        self._photo = None
        self._selected_appid: int | None = None
        self.client: SteamClient | None = None

        self._setup_style()
        self._build_ui(settings)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(300, self.refresh)

    # ---------- 样式 & 布局 ----------

    def _setup_style(self) -> None:
        style = ttk.Style(self)
        themes = style.theme_names()
        for name in ("vista", "aqua", "clam"):
            if name in themes:
                style.theme_use(name)
                break
        f = default_font()
        self._font = f
        self._bold = (f[0], f[1], "bold")
        self.option_add("*Font", f)
        style.configure(".", font=f)
        style.configure("Treeview", rowheight=26, font=f)
        style.configure("Treeview.Heading", font=self._bold)

    def _build_ui(self, s: dict) -> None:
        # ── 第一行：操作 & 扫描设置 ──
        bar = ttk.Frame(self, padding=(10, 8, 10, 2))
        bar.pack(fill="x")
        self.btn_refresh = ttk.Button(bar, text="⟳ 立即扫描", command=self.refresh)
        self.btn_refresh.pack(side="left")
        self.btn_stop = ttk.Button(bar, text="停止", command=self.stop, state="disabled")
        self.btn_stop.pack(side="left", padx=(4, 0))
        ttk.Separator(bar, orient="vertical").pack(side="left", fill="y", padx=10)
        self.var_auto = tk.BooleanVar(value=bool(s.get("auto", True)))
        ttk.Checkbutton(bar, text="自动扫描，每", variable=self.var_auto,
                        command=self._schedule_auto).pack(side="left")
        self.var_interval = tk.IntVar(value=int(s.get("interval", 30)))
        ttk.Spinbox(bar, from_=5, to=720, increment=5, textvariable=self.var_interval, width=5,
                    command=self._schedule_auto).pack(side="left", padx=4)
        ttk.Label(bar, text="分钟").pack(side="left")
        self.var_popup = tk.BooleanVar(value=bool(s.get("popup", True)))
        ttk.Checkbutton(bar, text="发现新免费时弹窗提醒", variable=self.var_popup).pack(side="left", padx=(12, 0))
        ttk.Button(bar, text="导出 CSV", command=self.export_csv).pack(side="right")
        ttk.Button(bar, text="打开全部未领取", command=self.open_all_unclaimed).pack(side="right", padx=(0, 6))

        # ── 第二行：地区 & 网络 ──
        sb2 = ttk.Frame(self, padding=(10, 4, 10, 2))
        sb2.pack(fill="x")
        ttk.Label(sb2, text="地区").pack(side="left")
        self.var_cc = tk.StringVar(value=s.get("cc", "cn"))
        cb_cc = ttk.Combobox(sb2, textvariable=self.var_cc, values=REGIONS, width=4, state="readonly")
        cb_cc.pack(side="left", padx=(4, 0))
        cb_cc.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Separator(sb2, orient="vertical").pack(side="left", fill="y", padx=10)

        ttk.Label(sb2, text="网络").pack(side="left")
        self.var_proxy_mode = tk.StringVar(value=s.get("proxy_mode", "自动"))
        cb_net = ttk.Combobox(sb2, textvariable=self.var_proxy_mode, values=SteamClient.PROXY_MODES,
                              width=7, state="readonly")
        cb_net.pack(side="left", padx=(4, 0))
        cb_net.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        ttk.Label(sb2, text="代理地址").pack(side="left", padx=(8, 0))
        self.var_proxy_url = tk.StringVar(value=s.get("proxy_url", ""))
        ent_proxy = ttk.Entry(sb2, textvariable=self.var_proxy_url, width=24)
        ent_proxy.pack(side="left", padx=(4, 0))
        ent_proxy.bind("<Return>", lambda e: self.refresh())
        ttk.Label(sb2, text="例如 127.0.0.1:7890（开着加速器/VPN 时填它的 HTTP 端口）",
                  foreground="#888888").pack(side="left", padx=(6, 0))

        # ── 过滤栏 ──
        fb = ttk.Frame(self, padding=(10, 4, 10, 6))
        fb.pack(fill="x")
        ttk.Label(fb, text="分类方式").pack(side="left")
        self.var_group = tk.StringVar(value=s.get("group", "评价等级"))
        cb_group = ttk.Combobox(fb, textvariable=self.var_group, values=self.GROUP_MODES, width=9, state="readonly")
        cb_group.pack(side="left", padx=(4, 12))
        cb_group.bind("<<ComboboxSelected>>", self._apply_filters)

        ttk.Label(fb, text="搜索").pack(side="left")
        self.var_search = tk.StringVar()
        self.var_search.trace_add("write", self._apply_filters)
        ttk.Entry(fb, textvariable=self.var_search, width=22).pack(side="left", padx=(4, 12))

        ttk.Label(fb, text="好评率 ≥").pack(side="left")
        self.var_min_pct = tk.IntVar(value=0)
        sp1 = ttk.Spinbox(fb, from_=0, to=100, increment=5, textvariable=self.var_min_pct, width=4,
                          command=self._apply_filters)
        sp1.pack(side="left", padx=(4, 2))
        sp1.bind("<KeyRelease>", self._apply_filters)
        ttk.Label(fb, text="%    评测数 ≥").pack(side="left")
        self.var_min_rev = tk.IntVar(value=0)
        sp2 = ttk.Spinbox(fb, from_=0, to=1_000_000, increment=50, textvariable=self.var_min_rev, width=7,
                          command=self._apply_filters)
        sp2.pack(side="left", padx=(4, 12))
        sp2.bind("<KeyRelease>", self._apply_filters)

        self.var_only_games = tk.BooleanVar(value=False)
        ttk.Checkbutton(fb, text="仅游戏（隐藏 DLC 等）", variable=self.var_only_games,
                        command=self._apply_filters).pack(side="left")
        self.var_hide_claimed = tk.BooleanVar(value=False)
        ttk.Checkbutton(fb, text="隐藏已领取", variable=self.var_hide_claimed,
                        command=self._apply_filters).pack(side="left", padx=(12, 0))
        self.var_count = tk.StringVar(value="")
        ttk.Label(fb, textvariable=self.var_count, foreground="#555555").pack(side="right")

        # ── 主区域：列表 + 详情 ──
        pw = ttk.Panedwindow(self, orient="vertical")
        pw.pack(fill="both", expand=True, padx=10)

        tf = ttk.Frame(pw)
        pw.add(tf, weight=5)
        cols = [c for c, *_ in self.COLUMNS if c != "#0"]
        self.tree = ttk.Treeview(tf, columns=cols, show="tree headings", selectmode="browse")
        for col, text, width, anchor in self.COLUMNS:
            self.tree.heading(col, text=text, anchor="w" if col == "#0" else "center",
                              command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=width, minwidth=50, anchor=anchor, stretch=(col in ("#0", "genres")))
        vsb = ttk.Scrollbar(tf, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tf, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        tf.rowconfigure(0, weight=1)
        tf.columnconfigure(0, weight=1)

        self.tree.tag_configure("group", font=self._bold)
        self.tree.tag_configure("claimed", foreground="#9a9a9a")
        self.tree.tag_configure("great", foreground="#1a7f37")
        self.tree.tag_configure("bad", foreground="#b3261e")
        self.tree.tag_configure("new", background="#fff3c4")

        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda e: self.open_store())
        self.tree.bind("<Return>", lambda e: self.open_store())
        self.tree.bind("<space>", lambda e: self.toggle_claimed())
        self.tree.bind("<Button-3>", self._popup_menu)
        self.tree.bind("<Button-2>", self._popup_menu)  # macOS 右键

        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="打开商店页面（浏览器）", command=self.open_store)
        self.menu.add_command(label="在 Steam 客户端中打开", command=self.open_in_steam)
        self.menu.add_command(label="复制链接", command=self.copy_link)
        self.menu.add_separator()
        self.menu.add_command(label="标记已领取 / 取消标记", command=self.toggle_claimed)

        df = ttk.Frame(pw, padding=(0, 8, 0, 0))
        pw.add(df, weight=2)
        self.img_label = ttk.Label(df, anchor="n", width=34, foreground="#888888")
        self.img_label.pack(side="left", padx=(0, 10), anchor="n")
        right = ttk.Frame(df)
        right.pack(side="left", fill="both", expand=True)
        self.detail = tk.Text(right, height=7, wrap="word", relief="flat", padx=6, pady=4,
                              state="disabled", cursor="arrow", font=self._font)
        self.detail.pack(fill="both", expand=True)
        self.detail.tag_configure("h", font=self._bold)
        self.detail.tag_configure("dim", foreground="#666666")
        self.detail.tag_configure("link", foreground="#1a5fb4", underline=True)
        self.detail.tag_bind("link", "<Button-1>", lambda e: self.open_store())
        self.detail.tag_bind("link", "<Enter>", lambda e: self.detail.configure(cursor="hand2"))
        self.detail.tag_bind("link", "<Leave>", lambda e: self.detail.configure(cursor="arrow"))

        btns = ttk.Frame(right)
        btns.pack(fill="x", pady=(4, 0))
        ttk.Button(btns, text="打开商店页", command=self.open_store).pack(side="left")
        ttk.Button(btns, text="Steam 客户端打开", command=self.open_in_steam).pack(side="left", padx=4)
        ttk.Button(btns, text="复制链接", command=self.copy_link).pack(side="left")
        self.btn_claim = ttk.Button(btns, text="✔ 标记已领取", command=self.toggle_claimed)
        self.btn_claim.pack(side="left", padx=(12, 0))

        # ── 状态栏 ──
        sb = ttk.Frame(self, padding=(10, 4, 10, 8))
        sb.pack(fill="x")
        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(sb, textvariable=self.var_status).pack(side="left")
        self.var_last = tk.StringVar(value="")
        ttk.Label(sb, textvariable=self.var_last, foreground="#666666").pack(side="right")
        self.progress = ttk.Progressbar(sb, length=220, mode="determinate")
        self.progress.pack(side="right", padx=10)

        self._show_detail(None)

    # ---------- 扫描（后台线程） ----------

    def refresh(self, manual: bool = True) -> None:
        if self._worker and self._worker.is_alive():
            return
        self._manual = manual
        self._stop.clear()
        client = SteamClient(cc=self.var_cc.get() or "cn",
                             proxy_mode=self.var_proxy_mode.get() or "自动",
                             proxy_url=self.var_proxy_url.get())
        self.client = client
        self.btn_refresh.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.var_status.set("正在从 Steam 商店获取限时免费列表…")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self._worker = threading.Thread(target=self._do_refresh, args=(client,), daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
        self.var_status.set("正在停止…")

    def _ui(self, fn, *args) -> None:
        """从工作线程安全地调用界面方法。"""
        self.after(0, lambda: fn(*args))

    def _do_refresh(self, client: SteamClient) -> None:
        try:
            base = client.search_free_to_keep(progress=lambda msg: self._ui(self.var_status.set, msg))
            n = len(base)
            self._ui(self._progress_setup, n)
            results: list[dict] = []
            for i, it in enumerate(base, 1):
                if self._stop.is_set():
                    break
                self._ui(self.var_status.set, f"（{i}/{n}）获取评测与详情：{it['name']}")
                client.enrich(it)
                results.append(it)
                self._ui(self._progress_step)
                time.sleep(0.25)
            self._ui(self._on_refresh_done, results, None)
        except Exception as e:  # 网络错误等
            self._ui(self._on_refresh_done, None, e)

    def _progress_setup(self, n: int) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=max(n, 1), value=0)

    def _progress_step(self) -> None:
        self.progress.configure(value=float(self.progress["value"]) + 1)

    def _on_refresh_done(self, results: list[dict] | None, err: Exception | None) -> None:
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.btn_refresh.configure(state="normal")
        self.btn_stop.configure(state="disabled")

        if err is not None:
            first_line = str(err).splitlines()[0] if str(err) else "未知错误"
            self.var_status.set(f"扫描失败：{first_line}")
            if self._manual:
                tried = "\n".join(f"  • {line}" for line in str(err).splitlines())
                messagebox.showerror(
                    "扫描失败",
                    "无法连接 Steam 商店，已尝试的线路：\n" + tried + "\n\n"
                    "建议：\n"
                    "1. 开着加速器 / VPN 时，把它的 HTTP 代理端口填到「代理地址」\n"
                    "   （常见：127.0.0.1:7890、7897、10809），网络选「自定义」后再扫描。\n"
                    "2. 很多游戏加速器只加速游戏、不代理网页：可开启加速器的「浏览器/系统代理」模式，\n"
                    "   或先关掉加速器，网络选「直连」试试。",
                )
            self._schedule_auto()
            return

        now = datetime.now()
        first_run = not self.seen
        cutoff = now - timedelta(days=45)
        new_ids: set[int] = set()
        for it in results or []:
            key = str(it["appid"])
            prev = self.seen.get(key)
            try:
                is_new = prev is None or datetime.fromisoformat(prev) < cutoff
            except ValueError:
                is_new = True
            if is_new and not first_run:
                new_ids.add(it["appid"])
            self.seen[key] = now.isoformat(timespec="seconds")
        # 清理一年以上的旧记录
        keep_after = now - timedelta(days=365)
        pruned = {}
        for k, v in self.seen.items():
            try:
                if datetime.fromisoformat(v) >= keep_after:
                    pruned[k] = v
            except ValueError:
                pass
        self.seen = pruned
        self.state_data["seen"] = self.seen
        save_state(self.state_data)

        self.items = results or []
        self.new_ids = new_ids
        self.var_last.set(f"上次更新 {now:%m-%d %H:%M}")

        route = f"　线路：{self.client.route_desc}" if self.client else ""
        if not self.items:
            self.var_status.set("当前 Steam 没有正在进行的免费入库活动（100% 折扣）。稍后会自动再次检查。" + route)
        else:
            msg = f"共找到 {len(self.items)} 个免费入库项目"
            if new_ids:
                msg += f"，其中 {len(new_ids)} 个是新出现的！"
            if self._stop.is_set():
                msg += "（已手动停止，列表可能不完整）"
            self.var_status.set(msg + route)

        self._apply_filters()

        if new_ids:
            self.bell()
            if self.var_popup.get():
                names = [it["name"] for it in self.items if it["appid"] in new_ids]
                shown = "\n".join(f"• {n}" for n in names[:12])
                more = f"\n…共 {len(names)} 个" if len(names) > 12 else ""
                messagebox.showinfo("发现新的免费入库！", shown + more)

        self._schedule_auto()

    def _schedule_auto(self) -> None:
        if self._auto_job:
            self.after_cancel(self._auto_job)
            self._auto_job = None
        if self.var_auto.get():
            mins = max(1, self._int(self.var_interval, 30))
            self._auto_job = self.after(mins * 60 * 1000, self._auto_tick)

    def _auto_tick(self) -> None:
        self._auto_job = None
        if self._worker and self._worker.is_alive():
            self._schedule_auto()
            return
        self.refresh(manual=False)

    # ---------- 过滤 / 分类 / 排序 / 渲染 ----------

    @staticmethod
    def _int(var, default: int = 0) -> int:
        try:
            return int(var.get())
        except (tk.TclError, ValueError):
            return default

    def _filtered_items(self) -> list[dict]:
        q = self.var_search.get().strip().lower()
        min_pct = self._int(self.var_min_pct)
        min_rev = self._int(self.var_min_rev)
        only_games = self.var_only_games.get()
        hide_claimed = self.var_hide_claimed.get()
        out = []
        for it in self.items:
            if q and q not in it["name"].lower() and q not in " ".join(it["genres"]).lower():
                continue
            if only_games and it["type"] != "game":
                continue
            if hide_claimed and it["appid"] in self.claimed:
                continue
            pct = it["percent"]
            if min_pct > 0 and (pct is None or pct < min_pct):
                continue
            if min_rev > 0 and (it["total"] or 0) < min_rev:
                continue
            out.append(it)
        return out

    def _sort_key(self, it: dict):
        col = self._sort[0]
        pct = it["percent"] if it["percent"] is not None else -1
        tot = it["total"] or 0
        score = it["review_score"] if it["review_score"] is not None else -1
        if col == "percent":
            return (pct, tot)
        if col == "total":
            return (tot, pct)
        if col == "label":
            return (score, pct, tot)
        if col == "type":
            return it["type_label"]
        if col == "genres":
            return " ".join(it["genres"])
        if col == "orig":
            return price_number(it["original_price"])
        if col == "release":
            return it["release"]
        if col == "status":
            return (it["appid"] in self.claimed, it["appid"] in self.new_ids)
        return it["name"].lower()

    def _sort_by(self, col: str) -> None:
        cur, rev = self._sort
        if cur == col:
            self._sort = (col, not rev)
        else:
            self._sort = (col, col in ("percent", "total", "label", "orig"))
        self._apply_filters()

    def _group_key(self, it: dict) -> str:
        mode = self.var_group.get()
        if mode == "评价等级":
            return it["review_label"] or "未知"
        if mode == "内容类型":
            return it["type_label"] or "未知"
        if mode == "游戏类别":
            return it["genres"][0] if it["genres"] else "未分类"
        return ""

    def _ordered_groups(self, groups: dict[str, list[dict]]):
        mode = self.var_group.get()
        if mode == "评价等级":
            order = TIER_ORDER
        elif mode == "内容类型":
            order = TYPE_ORDER
        else:
            return sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        return sorted(groups.items(),
                      key=lambda kv: (order.index(kv[0]) if kv[0] in order else len(order), kv[0]))

    def _apply_filters(self, *_) -> None:
        shown = self._filtered_items()
        self._render(shown)
        self.var_count.set(f"显示 {len(shown)} / 共 {len(self.items)} 项")

    def _render(self, items: list[dict]) -> None:
        selected = self._selected_appid
        self.tree.delete(*self.tree.get_children())

        for col, text, *_ in self.COLUMNS:
            mark = ""
            if col == self._sort[0]:
                mark = " ▼" if self._sort[1] else " ▲"
            self.tree.heading(col, text=text + mark)

        items = sorted(items, key=self._sort_key, reverse=self._sort[1])
        groups: dict[str, list[dict]] = {}
        for it in items:
            groups.setdefault(self._group_key(it), []).append(it)

        for gname, gitems in self._ordered_groups(groups):
            parent = ""
            if gname:
                parent = self.tree.insert("", "end", text=f"{gname}（{len(gitems)}）", open=True, tags=("group",))
            for it in gitems:
                appid = it["appid"]
                claimed = appid in self.claimed
                is_new = appid in self.new_ids
                score = it["review_score"] or 0
                if claimed:
                    tags = ["claimed"]
                elif score >= 8:
                    tags = ["great"]
                elif 0 < score <= 4:
                    tags = ["bad"]
                else:
                    tags = []
                if is_new and not claimed:
                    tags.append("new")
                pct = f"{it['percent']:.0f}%" if it["percent"] is not None else "—"
                tot = f"{it['total']:,}" if it["total"] else "—"
                status = "✔ 已领取" if claimed else ("★ 新" if is_new else "")
                self.tree.insert(
                    parent, "end", iid=f"app_{appid}", text=it["name"], tags=tuple(tags),
                    values=(pct, tot, it["review_label"], it["type_label"], ", ".join(it["genres"]),
                            it["original_price"] or "—", it["release"] or "—", status),
                )

        iid = f"app_{selected}" if selected is not None else ""
        if iid and self.tree.exists(iid):
            self.tree.selection_set(iid)
            self.tree.see(iid)
        else:
            self._selected_appid = None
            self._show_detail(None)

    # ---------- 选中项 / 详情面板 ----------

    def _selected_item(self) -> dict | None:
        sel = self.tree.selection()
        if not sel or not sel[0].startswith("app_"):
            return None
        appid = int(sel[0][4:])
        return next((it for it in self.items if it["appid"] == appid), None)

    def _on_select(self, _event=None) -> None:
        it = self._selected_item()
        self._selected_appid = it["appid"] if it else None
        self._show_detail(it)

    def _show_detail(self, it: dict | None) -> None:
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        if it is None:
            self.detail.insert("end", "选择一个项目查看详情。双击打开 Steam 商店页面，在页面上点击「添加至账户」即可免费入库。", "dim")
            self.btn_claim.configure(text="✔ 标记已领取")
        else:
            claimed = it["appid"] in self.claimed
            self.detail.insert("end", it["name"] + "\n", "h")
            if it["percent"] is not None:
                rev = f"{it['percent']:.0f}% 好评"
                if it["total"]:
                    rev += f"（{it['total']:,} 篇评测：{it['positive'] or 0:,} 好评 / {it['negative'] or 0:,} 差评）"
            else:
                rev = "暂无评测数据"
            line = f"{it['review_label']}   {rev}   类型：{it['type_label']}"
            if it["fullgame"]:
                line += f"（本体：{it['fullgame']}）"
            if it["original_price"]:
                line += f"   原价 {it['original_price']} → 免费"
            self.detail.insert("end", line + "\n", "dim")
            meta = []
            if it["genres"]:
                meta.append("类别：" + " / ".join(it["genres"]))
            if it["categories"]:
                meta.append("特性：" + " / ".join(it["categories"][:6]))
            if it["platforms"]:
                meta.append("平台：" + it["platforms"])
            if it["release"]:
                meta.append("发行：" + it["release"])
            if meta:
                self.detail.insert("end", "    ".join(meta) + "\n", "dim")
            if it["short_desc"]:
                self.detail.insert("end", it["short_desc"] + "\n")
            self.detail.insert("end", it["url"], "link")
            if it["error"]:
                self.detail.insert("end", "\n⚠ " + it["error"], "dim")
            self.btn_claim.configure(text="↩ 取消已领取" if claimed else "✔ 标记已领取")
        self.detail.configure(state="disabled")
        self._show_image(it)

    def _show_image(self, it: dict | None) -> None:
        self._photo = None
        self.img_label.configure(image="", text="")
        if it is None:
            return
        if not HAS_PIL:
            self.img_label.configure(text="安装 pillow 后可显示封面\npip install pillow")
            return
        appid, url = it["appid"], it["header_image"]
        if not url:
            return
        if appid in self._img_cache:
            self._set_photo(self._img_cache[appid])
            return
        self.img_label.configure(text="加载封面…")
        sess = self.client.session if self.client else requests

        def work() -> None:
            try:
                data = sess.get(url, timeout=10).content
            except Exception:
                data = None
            self.after(0, lambda: self._img_loaded(appid, data))

        threading.Thread(target=work, daemon=True).start()

    def _img_loaded(self, appid: int, data: bytes | None) -> None:
        if data:
            self._img_cache[appid] = data
        if self._selected_appid == appid:
            self._set_photo(data)

    def _set_photo(self, data: bytes | None) -> None:
        if not data:
            self.img_label.configure(text="", image="")
            return
        try:
            img = Image.open(io.BytesIO(data))
            img.thumbnail((250, 118))
            self._photo = ImageTk.PhotoImage(img)
            self.img_label.configure(image=self._photo, text="")
        except Exception:
            self.img_label.configure(text="", image="")

    # ---------- 动作 ----------

    def _popup_menu(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid or not iid.startswith("app_"):
            return
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def open_store(self) -> None:
        it = self._selected_item()
        if it:
            open_external(it["url"])

    def open_in_steam(self) -> None:
        it = self._selected_item()
        if it and not open_external(f"steam://store/{it['appid']}"):
            open_external(it["url"])

    def copy_link(self) -> None:
        it = self._selected_item()
        if it:
            self.clipboard_clear()
            self.clipboard_append(it["url"])
            self.var_status.set("已复制链接：" + it["url"])

    def toggle_claimed(self) -> None:
        it = self._selected_item()
        if not it:
            return
        if it["appid"] in self.claimed:
            self.claimed.discard(it["appid"])
        else:
            self.claimed.add(it["appid"])
        self.state_data["claimed"] = sorted(self.claimed)
        save_state(self.state_data)
        self._apply_filters()
        if not self.tree.exists(f"app_{it['appid']}"):
            self._show_detail(None)

    def open_all_unclaimed(self) -> None:
        todo = [it for it in self._filtered_items() if it["appid"] not in self.claimed]
        if not todo:
            messagebox.showinfo(APP_NAME, "当前列表里没有未领取的项目。")
            return
        if len(todo) > 8 and not messagebox.askyesno(APP_NAME, f"将在浏览器中打开 {len(todo)} 个标签页，继续？"):
            return
        urls = [it["url"] for it in todo]

        def step() -> None:
            if urls:
                open_external(urls.pop(0))
                self.after(250, step)

        step()

    def export_csv(self) -> None:
        if not self.items:
            messagebox.showinfo(APP_NAME, "还没有可导出的数据，请先扫描。")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV 文件", "*.csv")],
            initialfile=f"steam_free_{datetime.now():%Y%m%d_%H%M}.csv")
        if not path:
            return
        rows = sorted(self._filtered_items(), key=self._sort_key, reverse=self._sort[1])
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["AppID", "名称", "类型", "好评率%", "评测数", "好评", "差评", "评价",
                            "类别", "原价", "发行日期", "平台", "已领取", "链接"])
                for it in rows:
                    w.writerow([
                        it["appid"], it["name"], it["type_label"],
                        "" if it["percent"] is None else it["percent"], it["total"] or "",
                        it["positive"] if it["positive"] is not None else "",
                        it["negative"] if it["negative"] is not None else "",
                        it["review_label"], " / ".join(it["genres"]), it["original_price"],
                        it["release"], it["platforms"], "是" if it["appid"] in self.claimed else "", it["url"],
                    ])
        except OSError as e:
            messagebox.showerror("导出失败", str(e))
            return
        self.var_status.set(f"已导出 {len(rows)} 条到 {path}")

    def _on_close(self) -> None:
        self.state_data["settings"] = {
            "cc": self.var_cc.get(), "auto": self.var_auto.get(),
            "interval": self._int(self.var_interval, 30), "popup": self.var_popup.get(),
            "group": self.var_group.get(),
            "proxy_mode": self.var_proxy_mode.get(), "proxy_url": self.var_proxy_url.get().strip(),
        }
        self.state_data["claimed"] = sorted(self.claimed)
        save_state(self.state_data)
        self._stop.set()
        self.destroy()


def main() -> None:
    if sys.platform.startswith("win"):
        try:  # 高分屏下文字更清晰
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
