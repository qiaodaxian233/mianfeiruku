# 对话记忆（给下一次对话的 Claude 和我自己看）

> 用法：在新对话里对 Claude 说
> 「请先读 https://raw.githubusercontent.com/qiaodaxian233/mianfeiruku/main/MEMORY.md
> 和 https://raw.githubusercontent.com/qiaodaxian233/mianfeiruku/main/steam_free_finder.py ，然后继续帮我改这个项目」。
> 每次改完项目，请同步更新本文件的「当前状态」和「版本记录」。

## 项目是什么

- 名称：Steam 免费入库雷达（仓库名 mianfeiruku）
- 目的：自动找出 Steam 上正在进行「限时 100% 折扣 / 免费入库」的游戏和 DLC，按好评率排序、自动分类，方便快速领取。
- 形态：单文件 Python 程序 `steam_free_finder.py`，Tkinter 图形界面，依赖 requests + beautifulsoup4（pillow 可选，显示封面）。
- 用户环境：Windows，简体中文，日常开着游戏加速器；希望工具「有 UI、Python、自动分类、优先好评多的」。

## 文件

| 文件 | 作用 |
|---|---|
| `steam_free_finder.py` | 全部代码。`SteamClient` 负责网络与解析，`App` 负责界面 |
| `debug_network.py` | 网络诊断工具（Tk 窗口/命令行），逐项检查并给出结论，报告存 `debug_report.txt` |
| `requirements.txt` | requests、beautifulsoup4；可选 pillow / truststore / pysocks |
| `run.bat` / `run.sh` | 一键安装依赖并启动 |
| `README.md` | 使用说明（含加速器/代理问题的处理） |
| `MEMORY.md` | 本文件 |
| `steam_free_finder_data.json` | 运行时生成：已领取列表、见过的 appid（用于判定“新出现”）、界面设置。已在 .gitignore 中 |

## 关键技术决策

1. **数据来源（无需登录）**
   - 搜索：`GET store.steampowered.com/search/results/?query=&start=0&count=50&dynamic_data=&sort_by=_ASC&maxprice=free&specials=1&infinite=1&cc=cn&l=schinese`，返回 JSON，`results_html` 里是 `<a class="search_result_row" data-ds-appid=…>` 行。
   - 只保留 `data-discount="100"`（或 `.discount_pct` 文本 -100%）的项，排除永久免费游戏。
   - 评测：`GET store.steampowered.com/appreviews/{appid}?json=1&language=all&purchase_type=all&num_per_page=0` → `query_summary.total_positive/total_negative/review_score`。
   - 详情：`GET store.steampowered.com/api/appdetails?appids={appid}&cc=cn&l=schinese` → type / genres / categories / short_description / header_image / release_date。
   - 带 cookie `birthtime, lastagecheckage, mature_content` 绕过年龄验证。
2. **评价等级**：用 appreviews 的 `review_score` 映射（9 好评如潮 … 1 差评如潮，评测 <10 篇为「评测不足」），不用自己按百分比推算。
3. **分类**：三种分组方式（评价等级 / 内容类型 / 游戏类别=第一个 genre）+ 不分组；默认按好评率降序、评测数次序。
4. **网络线路（v1.1 新增）**：`session.trust_env=False`，自己控制代理。模式：自动（直连 → 系统代理 → 自定义）/ 直连 / 系统代理 / 自定义。
   - 系统代理来自 `urllib.request.getproxies()`；Windows 注册表常给出 `https://127.0.0.1:port`，urllib3 会尝试和代理做 TLS 握手导致 `ProxyError(SSLEOFError)`，所以 `normalize_proxy()` 一律改写为 `http://`。
   - 某条线路成功一次后锁定，后续请求只用它；全部失败时把每条线路的失败原因合成一段话弹窗提示。
5. **“新出现”判定**：`seen` 字典记录 appid → 最后见到时间；45 天内没见过的视为新；首次运行不提示；一年以上的记录自动清理。
6. **浏览器线路（v1.2 新增）**：`BrowserFetcher` 找到本机 Edge/Chrome（Program Files / LocalAppData / 注册表 App Paths），
   以 `--headless=new --dump-dom URL` 方式抓页面，从 `<pre>` 里取 JSON（兼容 Edge JSON 查看器：去标签后取首尾大括号）。
   识别 Chromium 错误页（`class="neterror"` + `ERR_*`）视为失败。使用独立 `--user-data-dir`（临时目录）避免和正在运行的浏览器冲突。
   之所以能用：雷神等加速器的进程模式只放行 Steam 和浏览器进程，无头浏览器同样是 msedge.exe/chrome.exe。
   自动模式顺序：直连 → 系统代理 → 自定义 → 浏览器；成功的线路写入设置 `last_route`，下次优先尝试。浏览器线路下不加载封面图。
7. **网络诊断（v1.3 新增）** `debug_network.py`：环境 → 代理设置（env / urllib / 注册表 ProxyServer+AutoConfigURL(PAC) / netsh winhttp）
   → DNS（getaddrinfo、阿里 DoH 参考、hosts、nslookup）→ 每个 IP 的 TCP+TLS（用系统证书库）→ requests 直连与候选代理
   → netstat/tasklist 列出监听端口与进程，疑似加速器进程端口 + 常见代理端口逐个当 HTTP(/SOCKS5) 代理试
   → curl 与无头 Edge/Chrome 对比 → 进程/虚拟网卡/默认路由痕迹 → 结论（进程模式 / 找到可用代理 / 证书中间人 / IPv6 / DNS）。
   主程序右上角「网络诊断」按钮用 `subprocess.Popen([sys.executable, debug_network.py])` 启动它。
   主程序可选 `truststore.inject_into_ssl()`（已安装才生效）。
8. **不做的事**：不自动点击「添加至账户」（需要用户登录凭证，不安全）；不抓 SteamDB（有 Cloudflare 且违反其 ToS）。

## 版本记录

- **v1.0（2026-09-10）** 初版：扫描、好评数据、三种分类、排序过滤、新品提醒、已领取标记、导出 CSV、封面显示。
- **v1.1（2026-09-10）** 修复开着加速器时 `ProxyError … SSLEOFError` 无法连接：新增网络线路自动切换与代理设置；工具栏拆成两行；错误弹窗列出每条线路的失败原因与建议。
- **v1.4（2026-09-10）** 用户回传诊断报告（见下"已确认的用户环境"）。浏览器线路改为：Windows 默认浏览器优先（注册表 UrlAssociations\https ProgId）、界面可手动选浏览器、记住上次能用的浏览器（设置 `browser_exe`）、卡住超时的浏览器本次不再尝试；自动模式探路时每条线路只试一次；状态栏实时显示"正在尝试线路：…"。
- **v1.3（2026-09-10）** 用户反馈"浏览器能开商店但软件不行、不知道哪出问题"。新增 `debug_network.py` 网络诊断工具 + 主程序「网络诊断」按钮；主程序支持可选 truststore。
- **v1.2（2026-09-10）** 用户用雷神加速器（AI 智能模式）仍报「直连：连接超时」——加速器只给浏览器/Steam 进程加速。新增「浏览器」线路（Edge/Chrome 无头抓取），自动模式兜底到它；记住上次成功线路；错误提示改为建议切「路由模式」或选「浏览器」。

## 已确认的用户环境（2026-09-10 诊断报告）

- Windows 10 19045，Python 3.10.4，主程序在 `E:\jiaoben\ruku\`。
- 雷神加速器（leigod.exe）Steam 商店|社区加速，AI 智能模式 = 进程模式。**只有 Chrome（默认浏览器）被放行**：无头 chrome.exe 1.8s 拿到 JSON；直连 TCP、curl、无头 msedge.exe 全部超时。
- 机器上还有 Clash（clash-core-service.exe 监听 127.0.0.1:53000，是 API 口不是代理口，CONNECT 返回 403）、Radmin VPN（26.0.0.1 默认路由）、TAP-Windows V9 网卡、AdGuard Home 做 DNS（192.168.1.1）。注册表残留 ProxyServer 127.0.0.1:10808 但 ProxyEnable=0 且端口未开。
- 结论：用主程序「浏览器」线路（Chrome），或把雷神改成路由模式。

## 当前状态

- 代码在无网络的沙盒中完成了：语法检查、搜索页解析单元测试（中/英文评测提示、新旧价格标签）、线路切换模拟测试、Tkinter 界面无头截图检查。
- 用户环境：Windows + 雷神加速器（Steam商店|社区，中国港服商店，AI 智能模式）。v1.0 报 ProxyError（当时有系统代理），v1.1 报「直连：连接超时」（无系统代理、加速器只放行浏览器）。
- 诊断已完成，原因明确（雷神进程模式只放行 Chrome）。**无头 Chrome 抓取 Steam 已在用户真机验证可行**（诊断工具里成功）。
- 等待用户用 v1.4 主程序实际扫描一次的反馈（网络选「浏览器」或「自动」，浏览器选 chrome.exe 或「自动」）。
- 若浏览器线路也失败，看弹窗里浏览器那行的原因：「未找到 Edge/Chrome」→ 路径问题；「打不开页面（ERR_…）」→ 无头浏览器没被加速器放行，改用雷神路由模式；「超时」→ 加大 `fetch_dom` 的 timeout 或检查是否弹出了首次运行界面。

## 待办 / 可能的下一步

- [ ] 用户在真实网络下验证 v1.2（雷神 + 浏览器线路 / 路由模式）能否扫描成功；若仍失败，看弹窗里每条线路的失败原因再调。
- [ ] 可选：浏览器线路下用一次浏览器进程抓多个 URL（现在每个请求拉起一次 Chrome，约 2 秒/次；10 个游戏约 21 次 ≈ 40 秒）。可用 `--remote-debugging-port` + CDP 复用进程。
- [ ] 若 Steam 搜索页结构有变，优先检查 `SteamClient.parse_search_rows`。
- [ ] 可选功能：显示免费活动截止时间（需抓商店页 `game_purchase_discount_countdown`）；系统托盘 / 开机自启；深色主题。
- [ ] 若要自动更新仓库，用户需再提供一个只含 Contents 写权限、短有效期的 GitHub Token（不要放进仓库）。

## 用户偏好

- 中文交流，回答简洁；喜欢直接给能运行的完整程序。
- 经常因为对话额度用完而换新对话，所以用这个仓库当“记忆”。
