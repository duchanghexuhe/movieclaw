import type { EpisodeSeason, EpisodeWindow, SeasonOverview } from "@/lib/api/subscriptions";

/**
 * 订阅上限：一季上千集时先订阅前面一部分（docs/design/subscription-episode-cap.md）。
 *
 * 判定都在服务端（episode_windows / episode_seasons），这里只放两样东西：
 * 什么时候给用户看这个选项，以及订阅弹层里用户改动的草稿怎么变成提交内容。
 */

/**
 * 单季集数达到这个数才提供「先订阅一部分」。更短的季整季订阅就好，不给用户多出
 * 一个与他无关的选项（最少配置、最少交互）。长寿动画、日更剧、国产
 * 漫剧动辄上百上千集，一次全下既占磁盘也未必看得完。
 */
export const EPISODE_CAP_THRESHOLD = 100;

/** 超长的一季默认先订阅的集数；「继续订阅」每次再加这么多。 */
export const EPISODE_CAP_BATCH = 20;

type SeasonCounts = Pick<SeasonOverview, "season_number" | "episode_count" | "aired_count">;

/** 千分位的集数：1,043。 */
export function formatEpisodes(count: number): string {
  return count.toLocaleString("en-US");
}

/** 一季的总集数（弹层只有季概览，取宣称集数与已播集数中大的）。 */
export function seasonTotal(season: Pick<SeasonOverview, "episode_count" | "aired_count">): number {
  return Math.max(season.episode_count ?? 0, season.aired_count);
}

/** 这一季长到值得问一句「先订阅一部分吗」。特别季不算。 */
export function isLongSeason(season: SeasonCounts): boolean {
  return season.season_number > 0 && seasonTotal(season) >= EPISODE_CAP_THRESHOLD;
}

/**
 * 弹层里用户对上限的改动：键缺失 = 没动过（超长的季按默认先订阅一批），
 * null = 用户选了「全部」，数字 = 用户设的集号。
 */
export type CapDraft = Record<number, number | null>;

/** 这一季当前生效的上限；null = 订阅整季（不长、选了全部、或上限不低于总集数）。 */
export function effectiveCap(season: SeasonCounts, draft: CapDraft): number | null {
  if (!isLongSeason(season)) return null;
  const value = draft[season.season_number];
  const cap = value === undefined ? EPISODE_CAP_BATCH : value;
  return cap !== null && cap >= 1 && cap < seasonTotal(season) ? cap : null;
}

/** 提交给服务端的上限：只含被勾选、且确实截断了本季的季。 */
export function capsPayload(
  seasons: readonly SeasonCounts[],
  selected: ReadonlySet<number>,
  draft: CapDraft,
): Record<number, number> {
  const caps: Record<number, number> = {};
  for (const season of seasons) {
    if (!selected.has(season.season_number)) continue;
    const cap = effectiveCap(season, draft);
    if (cap !== null) caps[season.season_number] = cap;
  }
  return caps;
}

/**
 * 已有订阅的草稿起点：订阅范围内每一季都记下它现在的状态（没有上限就是明确的
 * 「全部」），免得存量订阅一打开调整弹层就被默认值悄悄改成「先订阅一批」。
 * 弹层里新勾上的超长季没有记录，仍按默认先订阅一批。
 */
export function draftFromSeasons(seasons: readonly EpisodeSeason[] | null | undefined): CapDraft {
  const draft: CapDraft = {};
  for (const season of seasons ?? []) draft[season.season_number] = season.cap ?? null;
  return draft;
}

/** 详情页已设的上限 {季号: 集号}（整体替换提交时，未改的季原样带回）。 */
export function currentCaps(seasons: readonly EpisodeSeason[] | null | undefined) {
  const caps: Record<number, number> = {};
  for (const season of seasons ?? []) {
    if (season.cap) caps[season.season_number] = season.cap;
  }
  return caps;
}

/** 两份上限是否相同（与键顺序无关）。 */
export function sameCaps(a: Record<number, number>, b: Record<number, number>): boolean {
  const keys = Object.keys(a);
  return keys.length === Object.keys(b).length && keys.every((key) => a[Number(key)] === b[Number(key)]);
}

/** 「继续订阅」这一步：再订阅一批；剩下的不足一批就一次订完。 */
export function continueStep(win: EpisodeWindow): { next: number; remaining: number; last: boolean } {
  const remaining = win.total - win.cap;
  const next = Math.min(win.total, win.cap + EPISODE_CAP_BATCH);
  return { next, remaining, last: next >= win.total };
}

/** 把某一季的上限改成 next 之后的完整上限表；next 不低于总集数就是订阅整季（去掉这一季）。 */
export function capsAfter(
  current: Record<number, number>,
  win: EpisodeWindow,
  next: number,
): Record<number, number> {
  const caps = { ...current };
  if (next >= win.total) delete caps[win.season_number];
  else caps[win.season_number] = next;
  return caps;
}

/** 详情页「收录范围」：第 1 季（前 20 集）、第 2 季。 */
export function rangeLabel(
  selectedSeasons: readonly number[],
  windows: readonly EpisodeWindow[] | null | undefined,
): string {
  const cap = new Map((windows ?? []).map((win) => [win.season_number, win.cap]));
  const parts = selectedSeasons.map((season) => {
    const name = season === 0 ? "特别篇" : `第 ${season} 季`;
    const limit = cap.get(season);
    return limit === undefined ? name : `${name}（前 ${formatEpisodes(limit)} 集）`;
  });
  return parts.join("、");
}
