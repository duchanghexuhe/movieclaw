"""预转码任务（docs/design/preload-transcode.md §3）。

用户在服务端下的是**任务**（target file → preset），不是直接下"转码"：
服务端维护全局任务队列，任何在线的预转码 Worker 都可以领取。产物登记为
``LibraryFile`` 的派生行（§6 派生物身份），任务台账只管"这件事做没做完"。

任务与产物分两张表的理由：产物是**版本**（进库存台账、参与播放、可删除），
任务是**过程**（排队/派发/失败重试，完成后对用户只剩历史）。产物删了任务
记录还在——"为什么这部片有个 8 GB 的版本"要能追溯到一次明确的下单。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import JSON, BigInteger, Column, ForeignKey, Index, Integer, String, Text
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class PreloadTaskStatus(StrEnum):
    """preload_task.status 的取值——任务状态机的唯一判别式。

    状态迁移只允许经 ``services/preload/service.py`` 的写函数：
    queued → claimed → (running) → completed / failed；
    queued/claimed/running → cancelled；failed → queued（重试=新排队）
    或 cancelled（放弃重试）。
    """

    QUEUED = "queued"  # 已排队，等 Worker 领取
    CLAIMED = "claimed"  # 已被 Worker 领取（有租约，可能 Worker 已失联）
    RUNNING = "running"  # Worker 上报执行中（有进度）
    COMPLETED = "completed"  # 产物已校验并登记为版本
    FAILED = "failed"  # Worker 失败上报 / 租约超时回收，可重试
    CANCELLED = "cancelled"  # 用户取消（产物半成品一并清理）
    # 源被回收/换源/冷数据清理后，曾完成的任务产物已删——区别于失败：
    # 「已失效，可重新转码」（preload-transcode.md §6 洗版联动提示）
    INVALIDATED = "invalidated"


class PreloadTask(TimestampMixin, table=True):
    """一次"把这个源文件转成小体积预转码版本"的任务台账。

    - ``target_file_id`` 是**源**文件（库里的原始版本），产物登记时另立
      ``LibraryFile`` 行并回填 ``id``；
    - ``lease_expires_at`` 是领取租约：Worker 在线时领、离线无碍——租约到期
      仍无进度上报的任务自动回到 queued（断点台账保留，续转不丢）；
    - ``resume_parts`` 是断点续转的台账：Worker 按固定时长分段转码上传，
      服务端记录已收到的连续段数，Worker 重领时跳过这些段。
    """

    __tablename__ = "preload_task"
    __table_args__ = (
        # 排队领取的热路径：Worker 每次领取按状态+入队时间扫队列
        Index("ix_preload_task_queue", "status", "created_at"),
        # 同源同档去重查询
        Index("ix_preload_task_target", "target_file_id", "preset_id"),
    )

    id: int | None = Field(default=None, primary_key=True)

    target_file_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("library_file.id", ondelete="CASCADE"),
            nullable=False,
        ),
        description="源文件（转码对象）；产物另立 library_file 行",
    )
    preset_id: str = Field(default="saver", description="目标档位枚举（§2）")
    video_format: str | None = Field(
        default=None,
        sa_column=Column(String(8), nullable=True),
        description=(
            "下单时快照的编码格式（h264/hevc）：改全局设置不影响在途任务；"
            "NULL=格式维度上线前的历史行（读侧回退 h264）"
        ),
    )

    status: str = Field(
        default=PreloadTaskStatus.QUEUED,
        index=True,
        description="任务状态（PreloadTaskStatus）",
    )
    # 排队前配额预检失败标记：不占队列位，只在详情页提示"超配额暂停"
    quota_blocked: bool = Field(default=False, description="下单时配额预检未过；入队被暂缓")

    # -- 派发与租约 ----------------------------------------------------------
    worker_device_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer, ForeignKey("login_device.id", ondelete="SET NULL"), nullable=True
        ),
        description="当前/最近一次领取的 Worker 设备；NULL=从未领取",
    )
    claimed_at: datetime | None = Field(default=None, description="最近一次领取时间")
    lease_expires_at: datetime | None = Field(
        default=None, description="领取租约到期时间；到期无心跳自动回队"
    )

    # -- 体积预估与实际 ------------------------------------------------------
    estimated_bytes: int = Field(
        default=0,
        sa_column=Column(BigInteger, default=0),
        description="下单时的体积预估（字节）；配额预检与价格标签用",
    )
    actual_bytes: int = Field(
        default=0,
        sa_column=Column(BigInteger, default=0),
        description="产物实际大小；完成时回填（验收：预估偏差 <±20%）",
    )

    # -- 断点续转台账 ----------------------------------------------------------
    # 产物以固定大小分段上传（除最后一段）；服务端记录已完整收到的段数，
    # Worker 重领时从该段续传。0=从头开始
    resume_parts: int = Field(default=0, description="已完整收到的产物分段数（断点续转）")
    part_bytes: int = Field(
        default=0,
        sa_column=Column(BigInteger, default=0),
        description="单段大小（字节）；领取时与 Worker 约定，续转按它对齐",
    )

    # -- 实时进度（Worker 进度上报落台账）--------------------------------------
    # {phase, downloaded_bytes, total_bytes, speed_bps, segment_index,
    #  segment_total, eta_seconds}：活动页进行中卡片据此显示下载量/速度/第几段/剩余时间。
    # 领取与租约回队时清零——旧执行的最后进度不属于新执行
    progress: dict | None = Field(
        default=None,
        sa_column=Column(JSON, nullable=True),
        description="最近一次进度上报（Worker 写、服务端转发）；执行态之外为 NULL",
    )

    # -- 结果与诊断 ----------------------------------------------------------
    result_file_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer, ForeignKey("library_file.id", ondelete="SET NULL"), nullable=True
        ),
        description="产物版本行；完成时回填",
    )
    error: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True), description="最近一次失败原因"
    )
    attempts: int = Field(default=0, description="已被领取的次数（含失败重试）")

    # 完成时间（展示与"最老的 Z 天前"统计）；cancelled/failed 为 NULL
    completed_at: datetime | None = Field(default=None, description="任务完成时间")
