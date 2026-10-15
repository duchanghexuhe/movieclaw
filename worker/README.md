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
| `worker_gui.py` | 图形界面（托盘常驻；`pythonw worker_gui.pyw`） |
| `engine.py` | 内部引擎：WS 客户端 / 分片器 / 回环代理（实现细节，不单独运行） |
| `single_instance.py` | 同机单实例防呆锁 |

## 使用

1. 准备 ffmpeg（jellyfin-ffmpeg 或任何带 `tonemapx` 滤镜的 build；
   gyan.dev 的 full build 也可以）。HDR 源的色调映射依赖 `tonemapx`，
   没有它会失败并上报。
2. 运行（CLI）：

   ```powershell
   python worker.py --server http://<NAS地址>:3000 --ffmpeg C:\tools\ffmpeg.exe
   ```

   或图形界面：`python worker_gui.py`（配置：服务器 / ffmpeg / 本机并发
   / 仅接电 / 实时开关 / 开机自启，全部即改即存）。
3. 首次运行打印配对码，浏览器打开打印的地址（NAS 的激活页）批准——
   批准后拿到 `scope=transcode` 的设备凭证，存进同目录 `worker.json`
   （旧部署的 `preload_worker.json` 自动沿用）。
4. 挂机即可。**本机并发是全机唯一并发旋钮**：实时任务永远优先，预转码
   占「并发−1」；并发=1 的机器只接实时任务。NAS 侧还有本地优先的自动
   调度：NAS 空闲时预转码任务优先由 NAS 自己跑，忙时才推给 Worker。

## 断点续转

预转码产物按段上传，服务端记录已收段数；ffmpeg 转完上传中断时重跑
Worker 即从断点续传。转码本身中断（进程被杀/被实时任务抢占）时任务
回队，重新领取后按断点续转。

## 退出码

ffmpeg 失败/上传失败会上报 `fail`（带 stderr 尾巴），任务在网页端可见
失败原因，可一键重试。
