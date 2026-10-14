"""预转码的用户面路由（docs/design/preload-transcode.md §3/§6）。

成员区：下单/取消/重试/任务列表/冷数据清理——全部挂登录鉴权（router.py
成员区统一注入），下单与产物删除的管理边界在服务层（谁能点详情页按钮谁
就能给自己看得见的版本下单；配额与失效是机制行为，不是权限）。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import NotFoundException
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.preload import service as preload_service
from movieclaw_api.services.preload.ffmpeg_preload import PRESETS, estimate_size_bytes
from movieclaw_db.engine import get_session
from movieclaw_db.models import PreloadTask, PreloadTaskStatus

router = APIRouter(prefix="/preload", tags=["preload"])


class PreloadCreatePayload(BaseModel):
    file_id: int = Field(description="源文件 id（库里的原始版本）")
    preset_id: str = Field(default="portable_1080p", description="目标档位")


class PreloadEstimatePayload(BaseModel):
    file_id: int
    preset_id: str = "portable_1080p"


class StaleCleanupPayload(BaseModel):
    days: int | None = Field(default=None, ge=1, description="N 天未播；缺省用设置值")
    confirm: bool = Field(default=False, description="false=预览；true=执行删除")


def _task_view(task: PreloadTask, session: AsyncSession) -> dict[str, Any]:
    return {
        "id": task.id,
        "target_file_id": task.target_file_id,
        "preset_id": task.preset_id,
        "status": task.status,
        "estimated_bytes": task.estimated_bytes,
        "actual_bytes": task.actual_bytes,
        "resume_parts": task.resume_parts,
        "attempts": task.attempts,
        "error": task.error,
        "result_file_id": task.result_file_id,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }


@router.get(
    "/presets",
    summary="可用档位",
    operation_id="preload.presets",
)
async def list_presets() -> ApiResponse[list[dict[str, str]]]:
    return ok([{"id": pid, "title": p["title"]} for pid, p in PRESETS.items()])


@router.post(
    "/estimate",
    summary="体积预估（价格标签）",
    operation_id="preload.estimate",
)
async def estimate(
    payload: PreloadEstimatePayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict[str, Any]]:
    """点「转码为便携版本」时先展示预估体积与当前剩余配额（§6 价格标签）。"""
    file = await preload_service.get_target_file(session, payload.file_id)
    estimated = estimate_size_bytes(duration_seconds=file.duration_seconds)
    setting = await get_setting_store_dep()
    used = await preload_service.preload_usage_bytes(session)
    return ok(
        {
            "estimated_bytes": estimated,
            "quota_bytes": setting.quota_bytes,
            "used_bytes": used,
            "free_bytes": max(setting.quota_bytes - used, 0),
        }
    )


async def get_setting_store_dep():
    from movieclaw_api.settings.store import get_setting_store
    from movieclaw_api.settings import PreloadSetting

    return await get_setting_store().get(PreloadSetting)


@router.get(
    "/overview",
    summary="预转码统计（共 X GB / 共 Y 部 / 最老 Z 天前）",
    operation_id="preload.overview",
)
async def overview(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict[str, Any]]:
    return ok(await preload_service.overview(session))


@router.post(
    "/tasks",
    summary="下单预转码任务",
    operation_id="preload.create",
)
async def create_task(
    payload: PreloadCreatePayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict[str, Any]]:
    task = await preload_service.create_preload_task(
        session, file_id=payload.file_id, preset_id=payload.preset_id
    )
    return ok(_task_view(task, session), message="已加入预转码队列，等 Worker 领取")


@router.get(
    "/tasks",
    summary="任务列表（最近 100 条）",
    operation_id="preload.tasks",
)
async def list_tasks(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[dict[str, Any]]]:
    tasks = await preload_service.list_tasks(session)
    return ok([_task_view(t, session) for t in tasks])


@router.get(
    "/tasks/{task_id}",
    summary="任务详情",
    operation_id="preload.task.get",
)
async def get_task(
    task_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict[str, Any]]:
    task = await session.get(PreloadTask, task_id)
    if task is None:
        raise NotFoundException("任务不存在")
    return ok(_task_view(task, session))


@router.post(
    "/tasks/{task_id}/cancel",
    summary="取消任务",
    operation_id="preload.task.cancel",
)
async def cancel_task(
    task_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict[str, Any]]:
    task = await preload_service.cancel_task(session, task_id)
    return ok(_task_view(task, session), message="任务已取消")


@router.post(
    "/tasks/{task_id}/retry",
    summary="失败任务重试",
    operation_id="preload.task.retry",
)
async def retry_task(
    task_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict[str, Any]]:
    task = await preload_service.retry_task(session, task_id)
    return ok(_task_view(task, session), message="已重新排队")


@router.delete(
    "/files/{file_id}",
    summary="删除预转码产物版本",
    operation_id="preload.file.delete",
)
async def delete_preload_file(
    file_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[None]:
    await preload_service.delete_derived_file(session, file_id)
    return ok(None, message="预转码产物已删除")


@router.post(
    "/cleanup-stale",
    summary="清理冷数据（N 天未播）",
    operation_id="preload.cleanup-stale",
)
async def cleanup_stale(
    payload: StaleCleanupPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict[str, Any]]:
    """预览：``confirm=false`` 返回将删列表与可释放总量；确认：执行删除（§6）。"""
    setting = await get_setting_store_dep()
    days = (payload.days if payload else None) or setting.stale_days
    result = await preload_service.cleanup_stale(
        session, days=days, dry_run=not (payload.confirm if payload else False)
    )
    return ok(result)
