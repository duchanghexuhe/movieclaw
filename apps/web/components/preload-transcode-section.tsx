"use client";

import { useCallback, useEffect, useState } from "react";

import { Banner } from "@/components/cloud-push-ui";
import { useConfirm, useToast } from "@/components/feedback";
import {
  SETTINGS_BUTTON_CLASS,
  SETTINGS_INPUT_CLASS,
  SettingsEmpty,
  SettingsList,
  SettingsRow,
  SettingsSection,
  SettingsTabs,
} from "@/components/settings-ui";
import {
  PRELOAD_STATUS_LABELS,
  type PreloadOverview,
  type PreloadPreset,
  type PreloadTaskView,
  type PreloadVideoFormat,
  type StaleCleanupResult,
  cancelPreloadTask,
  cleanupStalePreload,
  getPreloadOverview,
  getPreloadPresets,
  getPreloadSettings,
  listPreloadTasks,
  preloadTaskTitle,
  retryPreloadTask,
  savePreloadSettings,
} from "@/lib/api/preload";

const GB = 1024 ** 3;

function formatGB(bytes: number): string {
  return `${(bytes / GB).toFixed(1)} GB`;
}

/** 清理预览的一行：《片名》SxxEyy（电影无季集号）；未识别的条目落回产物编号。 */
function cleanupEntryTitle(file: StaleCleanupResult["files"][number]): string {
  if (!file.title) return `未识别条目的预转码版本 #${file.file_id}`;
  const unit =
    file.season_number > 0
      ? ` S${String(file.season_number).padStart(2, "0")}E${String(file.episode_number).padStart(2, "0")}`
      : "";
  return `《${file.title}》${unit}`;
}

/**
 * 失败原因：长报错（ffmpeg stderr 尾巴服务端截到 2000 字符）折叠成一行 +
 * 「查看详情」展开，短消息直接平铺。展开状态行内自管——每行任务独立。
 */
function TaskErrorText({ error }: { error: string }) {
  const [expanded, setExpanded] = useState(false);
  const collapsible = error.includes("\n") || error.length > 100;
  if (!collapsible) {
    return <span className="mt-1 block text-[var(--warn)]">{error}</span>;
  }
  return (
    <span className="mt-1 block text-[var(--warn)]">
      <span
        className={
          expanded
            ? "block whitespace-pre-wrap break-words [overflow-wrap:anywhere]"
            : "block truncate"
        }
      >
        {error}
      </span>
      <button
        type="button"
        className="mt-0.5 block text-caption underline underline-offset-2"
        onClick={() => setExpanded((value) => !value)}
      >
        {expanded ? "收起" : "查看详情"}
      </button>
    </span>
  );
}

/**
 * 预转码总量三格（preload-transcode.md §6）：共占/共 N 部 · 配额 | 最老未播 |
 * 未播阈值（自动清理开）。设置页预转码分区与缓存管理页的「预转码产物」
 * 区块共用这一套规格（§6 入口展示）。
 */
export function PreloadStatsGrid({ overview }: { overview: PreloadOverview }) {
  return (
    <div className="grid grid-cols-3 gap-3 rounded-xl border border-white/[0.06] bg-white/[0.02] px-4 py-3 text-sub">
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
  );
}

/**
 * 「媒体库」分区的预转码设置（preload-transcode.md §6）。
 *
 * 管理任务队列（取消/重试）与「清理冷数据」（预览 → 执行）。Worker 的
 * 配对与授权在「设备」分区（与实时转码是同一个 Worker；两种活
 * 的运行状态都在「转码」页看——实时节看长连接，本节看任务队列）。
 */
export function TranscodeSection() {
  const [overview, setOverview] = useState<PreloadOverview | null>(null);
  const [tasks, setTasks] = useState<PreloadTaskView[] | null>(null);
  const [tasksError, setTasksError] = useState(false);
  const [presets, setPresets] = useState<PreloadPreset[]>([]);
  // 编码格式：本地值即时切换（乐观更新），失败回滚到已保存值
  const [videoFormat, setVideoFormat] = useState<PreloadVideoFormat>("h264");
  const [savedFormat, setSavedFormat] = useState<PreloadVideoFormat>("h264");
  const [cleanupPreview, setCleanupPreview] = useState<StaleCleanupResult | null>(null);
  const [staleDays, setStaleDays] = useState("90");
  // 预览时的天数快照：确认执行用的是它，不是输入框的当前值——预览与确认
  // 之间用户改输入框，删除集合必须还是预览过的那份
  const [previewDays, setPreviewDays] = useState<number | null>(null);
  const [cleaning, setCleaning] = useState(false);
  const toast = useToast();
  const confirm = useConfirm();

  const reload = useCallback(() => {
    listPreloadTasks()
      .then((data) => {
        setTasks(data);
        setTasksError(false);
      })
      .catch(() => setTasksError(true));
    getPreloadOverview()
      .then(setOverview)
      .catch(() => {});
  }, []);
  useEffect(reload, [reload]);
  // 任务在 Worker 侧推进，设置页定期跟随（任务中心已由全局 Provider 轮询）
  useEffect(() => {
    const timer = setInterval(reload, 15_000);
    return () => clearInterval(timer);
  }, [reload]);
  useEffect(() => {
    getPreloadPresets()
      .then(setPresets)
      .catch(() => {});
    getPreloadSettings()
      .then((s) => {
        setVideoFormat(s.video_format);
        setSavedFormat(s.video_format);
      })
      .catch(() => {});
  }, []);

  // 切格式即存：成功后重拉档位（标题随格式变）。失败回滚 + 报错。
  const changeFormat = useCallback(
    (next: PreloadVideoFormat) => {
      if (next === savedFormat) return;
      setVideoFormat(next);
      savePreloadSettings(next)
        .then((s) => {
          setSavedFormat(s.video_format);
          return getPreloadPresets().then(setPresets);
        })
        .catch((e) => {
          setVideoFormat(savedFormat);
          toast.error(e instanceof Error ? e.message : "保存失败，请稍后重试");
        });
    },
    [savedFormat, toast],
  );

  const previewCleanup = useCallback(async () => {
    setCleaning(true);
    try {
      const days = Number(staleDays) || undefined;
      const result = await cleanupStalePreload(days, false);
      setCleanupPreview(result);
      setPreviewDays(days ?? null);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "预览失败，请稍后重试");
    } finally {
      setCleaning(false);
    }
  }, [staleDays, toast]);

  const runCleanup = useCallback(async () => {
    if (!cleanupPreview) return;
    const ok = await confirm({
      title: `清理 ${cleanupPreview.count} 个长期未播的预转码产物？`,
      description: `将释放约 ${formatGB(cleanupPreview.total_bytes)}。删除后可在影片详情页重新下单转码。`,
      confirmLabel: "确认清理",
      cancelLabel: "先不",
      tone: "danger",
    });
    if (!ok) return;
    setCleaning(true);
    try {
      const result = await cleanupStalePreload(previewDays ?? undefined, true);
      toast.success(`已清理 ${result.count} 个版本，释放约 ${formatGB(result.freed_bytes ?? 0)}`);
      setCleanupPreview(null);
      setPreviewDays(null);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "清理失败，请稍后重试");
    } finally {
      setCleaning(false);
    }
  }, [cleanupPreview, confirm, previewDays, toast]);

  return (
      <SettingsSection
        title="预转码"
        description="转码 Worker 闲时把指定影片提前转成小体积版本，档位只有省流/高清两档；播放时选了对应清晰度就直接发预转文件，不用实时转码。同一个 Worker 播放时接实时单、闲时接预转码单，在「设备」分区配对；任务进度看「活动」页。"
      >
      {overview && (
        <div className="mb-4">
          <PreloadStatsGrid overview={overview} />
        </div>
      )}
      <div className="mb-4">
        <SettingsList>
          <SettingsRow
            label="预转码格式"
            description={
              videoFormat === "hevc"
                ? "省流档即可到 1080p · 1.5M——同等流量比 H.264 清晰度高得多；高清档 1080p · 4M 画质更佳。近几年手机/平板及 Safari、Edge 支持；Firefox 和部分老设备不支持，会回退服务器实时转码。拿不准就选 H.264。"
                : "所有设备都能直接播放——各类浏览器、老手机、电视盒通吃。省流档 480p、高清档 720p，与播放页清晰度对应，选了对应清晰度就直接用预转文件。推荐大多数部署使用。"
            }
          >
            <SettingsTabs<PreloadVideoFormat>
              tabs={[
                { id: "h264", label: "H.264" },
                { id: "hevc", label: "HEVC" },
              ]}
              value={videoFormat}
              onChange={changeFormat}
            />
          </SettingsRow>
        </SettingsList>
      </div>
      {tasksError ? (
        <Banner tone="warn">
          任务列表加载失败。请检查与服务端的连接后重试
          <button type="button" className="ml-2 underline underline-offset-2" onClick={reload}>
            重试
          </button>
        </Banner>
      ) : tasks === null ? (
        <SettingsEmpty title="加载中…" />
      ) : (
        <div className="space-y-10">
          <SettingsList>
            {tasks.length === 0 ? (
              <SettingsEmpty title="还没有预转码任务" description="在影片详情页顶部的「预转码」里下单。" />
            ) : (
              tasks.map((task) => (
                <SettingsRow
                  key={task.id}
                  label={
                    <>
                      任务 #{task.id}
                      <span className="ml-2 text-caption font-normal text-[var(--text-faint)]">
                        {[
                          presets.find((p) => p.id === task.preset_id)?.short ?? task.preset_id,
                          // 这单转的是哪部片子——没有身份（未识别/源已删）才落回文件号
                          preloadTaskTitle(task),
                          task.source_quality,
                        ]
                          .filter(Boolean)
                          .join(" · ")}
                      </span>
                    </>
                  }
                  description={
                    <>
                      {PRELOAD_STATUS_LABELS[task.status]} · 预估 {formatGB(task.estimated_bytes)}
                      {task.actual_bytes > 0 && ` · 实际 ${formatGB(task.actual_bytes)}`}
                      {task.error && <TaskErrorText error={task.error} />}
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
                    <>
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
                    </>
                  )}
                </SettingsRow>
              ))
            )}
          </SettingsList>

          <SettingsSection
            title="清理冷数据"
            description="删除「N 天未播」的预转码产物。执行前先预览将删列表与可释放总量。"
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
                  "没有超过阈值的预转码产物。"
                ) : (
                  <>
                    将删除 {cleanupPreview.count} 个版本，释放约{" "}
                    {formatGB(cleanupPreview.total_bytes)}。
                    <ul className="scroll-thin mt-2 max-h-56 space-y-1 overflow-y-auto">
                      {cleanupPreview.files.map((file) => (
                        <li
                          key={file.file_id}
                          className="flex items-baseline justify-between gap-3 text-caption"
                        >
                          <span className="min-w-0 truncate">{cleanupEntryTitle(file)}</span>
                          <span className="tnum shrink-0 text-[var(--text-faint)]">
                            {formatGB(file.size_bytes)} · 未播 {file.days_since_play} 天
                          </span>
                        </li>
                      ))}
                    </ul>
                    <button
                      type="button"
                      className="mt-2 underline underline-offset-2"
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
        </div>
      )}
    </SettingsSection>
  );
}
