"""WorkerRuntime 进度台账的最小回归：GUI 状态栏进度行的数据源。"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "worker"))

import worker as worker_mod  # noqa: E402
from worker import WorkerRuntime  # noqa: E402


def test_progress_view_returns_display_and_progress_snapshot():
    rt = WorkerRuntime(server="http://nas", token="t", ffmpeg="ffmpeg")
    rt.tasks[7] = "a.mkv"
    rt._progress[7] = {"phase": "transcoding", "segment_index": 3, "segment_total": 12}
    rt.tasks[9] = "b.mkv"
    view = rt.progress_view()
    assert [name for name, _ in view] == ["a.mkv", "b.mkv"]
    assert view[0][1] == {"phase": "transcoding", "segment_index": 3, "segment_total": 12}
    # 未登记进度的任务（刚接单还没进 process_task）给空 dict，GUI 显示「准备中」
    assert view[1][1] == {}
    # 快照语义：之后原 dict 再变，已返回的视图不受影响
    rt._progress[7]["segment_index"] = 4
    assert view[0][1]["segment_index"] == 3


def test_run_preload_wires_progress_dict_and_clears_it(monkeypatch):
    rt = WorkerRuntime(server="http://nas", token="t", ffmpeg="ffmpeg")
    rt.api = object()  # 假执行器不触网，仅过 _run_preload 的断言
    seen: dict = {}

    def fake_process_task(api, ffmpeg, task, workdir, *, stop=None, register=None,
                          progress=None):
        seen["progress"] = progress
        progress["phase"] = "transcoding"
        return True

    monkeypatch.setattr(worker_mod, "process_task", fake_process_task)
    ok = rt._run_preload({"task_id": 7, "display_name": "a.mkv"})
    assert ok is True
    # process_task 拿到的就是台账 dict 本体（原地更新，不是副本）
    assert seen["progress"]["phase"] == "transcoding"
    # 收尾摘除：任务结束后状态栏不再显示
    assert 7 not in rt.tasks and 7 not in rt._progress
    assert rt.progress_view() == []
