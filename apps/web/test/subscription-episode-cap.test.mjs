import assert from "node:assert/strict";
import test from "node:test";

import {
  EPISODE_CAP_BATCH,
  EPISODE_CAP_THRESHOLD,
  capsAfter,
  capsPayload,
  continueStep,
  currentCaps,
  draftFromSeasons,
  effectiveCap,
  formatEpisodes,
  isLongSeason,
  rangeLabel,
  sameCaps,
  seasonTotal,
} from "../lib/subscription-episode-cap.ts";
import { subscriptionProgressNote } from "../lib/subscription-ui.ts";

function season(seasonNumber, episodeCount, airedCount = episodeCount ?? 0) {
  return { season_number: seasonNumber, episode_count: episodeCount, aired_count: airedCount };
}

test("只有单季集数达到阈值才提供「先订阅一部分」", () => {
  assert.equal(isLongSeason(season(1, EPISODE_CAP_THRESHOLD - 1)), false);
  assert.equal(isLongSeason(season(1, EPISODE_CAP_THRESHOLD)), true);
  // TMDB 宣称的集数缺失时看已播集数
  assert.equal(isLongSeason(season(1, null, 1043)), true);
  assert.equal(seasonTotal(season(1, 80, 120)), 120);
  // 特别季再长也不问
  assert.equal(isLongSeason(season(0, 500)), false);
});

test("超长的季不动草稿就按默认先订阅一批；选了「全部」才是整季", () => {
  const long = season(1, 1043);
  assert.equal(effectiveCap(long, {}), EPISODE_CAP_BATCH);
  assert.equal(effectiveCap(long, { 1: null }), null);
  assert.equal(effectiveCap(long, { 1: 50 }), 50);
  // 不低于总集数 / 非法值 = 订阅整季，服务端同口径丢弃
  assert.equal(effectiveCap(long, { 1: 1043 }), null);
  assert.equal(effectiveCap(long, { 1: 5000 }), null);
  assert.equal(effectiveCap(long, { 1: 0 }), null);
  // 不长的季没有这个选项，草稿里有数字也无效
  assert.equal(effectiveCap(season(2, 12), {}), null);
  assert.equal(effectiveCap(season(2, 12), { 2: 5 }), null);
});

test("提交内容只含被勾选、且确实截断了本季的季", () => {
  const seasons = [season(1, 1043), season(2, 300), season(3, 12)];
  assert.deepEqual(capsPayload(seasons, new Set([1, 2, 3]), {}), { 1: 20, 2: 20 });
  assert.deepEqual(capsPayload(seasons, new Set([2, 3]), { 2: null }), {});
  assert.deepEqual(capsPayload(seasons, new Set([1]), { 1: 60 }), { 1: 60 });
});

test("存量订阅打开调整弹层不会被默认值悄悄改成先订阅一批", () => {
  const stored = [
    { season_number: 1, tmdb_count: 1043, floor: null, cap: null },
    { season_number: 2, tmdb_count: 300, floor: null, cap: 40 },
  ];
  const seasons = [season(1, 1043), season(2, 300), season(3, 500)];
  const draft = draftFromSeasons(stored);
  assert.deepEqual(draft, { 1: null, 2: 40 });
  // 第 1 季原本就是整季：保持整季；第 2 季保持 40；弹层里新勾上的第 3 季才按默认
  assert.deepEqual(capsPayload(seasons, new Set([1, 2, 3]), draft), { 2: 40, 3: 20 });
  assert.deepEqual(currentCaps(stored), { 2: 40 });
  assert.deepEqual(currentCaps(null), {});
});

test("上限表按内容比较，与键顺序无关", () => {
  assert.equal(sameCaps({ 1: 20, 2: 40 }, { 2: 40, 1: 20 }), true);
  assert.equal(sameCaps({ 1: 20 }, { 1: 40 }), false);
  assert.equal(sameCaps({ 1: 20 }, {}), false);
  assert.equal(sameCaps({}, {}), true);
});

test("继续订阅：每次再加一批，剩下不足一批就一次订完", () => {
  assert.deepEqual(continueStep({ season_number: 1, cap: 20, total: 1043 }), {
    next: 40,
    remaining: 1023,
    last: false,
  });
  assert.deepEqual(continueStep({ season_number: 1, cap: 1030, total: 1043 }), {
    next: 1043,
    remaining: 13,
    last: true,
  });
  // 刚好剩一批：这一步订完整季
  assert.equal(continueStep({ season_number: 1, cap: 80, total: 100 }).last, true);
});

test("改上限时整体替换：其它季原样带回，订到末尾等于去掉这一季的上限", () => {
  const window = { season_number: 1, cap: 20, total: 100 };
  assert.deepEqual(capsAfter({ 1: 20, 2: 50 }, window, 40), { 1: 40, 2: 50 });
  assert.deepEqual(capsAfter({ 1: 20, 2: 50 }, window, 100), { 2: 50 });
});

test("详情页收录范围标出被截断的季", () => {
  assert.equal(rangeLabel([1], [{ season_number: 1, cap: 20, total: 1043 }]), "第 1 季（前 20 集）");
  assert.equal(
    rangeLabel([1, 2], [{ season_number: 2, cap: 1020, total: 1043 }]),
    "第 1 季、第 2 季（前 1,020 集）",
  );
  assert.equal(rangeLabel([0, 1], null), "特别篇、第 1 季");
  assert.equal(formatEpisodes(1043), "1,043");
});

// --- 订阅墙：被截断的订阅下完这一批后不能说「等待新集播出」 ---

function subscription({ status = "active", wanted = 0, grabbed = 0, imported = 20, windows } = {}) {
  return {
    media: { kind: "tv", status: "Ended" },
    status,
    selected_seasons: [1],
    follow_future: false,
    season_collection: [],
    episode_windows: windows,
    progress: { total: wanted + grabbed + imported, wanted, grabbed, downloaded: 0, imported },
  };
}

test("下完这一批：说订阅到第几集、可继续，而不是等新集播出", () => {
  const windows = [{ season_number: 1, cap: 20, total: 1043 }];
  assert.equal(subscriptionProgressNote(subscription({ windows })), "已订阅到第 20 集 · 可继续");
  // 没有上限的订阅保持原样
  assert.equal(subscriptionProgressNote(subscription({})), "等待新集播出");
  // 还在下载时照旧报下载进度
  assert.equal(
    subscriptionProgressNote(subscription({ grabbed: 3, windows })),
    "3 集下载中 · 已入库 20",
  );
});
