/**
 * 预转码任务列表（设置 → 转码）的排序与分页口径。
 *
 * 抽成纯函数模块的理由与 preload-progress.ts 相同：.tsx 带 JSX，node --test
 * 解析不了，而这两条口径（活跃置顶、按更新时间倒序）是用户在这一页最直接的
 * 感受，值得钉住。
 */
import type { PreloadTaskView } from "./api/preload.ts";

/** 一屏显示多少条；「显示更多」按这个粒度往上加。 */
export const TASK_PAGE_SIZE = 20;

const ACTIVE_STATUSES = ["queued", "claimed", "running"] as const;

/** 进行中的任务（排队/已领取/转码中）。 */
export function isActiveTask(task: PreloadTaskView): boolean {
  return (ACTIVE_STATUSES as readonly string[]).includes(task.status);
}

/**
 * 列表顺序：**进行中的永远排最前**（这一页最常看的是「现在转到哪了」），
 * 同组内按最近更新时间倒序；时间缺失的排最后。
 */
export function orderTasks(tasks: PreloadTaskView[]): PreloadTaskView[] {
  return [...tasks].sort((left, right) => {
    const byActive = Number(!isActiveTask(left)) - Number(!isActiveTask(right));
    if (byActive !== 0) return byActive;
    const leftAt = Date.parse(left.updated_at ?? left.created_at ?? "");
    const rightAt = Date.parse(right.updated_at ?? right.created_at ?? "");
    const leftValid = Number.isFinite(leftAt);
    const rightValid = Number.isFinite(rightAt);
    if (!leftValid && !rightValid) return 0;
    if (!leftValid) return 1;
    if (!rightValid) return -1;
    return rightAt - leftAt;
  });
}

/** 这一单现在能不能删产物：只有已完成、且产物行还在的单子。 */
export function canDeleteProduct(task: PreloadTaskView): boolean {
  return task.status === "completed" && task.result_file_id != null;
}
