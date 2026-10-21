# IPTV 插件：社区实现调研与待选方案

> 状态：**调研稿，待用户选定方向**（2026-10-21）。选定后另出设计文档。
> 关联：`plugin-phase3.md`（插件包、默认独立进程）、`plugin-ipc-http.md`（插件路由经反代、支持流式）、
> `plugin-phase4.md`（设置 Schema）、`plugin-web-pages.md`（插件网页，已实现）、`strm-playback.md`（远端源播放）。

## 0. 结论先行

1. **社区的成熟做法高度收敛成一个分层**：输入（M3U / Xtream Codes / TXT）→ 频道整理（过滤、改名、分组、合并同台多源、测活）
   → 节目单（XMLTV 匹配到频道）→ 输出（标准 M3U + XMLTV 地址，可选中转）→ 播放端。差异只在「播放端放在哪」。
2. **MovieClaw 内核现在没有直播播放**：播放链路全是点播（VOD 列表、关键帧索引、Range 读源），`.strm` 也是按点播做的。
   所以「播放放在哪」决定了是**纯插件**还是**要动内核**，这是唯一需要你拍板的方向（§3）。
3. **插件本身不带任何直播源**（与 iptv-api 2.0 起的做法一致）：用户自己填订阅地址，插件只做整理、节目单、输出与中转。

## 1. 社区实现

| 项目 | 形态 | 输入 | 节目单 | 输出 / 播放 | 取什么 |
|---|---|---|---|---|---|
| [Jellyfin Live TV](https://jellyfin.org/docs/general/server/live-tv/) | 媒体服务器内置 | 「调谐器」：M3U、HDHomeRun，插件可加类型（`ITunerHost`） | 「节目单提供方」：XMLTV、Schedules Direct（`IListingsProvider`），频道手动 / 自动映射 | 自家客户端的直播页、节目表 | **两个扩展点的切分**（调谐器 × 节目单提供方）；数据模型 `ChannelInfo` / `ProgramInfo` |
| [Threadfin](https://docs.saltbox.dev/sandbox/apps/threadfin)（xTeVe 后继，MIT） | 独立 Go 服务 | M3U + XMLTV | 过滤、映射、改名 | 伪装成 HDHomeRun 调谐器给 Plex / Emby / Jellyfin；ffmpeg / VLC 缓冲，限并发 | 「整理后再输出标准地址」；并发上限 |
| [Dispatcharr](https://github.com/Dispatcharr/Dispatcharr)（AGPL，约 4.3k★） | 独立 Django 服务 | M3U、Xtream Codes | XMLTV、自动匹配、无节目单时生成占位节目 | M3U、XMLTV、Xtream API、HDHomeRun；流配置（ffmpeg / Streamlink）、**断流自动切备用源** | 同台多源 + 故障切换；占位节目单 |
| [akiralereal/iptv](https://github.com/akiralereal/iptv)（GPL-3.0，约 1.4k★） | 自托管 Node 服务，面向 NAS | 内置抓取模块 + 用户 M3U / TXT 订阅 | 模块自带 + 外部 XMLTV | `/interface.m3u`、`/playback.xml`；**三档中转**：直连 / 改写列表 / 全量转发；后台用 hls.js、mpegts.js 预览 | 三档中转（国内源常需要）；网页预览播放 |
| [Guovin/iptv-api](https://github.com/Guovin/iptv-api)（MIT，约 24k★） | 采集 + 测速工具 | 订阅、组播、酒店源等 | 读 M3U 头里的 `x-tvg-url` 合并 | 生成 M3U / TXT；2.0 起**不再内置源** | 测速择优、按 IPv4 / IPv6 分；不内置源 |
| [Kodi IPTV Simple](https://github.com/kodi-pvr/pvr.iptvsimple) | 播放端插件 | M3U | XMLTV（`tvg-id` → `channel id`，退回 `tvg-name`） | Kodi 原生直播 UI；**回看**（`catchup` / `catchup-source` / `catchup-days`） | M3U 扩展属性的事实标准；回看字段 |

共同点：

- **M3U 扩展属性 + XMLTV 是事实标准**（`tvg-id`、`tvg-name`、`tvg-logo`、`group-title`、`x-tvg-url`、`catchup*`），
  Xtream Codes API 是付费源的事实标准输入。按 `plugin-web-pages.md` 的原则「能用标准就用标准」，插件的输入、输出都只用这三样，不造格式。
- **国内源的实际痛点**：HTTP 源嵌在 HTTPS 页面里被浏览器拦（混合内容）、跨域、防盗链 Referer / UA、源不稳定。
  社区一律用「服务端中转 + 多源择优」解决。

## 2. 待选方案

三个方案是**递进**关系：B 包含 A，C 包含 B。都按现有插件规范：插件包、默认独立进程运行（`runtime = "process"`）、
插件路由 `/api/v1/plugins/iptv/…`（经反代、支持流式）、设置走 JSON Schema、管理页走插件网页 `web/`。

### 方案 A：频道源管家（纯输出，不在 MovieClaw 里播）

参照 Threadfin / Dispatcharr 的「整理 + 输出」部分。

```
iptv/
  movieclaw-plugin.toml     # requires: plugin-data、plugin-routes
  iptv/                     # 后端：拉订阅、解析 M3U / Xtream、拉 XMLTV、测活、出列表
  web/                      # 管理页：订阅源、频道（改名 / 分组 / 隐藏 / 排序）、节目单匹配
    manifest.webmanifest    # 侧边栏「直播」
```

- 用户：添加订阅地址（或 Xtream 账号）→ 自动拉取、合并同名频道、匹配节目单 → 复制两个地址
  （`…/iptv/playlist.m3u`、`…/iptv/epg.xml`）到 APTV / TiviMate / Kodi / Infuse / Jellyfin 的 M3U 调谐器。
- 两个输出地址用插件签名链接（不带登录 Cookie 的播放器也能取）。
- **内核零改动。** 工作量最小。
- 缺点：MovieClaw 自己的各端都看不了直播，用户要另装播放器；对「影视中心」的价值有限。

### 方案 B：A + 网页直播间（推荐）

在 A 的基础上，插件网页里直接看：

- 页面：左侧频道列表（分组、搜索），右侧播放器 + 当前 / 下一个节目；顶部节目表（时间轴网格）。
  播放器用 hls.js（HLS）+ mpegts.js（FLV / TS），插件自己打包，不依赖主程序前端。
- **中转**（参照 akiralereal/iptv 的三档，但不让用户选）：默认直连；遇到 HTTP 源、跨域、需要 Referer / UA 的源，
  插件路由自动改写列表并转发分片。用户看不到「中转模式」这个选项，只在频道详情里显示「经服务器中转」（AGENTS.md §5）。
- 多源：同一频道多条地址，按测活结果排序，播放失败自动换下一条（参照 Dispatcharr 的故障切换）。
- **内核零改动**，只用已实现的插件网页、插件路由、插件数据。
- 缺点：只有网页能看；iPhone / TV / Android 原生端第一版没有（插件网页本来就只做网页，`plugin-web-pages.md` §9）。

### 方案 C：B + 内核直播能力（各端原生看直播）

参照 Jellyfin Live TV 的两个扩展点：

- 内核新增**直播频道**扩展点（对应 Jellyfin `ITunerHost`：列频道、取播放地址）与**节目单**扩展点（对应 `IListingsProvider`），
  SDK 公开契约；IPTV 插件往里贡献实现。将来 HDHomeRun、运营商组播（udpxy）、B 站 / 斗鱼直播都可以是别的插件。
- 内核播放链路加**直播模式**：无限时长、滑动窗口 HLS、不做关键帧索引和进度记录；需要转码时 ffmpeg 读直播源。
- Jellyfin 兼容层补 `/LiveTv/Channels`、`/LiveTv/Programs`、`/LiveTv/Programs/Recommended` 等，
  让走 Jellyfin 协议的第三方客户端直接出直播页。
- 自家 7 个客户端各自做直播页（频道列表、节目表、换台）。
- 缺点：动内核播放链路（当前最敏感的部分）、动 SDK 公开契约、七端都要跟；周期以月计。

### 对比

| | A 源管家 | B + 网页直播间 | C + 内核直播 |
|---|---|---|---|
| 在 MovieClaw 里能看 | 否 | 网页 | 全部客户端 + Jellyfin 协议客户端 |
| 内核改动 | 无 | 无 | 播放链路、SDK 扩展点、Jellyfin 兼容层 |
| 客户端改动 | 无 | 无 | 七端 |
| 外部播放器（APTV / Kodi / Infuse） | ✅ | ✅ | ✅ |
| 回退成本 | 卸载插件 | 卸载插件 | 多个 PR 回滚 |
| 后续能否升级 | → B | → C（数据模型按 Jellyfin 的频道 / 节目字段定，插件逻辑原样搬进扩展点） | — |

## 3. 需要你拍板的一件事

**播放放在哪？** 推荐 **B**：内核不动、网页当天能看、外部播放器也能用；数据模型一开始就对齐 Jellyfin 的
频道 / 节目字段，将来真要上原生端（C）时插件逻辑直接搬进扩展点，不返工。

以下我按默认值处理，不单独问：

| 事项 | 默认 |
|---|---|
| 内置直播源 | 不内置，用户自己填订阅地址 |
| 输入格式 | M3U / M3U8、TXT（`频道名,地址`）、Xtream Codes；运营商组播走用户自己的 udpxy 地址，当普通 HTTP 源处理 |
| 节目单 | XMLTV（含 `.xml.gz`）；自动读 M3U 头里的 `x-tvg-url`，用户不填也能有节目单 |
| 谁能看 | 沿用插件网页的「成员也能看到」开关，不另加权限 |
| 回看、录制 | 第一版不做；`catchup*` 属性原样保留在输出列表里，外部播放器能用 |
| 插件 id | `iptv`（地址 `/apps/iptv/`，侧边栏「直播」） |
