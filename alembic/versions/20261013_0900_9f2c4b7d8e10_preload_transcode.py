"""add preload transcode: preload_task table and library_file derived columns

预转码（docs/design/preload-transcode.md）：server 维护全局任务队列，独立
Windows Worker 闲时领取任务、跑 ffmpeg、上传整文件产物，产物登记为
library_file 的派生行（derives_from_file_id / derivation）。

向前兼容：只加一张新表与两个可空列，无数据改写。回退到旧版本时旧代码
不读这些列/表，预转码产物退化为普通文件版本照常播放。

Revision ID: 9f2c4b7d8e10
Revises: 5b8d2e6f1a47
Create Date: 2026-10-13 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9f2c4b7d8e10"
down_revision: str | None = "5b8d2e6f1a47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "preload_task",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("target_file_id", sa.Integer(), nullable=False),
        sa.Column("preset_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("quota_blocked", sa.Boolean(), nullable=False),
        sa.Column("worker_device_id", sa.Integer(), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("estimated_bytes", sa.BigInteger(), nullable=False),
        sa.Column("actual_bytes", sa.BigInteger(), nullable=False),
        sa.Column("resume_parts", sa.Integer(), nullable=False),
        sa.Column("part_bytes", sa.BigInteger(), nullable=False),
        sa.Column("result_file_id", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["target_file_id"], ["library_file.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["worker_device_id"], ["login_device.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["result_file_id"], ["library_file.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("preload_task", schema=None) as batch_op:
        batch_op.create_index("ix_preload_task_queue", ["status", "created_at"])
        batch_op.create_index("ix_preload_task_target", ["target_file_id", "preset_id"])
        batch_op.create_index(batch_op.f("ix_preload_task_status"), ["status"], unique=False)

    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.add_column(sa.Column("derives_from_file_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("derivation", sa.JSON(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_library_file_derives_from_file_id"),
            ["derives_from_file_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            "fk_library_file_derives_from_file_id",
            "library_file",
            ["derives_from_file_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.drop_constraint("fk_library_file_derives_from_file_id", type_="foreignkey")
        batch_op.drop_index(batch_op.f("ix_library_file_derives_from_file_id"))
        batch_op.drop_column("derivation")
        batch_op.drop_column("derives_from_file_id")
    with op.batch_alter_table("preload_task", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_preload_task_status"))
        batch_op.drop_index("ix_preload_task_target")
        batch_op.drop_index("ix_preload_task_queue")
    op.drop_table("preload_task")
