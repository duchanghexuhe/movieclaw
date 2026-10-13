# 预转码 Windows Worker（docs/design/preload-transcode.md §7）

独立后台程序：连 NAS 领预转码任务 → 本机 ffmpeg 转码 → 上传产物 → 服务端
登记为该影片的可选版本。**与桌面客户端完全无关**——不改 `apps/desktop/`
一行代码，上游发新客户端照装不误。

## 使用

1. 准备 ffmpeg（jellyfin-ffmpeg 或任何带 `tonemapx` 滤镜的 build；
   gyan.dev 的 full build 也可以）。HDR 源的色调映射依赖 `tonemapx`，
   没有它会失败并上报。
2. 运行：

   ```powershell
   python preload_worker.py --server http://<NAS地址>:3000 --ffmpeg C:\tools\ffmpeg.exe
   ```

3. 首次运行打印配对码，浏览器打开打印的地址（NAS 的激活页）批准——
   批准后拿到 `scope=transcode` 的设备凭证，存进同目录 `preload_worker.json`。
4. 挂机即可：队列空时每 30 秒问一次；「仅接通电源时执行」「开机自启」
   直接用 Windows 任务计划程序/电源策略实现（第二版再做 Worker 内开关）。

## 断点续转

产物按 64 MiB 分段上传，服务端记录已收段数；ffmpeg 转完上传中断时重跑
Worker 即从断点续传。转码本身中断（进程被杀）时任务租约超时回队，
重新领取后从头转（源码级断点续转留给第二版）。

## 退出码

ffmpeg 失败/上传失败会上报 `fail`（带 stderr 尾巴），任务在网页端可见
失败原因，可一键重试。
