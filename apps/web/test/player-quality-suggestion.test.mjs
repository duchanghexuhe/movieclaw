// 换低画质的提示：用例逐条照搬 iOS 的 PlaybackRoutingTests（MovieClawTests/PlaybackRoutingTests.swift），
// 两端同一套判据，改一边就要改另一边。
import assert from "node:assert/strict";
import test from "node:test";

import {
  GRACE_SECONDS,
  QualitySuggestion,
  bandwidthRestartNotice,
  recommendedHeight,
  transcodeBitrateBps,
} from "../lib/player/quality-suggestion.ts";

const mbps = 1_000_000;

/** 按秒喂：先正常播 grace 秒，再卡 stallSeconds 秒、恢复 playSeconds 秒，重复 times 次 */
function feed(suggestion, { stallSeconds, playSeconds, times, speed }) {
  for (let i = 0; i < GRACE_SECONDS; i += 1) suggestion.tick({ stalled: false, loadingBps: null });
  for (let t = 0; t < times; t += 1) {
    for (let i = 0; i < stallSeconds; i += 1) suggestion.tick({ stalled: true, loadingBps: speed });
    for (let i = 0; i < playSeconds; i += 1) suggestion.tick({ stalled: false, loadingBps: 0 });
  }
}

const offer = (s, bitrate, height = 2160) => s.offer({ streamBitrateBps: bitrate, currentHeight: height });

test("开播后卡一次、没卡满 8 秒不提示：也许暂停攒一会缓冲就能接着看", () => {
  const s = new QualitySuggestion();
  feed(s, { stallSeconds: 7, playSeconds: 60, times: 1, speed: 5 * mbps });
  assert.equal(offer(s, 76 * mbps), null);
});

test("慢线路上 5 分钟卡 2 次（每次都不到 8 秒）→ 提示一次，推荐 1080p；本单元不再提第二次", () => {
  const s = new QualitySuggestion();
  feed(s, { stallSeconds: 5, playSeconds: 60, times: 2, speed: 54 * mbps });
  assert.deepEqual(offer(s, 76 * mbps), { measuredBps: 54 * mbps, requiredBps: 76 * mbps, maxHeight: 1080 });
  feed(s, { stallSeconds: 8, playSeconds: 60, times: 3, speed: 54 * mbps });
  assert.equal(offer(s, 76 * mbps), null);
});

test("等首帧连续等满 8 秒就提示，不必等开播后再卡", () => {
  const s = new QualitySuggestion();
  s.restartGrace();
  for (let i = 0; i < 7; i += 1) s.tick({ stalled: true, loadingBps: 6 * mbps });
  assert.equal(offer(s, 20.7 * mbps), null);
  s.tick({ stalled: true, loadingBps: 6 * mbps });
  assert.equal(offer(s, 20.7 * mbps)?.maxHeight, 720);
});

test("一次长等按最快那一秒估线路；最快那秒已够码率（等的是别的）不提示", () => {
  const slow = new QualitySuggestion();
  slow.restartGrace();
  for (const speed of [null, null, null, null, 2, 1, 0, 0.5]) {
    slow.tick({ stalled: true, loadingBps: speed === null ? null : speed * mbps });
  }
  assert.deepEqual(offer(slow, 19 * mbps), { measuredBps: 2 * mbps, requiredBps: 19 * mbps, maxHeight: 480 });
  const fast = new QualitySuggestion();
  fast.restartGrace();
  for (const speed of [0.5, 1, 25, 0, 0, 1, 0.2, 0.1]) fast.tick({ stalled: true, loadingBps: speed * mbps });
  assert.equal(offer(fast, 19 * mbps), null);
});

test("跳转后等满 8 秒提示；没等满的跳转不会凑成「反复卡」；拖动中换落点等待重新算", () => {
  const s = new QualitySuggestion();
  feed(s, { stallSeconds: 0, playSeconds: 30, times: 1, speed: 0 });
  s.restartGrace();
  for (let i = 0; i < 8; i += 1) s.tick({ stalled: true, seeking: true, loadingBps: 4 * mbps });
  assert.equal(offer(s, 14.4 * mbps)?.maxHeight, 720);

  const seeks = new QualitySuggestion();
  feed(seeks, { stallSeconds: 0, playSeconds: 30, times: 1, speed: 0 });
  for (let k = 0; k < 4; k += 1) {
    seeks.restartGrace();
    for (let i = 0; i < 11; i += 1) seeks.tick({ stalled: false, loadingBps: null });
    for (let i = 0; i < 7; i += 1) seeks.tick({ stalled: true, seeking: true, loadingBps: 4 * mbps });
    for (let i = 0; i < 30; i += 1) seeks.tick({ stalled: false, loadingBps: null });
  }
  assert.equal(offer(seeks, 14.4 * mbps), null);

  const scrub = new QualitySuggestion();
  for (let k = 0; k < 5; k += 1) {
    scrub.restartGrace();
    for (let i = 0; i < 6; i += 1) scrub.tick({ stalled: true, seeking: true, loadingBps: 4 * mbps });
  }
  assert.equal(offer(scrub, 14.4 * mbps), null);
});

test("速度够还卡、或卡的时候一个字节都没有：不是线路问题，不提示", () => {
  const enough = new QualitySuggestion();
  feed(enough, { stallSeconds: 8, playSeconds: 30, times: 4, speed: 90 * mbps });
  assert.equal(offer(enough, 76 * mbps), null);
  const silent = new QualitySuggestion();
  feed(silent, { stallSeconds: 8, playSeconds: 30, times: 4, speed: 0 });
  assert.equal(offer(silent, 76 * mbps), null);
});

test("起播 / 跳转后 10 秒宽限内的缓冲不算卡；5 分钟前的卡顿过期", () => {
  const s = new QualitySuggestion();
  for (let k = 0; k < 6; k += 1) {
    s.restartGrace();
    for (let i = 0; i < 7; i += 1) s.tick({ stalled: true, loadingBps: 5 * mbps });
    for (let i = 0; i < 30; i += 1) s.tick({ stalled: false, loadingBps: null });
  }
  assert.equal(offer(s, 76 * mbps), null);
  const spread = new QualitySuggestion();
  feed(spread, { stallSeconds: 5, playSeconds: 30, times: 1, speed: 5 * mbps });
  for (let i = 0; i < 360; i += 1) spread.tick({ stalled: false, loadingBps: null });
  feed(spread, { stallSeconds: 5, playSeconds: 30, times: 1, speed: 5 * mbps });
  assert.equal(offer(spread, 76 * mbps), null);
});

test("推荐档位：留两成余量装得下实测速度的最高一档；已在最低档没有可换的", () => {
  assert.equal(recommendedHeight(54 * mbps, 2160), 1080);
  assert.equal(recommendedHeight(4 * mbps, 2160), 720);
  assert.equal(recommendedHeight(1 * mbps, 2160), 480);
  assert.equal(recommendedHeight(54 * mbps, 1080), 720);
  assert.equal(recommendedHeight(1 * mbps, 480), null);
  // 片源高度未知：从阶梯顶上挑
  assert.equal(recommendedHeight(54 * mbps, null), 1080);
});

test("码率未知不提示", () => {
  const s = new QualitySuggestion();
  s.restartGrace();
  for (let i = 0; i < 9; i += 1) s.tick({ stalled: true, loadingBps: 1 * mbps });
  assert.equal(s.offer({ streamBitrateBps: null, currentHeight: 2160 }), null);
  assert.equal(s.offered, false);
});

// 下面是网页独有的（App 几乎总是直出原文件，转码流的码率在那边不是问题）
test("转码流的码率按服务端转码阶梯算，不是片源码率", () => {
  assert.equal(transcodeBitrateBps({ height: 1080, bitrate_cap_bps: null }), 6 * mbps);
  // 向上取最近一档，与 ffmpeg_args.maxrate_for_height 同一规则
  assert.equal(transcodeBitrateBps({ height: 1000, bitrate_cap_bps: null }), 6 * mbps);
  assert.equal(transcodeBitrateBps({ height: 2160, bitrate_cap_bps: null }), 16 * mbps);
  assert.equal(transcodeBitrateBps({ height: 360, bitrate_cap_bps: null }), 1.5 * mbps);
  assert.equal(transcodeBitrateBps({ height: 4320, bitrate_cap_bps: null }), 16 * mbps);
  // 没有目标高度按 1080p
  assert.equal(transcodeBitrateBps({ height: null }), 6 * mbps);
  // 按线路定的上限比阶梯低就取上限（NAS 实测：2.9 Mbps 线路上重开成 480p、1.5 Mbps）
  assert.equal(transcodeBitrateBps({ height: 480, bitrate_cap_bps: 1.5 * mbps }), 1.5 * mbps);
  assert.equal(transcodeBitrateBps({ height: 1080, bitrate_cap_bps: 2 * mbps }), 2 * mbps);
  assert.equal(transcodeBitrateBps({ height: 720, bitrate_cap_bps: 9 * mbps }), 3 * mbps);
});

test("按线路压过码率的转码流，线路够了就不再提示", () => {
  // 4K HDR 转 SDR 按 2.9 Mbps 线路重开成 480p / 1.5 Mbps 后，起播的长等里最快一秒 2.9 Mbps：
  // 拿片源码率（90 Mbps）比会误判「跟不上」、弹一张「这一版需要约 10.7 MB/s」的卡
  const s = new QualitySuggestion();
  s.restartGrace();
  for (let i = 0; i < 9; i += 1) s.tick({ stalled: true, loadingBps: 2.9 * mbps });
  const bitrate = transcodeBitrateBps({ height: 480, bitrate_cap_bps: 1.5 * mbps });
  assert.equal(s.offer({ streamBitrateBps: bitrate, currentHeight: 480 }), null);
  assert.notEqual(s.offer({ streamBitrateBps: 90 * mbps, currentHeight: 2160 }), null);
});

test("按带宽自动重开后的提示：降了分辨率说降到几 p，只压了码率说压了码率", () => {
  assert.equal(bandwidthRestartNotice(1080, 720), "线路较慢，已调到 720p");
  assert.equal(bandwidthRestartNotice(1080, 1080), "线路较慢，已调低码率");
  // 转码计划没带目标高度（按 1080p 阶梯）：服务端只压码率
  assert.equal(bandwidthRestartNotice(null, null), "线路较慢，已调低码率");
});
