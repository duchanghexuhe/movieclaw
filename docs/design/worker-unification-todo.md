# Worker 体系改造 TODO（2026-10-15 讨论定案）

> 依据：2026-10-15 六场讨论，过程细节不在此文，可向当次会话索要；实现时的代码事实
> 入口在文末「实现锚点」。状态：T1-T4 已全部实现（2026-10-15，分支 feat/preload-transcode）。

## TODO 看板（按依赖排序）

- [x] **T1 Worker 单实例防呆锁**（独立可先行）
  Windows 命名互斥体 `Global\MovieClawWorker`，三入口（remote_worker / preload_worker / preload_worker_gui）全覆盖；
  GUI 拿不到锁弹窗 / CLI 打日志退出 / 自启静默退（exit 0）；
  顺手静默 job.playback 未知消息刷屏。约半天。
- [x] **T2 统一调度一步到位**（已拍板方向）
  协议 v2（hello 报 roles+max_concurrency，job.start 加 class 字段）；
  预转码收编进 WS 推送派单，realtime 永远优先（预算>1 时 preload 占 预算−1，兜底 job.stop 断点续转）；
  **并发只留 Worker 本机一处**（本机最大同时转码），服务端「同时转码」下拉整条链废弃；
  **不做分期不做兼容层**，单一提交/单一发布一次切换。
  服务端保留：preload DB 任务队列/产物校验登记/派生物生命周期。
  实现注：Mac Swift 客户端同提交升 v2（roles=realtime，收到 class=preload 明确拒绝）。
- [x] **T3 转码自动分流 + 上限随执行者**（与 T2 绑定实施）
  分流规则：HDR 或 ≥4K → Worker，其余本地（按片属性，不按负载）；
  高度上限：远程会话 2160、本地维持 1080；
  实施前置：4K 原生档 Worker 真片实测；4K+烧字幕组合单独验。
  实测结论（2026-10-15 2070S + jellyfin-ffmpeg 8.1.3）：16:9 4K HDR 全 GPU 链 4.67×；
  4K+烧 ASS 4.57×（烧录只在出字幕时软件化，不掉速）。**额外发现：宽银幕 4K
  （3840x1604）按 2160 高度放大后宽 5172 超 h264_nvenc 编码上限 4096 直接炸**——
  修复为硬件 scale 宽度走 `min(4096,trunc(iw*h/ih/2)*2)` 表达式，宽触顶时高随比例
  同收（实测 4096x1710 无变形，5.13×）。
- [x] **T4 进度条阴影两端截断修复**（独立，最小改动）
  globals.css `.player-scrub-shade::before` 左右各加 ~12px 横向淡出（CSS mask）；
  改后亮画面下肉眼验收两端无硬边。
- [x] ~~并发改名独立小修~~ —— 被 T2 吸收（并发归属随 v2 收敛到 Worker 本机，不再单独改名）

依赖关系：T3 必须随 T2 落地（上限放开只对远程执行生效，否则核显编 4K 压死）；T1、T4 与其他项无依赖，随时可做。

## 实现锚点（新会话从这些文件/行为入手，均已核实存在于当前代码）

- 执行后端选择：`src/movieclaw_api/services/playback/plan.py` `select_execution_backend`
  （现状已有 `prefer_remote` 参数，由 `RemoteTranscodeSetting.enabled && prefer_remote` 驱动；
  T3 = 在 prefer_remote=False 分支里按片属性插入远程候选）。分流依据在决策层
  `movieclaw_playback/decide.py`（`media.hdr` / `media.height`，计划上已有 `tone_map`）。
- 高度上限：`src/movieclaw_api/services/playback/limits.py` `MAX_TRANSCODE_HEIGHT=1080`
  （经 `plan.load_policy` → `PlaybackPolicy.max_transcode_height` 进引擎；
  T3 改法=上限随执行者，不是拔高常量）。码率阶梯 2160:16M 已在
  `services/playback/ffmpeg_args.py` `BITRATE_LADDER`。
- 服务端 Worker 注册/派单：`services/playback/remote_worker.py` `RemoteWorkerRegistry`
  （同 ID 替换语义在 register：旧连接 jobs 判失败；并发上限链=
  `api/routes/transcode_worker.py` + `services/playback/worker_config.py` +
  `settings/remote_transcode.py::WorkerLimitsSetting`，T2 整条废弃）。
  预转码队列：`services/preload/service.py`（claim 原子抢占/租约回收），T2 保留队列、
  出队改注册表推送。
- Worker 端：`worker/realtime_worker.py`（WS 客户端 hello/heartbeat/job.start；**现坑**：
  握手报 server_config=True 但 max_jobs 写死 1，GUI 并发旋钮对实时从未生效——T2 一并消灭）；
  `worker/preload_worker.py`（REST claim 轮询+分段断点续转）；`worker/preload_worker_gui.py`。
- 真实用户路径：GUI/CLI Worker 同机预转码+实时双角色挂着，worker/*.json 指向 NAS 8096。

## 关键数据引用
- NAS 虚拟核显 UHD 630：4K HDR10→480p 约 1.8× 实时（2026-10-15 真片二分变体 3/7，9 分片/20s 截止）
- Worker 2070S：省流档 p2 链路 ~12.7×（2026-10-14 真机）；4K 原生档未测，T3 实施前必须真片验证
- 远程供片延迟代价：起播/深 seek 慢 1~4 秒（取源绕两趟网络 + 按段整传天生 +4s，见 git log 5ef5f5c3/0d55051b）
