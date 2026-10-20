"""merge upstream v0.41.0 playback_preference into the local migration line

合并上游 v0.41.0（53 提交）后 alembic 出现两个 head，升级会直接报错：

  - 本机一线：67e72d553e7e（drives / preload-transcode / 订阅上限三线合流）
  - 上游新增：f7d2b4e8a139 → a3e9c5d2b814 → b8d4f1a6c372 → c5f1a8e3d9b2
    （library_file_replica 随删、strm 上传副本清理、playback_preference 表）
    两条线都挂在 f7d2b4e8a139 底下。

**为什么用 merge 节点而不是把 down_revision 改成串行**：同 67e72d553e7e /
1cde1f667494——串行改法会让已经跑过本机那一条的库（NAS 生产库正停在
67e72d553e7e）把上游这条线当成"已应用"跳过，表里缺 playback_preference 等对象。
merge 节点会让这类库先补上没跑过的上游迁移，再落到本节点。

本节点自身不改任何 schema（两条线改的表和列互不相干，纯粹是拓扑合流）。

Revision ID: a7f3c2e91b04
Revises: 67e72d553e7e, c5f1a8e3d9b2
Create Date: 2026-10-20 18:15:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "a7f3c2e91b04"
down_revision: str | Sequence[str] | None = ("67e72d553e7e", "c5f1a8e3d9b2")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """空操作：两条线各自的 upgrade 已经做完了，这里只负责合流。"""


def downgrade() -> None:
    """空操作：回退时拓扑重新分叉，两条线各自的 downgrade 负责真正的回退。"""
