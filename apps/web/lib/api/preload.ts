import { request } from "@/lib/http";

/** 后端统一响应信封（见 movieclaw_api.schemas.response.ApiResponse）。 */
interface ApiEnvelope<T> {
  success: boolean;
  code: string;
  message: string;
  data: T;
}

async function unwrap<T>(promise: Promise<ApiEnvelope<T>>): Promise<T> {
  return (await promise).data;
}

/** Worker 实时进度上报（活动页进行中卡片渲染；旧版 Worker 不发为 null）。 */
export interface PreloadProgress {
  /** downloading=取源中 / transcoding=转码中 / finalizing=拼接登记 */
  phase?: string;
  downloaded_bytes?: number;
  total_bytes?: number;
  /** 取源速度（字节/秒，Worker 按 1 秒窗口滑动平均；非下载阶段为 0/缺省） */
  speed_bps?: number;
  /** 正在转的第几段（1 起） */
  segment_index?: number;
  segment_total?: number;
  /** 预计剩余秒数（下载期按速度算，转码期按已完成段平均耗时 × 剩余段数） */
  eta_seconds?: number;
}

export interface PreloadTaskView {
  id: number;
  target_file_id: number;
  preset_id: string;
  status: "queued" | "claimed" | "running" | "completed" | "failed" | "cancelled" | "invalidated";
  estimated_bytes: number;
  actual_bytes: number;
  resume_parts: number;
  attempts: number;
  error: string | null;
  result_file_id: number | null;
  progress: PreloadProgress | null;
  created_at: string | null;
  completed_at: string | null;
  updated_at: string | null;
  /** 这单转的是哪部片子；未识别/源已删为 null，展示落回「源文件 #id」 */
  media_title: string | null;
  season_number: number | null;
  episode_number: number | null;
  /** 源文件规格签名（「2160p WEB-DL DV」） */
  source_quality: string | null;
  /** 执行中的任务由谁转：「NAS 本地」或 Worker 设备名；排队中为 null */
  executor: string | null;
}

/** 任务状态中文标签（设置页任务行与活动页任务卡共用一套口径）。 */
export const PRELOAD_STATUS_LABELS: Record<PreloadTaskView["status"], string> = {
  queued: "排队中",
  claimed: "已领取",
  running: "转码中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  invalidated: "已失效（可重新转码）",
};

/** 任务卡的标识：《片名》[ SxxEyy]，没有身份时落回「源文件 #id」。 */
export function preloadTaskTitle(task: PreloadTaskView): string {
  if (!task.media_title) return `源文件 #${task.target_file_id}`;
  const unit =
    (task.season_number ?? 0) > 0
      ? ` S${String(task.season_number).padStart(2, "0")}E${String(task.episode_number ?? 0).padStart(2, "0")}`
      : "";
  return `《${task.media_title}》${unit}`;
}

/** 任务卡的副标识：档位短名（省流/高清）+ 源规格，没有档位表时落回档位 id。 */
export function preloadTaskSubtitle(task: PreloadTaskView, presets: { id: string; short: string }[]): string {
  const preset = presets.find((p) => p.id === task.preset_id)?.short ?? task.preset_id;
  return [preset, task.source_quality].filter(Boolean).join(" · ");
}

export interface PreloadEstimate {
  estimated_bytes: number;
  quota_bytes: number;
  used_bytes: number;
  free_bytes: number;
}

export interface StaleCleanupResult {
  count: number;
  total_bytes: number;
  freed_bytes?: number;
  files: {
    file_id: number;
    media_item_id: number | null;
    /** 片名；未识别为 null */
    title: string | null;
    /** 电影为 0 */
    season_number: number;
    episode_number: number;
    size_bytes: number;
    days_since_play: number;
  }[];
}

export interface PreloadOverview {
  count: number;
  total_bytes: number;
  quota_bytes: number;
  oldest_days: number;
  stale_days: number;
  auto_cleanup: boolean;
}

/** 预转码统计（共 X GB / 共 Y 部 / 最老 Z 天前）。 */
export function getPreloadOverview(): Promise<PreloadOverview> {
  return unwrap(request<ApiEnvelope<PreloadOverview>>("/preload/overview"));
}

export interface PreloadPreset {
  id: string;
  /** 档位短名（省流/高清），切换胶囊与任务行用 */
  short: string;
  title: string;
}

/** 可用档位（省流在前=默认档；标题随全局格式动态生成）。 */
export function getPreloadPresets(): Promise<PreloadPreset[]> {
  return unwrap(request<ApiEnvelope<PreloadPreset[]>>("/preload/presets"));
}

export type PreloadVideoFormat = "h264" | "hevc";

export interface PreloadSettings {
  /** 预转码编码格式：h264 全设备兼容；hevc 同流量清晰度更高 */
  video_format: PreloadVideoFormat;
}

/** 预转码设置（管理员）。 */
export function getPreloadSettings(): Promise<PreloadSettings> {
  return unwrap(request<ApiEnvelope<PreloadSettings>>("/preload/settings"));
}

/** 保存预转码设置；改格式只影响之后的下单（在途任务按下单时快照跑）。 */
export function savePreloadSettings(videoFormat: PreloadVideoFormat): Promise<PreloadSettings> {
  return unwrap(
    request<ApiEnvelope<PreloadSettings>>("/preload/settings", {
      method: "PUT",
      body: JSON.stringify({ video_format: videoFormat }),
    }),
  );
}

/** 体积预估（价格标签）。缺省档位由后端默认（省流）。 */
export function estimatePreload(fileId: number, presetId?: string): Promise<PreloadEstimate> {
  return unwrap(
    request<ApiEnvelope<PreloadEstimate>>("/preload/estimate", {
      method: "POST",
      body: JSON.stringify({ file_id: fileId, ...(presetId ? { preset_id: presetId } : {}) }),
    }),
  );
}

/** 下单预转码任务（同源同档有活跃任务时幂等返回现有任务）。 */
export function createPreloadTask(fileId: number, presetId?: string): Promise<PreloadTaskView> {
  return unwrap(
    request<ApiEnvelope<PreloadTaskView>>("/preload/tasks", {
      method: "POST",
      body: JSON.stringify({ file_id: fileId, ...(presetId ? { preset_id: presetId } : {}) }),
    }),
  );
}

/** 任务列表（最近 100 条）。 */
export function listPreloadTasks(): Promise<PreloadTaskView[]> {
  return unwrap(request<ApiEnvelope<PreloadTaskView[]>>("/preload/tasks"));
}

/** 取消任务。 */
export function cancelPreloadTask(taskId: number): Promise<PreloadTaskView> {
  return unwrap(
    request<ApiEnvelope<PreloadTaskView>>(`/preload/tasks/${taskId}/cancel`, { method: "POST" }),
  );
}

/** 失败任务重试。 */
export function retryPreloadTask(taskId: number): Promise<PreloadTaskView> {
  return unwrap(
    request<ApiEnvelope<PreloadTaskView>>(`/preload/tasks/${taskId}/retry`, { method: "POST" }),
  );
}

/** 删除预转码产物版本。 */
export function deletePreloadFile(fileId: number): Promise<null> {
  return unwrap(
    request<ApiEnvelope<null>>(`/preload/files/${fileId}`, { method: "DELETE" }),
  );
}

/** 清理冷数据：confirm=false 预览，true 执行。 */
export function cleanupStalePreload(
  days: number | undefined,
  confirm: boolean,
): Promise<StaleCleanupResult> {
  return unwrap(
    request<ApiEnvelope<StaleCleanupResult>>("/preload/cleanup-stale", {
      method: "POST",
      body: JSON.stringify({ days, confirm }),
    }),
  );
}
