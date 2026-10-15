"""预转码 Worker 的控制面与数据面（docs/design/preload-transcode.md §7）。

REST 协议，复用 device-auth 的转码凭证（``require_transcode_worker``）与
remote_signing 的签名 URL 域（kind 扩展 ``preload.source`` / ``preload.artifact``）：

- ``POST /preload-worker/claim``：领任务（在线时领、离线无碍；响应含 argv 与
  取源/回传的签名地址）；
- ``POST /preload-worker/tasks/{id}/progress``：进度上报 + 租约续约；
- ``PUT  /preload-worker/tasks/{id}/artifacts/{name}``：产物分段上传（断点续转）；
- ``POST /preload-worker/tasks/{id}/complete``：完成上报（服务端校验 + 登记）；
- ``POST /preload-worker/tasks/{id}/fail``：失败上报（断点保留，可重试）。

与实时转码 WS 的分工：预转码是"整任务派发、小时级执行"，不需要滚动分片
计划与连接级心跳；任务状态就是台账行，Worker 掉线后租约到期自动回队。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi import Path as PathParam
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import ClientDisconnect

from movieclaw_api.api.deps import require_transcode_worker
from movieclaw_api.exceptions import (
    BadRequestException,
    NotFoundException,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.media_probe import probe_media
from movieclaw_api.services.preload import service as preload_service
from movieclaw_api.services.preload.service import (
    ARTIFACT_UPLOAD_NAME,
)
from movieclaw_api.services.preload.signing import (
    issue_preload_grant,
    verify_preload_grant,
)
from movieclaw_db.engine import get_session
from movieclaw_db.models import PreloadTask, PreloadTaskStatus

logger = logging.getLogger("movieclaw_api.preload.worker")

router = APIRouter(prefix="/preload-worker", tags=["preload-worker"])

#: 领取响应/签名 URL 的有效期（签的是整个任务周期，Worker 转完一次上传）
GRANT_TTL_S = 24 * 3600


async def _load_task(session: AsyncSession, task_id: int) -> PreloadTask:
    task = await session.get(PreloadTask, task_id)
    if task is None:
        raise NotFoundException("预转码任务不存在")
    return task

def _assert_claimer(task: PreloadTask, principal) -> None:
    """只有领取它的那台 Worker 能对这个任务做数据面操作。"""
    if task.worker_device_id != principal.device.id:
        raise NotFoundException("任务不属于这台 Worker")


@router.post(
    "/claim",
    summary="预转码 Worker 领取任务",
    operation_id="preload.claim",
    openapi_extra={"x-cli-hidden": True},
)
async def claim_preload_task(
    payload: dict[str, Any] | None = None,
    principal=Depends(require_transcode_worker),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict[str, Any]]:
    """领取下一个排队任务；队列空时返回 ``task: null``（Worker 稍后再来）。

    ``payload.caps`` 是 Worker 上报的本机能力（encoders/filters/hw_decoders），
    服务端据此选 NVENC 或软件编码链；不报按软编。

    源文件是 strm 网盘条目时返回 ``source_url``（跟随跳转的直链），否则返回
    服务端签名 URL：Worker 从这里 Range 读源。产物回传同样是签名 URL。
    """
    claimed = await preload_service.claim_next_task(session, device_id=principal.device.id)
    if claimed is None:
        return ok({"task": None})
    task, file = claimed
    task_id = task.id
    grant = await issue_preload_grant(task_id=task_id, file_id=file.id, ttl_seconds=GRANT_TTL_S)
    command = preload_service.build_task_command(
        task, file, worker_caps=(payload or {}).get("caps")
    )
    # argv 里的 source.mkv 是 Worker 本地落盘的临时文件名；真实取源地址单独给
    # 相对路径带 API 前缀：Worker 直接 server + "/" + path 拼接
    command["source_url"] = f"api/v1/preload-worker/preload-source/{task_id}?token={grant}"
    command["artifact_url"] = (
        f"api/v1/preload-worker/tasks/{task_id}/artifacts/{ARTIFACT_UPLOAD_NAME}?token={grant}"
    )
    command["task_id"] = task_id
    command["display_name"] = Path(file.file_path).name
    command["resume_parts"] = task.resume_parts
    command["part_bytes"] = task.part_bytes
    return ok({"task": command})


@router.post(
    "/tasks/{task_id}/progress",
    summary="预转码进度上报（顺带续约）",
    operation_id="preload.progress",
    openapi_extra={"x-cli-hidden": True},
)
async def report_preload_progress(
    task_id: Annotated[int, PathParam()],
    payload: dict[str, Any] | None = None,
    principal=Depends(require_transcode_worker),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[None]:
    from movieclaw_api.services.preload.service import renew_lease

    task = await _load_task(session, task_id)
    _assert_claimer(task, principal)
    if task.status not in (PreloadTaskStatus.CLAIMED, PreloadTaskStatus.RUNNING):
        raise BadRequestException("任务已不在执行态，停止上报")
    if task.status == PreloadTaskStatus.CLAIMED:
        task.status = PreloadTaskStatus.RUNNING
    await renew_lease(session, task_id)
    await session.commit()
    return ok(None)


@router.put(
    "/tasks/{task_id}/artifacts/{name}",
    summary="预转码产物分段上传（断点续转）",
    operation_id="preload.artifact.put",
    openapi_extra={"x-cli-hidden": True},
)
async def put_preload_artifact(
    task_id: Annotated[int, PathParam()],
    name: Annotated[str, PathParam()],
    request: Request,
    part: Annotated[
        int | None, Query(description="分段号，从 0 开始；缺省按服务端断点台账")
    ] = None,
    token: Annotated[str | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """接收产物的一个分段，写到临时分片 ``<task>.partNNNN``。

    - ``part`` 与服务端台账一致才写（乱序/跳段回 409，Worker 按断点重传）；
    - 半段连接断开截回段首，Worker 重传不写重；
    - 收到的完整段数推进台账（``resume_parts``），Worker 失败重试时按它续传。
    """
    if name != ARTIFACT_UPLOAD_NAME:
        raise NotFoundException("产物名称无效")
    grant = await verify_preload_grant(token, task_id=task_id, kind="preload.artifact")
    if grant is None:
        raise NotFoundException("凭据无效或已过期")
    task = await _load_task(session, task_id)
    if part is None:
        part = task.resume_parts  # 缺省按台账续传位
    # 段号校验在写盘**之前**：乱序/跳段回 409，不留半截文件
    if part < task.resume_parts:
        return Response(status_code=201, headers={"Cache-Control": "no-store"})  # 重传幂等
    if part > task.resume_parts:
        return Response(status_code=409, headers={"Cache-Control": "no-store"})
    directory = preload_service.artifact_dir_for(task_id)
    directory.mkdir(parents=True, exist_ok=True)
    part_path = directory / f"seg{part:05d}.mkv"

    # 段上传时代（§7 断点续转）：一段是 600s 的转码产物（约 300MB），旧的
    # 64MiB byte-range 上限只会在中途断连（服务端断、客户端 10053）。
    # 上限放宽到 2GB，仅作防呆
    part_bytes = max(task.part_bytes or 0, 2 * 1024**3)
    written = 0
    try:
        # **整文件覆盖写**（每段是独立完整的 MKV，不是 byte-range）：
        # 追加模式 + 上次中断的半截文件 = 段损坏、concat 时长全是垃圾
        # （2026-10-14 本地流程实测 514% 时长偏差即此因）
        with open(part_path, "wb") as output:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > part_bytes:
                    output.truncate(0)
                    raise BadRequestException("分段超过大小上限")
                output.write(chunk)
    except ClientDisconnect:
        # 半段不能留：中断的段在重传时整段覆盖
        part_path.unlink(missing_ok=True)
        return Response(status_code=499, headers={"Cache-Control": "no-store"})
    except BaseException:
        part_path.unlink(missing_ok=True)
        raise
    received = await preload_service.record_part_count(session, task_id, part=part)
    return Response(
        content=str(received), status_code=201, headers={"Cache-Control": "no-store"}
    )


@router.post(
    "/tasks/{task_id}/complete",
    summary="预转码任务完成上报",
    operation_id="preload.complete",
    openapi_extra={"x-cli-hidden": True},
)
async def complete_preload_task(
    task_id: Annotated[int, PathParam()],
    payload: dict[str, Any] | None = None,
    principal=Depends(require_transcode_worker),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict[str, Any]]:
    """Worker 上报转码完成：服务端合并分片、探测校验、登记为版本。

    校验失败（时长漂移 >5%、编码不符、探测失败）整件作废并置任务失败——
    不做部分采纳（§7 扩展点 2）。校验通过返回登记后的文件版本 id。
    """
    task = await _load_task(session, task_id)
    _assert_claimer(task, principal)
    if task.status not in (PreloadTaskStatus.CLAIMED, PreloadTaskStatus.RUNNING):
        raise BadRequestException("任务不在执行态")
    directory = preload_service.artifact_dir_for(task_id)
    # 最终名带片名与码率（complete 时按源条目定名），分段拼装到它
    final_path = await preload_service.artifact_path_for(session, task)
    if final_path.exists():
        final_path.unlink()  # 重跑 complete 的兜底
    # 按段号拼接：转码分段（§7 断点续转）每段是独立编码的 MKV，用 concat
    # demuxer -c copy 免重编码拼回整文件；直拷/旧单段形态只有一段，同样适用
    parts = sorted(directory.glob("seg*.mkv"))
    if not parts:
        raise BadRequestException("没有收到任何产物分段")
    concat_list = directory / "concat.txt"
    with open(concat_list, "w", encoding="utf-8") as listing:
        for part_path in parts:
            # 单引号转义防路径特殊字符；absolute 可读（同目录相对亦可）
            listing.write("file '" + part_path.resolve().as_posix() + "'\n")
    concat = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(concat_list),
            "-c", "copy", "-map_chapters", "0", str(final_path),
        ],
        capture_output=True, text=True, timeout=3600,
    )
    concat_list.unlink(missing_ok=True)
    if concat.returncode != 0:
        logger.warning("预转码 concat 失败：task=%s %s", task_id, concat.stderr[-500:])
        raise BadRequestException("产物分段拼接失败，整件作废")
    for part_path in parts:
        part_path.unlink()
    size = final_path.stat().st_size
    if size <= 0:
        raise BadRequestException("产物为空，整件作废")

    spec = probe_media(str(final_path))
    duration = spec.duration_seconds if spec else None
    video_codec = spec.video_codec if spec else None
    try:
        derived = await preload_service.complete_task(
            session,
            task,
            actual_bytes=size,
            duration_seconds=duration,
            video_codec=video_codec,
        )
    except BadRequestException:
        # 整件作废：产物与分片一并清掉，任务置失败
        final_path.unlink(missing_ok=True)
        raise
    # 一个任务结束，空闲出来的并发位立刻可以派下一个
    from movieclaw_api.services.preload.dispatcher import dispatch_queued_tasks

    await dispatch_queued_tasks()
    return ok({"file_id": derived.id, "size_bytes": size})


@router.post(
    "/tasks/{task_id}/fail",
    summary="预转码任务失败上报",
    operation_id="preload.fail",
    openapi_extra={"x-cli-hidden": True},
)
async def fail_preload_task(
    task_id: Annotated[int, PathParam()],
    payload: dict[str, Any] | None = None,
    principal=Depends(require_transcode_worker),
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[None]:
    task = await _load_task(session, task_id)
    _assert_claimer(task, principal)
    error = str((payload or {}).get("error") or "Worker 上报失败")
    await preload_service.fail_task(session, task, error)
    # 失败也释放并发位：下一个排队任务立刻可以派
    from movieclaw_api.services.preload.dispatcher import dispatch_queued_tasks

    await dispatch_queued_tasks()
    return ok(None)


@router.get(
    "/preload-source/{task_id}",
    summary="预转码源文件（Range 读取）",
    operation_id="preload.source",
    openapi_extra={"x-cli-hidden": True},
)
async def preload_source(
    task_id: Annotated[int, PathParam()],
    request: Request,
    token: Annotated[str | None, Query()] = None,
    session: AsyncSession = Depends(get_session),
):
    """给 Worker 提供支持 Range 的源文件读取（与实时转码取源同形态）。"""
    from movieclaw_db.models import LibraryFile
    from movieclaw_playback.streaming import DisconnectAwareFileResponse

    grant = await verify_preload_grant(token, task_id=task_id, kind="preload.source")
    if grant is None:
        raise NotFoundException("凭据无效或已过期")
    await _load_task(session, task_id)  # 任务存在性一并校验
    file = await session.get(LibraryFile, grant.file_id)
    if file is None or not Path(file.file_path).is_file():
        raise NotFoundException("源文件不存在")
    return DisconnectAwareFileResponse(
        file.file_path,
        headers={"Cache-Control": "no-store", "Accept-Ranges": "bytes"},
    )
