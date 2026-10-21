"""merge upstream smart_memory into the local migration line

合并上游「智能订阅记忆」（c9e1888c，smart_memory 表 + subscription.smart_overrides）
之后 alembic 又出现两个 head，`upgrade head` 会直接报 MultipleHeads：

  - 本机一线：67e72d553e7e → a7f3c2e91b04（drives / preload / 订阅上限三条线合流）
  - 上游新增：c5f1a8e3d9b2 → d7a2e4f9c615（smart_memory 表 + smart_overrides 列）

两条线都挂在 c5f1a8e3d9b2 底下，所以和 a7f3c2e91b04 那个节点同样的做法：
用一个空操作的 merge 节点把拓扑合流，而不是改 down_revision 串起来。

**为什么必须用 merge 节点**：已经跑过 a7f3c2e91b04 的库（NAS 生产库就停在这条线上）
如果把 d7a2e4f9c615 串到它前面，alembic 会认为这条迁移「已经应用过」而跳过，
结果表里缺 smart_memory / smart_overrides，智能订阅一用就炸。merge 节点会让这类库
先补跑 d7a2e4f9c615，再落到本节点。

本节点自身不改任何 schema（两条线改的表和列互不相干）。

Revision ID: 3d1e07af258c
Revises: a7f3c2e91b04, d7a2e4f9c615
Create Date: 2026-10-21 17:10:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "3d1e07af258c"
down_revision: str | Sequence[str] | None = ("a7f3c2e91b04", "d7a2e4f9c615")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """空操作：两条线各自的 upgrade 已经做完了，这里只负责合流。"""


def downgrade() -> None:
    """空操作：回退时拓扑重新分叉，两条线各自的 downgrade 负责真正的回退。"""
