"""add preload_task.video_format + re-key legacy preset ids

预转码格式维度：全局设置（library.preload_transcode.video_format）在
下单时快照到任务行，改设置不影响在途任务。存量行两步迁移：
1. 加可空列 video_format（NULL=读侧回退 h264，兜底在代码里）；
2. 旧档位 ID 改写为新注册表（saver_1080p→saver、portable_1080p→premium），
   并按旧映射回填格式（saver_1080p 本就是 HEVC、portable_1080p 本就是
   H.264）——两档规格在新表下保持不变，历史任务语义零损伤。

Revision ID: b7e4c9d2a6f1
Revises: a3d8f1c2b4e5
Create Date: 2026-10-16 22:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e4c9d2a6f1"
down_revision: str | None = "a3d8f1c2b4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("preload_task", schema=None) as batch_op:
        batch_op.add_column(sa.Column("video_format", sa.String(length=8), nullable=True))
    op.execute(
        "UPDATE preload_task SET video_format = 'hevc' WHERE preset_id = 'saver_1080p'"
    )
    op.execute(
        "UPDATE preload_task SET video_format = 'h264' WHERE preset_id = 'portable_1080p'"
    )
    op.execute("UPDATE preload_task SET preset_id = 'saver' WHERE preset_id = 'saver_1080p'")
    op.execute(
        "UPDATE preload_task SET preset_id = 'premium' WHERE preset_id = 'portable_1080p'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE preload_task SET preset_id = 'saver_1080p' WHERE preset_id = 'saver'"
    )
    op.execute(
        "UPDATE preload_task SET preset_id = 'portable_1080p' WHERE preset_id = 'premium'"
    )
    with op.batch_alter_table("preload_task", schema=None) as batch_op:
        batch_op.drop_column("video_format")
