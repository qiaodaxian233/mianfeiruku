# Steam 免费入库雷达（mianfeiruku）

自动查找 Steam 商店里正在进行 **限时 100% 折扣（免费入库）** 的游戏和 DLC，
拉取好评率与评测数，按好评排序并自动分类。Python + Tkinter 图形界面，无需登录 Steam。

## 运行

```bash
pip install -r requirements.txt
python steam_free_finder.py
```

Windows 直接双击 `run.bat`；macOS / Linux 运行 `./run.sh`。需要 Python 3.9+。

## 开着加速器 / VPN 时怎么用

程序默认「网络：自动」，会依次尝试 **直连 → 系统代理 → 自定义代理**，哪条通就用哪条，状态栏会显示当前线路。

如果仍然失败：
1. 把加速器 / VPN 的 HTTP 代理端口填到「代理地址」（常见 `127.0.0.1:7890`、`7897`、`10809`），网络选「自定义」，再点「立即扫描」。
2. 很多**游戏加速器只加速游戏流量，不代理网页**。可以开启加速器的「浏览器/系统代理」模式，或者先关掉加速器、网络选「直连」。

> 背景：Windows 系统代理常写成 `https://127.0.0.1:端口`，Python 的 requests 会误以为要和代理做 TLS 握手，
> 报 `ProxyError … SSLEOFError`。程序已自动把它改写成 `http://`。

## 功能

| 功能 | 说明 |
|---|---|
| 自动扫描 | 启动即扫描，之后按设定间隔（默认 30 分钟）自动重扫 |
| 好评数据 | 好评率、评测总数、好评/差评数、Steam 评价等级（好评如潮 / 特别好评 …） |
| 自动分类 | 按评价等级 / 内容类型（游戏、DLC、软件…）/ 游戏类别（动作、冒险…）分组，或不分组 |
| 排序过滤 | 点表头排序；搜索、最低好评率、最少评测数、仅游戏、隐藏已领取 |
| 新品提醒 | 新出现的免费项目高亮 + 响铃 + 可选弹窗 |
| 领取辅助 | 双击打开商店页；右键在 Steam 客户端打开、复制链接；「打开全部未领取」一键开所有页面 |
| 记录 | 标记已领取（空格键切换），设置与记录保存在 `steam_free_finder_data.json` |
| 导出 | 导出当前列表为 CSV（Excel 可直接打开） |

## 原理

只用 Steam 商店的公开接口：

- 搜索页 `store.steampowered.com/search/results/?maxprice=free&specials=1` 找出正在打折且价格为 0 的项目，再按 `-100%` 折扣过滤（排除永久免费游戏）
- `store.steampowered.com/appreviews/{appid}` 取好评/差评数与评价等级
- `store.steampowered.com/api/appdetails` 取类型、类别、简介、封面

## 注意

- 「免费入库」需要在 Steam 页面上点击 **添加至账户**（需登录）。本工具负责发现、分类和打开页面，不代替你点击。
- Steam 页面结构若有调整，解析规则可能需要更新；相关代码集中在 `SteamClient.parse_search_rows`。
- 项目状态与对话记忆见 [`MEMORY.md`](MEMORY.md)。
