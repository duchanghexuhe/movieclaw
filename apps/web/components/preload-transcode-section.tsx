"use client";

import { useCallback, useEffect, useState } from "react";

import { Banner, Toggle } from "@/components/cloud-push-ui";
import { useConfirm, useToast } from "@/components/feedback";
import {
  SETTINGS_BUTTON_CLASS,
  SETTINGS_INPUT_CLASS,
  SettingsEmpty,
  SettingsList,
  SettingsRow,
  SettingsSection,
} from "@/components/settings-ui";
import {
  type PreloadOverview,
  type PreloadTaskView,
  type StaleCleanupResult,
  cancelPreloadTask,
  cleanupStalePreload,
  getPreloadOverview,
  listPreloadTasks,
  retryPreloadTask,
} from "@/lib/api/preload";

const GB = 1024 ** 3;

function formatGB(bytes: number): string {
  return `${(bytes / GB).toFixed(1)} GB`;
}

const STATUS_LABELS: Record<PreloadTaskView["status"], string> = {
  queued: "排队中",
  claimed: "已领取",
  running: "转码中",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
  invalidated: "已失效（可重新转码）",
};

/**
 * 「媒体库」分区的预转码设置（preload-transcode.md §6）。
 *
 * 管理任务队列（取消/重试）与「清理冷数据」（预览 → 执行）。Worker 的
 * 配对与授权在「设备」分区（与实时转码 Worker 同一套）。
 */
export function TranscodeSection() {
  const [overview, setOverview] = useState<PreloadOverview | null>(null);
  const [tasks, setTasks] = useState<PreloadTaskView[] | null>(null);
  const [cleanupPreview, setCleanupPreview] = useState<StaleCleanupResult | null>(null);
  const [staleDays, setStaleDays] = useState("90");
  const [cleaning, setCleaning] = useState(false);
  const toast = useToast();
  const confirm = useConfirm();

  const reload = useCallback(() => {
    listPreloadTasks()
      .then(setTasks)
      .catch(() => setTasks([]));
    getPreloadOverview()
      .then(setOverview)
      .catch(() => {});
  }, []);
  useEffect(reload, [reload]);

  const previewCleanup = useCallback(async () => {
    setCleaning(true);
    try {
      const result = await cleanupStalePreload(Number(staleDays) || undefined, false);
      setCleanupPreview(result);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "预览失败，请稍后重试");
    } finally {
      setCleaning(false);
    }
  }, [staleDays, toast]);

  const runCleanup = useCallback(async () => {
    if (!cleanupPreview) return;
    const ok = await confirm({
      title: `清理 ${cleanupPreview.count} 个长期未播的便携版本？`,
      description: `将释放约 ${formatGB(cleanupPreview.total_bytes)}。删除后可在影片详情页重新下单转码。`,
      confirmLabel: "确认清理",
      cancelLabel: "先不",
      tone: "danger",
    });
    if (!ok) return;
    setCleaning(true);
    try {
      const result = await cleanupStalePreload(Number(staleDays) || undefined, true);
      toast.success(`已清理 ${result.count} 个版本，释放约 ${formatGB(result.freed_bytes ?? 0)}`);
      setCleanupPreview(null);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "清理失败，请稍后重试");
    } finally {
      setCleaning(false);
    }
  }, [cleanupPreview, confirm, staleDays, toast]);

  return (
    <SettingsSection
      title="预转码"
      description="预转码 Worker 闲时把指定影片转成 1080p H.264 的低码率版本，外网播放时直接发小文件。Worker 在「设备」分区配对。"
    >
      {overview && (
        <div className="mb-4 grid grid-cols-3 gap-3 rounded-xl border border-white/[0.06] bg-white/[0.02] px-4 py-3 text-sub">
          <div>
            <div className="tnum text-body font-semibold text-[var(--text)]">
              {(overview.total_bytes / GB).toFixed(1)} GB
            </div>
            <div className="mt-0.5 text-caption text-[var(--text-faint)]">
              共 {overview.count} 部 · 配额 {(overview.quota_bytes / GB).toFixed(0)} GB
            </div>
          </div>
          <div>
            <div className="tnum text-body font-semibold text-[var(--text)]">
              {overview.oldest_days > 0 ? `${overview.oldest_days} 天` : "—"}
            </div>
            <div className="mt-0.5 text-caption text-[var(--text-faint)]">最老未播</div>
          </div>
          <div>
            <div className="tnum text-body font-semibold text-[var(--text)]">
              {overview.stale_days} 天
            </div>
            <div className="mt-0.5 text-caption text-[var(--text-faint)]">
              未播阈值{overview.auto_cleanup ? " · 自动清理开" : ""}
            </div>
          </div>
        </div>
      )}
      {tasks === null ? (
        <SettingsEmpty title="加载中…" />
      ) : (
        <>
          <SettingsList>
            {tasks.length === 0 ? (
              <SettingsEmpty title="还没有预转码任务" description="在影片详情页的文件区点「转便携版」下单。" />
            ) : (
              tasks.map((task) => (
                <SettingsRow
                  key={task.id}
                  label={
                    <>
                      任务 #{task.id}
                      <span className="ml-2 text-caption font-normal text-[var(--text-faint)]">
                        源文件 #{task.target_file_id}
                      </span>
                    </>
                  }
                  description={
                    <>
                      {STATUS_LABELS[task.status]} · 预估 {formatGB(task.estimated_bytes)}
                      {task.actual_bytes > 0 && ` · 实际 ${formatGB(task.actual_bytes)}`}
                      {task.error && (
                        <span className="mt-1 block text-[var(--warn)]">{task.error}</span>
                      )}
                    </>
                  }
                >
                  {["queued", "claimed", "running"].includes(task.status) && (
                    <button
                      type="button"
                      className={SETTINGS_BUTTON_CLASS}
                      onClick={async () => {
                        try {
                          await cancelPreloadTask(task.id);
                          toast.success("任务已取消");
                          reload();
                        } catch (e) {
                          toast.error(e instanceof Error ? e.message : "取消失败");
                        }
                      }}
                    >
                      取消
                    </button>
                  )}
                  {task.status === "failed" && (
                    <button
                      type="button"
                      className={SETTINGS_BUTTON_CLASS}
                      onClick={async () => {
                        try {
                          await retryPreloadTask(task.id);
                          toast.success("已重新排队");
                          reload();
                        } catch (e) {
                          toast.error(e instanceof Error ? e.message : "重试失败");
                        }
                      }}
                    >
                      重试
                    </button>
                  )}
                </SettingsRow>
              ))
            )}
          </SettingsList>

          <SettingsSection
            title="清理冷数据"
            description="删除「N 天未播」的便携版本。执行前先预览将删列表与可释放总量。"
          >
            <SettingsList>
              <SettingsRow label="未播天数" description="超过这么多天没有播放记录的版本视为冷数据">
                <input
                  className={`${SETTINGS_INPUT_CLASS} w-20 tnum`}
                  inputMode="numeric"
                  value={staleDays}
                  onChange={(e) => setStaleDays(e.target.value.replace(/\D/g, ""))}
                  aria-label="未播天数"
                />
                <button
                  type="button"
                  className={SETTINGS_BUTTON_CLASS}
                  disabled={cleaning}
                  onClick={previewCleanup}
                >
                  预览
                </button>
              </SettingsRow>
            </SettingsList>
            {cleanupPreview && (
              <Banner tone="info">
                {cleanupPreview.count === 0 ? (
                  "没有超过阈值的便携版本。"
                ) : (
                  <>
                    将删除 {cleanupPreview.count} 个版本，释放约{" "}
                    {formatGB(cleanupPreview.total_bytes)}。
                    <button
                      type="button"
                      className="ml-2 underline underline-offset-2"
                      disabled={cleaning}
                      onClick={runCleanup}
                    >
                      确认清理
                    </button>
                  </>
                )}
              </Banner>
            )}
          </SettingsSection>
        </>
      )}
    </SettingsSection>
  );
}
