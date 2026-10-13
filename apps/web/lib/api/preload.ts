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

export interface PreloadTaskView {
  id: number;
  target_file_id: number;
  preset_id: string;
  status: "queued" | "claimed" | "running" | "completed" | "failed" | "cancelled";
  estimated_bytes: number;
  actual_bytes: number;
  resume_parts: number;
  attempts: number;
  error: string | null;
  result_file_id: number | null;
  created_at: string | null;
  completed_at: string | null;
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
    title: string | null;
    size_bytes: number;
    days_since_play: number;
  }[];
}

/** 可用档位。 */
export function getPreloadPresets(): Promise<{ id: string; title: string }[]> {
  return unwrap(request<ApiEnvelope<{ id: string; title: string }[]>>("/preload/presets"));
}

/** 体积预估（价格标签）。 */
export function estimatePreload(fileId: number): Promise<PreloadEstimate> {
  return unwrap(
    request<ApiEnvelope<PreloadEstimate>>("/preload/estimate", {
      method: "POST",
      body: JSON.stringify({ file_id: fileId }),
    }),
  );
}

/** 下单预转码任务（同源同档有活跃任务时幂等返回现有任务）。 */
export function createPreloadTask(fileId: number): Promise<PreloadTaskView> {
  return unwrap(
    request<ApiEnvelope<PreloadTaskView>>("/preload/tasks", {
      method: "POST",
      body: JSON.stringify({ file_id: fileId }),
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
