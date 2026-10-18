"""merge drives, preload-transcode and subscription episode caps

把分叉的三条迁移线并回一条：它们都挂在 5b8d2e6f1a47 底下，合并分支后
alembic 出现三个 head，升级会直接报错。

  - 上游主干的离线下载 / 网盘一线：9f3c6a2e1b58 → … → f7d2b4e8a139
    （member / playback_log、drive_account、library_file_replica 等）
  - 预转码：9f2c4b7d8e10 → a3d8f1c2b4e5 → b7e4c9d2a6f1
    （preload_task，library_file 的 derives_from_file_id / derivation）
  - 订阅上限：c5f1a8d3e7b2（subscription.episode_caps）

**为什么用 merge 节点而不是把 down_revision 改成串行**：同 1cde1f667494——
串行改法会让已经跑过其中某一条的库（预转码的测试部署就是）永远漏掉另外两条，
alembic 只认 alembic_version 里记的 revision，链一改就把没跑过的迁移当成
"已应用"跳过，表里缺列缺表。merge 节点则会让这类库先补上没跑过的，再落到本节点。

本节点自身不改任何 schema（三条线改的表和列互不相干，纯粹是拓扑合流）。

Revision ID: 67e72d553e7e
Revises: f7d2b4e8a139, b7e4c9d2a6f1, c5f1a8d3e7b2
Create Date: 2026-10-18 14:03:37.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "67e72d553e7e"
down_revision: str | Sequence[str] | None = ("f7d2b4e8a139", "b7e4c9d2a6f1", "c5f1a8d3e7b2")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """空操作：三条线各自的 upgrade 已经做完了，这里只负责合流。"""


def downgrade() -> None:
    """空操作：回退时拓扑重新分叉，三条线各自的 downgrade 负责真正的回退。"""
