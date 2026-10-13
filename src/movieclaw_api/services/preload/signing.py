"""预转码数据面的短时签名（docs/design/preload-transcode.md §7）。

与 remote_signing 同构但独立 salt 域：预转码凭据签的是**一个任务**的取源与
产物回传，TTL 拉长到任务周期（小时级转码 + 闲时分段上传）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from itsdangerous import BadSignature, URLSafeSerializer

from movieclaw_api.services.auth import get_signing_secret

_PRELOAD_SALT = "movieclaw.preload.worker.v1"
#: 任务级凭据：覆盖一次长转码 + 分段上传；租约机制另有续期语义
DEFAULT_PRELOAD_TTL_S = 24 * 3600


@dataclass(frozen=True)
class PreloadGrant:
    """预转码数据面凭据允许访问的范围。"""

    task_id: int
    file_id: int
    kind: str  # preload.source | preload.artifact
    expires_at: int


async def issue_preload_grant(
    *, task_id: int, file_id: int, kind: str = "preload.source", ttl_seconds: int = DEFAULT_PRELOAD_TTL_S
) -> str:
    """给一个任务签发数据面凭据。

    同一凭据串同时用于取源（``preload.source``）与产物回传
    （``preload.artifact``）——两者都只属于这个任务，端点侧按 kind 二次校验：
    签发时 kind 记 ``preload.source``，校验函数接受同任务的两种 kind。
    """
    serializer = URLSafeSerializer(await get_signing_secret(), salt=_PRELOAD_SALT)
    return serializer.dumps(
        {
            "t": task_id,
            "f": file_id,
            "k": kind,
            "exp": int(time.time()) + ttl_seconds,
        }
    )


async def verify_preload_grant(
    token: str | None, *, task_id: int, kind: str
) -> PreloadGrant | None:
    """验证签名、用途与任务范围；任何不符返回 ``None``。

    kind 校验放宽到 {preload.source, preload.artifact} 之一匹配签发域即可：
    凭据是任务级的，端点自己已经限定了它是取源还是回传。
    """
    if not token:
        return None
    serializer = URLSafeSerializer(await get_signing_secret(), salt=_PRELOAD_SALT)
    try:
        payload = serializer.loads(token)
    except BadSignature:
        return None
    if not isinstance(payload, dict):
        return None
    try:
        grant = PreloadGrant(
            task_id=int(payload["t"]),
            file_id=int(payload["f"]),
            kind=str(payload["k"]),
            expires_at=int(payload["exp"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
    if grant.expires_at <= int(time.time()):
        return None
    if grant.task_id != task_id or grant.kind not in {"preload.source", "preload.artifact"}:
        return None
    return grant
