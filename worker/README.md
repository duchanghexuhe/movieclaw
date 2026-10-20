# MovieClaw Windows Worker（docs/design/preload-transcode.md · worker-unification-todo.md）

NAS 的转码执行器——**一个程序两种活**，统一调度 v2 协议：

- **实时转码**：播放时 NAS 派单（WS `job.start`），本机 ffmpeg 转 HLS、
  分片流式回传 NAS；
- **预转码**：NAS 推送任务（`job.start` + `class=preload`），分段转码 +
  断点续传，产物登记为该影片的可选版本。

**与桌面客户端完全无关**——不改 `apps/desktop/` 一行代码，上游发新客户端
照装不误。

## 文件

| 文件 | 角色 |
|------|------|
| `worker.py` | CLI 主程序（`python worker.py --server …`） |
| `worker_gui.py` | 图形界面（托盘常驻；实现本体） |
| `worker_gui.pyw` | 双击启动入口（pythonw 打开，无命令行窗口） |
| `engine.py` | 内部引擎：WS 客户端 / 分片器 / 回环代理（实现细节，不单独运行） |
| `single_instance.py` | 同机单实例防呆锁 |

## 使用

1. 准备 ffmpeg，**推荐 jellyfin-ffmpeg**。HDR 源的色调映射需要它的 `tonemapx`
   或 `tonemap_cuda` 滤镜：两个都没有的 build（如 gyan.dev 的 full build，视版本
   而定）只能处理 SDR 源，HDR 源会被服务端拒做，原因在网页端的任务上可见。
   自检：`ffmpeg -hide_banner -filters | findstr tonemap`。
2. 运行（CLI）：

   ```powershell
   python worker.py --server http://<NAS地址>:3000 --ffmpeg C:\tools\ffmpeg.exe
   ```

   或图形界面：双击 `worker_gui.pyw`（pythonw 打开，无命令行窗口；从
   终端 `python worker_gui.py` 运行可看输出调试。配置：服务器 / ffmpeg
   / 本机并发 / 仅接电 / 开机自启，全部即改即存）。点「启动」时先跑一次
   `ffmpeg -version`，找不到或跑不起来就不启动，并说明原因。
3. 首次运行会给出配对码和批准页地址（GUI 上有「打开批准页」按钮，CLI 打印在
   终端）。在浏览器里核对配对码后批准——批准后拿到 `scope=transcode` 的设备
   凭证，存进同目录 `worker.json`（旧部署的 `preload_worker.json` 自动沿用）。
   登记的机器标识（worker_id）默认取本机主机名，每台机器各一个；配置里填了
   `name` 的沿用它。
4. 挂机即可。**本机并发是全机唯一并发旋钮**，播放优先：有实时任务（播放转码、
   离线导出）在跑时，本机的预转码全部让位——断点保留，服务端立即退回队列，播放
   结束后自动从断点续转；空闲时整个并发都用来转预转码。NAS 侧的自动调度以 Worker
   优先：在线 Worker 有余量就先派给它，剩余任务才由 NAS 本地兜底执行。任务在跑时
   会阻止系统睡眠，空闲即恢复。开启「开机自启」后，已配对的机器开机直接收进托盘、
   开始工作。

## 连接状态与拒绝原因

状态行与日志里的连接情况都以 Worker 实际的连接为准。服务端拒绝接入时，关闭帧里
的理由会原样显示：凭证失效每 5 分钟重试一次；服务端没开远程转码每分钟重试一次；
其他原因（如协议版本不一致）每分钟重试一次——与 macOS 转码器的处理一致。

凭证失效时（例如在网页「设置 → 设备」里注销了这台 Worker），GUI 的按钮会变成
「重新配对」：点一下就丢掉旧凭证、重新走配对流程，不用改配置文件。CLI 用 `--reset-token`。

**已配对**时同一个位置是「断开并重新配置…」：停掉连接，并尽力在服务端
`DELETE /auth/devices/current` 注销这枚凭证，然后清本机令牌、回到未配对。只清本机是不够的
——服务端那枚凭证依然有效，「设置 → 设备」里会一直挂着一台已经断开、却还能连上来的机器
（Mac 转码器的「断开并重新配置」做的正是这件事）。服务器连不上时只清本机并在日志里说明，
之后可到网页上手动注销；本机的服务器地址、名称、并发等配置都会保留。

**配对入口就是「启动」**：没有凭证时启动即进入配对流程（窗口显示配对码，可点「打开批准页」）。
状态行会写明当前是「已配对（名称 …）」还是「未配对——点「启动」开始配对」。

## 断点续转

预转码产物按段上传，服务端记录已收段数；ffmpeg 转完上传中断时重跑
Worker 即从断点续传。播放到来、预转码让位时 Worker 立即告知服务端，任务当场
退回队列；本地的源文件与分段保留，重新领取后按断点续转。只有服务端取消任务
时，Worker 才会删掉本地的半成品。

## 实时任务的中转目录

Windows 上 ffmpeg 不直接 PUT 回 NAS（SMB 源 + 回环 PUT 会死锁，见
`docs/design/remote-transcode.md`），而是写一份本地中转文件、由 Worker 跟读上传。
Worker 在拉起 ffmpeg 之前会**让 ffmpeg 自己试写**一遍候选目录，取第一个写得进去的：

1. `MOVIECLAW_WORKER_WORK_DIR`（显式指定，排障用）；
2. 系统临时目录（`%TEMP%\mc-realtime-*`，默认就是它）；
3. Worker 自己所在的 `work/.realtime`；
4. `~/.movieclaw-worker/tmp`。

为什么要试写而不是直接用 `%TEMP%`：ffmpeg 的完整性级别可能**低于** Worker 进程
本身。二进制带着 Low 强制标签时（把它放在被沙盒工具打了 Low 标签的目录里就会
这样），它写不进 Medium 完整性级别的 `%TEMP%`，而 Worker 进程自己写得进去——
只按「Worker 能不能写」判断，任务就会被派到一个必失败的中转目录。2026-10-20
真机事故正是如此：四次远程任务全部 `Permission denied`，一个分片都没产出。
探测结论按 ffmpeg 路径在进程内缓存，只在换二进制时重探。

## 退出码

ffmpeg 失败/上传失败会上报 `fail`（带 stderr 尾巴），任务在网页端可见
失败原因，可一键重试。
