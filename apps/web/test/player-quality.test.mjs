import assert from "node:assert/strict";
import test from "node:test";

import {
  QUALITY_MEMORY_LIMIT,
  QUALITY_OPTIONS,
  lastPlaybackNetwork,
  loadQualityFor,
  qualityChangeNeedsRestart,
  qualityLabel,
  qualityLimits,
  rememberPlaybackNetwork,
  rememberQualityFor,
  sourceHeight,
} from "../lib/player/quality.ts";

/** node 环境没有 window：模拟一个内存 localStorage。 */
function withStorage(store) {
  globalThis.window = {
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => {
        store[k] = String(v);
      },
      removeItem: (k) => {
        delete store[k];
      },
    },
  };
  return () => {
    delete globalThis.window;
  };
}

test("默认自动：没记过时读回 null", () => {
  const restore = withStorage({});
  assert.equal(loadQualityFor(42, "lan"), null);
  assert.equal(loadQualityFor(42, "external"), null);
  restore();
});

test("按片记：给一部片选 720p 不影响别的片（原来是全局一个值，选一次之后所有片子都在转码）", () => {
  const store = {};
  const restore = withStorage(store);
  rememberQualityFor(42, "external", 720);
  assert.equal(loadQualityFor(42, "external"), 720);
  assert.equal(loadQualityFor(43, "external"), null);
  // 选回自动 = 删掉这一条；全删光就不留键
  rememberQualityFor(42, "external", null);
  assert.equal(loadQualityFor(42, "external"), null);
  assert.equal(Object.keys(store).length, 0);
  restore();
});

test("在家与外网分开记：外网选的 720p 回家不沿用，家里选的也不带到外网", () => {
  const restore = withStorage({});
  rememberQualityFor(42, "external", 720);
  assert.equal(loadQualityFor(42, "lan"), null);
  rememberQualityFor(42, "lan", 480);
  assert.equal(loadQualityFor(42, "lan"), 480);
  assert.equal(loadQualityFor(42, "external"), 720);
  // 家里选回自动只删家里那条
  rememberQualityFor(42, "lan", null);
  assert.equal(loadQualityFor(42, "lan"), null);
  assert.equal(loadQualityFor(42, "external"), 720);
  restore();
});

test("分网络之前记下的条目按外网读，下次写入时换成新记法", () => {
  const store = { "movieclaw.player.quality-by-title": '{"42":[720,1]}' };
  const restore = withStorage(store);
  assert.equal(loadQualityFor(42, "external"), 720);
  assert.equal(loadQualityFor(42, "lan"), null);
  rememberQualityFor(43, "lan", 480, 2);
  assert.deepEqual(JSON.parse(store["movieclaw.player.quality-by-title"]), {
    "external:42": [720, 1],
    "lan:43": [480, 2],
  });
  restore();
});

test("同一部片新旧记法并存时以新的为准", () => {
  const restore = withStorage({
    "movieclaw.player.quality-by-title": '{"external:42":[480,5],"42":[720,1]}',
  });
  assert.equal(loadQualityFor(42, "external"), 480);
  restore();
});

test("旧版的全局画质键读到就删，不再沿用", () => {
  const store = { "movieclaw.player.quality": "720" };
  const restore = withStorage(store);
  assert.equal(loadQualityFor(42, "external"), null);
  assert.equal("movieclaw.player.quality" in store, false);
  restore();
});

test("最多记 300 条，超出按最久没用的先丢", () => {
  const restore = withStorage({});
  for (let id = 1; id <= QUALITY_MEMORY_LIMIT + 5; id += 1) rememberQualityFor(id, "external", 480, id);
  assert.equal(loadQualityFor(1, "external"), null);
  assert.equal(loadQualityFor(5, "external"), null);
  assert.equal(loadQualityFor(6, "external"), 480);
  assert.equal(loadQualityFor(QUALITY_MEMORY_LIMIT + 5, "external"), 480);
  restore();
});

test("存了阶梯之外的脏值、损坏的 JSON 都回自动", () => {
  let restore = withStorage({ "movieclaw.player.quality-by-title": '{"external:42":[999,1]}' });
  assert.equal(loadQualityFor(42, "external"), null);
  restore();
  restore = withStorage({ "movieclaw.player.quality-by-title": "{not json" });
  assert.equal(loadQualityFor(42, "external"), null);
  restore();
});

test("没有 window（SSR）不抛错，回自动、按在家猜", () => {
  assert.equal(loadQualityFor(42, "external"), null);
  rememberQualityFor(42, "external", 720);
  assert.equal(lastPlaybackNetwork(), "lan");
  rememberPlaybackNetwork("external");
});

test("上一次会话的网络：没记过按在家，记下后下次开会话前按它猜", () => {
  const store = {};
  const restore = withStorage(store);
  assert.equal(lastPlaybackNetwork(), "lan");
  rememberPlaybackNetwork("external");
  assert.equal(lastPlaybackNetwork(), "external");
  rememberPlaybackNetwork("lan");
  assert.equal(lastPlaybackNetwork(), "lan");
  // 脏值按在家
  store["movieclaw.player.last-network"] = "mars";
  assert.equal(lastPlaybackNetwork(), "lan");
  restore();
});

test("上限不低于片源等于没限", () => {
  assert.equal(sourceHeight("2160p"), 2160);
  assert.equal(sourceHeight("1080i"), 1080);
  assert.equal(sourceHeight("SD"), null);
  assert.equal(qualityLimits(null, 2160), false);
  assert.equal(qualityLimits(1080, 1080), false);
  assert.equal(qualityLimits(720, 1080), true);
  assert.equal(qualityLimits(720, null), true);
});

test("选项含自动且高度全部在码率阶梯语义内", () => {
  assert.equal(QUALITY_OPTIONS[0].maxHeight, null);
  for (const option of QUALITY_OPTIONS.slice(1)) {
    assert.equal(typeof option.maxHeight, "number");
  }
  assert.equal(qualityLabel(null), "自动");
  assert.equal(qualityLabel(720), "720p");
});

test("换画质要不要重开：起播还没出画（videoHeight 为 0）照样按片源规格判", () => {
  const change = (over) =>
    qualityChangeNeedsRestart({ copying: true, maxHeight: 720, sourceResolution: "1080p", videoHeight: 0, ...over });
  // NAS 实测的坑：慢线路起播时弹卡点「改用 720p」，0 <= 720 被当成片源不超 720p、没重开
  assert.equal(change({}), true);
  assert.equal(change({ videoHeight: 1080 }), true);
  // 新上限没限住片源：直通计划不变，不重开
  assert.equal(change({ maxHeight: 1080 }), false);
  assert.equal(change({ maxHeight: null }), false);
  // 规格认不出时看出画后的 videoHeight；还没出画就按限住了算
  assert.equal(change({ sourceResolution: null, videoHeight: 720 }), false);
  assert.equal(change({ sourceResolution: null, videoHeight: 1080 }), true);
  assert.equal(change({ sourceResolution: null }), true);
  // 已经在转码：换上限一定重开
  assert.equal(change({ copying: false, maxHeight: 1080 }), true);
  assert.equal(change({ copying: false, maxHeight: null }), true);
});
