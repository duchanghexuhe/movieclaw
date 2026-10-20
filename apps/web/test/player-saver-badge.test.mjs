import assert from "node:assert/strict";
import test from "node:test";

import { saverBadge } from "../lib/player/saver-badge.ts";

/** 会话里标识只看 decision 与 external：其余字段一律给默认值 */
function session({ external = true, video = { action: "copy", height: null }, ...decision } = {}) {
  return {
    external,
    decision: {
      outcome: "plan",
      video,
      uplink_step_down: false,
      portable_source_file_id: null,
      portable_resolution: null,
      ...decision,
    },
  };
}

test("放原画（直连 / 换封装）不标", () => {
  assert.equal(saverBadge(null), null);
  assert.equal(saverBadge(session()), null);
  assert.equal(saverBadge(session({ external: false })), null);
});

test("便携版：标「便携版 · 720p」，切回带原片文件 id", () => {
  const badge = saverBadge(session({ portable_source_file_id: 7, portable_resolution: "720p" }));
  assert.deepEqual(badge, {
    label: "便携版",
    detail: "720p",
    toOriginal: { kind: "portable", fileId: 7 },
  });
});

test("上行省流（原片能直连）：标「省流 · 1080p」，给一键切回原画", () => {
  const badge = saverBadge(
    session({ uplink_step_down: true, video: { action: "transcode", height: 1080 } }),
  );
  assert.deepEqual(badge, { label: "省流", detail: "1080p", toOriginal: { kind: "uplink" } });
});

test("外网线路压低了转码（原片本来就放不了）：标「省流」但不给切回", () => {
  const badge = saverBadge(
    session({ video: { action: "transcode", height: 720, bitrate_cap_bps: 3_000_000 } }),
  );
  assert.deepEqual(badge, { label: "省流", detail: "720p", toOriginal: null });
});

test("内网的转码不标：编码不支持、HDR 是设备放不了原画，不是省流；内网按带宽压码率也不标", () => {
  assert.equal(saverBadge(session({ external: false, video: { action: "transcode", height: 1080 } })), null);
  assert.equal(
    saverBadge(
      session({ external: false, video: { action: "transcode", height: 720, bitrate_cap_bps: 3_000_000 } }),
    ),
    null,
  );
  // 外网但没被线路收紧（按阶梯码率转）也不标
  assert.equal(saverBadge(session({ video: { action: "transcode", height: 1080, bitrate_cap_bps: null } })), null);
});

test("旧版服务端没带 external：只认得出便携版和上行省流", () => {
  const old = session({ video: { action: "transcode", height: 720, bitrate_cap_bps: 3_000_000 } });
  delete old.external;
  assert.equal(saverBadge(old), null);
  const stepDown = session({ uplink_step_down: true, video: { action: "transcode", height: null } });
  delete stepDown.external;
  assert.deepEqual(saverBadge(stepDown), { label: "省流", detail: null, toOriginal: { kind: "uplink" } });
});
