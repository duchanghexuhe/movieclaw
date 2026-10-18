"""add preload_task.progress: real-time progress reported by the Worker

预转码详细进度（活动页进行中卡片）：Worker 进度上报（下载量/第几段/
剩余时间）落 preload_task.progress JSON 列，服务端转发给网页端。
向前兼容：只加一个可空列，无数据改写；回退到旧版本时旧代码不读它。

Revision ID: a3d8f1c2b4e5
Revises: 9f2c4b7d8e10
Create Date: 2026-10-16 19:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3d8f1c2b4e5"
down_revision: str | None = "9f2c4b7d8e10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("preload_task", schema=None) as batch_op:
        batch_op.add_column(sa.Column("progress", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("preload_task", schema=None) as batch_op:
        batch_op.drop_column("progress")
