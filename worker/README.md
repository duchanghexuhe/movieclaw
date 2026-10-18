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
4. 挂机即可。**本机并发是全机唯一并发旋钮**：实时任务永远优先，预转码
   占「并发−1」；并发=1 的机器只接实时任务。NAS 侧的自动调度以 Worker
   优先：在线 Worker 的预算吃满后，剩余任务才由 NAS 本地兜底执行。任务在跑时
   会阻止系统睡眠，空闲即恢复。开启「开机自启」后，已配对的机器开机直接收进托盘、
   开始工作。

## 连接状态与拒绝原因

状态行与日志里的连接情况都以 Worker 实际的连接为准。服务端拒绝接入时，关闭帧里
的理由会原样显示：凭证失效每 5 分钟重试一次；服务端没开远程转码每分钟重试一次；
其他原因（如协议版本不一致）每分钟重试一次——与 macOS 转码器的处理一致。

## 断点续转

预转码产物按段上传，服务端记录已收段数；ffmpeg 转完上传中断时重跑
Worker 即从断点续传。转码本身中断（进程被杀/被实时任务抢占）时任务
回队，重新领取后按断点续转。

## 退出码

ffmpeg 失败/上传失败会上报 `fail`（带 stderr 尾巴），任务在网页端可见
失败原因，可一键重试。
