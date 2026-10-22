"use client";

/**
 * 订阅上限：一季上千集时先订阅前面一部分（docs/design/subscription-episode-cap.md §3）。
 *
 * - SeasonCapLine：订阅弹层 / 调整订阅里，超长的一季多出的一行
 *   「先订阅前 20 集 · 本季共 1,043 集　全部」。只在单季集数达到阈值时出现
 *   （lib/subscription-episode-cap.ts），对超长的季预填默认值，一步能改回「全部」。
 * - EpisodeWindowBanner：订阅详情页，被截断的季的「继续订阅」入口。
 *
 * 判定全在服务端（episode_windows / episode_seasons），这里只渲染与提交。
 */

import { useEffect, useState } from "react";

import { useToast } from "@/components/feedback";
import {
  updateSubscription,
  type EpisodeWindow,
  type SeasonOverview,
  type SubscriptionDetail,
} from "@/lib/api/subscriptions";
import {
  EPISODE_CAP_BATCH,
  capsAfter,
  continueStep,
  currentCaps,
  formatEpisodes,
  seasonTotal,
} from "@/lib/subscription-episode-cap";

const LINK_BUTTON =
  "shrink-0 text-[var(--accent)] transition hover:opacity-80 disabled:opacity-40";

/**
 * 一季的「先订阅前 N 集」一行。cap 为 null 表示订阅整季；改动经 onChange 交还给调用方
 * 保存在草稿里（订阅弹层随创建提交，调整弹层随保存提交）。
 */
export function SeasonCapLine({
  season,
  cap,
  onChange,
  showName,
  className = "",
}: {
  season: SeasonOverview;
  cap: number | null;
  onChange: (next: number | null) => void;
  /** 勾了多季时行首带上季名，免得分不清是哪一季 */
  showName: boolean;
  className?: string;
}) {
  const total = seasonTotal(season);
  const [text, setText] = useState(cap === null ? "" : String(cap));
  useEffect(() => {
    setText(cap === null ? "" : String(cap));
  }, [cap]);

  // 输入框里的字在失焦 / 回车时才生效：边打边改会让 "2" → "20" 的中间态也被当成上限
  const commit = () => {
    const value = Number(text);
    if (!Number.isInteger(value) || value < 1) {
      setText(cap === null ? "" : String(cap));
      return;
    }
    onChange(value >= total ? null : value);
  };

  const name = showName ? `${season.season_number === 0 ? "特别篇" : `第 ${season.season_number} 季`} · ` : "";
  return (
    <div
      className={`flex flex-wrap items-center gap-x-2 gap-y-1.5 text-sub text-[var(--text-muted)] ${className}`}
    >
      {cap === null ? (
        <>
          <span>
            {name}订阅整季 {formatEpisodes(total)} 集
          </span>
          <button
            type="button"
            className={LINK_BUTTON}
            onClick={() => onChange(EPISODE_CAP_BATCH)}
          >
            先订阅一部分
          </button>
        </>
      ) : (
        <>
          <span>{name}先订阅前</span>
          <input
            type="number"
            inputMode="numeric"
            min={1}
            max={total - 1}
            aria-label={`第 ${season.season_number} 季先订阅的集数`}
            value={text}
            onChange={(event) => setText(event.target.value)}
            onBlur={commit}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                commit();
              }
            }}
            className="w-16 shrink-0 rounded-lg border border-white/[0.1] bg-white/[0.05] px-2 py-1 text-right text-ui tabular-nums text-white/90 outline-none focus:border-white/30"
          />
          <span>集 · 本季共 {formatEpisodes(total)} 集</span>
          <button type="button" className={LINK_BUTTON} onClick={() => onChange(null)}>
            全部
          </button>
        </>
      )}
    </div>
  );
}

/**
 * 订阅详情页：被上限截断的季。告诉用户只订阅了前面一部分，并给出继续的入口——
 * 主按钮再订阅一批，剩下不足一批时一次订完；「订阅全部」清掉上限。
 * 只关注的成员只看提示，不给操作（与集数提示同一规矩）。
 */
export function EpisodeWindowBanner({
  detail,
  canTune,
  onChanged,
}: {
  detail: SubscriptionDetail;
  canTune: boolean;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const windows = detail.episode_windows ?? [];
  if (windows.length === 0) return null;

  const setCap = async (win: EpisodeWindow, next: number, done: string) => {
    setBusy(true);
    try {
      await updateSubscription(detail.id, {
        episode_caps: capsAfter(currentCaps(detail.episode_seasons), win, next),
      });
      toast.success(done);
      onChanged();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "保存失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2">
      {windows.map((win) => {
        const step = continueStep(win);
        const season = win.season_number === 0 ? "特别篇" : `第 ${win.season_number} 季`;
        return (
          <section
            key={win.season_number}
            className="flex flex-wrap items-center gap-x-4 gap-y-3 rounded-xl border border-white/[0.08] bg-white/[0.04] px-4 py-3"
          >
            <div className="min-w-0 flex-1 basis-64">
              <p className="text-ui font-semibold text-white/90">
                已订阅到第 {formatEpisodes(win.cap)} 集
              </p>
              <p className="mt-0.5 text-sub leading-6 text-[var(--text-muted)]">
                {season}共 {formatEpisodes(win.total)} 集，现在只订阅了前{" "}
                {formatEpisodes(win.cap)} 集，其余的集不会搜索，也不会下载。
              </p>
            </div>
            {canTune && (
              <div className="flex shrink-0 gap-2">
                {!step.last && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void setCap(
                        win,
                        win.total,
                        `已订阅${season}全部 ${formatEpisodes(win.total)} 集`,
                      )
                    }
                    className="btn-glass h-9 px-4 text-sub font-medium disabled:opacity-40"
                  >
                    订阅全部
                  </button>
                )}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void setCap(
                      win,
                      step.next,
                      step.last
                        ? `已订阅${season}全部 ${formatEpisodes(win.total)} 集`
                        : `已继续订阅到第 ${formatEpisodes(step.next)} 集`,
                    )
                  }
                  className="btn-accent h-9 rounded-full px-4 text-sub font-semibold disabled:opacity-40"
                >
                  {step.last
                    ? `订阅剩余 ${formatEpisodes(step.remaining)} 集`
                    : `继续订阅 ${EPISODE_CAP_BATCH} 集`}
                </button>
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}
