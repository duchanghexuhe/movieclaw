import assert from "node:assert/strict";
import test from "node:test";

import {
  TASK_PAGE_SIZE,
  canDeleteProduct,
  isActiveTask,
  orderTasks,
} from "../lib/preload-task-list.ts";

const base = {
  target_file_id: 1,
  preset_id: "saver",
  estimated_bytes: 0,
  actual_bytes: 0,
  resume_parts: 0,
  attempts: 0,
  error: null,
  result_file_id: null,
  progress: null,
  media_title: "示例",
  season_number: 0,
  episode_number: 0,
  source_quality: null,
  artifact_name: null,
  executor: null,
};

function task(id, status, updatedAt, extra = {}) {
  return { ...base, id, status, updated_at: updatedAt, created_at: updatedAt, ...extra };
}

test("进行中的任务永远排最前，同组按更新时间倒序", () => {
  const rows = [
    task(1, "completed", "2026-10-20T10:00:00+00:00"),
    task(2, "running", "2026-10-20T09:00:00+00:00"),
    task(3, "failed", "2026-10-20T12:00:00+00:00"),
    task(4, "queued", "2026-10-20T08:00:00+00:00"),
  ];
  const ordered = orderTasks(rows).map((row) => row.id);
  // 2/4 是进行中（内部按时间倒序），1/3 是已结束（内部按时间倒序）
  assert.deepEqual(ordered, [2, 4, 3, 1]);
});

test("时间缺失的行不抛错，统一排到最后", () => {
  const rows = [
    task(1, "completed", null),
    task(2, "completed", "2026-10-20T10:00:00+00:00"),
  ];
  assert.deepEqual(
    orderTasks(rows).map((row) => row.id),
    [2, 1],
  );
});

test("活跃判定与任务状态口径一致", () => {
  assert.ok(isActiveTask(task(1, "queued", null)));
  assert.ok(isActiveTask(task(1, "claimed", null)));
  assert.ok(isActiveTask(task(1, "running", null)));
  for (const done of ["completed", "failed", "cancelled", "invalidated"]) {
    assert.equal(isActiveTask(task(1, done, null)), false, done);
  }
});

test("只有已完成的单子能删产物（产物行还在）", () => {
  assert.equal(canDeleteProduct(task(1, "completed", null, { result_file_id: 7 })), true);
  // 产物已被别的入口删掉：completed 但 result_file_id 已被置空，不能再删一次
  assert.equal(canDeleteProduct(task(1, "completed", null, { result_file_id: null })), false);
  assert.equal(canDeleteProduct(task(1, "running", null, { result_file_id: 7 })), false);
  assert.equal(canDeleteProduct(task(1, "invalidated", null, { result_file_id: 7 })), false);
});

test("一屏 20 条：列表再长也先给一批", () => {
  assert.equal(TASK_PAGE_SIZE, 20);
});
