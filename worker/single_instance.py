"""Windows 单实例防呆锁：同机第二个 Worker 进程直接退出。

三入口（remote_worker / preload_worker / preload_worker_gui）共用一个命名
互斥体 ``Global\\MovieClawWorker``：跨登录会话生效，随进程句柄存活——进程
被杀也由内核回收，不存在死锁。同机双开的真实代价是两份 ffmpeg 抢同一块
GPU、两个 WS 连接互相顶掉（同 worker_id 重连会判旧任务失败）。
"""

from __future__ import annotations

import sys

MUTEX_NAME = "Global\\MovieClawWorker"

#: CreateMutexW 在互斥体已存在时返回有效句柄 + ERROR_ALREADY_EXISTS
_ERROR_ALREADY_EXISTS = 183


def acquire_singleton_lock() -> object | None:
    """尝试拿全局互斥体；已有实例返回 None，其余返回一个持有物（进程存活期间有效）。

    非 Windows 恒成功；创建失败（极少见的权限问题）也放行——防呆锁不能
    升级成可用性风险。返回值只需保持在进程生命周期内不被回收即可。
    """
    if sys.platform != "win32":
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return True
    if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(handle)
        return None
    return handle
