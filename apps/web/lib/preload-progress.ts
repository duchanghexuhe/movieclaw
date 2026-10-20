/**
 * 预转码任务的进度文案：进行中卡片的详细行 + 总览「进行中」行的短尾缀。
 *
 * 抽成纯函数模块（而不是留在 task-center-view.tsx 里）是为了能被 node 测试
 * 直接 import —— .tsx 带 JSX，node --test 解析不了。**阶段口径是两端的契约**：
 * Worker 与 NAS 本地执行者写 `downloading` / `transcoding` / `finalizing`，
 * 三个阶段的文案都在这里定。
 */
import { formatBytes, formatSpeed } from "./format.ts";

/** 进度载荷的形状（与 lib/api/preload 的 PreloadProgress 同构，取用子集）。 */
export interface ProgressLike {
  phase?: string;
  downloaded_bytes?: number;
  total_bytes?: number;
  speed_bps?: number;
  segment_index?: number;
  segment_total?: number;
  eta_seconds?: number;
}

/**
 * 进行中卡片的进度行：取源多少/第几段/预计剩余；没有进度为 null。
 *
 * `finalizing`（服务端拼接+校验+登记，分钟级）必须单独成一句 —— 它没有百分比，
 * 落在「第 N/N 段」后面会让人以为转码卡在最后一段。
 */
export function preloadProgressText(progress: ProgressLike | null): string | null {
  const p = progress;
  if (!p) return null;
  const parts: string[] = [];
  if (p.phase === "downloading") {
    const speed = formatSpeed(p.speed_bps);
    parts.push(
      [
        (p.total_bytes ?? 0) > 0
          ? `取源中 ${formatBytes(p.downloaded_bytes ?? 0)} / ${formatBytes(p.total_bytes ?? 0)}`
          : "取源中",
        speed,
      ]
        .filter(Boolean)
        .join(" · "),
    );
  } else if (p.phase === "finalizing") {
    parts.push("拼接登记中");
  } else if (p.segment_index != null && (p.segment_total ?? 0) > 0) {
    parts.push(`转码第 ${p.segment_index}/${p.segment_total} 段`);
  }
  if ((p.eta_seconds ?? 0) > 0) parts.push(`剩余约 ${Math.ceil((p.eta_seconds ?? 0) / 60)} 分钟`);
  return parts.length > 0 ? parts.join(" · ") : null;
}

/** 「进行中」行的短进度尾缀：完整进度与进度条在详情视图的卡片上。 */
export function preloadProgressShort(progress: ProgressLike | null): string | null {
  const p = progress;
  if (!p) return null;
  if (p.phase === "downloading") {
    const speed = formatSpeed(p.speed_bps);
    const base =
      (p.total_bytes ?? 0) > 0
        ? `取源中 ${Math.min(99, Math.round(((p.downloaded_bytes ?? 0) / (p.total_bytes ?? 1)) * 100))}%`
        : "取源中";
    return speed ? `${base} · ${speed}` : base;
  }
  if (p.phase === "finalizing") return "拼接登记中";
  if (p.segment_index != null && (p.segment_total ?? 0) > 0) {
    return `第 ${p.segment_index}/${p.segment_total} 段`;
  }
  return null;
}
