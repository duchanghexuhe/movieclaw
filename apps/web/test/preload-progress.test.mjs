import assert from "node:assert/strict";
import test from "node:test";

import { preloadProgressShort, preloadProgressText } from "../lib/preload-progress.ts";

test("收尾阶段必须自成一档：不能退化成「第 N/N 段」或空", () => {
  // 2026-10-20 实测缺陷：Worker 从不把这个 phase 报出去，活动页一直挂着最后一段。
  // 这里把渲染口径钉住——只要 phase=finalizing，就必须说「拼接登记中」。
  const progress = { phase: "finalizing", segment_index: 11, segment_total: 11 };

  assert.equal(preloadProgressText(progress), "拼接登记中");
  assert.equal(preloadProgressShort(progress), "拼接登记中");
  // 关键：不能被段号分支抢走（那会显示成「转码第 11/11 段」）
  assert.ok(!preloadProgressText(progress).includes("段"));
});

test("取源阶段带速度，没有速度时退回纯字节口径", () => {
  const withSpeed = {
    phase: "downloading",
    downloaded_bytes: 15 * 1024 ** 3,
    total_bytes: 20 * 1024 ** 3,
    speed_bps: 113.09 * 1024 ** 2,
    eta_seconds: 300,
  };
  const text = preloadProgressText(withSpeed);
  assert.match(text, /取源中 15\.00 GB \/ 20\.00 GB/);
  // formatBytes 对 ≥100 的值省小数（定宽列设计），所以是 "113 MB/s" 不是 "113.09"
  assert.match(text, /113 MB\/s/);
  assert.match(text, /剩余约 5 分钟/);

  // 旧版 Worker 不发 speed_bps：只剩字节，不能出现空的分隔符
  const noSpeed = { phase: "downloading", downloaded_bytes: 1024 ** 2, total_bytes: 2 * 1024 ** 2 };
  assert.equal(preloadProgressText(noSpeed), "取源中 1.00 MB / 2.00 MB");
  assert.equal(preloadProgressShort(noSpeed), "取源中 50%");
});

test("转码阶段按段报进度；没有进度时给 null", () => {
  assert.equal(
    preloadProgressText({ phase: "transcoding", segment_index: 3, segment_total: 12 }),
    "转码第 3/12 段",
  );
  assert.equal(preloadProgressShort({ phase: "transcoding", segment_index: 3, segment_total: 12 }), "第 3/12 段");
  assert.equal(preloadProgressText(null), null);
  assert.equal(preloadProgressShort(null), null);
  assert.equal(preloadProgressText({}), null);
});
