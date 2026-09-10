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
| `requirements.txt` | requests、beautifulsoup4、pillow（可选） |
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
6. **不做的事**：不自动点击「添加至账户」（需要用户登录凭证，不安全）；不抓 SteamDB（有 Cloudflare 且违反其 ToS）。

## 版本记录

- **v1.0（2026-09-10）** 初版：扫描、好评数据、三种分类、排序过滤、新品提醒、已领取标记、导出 CSV、封面显示。
- **v1.1（2026-09-10）** 修复开着加速器时 `ProxyError … SSLEOFError` 无法连接：新增网络线路自动切换与代理设置；工具栏拆成两行；错误弹窗列出每条线路的失败原因与建议。

## 当前状态

- 代码在无网络的沙盒中完成了：语法检查、搜索页解析单元测试（中/英文评测提示、新旧价格标签）、线路切换模拟测试、Tkinter 界面无头截图检查。
- **尚未在用户真实环境验证联网效果。** 用户上次运行 v1.0 报 ProxyError（加速器开着）。v1.1 理论上已解决，等待用户反馈。

## 待办 / 可能的下一步

- [ ] 用户在真实网络下验证 v1.1 能否扫描成功；若仍失败，看弹窗里每条线路的失败原因再调。
- [ ] 若 Steam 搜索页结构有变，优先检查 `SteamClient.parse_search_rows`。
- [ ] 可选功能：显示免费活动截止时间（需抓商店页 `game_purchase_discount_countdown`）；系统托盘 / 开机自启；深色主题。
- [ ] 若要自动更新仓库，用户需再提供一个只含 Contents 写权限、短有效期的 GitHub Token（不要放进仓库）。

## 用户偏好

- 中文交流，回答简洁；喜欢直接给能运行的完整程序。
- 经常因为对话额度用完而换新对话，所以用这个仓库当“记忆”。
