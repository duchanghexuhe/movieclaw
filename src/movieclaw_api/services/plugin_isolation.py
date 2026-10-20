"""插件进程隔离（docs/design/plugin-phase3.md §6.1、§6.3，C6b）。

宿主以 root 运行时（Docker 镜像里就是），进程外插件改以非特权用户运行（默认 65534 ``nobody``，
``MOVIECLAW_PLUGIN_UID`` 可改，写 ``off`` 关闭）。非特权用户读不到数据目录（NAS 上 ``/app/data``
是 0700），连 overlay 部署的代码都在它下面，所以拉起前把要用的代码复制到一个对它可读的运行目录：

- 宿主正在运行的后端源码 → ``/tmp/movieclaw-plugins/src-<指纹>/``（按版本与路径指纹缓存，
  一次启动只复制一次）；
- 插件代码（本地插件的模块 / 插件包的版本目录）→ ``/tmp/movieclaw-plugins/code/<条目 id>/``
  （每次拉起重拷）；
- 插件包的第三方依赖目录 → ``/tmp/movieclaw-plugins/deps/<目录名>/``（目录名含锁文件哈希与平台，
  内容不变，复制一次）；
- 插件自己的临时目录 ``/tmp/movieclaw-plugins/run/<条目 id>/``（归插件用户，作 HOME 与 TMPDIR）。

插件的文件读写一律经文件接口（``PLUGIN_FILES``），持久状态存插件数据，不需要直接碰数据目录。
开发机与 CI 不是 root，不切换用户，也不复制，插件直接用原路径运行。
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("movieclaw_api.plugin_isolation")

ENV = "MOVIECLAW_PLUGIN_UID"
DEFAULT_UID = 65534
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", "._*", ".DS_Store")


def runtime_root() -> Path:
    return Path(tempfile.gettempdir()) / "movieclaw-plugins"


@dataclass(frozen=True)
class Isolation:
    uid: int | None = None
    gid: int | None = None

    @property
    def enabled(self) -> bool:
        return self.uid is not None


def isolation() -> Isolation:
    """当前该不该、以哪个用户运行插件进程。"""
    setting = os.environ.get(ENV, "").strip().lower()
    if setting == "off" or not hasattr(os, "geteuid") or os.geteuid() != 0:
        return Isolation()
    uid = int(setting) if setting.isdigit() else DEFAULT_UID
    if uid == 0:
        logger.warning("%s=0 等于不隔离，插件进程仍以 root 运行", ENV)
        return Isolation()
    return Isolation(uid=uid, gid=uid)


_ACL_XATTRS = ("system.posix_acl_access", "system.posix_acl_default")

#: 这个平台有没有扩展属性（Windows 的 os 没有 removexattr；隔离本身也只在 Linux 宿主上生效）
_HAS_XATTR = hasattr(os, "removexattr")


def _strip_acl(path: str | os.PathLike[str]) -> None:
    """去掉这个文件 / 目录上的 POSIX ACL：权限只由 mode 位说了算。

    带 ACL 时 ``chmod`` 只改 mask 与 other，**点名某个 uid 的 ACE 原样保留**：
    源目录上一条 ``u:<插件 uid>:---`` 会把插件自己锁在门外，一条 ``u:<某用户>:rwx``
    又会让 ``_readable()`` 想收掉的写权限照旧存在。两种情况都是缺陷，所以复制出来
    的副本一律不带 ACL。文件系统不支持 xattr（Windows、部分 tmpfs）时什么都不做。
    """
    if not _HAS_XATTR:
        return
    for name in _ACL_XATTRS:
        with contextlib.suppress(OSError):  # ENODATA / ENOTSUP / EPERM：没有就没有
            os.removexattr(path, name)


def _copy_file(src: str, dst: str, *, follow_symlinks: bool = True) -> str:
    """``copytree`` 用的复制函数：只复制内容，不复制权限与扩展属性。

    ``shutil`` 默认的 ``copy2`` 会经 ``copystat`` 把 xattr（含 POSIX ACL）一起搬过来。
    运行目录是给降权用户看的只读副本，属于哪个用户、谁能不能读，一律由 ``_readable()``
    显式设定；源目录的 ACL、属主、时间戳都不该跟着进来。
    """
    result = shutil.copyfile(src, dst, follow_symlinks=follow_symlinks)
    # 可执行位要留（非 Python 插件的二进制靠它），其余按 _readable 的规则重设
    mode = os.stat(src, follow_symlinks=follow_symlinks).st_mode
    os.chmod(dst, 0o755 if mode & 0o111 else 0o644)
    _strip_acl(dst)
    return result


def _readable(root: Path) -> None:
    """目录 0755、文件 0644：插件用户只能读，不能改。

    逐项重设 mode，并清掉 ACL——复制过来的副本可能带着源目录的 ACL，而 ACL 会让
    这里的 ``chmod`` 部分失效（见 ``_strip_acl``）。
    """
    for dirpath, _dirnames, filenames in os.walk(root):
        _strip_acl(dirpath)
        os.chmod(dirpath, 0o755)
        for name in filenames:
            path = os.path.join(dirpath, name)
            _strip_acl(path)
            mode = os.lstat(path).st_mode
            if stat.S_ISREG(mode):
                os.chmod(path, 0o755 if mode & 0o111 else 0o644)


def _prepare_root() -> Path:
    root = runtime_root()
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o755)
    return root


def stage_source(src: Path, version: str) -> Path:
    """宿主后端源码的只读副本；同一版本、同一来源只复制一次，旧副本顺手清掉。"""
    root = _prepare_root()
    marker = src / "movieclaw_api" / "__init__.py"
    fingerprint = f"{src.resolve()}:{version}:{marker.stat().st_mtime_ns if marker.exists() else 0}"
    key = hashlib.sha1(fingerprint.encode()).hexdigest()[:12]
    target = root / f"src-{key}"
    if (target / ".complete").exists():
        return target
    tmp = root / f".src-{key}.{os.getpid()}.partial"
    shutil.rmtree(tmp, ignore_errors=True)
    # 用 _copy_file 而不是默认的 copy2：源目录的 ACL / 属主不能跟着进运行目录
    shutil.copytree(src, tmp, ignore=_IGNORE, symlinks=False, copy_function=_copy_file)
    _readable(tmp)
    (tmp / ".complete").write_text(fingerprint, encoding="utf-8")
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    os.replace(tmp, target)
    for old in root.glob("src-*"):
        if old != target:
            shutil.rmtree(old, ignore_errors=True)
    logger.info("插件运行目录已就绪：%s", target)
    return target


def stage_plugin(entry_id: str, path: Path, module: str) -> Path:
    """插件代码的只读副本：插件包复制整个版本目录（含 vendor/），本地插件只复制它自己的模块。"""
    target = _prepare_root() / "code" / entry_id
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o755)
    shutil.rmtree(target, ignore_errors=True)
    if (path / "movieclaw-plugin.toml").exists():
        shutil.copytree(path, target, ignore=_IGNORE, symlinks=False, copy_function=_copy_file)
    else:
        target.mkdir()
        if (path / module).is_dir():
            shutil.copytree(
                path / module,
                target / module,
                ignore=_IGNORE,
                symlinks=False,
                copy_function=_copy_file,
            )
        else:
            _copy_file(str(path / f"{module}.py"), str(target / f"{module}.py"))
    _readable(target)
    return target


def stage_dependencies(path: Path) -> Path:
    """依赖目录的只读副本：目录名就是内容（锁文件哈希 + 平台标签），已复制过的直接用。"""
    base = _prepare_root() / "deps"
    base.mkdir(exist_ok=True)
    os.chmod(base, 0o755)
    target = base / path.name
    if (target / ".complete").exists():
        return target
    tmp = base / f".{path.name}.{os.getpid()}.partial"
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(path, tmp, symlinks=False, copy_function=_copy_file)
    _readable(tmp)
    shutil.rmtree(target, ignore_errors=True)
    os.replace(tmp, target)
    return target


def scratch_dir(entry_id: str, iso: Isolation) -> Path:
    """插件自己的临时目录（HOME / TMPDIR），只有插件用户能读写；每次拉起清空。"""
    base = _prepare_root() / "run"
    base.mkdir(exist_ok=True)
    os.chmod(base, 0o755)
    target = base / entry_id
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(mode=0o700)
    if iso.uid is not None and iso.gid is not None:
        os.chown(target, iso.uid, iso.gid)
    return target
