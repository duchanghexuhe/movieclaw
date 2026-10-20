"""插件进程隔离（docs/design/plugin-phase3.md §6.1、§6.3，C6b）。

运行目录复制的单测；强制开启隔离（用户就是当前用户，不需要 root）跑一遍真实的进程外插件，
确认它从复制出的运行目录导入代码、HOME 指向自己的临时目录；以 root 运行时再验证真的切换了用户。
"""

from __future__ import annotations

import os
import stat
import sys
import textwrap

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services import durable_events, plugin_isolation, plugin_runtime


@pytest.fixture
def runtime_root(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    monkeypatch.setattr(plugin_isolation, "runtime_root", lambda: root)
    return root


def test_stage_source_copies_once_without_caches(tmp_path, runtime_root) -> None:
    src = tmp_path / "src"
    (src / "movieclaw_api" / "__pycache__").mkdir(parents=True)
    (src / "movieclaw_api" / "__init__.py").write_text("__version__ = 'x'\n")
    (src / "movieclaw_api" / "__pycache__" / "a.pyc").write_bytes(b"x")
    (src / "movieclaw_api" / "secret.py").write_text("X = 1\n")
    os.chmod(src / "movieclaw_api" / "secret.py", 0o600)

    first = plugin_isolation.stage_source(src, "1.0")
    assert (first / "movieclaw_api" / "__init__.py").exists()
    assert not (first / "movieclaw_api" / "__pycache__").exists()
    # 插件用户只读：目录 0755、文件 0644（原来 0600 的也放开读，但不可写）
    assert stat.S_IMODE((first / "movieclaw_api" / "secret.py").stat().st_mode) == 0o644
    assert stat.S_IMODE((first / "movieclaw_api").stat().st_mode) == 0o755
    # 同一版本复用；换版本重拷并清掉旧副本
    assert plugin_isolation.stage_source(src, "1.0") == first
    second = plugin_isolation.stage_source(src, "2.0")
    assert second != first and not first.exists()


def test_stage_plugin_copies_only_its_own_code(tmp_path, runtime_root) -> None:
    plugins = tmp_path / "plugins"
    (plugins / "packages").mkdir(parents=True)
    (plugins / "mine.py").write_text("A = 1\n")
    (plugins / "other.py").write_text("B = 2\n")
    staged = plugin_isolation.stage_plugin("acme.mine", plugins, "mine")
    assert sorted(p.name for p in staged.iterdir()) == ["mine.py"]

    package = tmp_path / "pkg"
    (package / "vendor").mkdir(parents=True)
    (package / "movieclaw-plugin.toml").write_text("")
    (package / "entry.py").write_text("")
    (package / "vendor" / "dep.py").write_text("")
    staged = plugin_isolation.stage_plugin("acme.pkg", package, "entry")
    assert (staged / "vendor" / "dep.py").exists() and (staged / "movieclaw-plugin.toml").exists()

    scratch = plugin_isolation.scratch_dir("acme.pkg", plugin_isolation.Isolation())
    assert stat.S_IMODE(scratch.stat().st_mode) == 0o700


def _acl_supported(path) -> bool:
    """这个文件系统能不能读写 POSIX ACL（tmpfs 可能不行，Windows 一定不行）。"""
    if not sys.platform.startswith("linux") or not hasattr(os, "setxattr"):
        return False
    try:
        os.setxattr(path, "system.posix_acl_access", os.getxattr(path, "system.posix_acl_access"))
    except OSError:
        return False
    return True


def _grant_acl(path, uid: int, perm: int) -> None:
    """给 ``path`` 加一条「点名 uid」的 ACL 条目（模拟 QNAP 共享目录那种源目录）。"""
    import struct

    raw = os.getxattr(path, "system.posix_acl_access")
    entries = [raw[:4]]
    found = False
    for i in range(4, len(raw), 8):
        tag, value, ident = struct.unpack("<HHI", raw[i : i + 8])
        if tag == 2 and ident == uid:  # ACL_USER：改名点的那条
            value, found = perm, True
        entries.append(struct.pack("<HHI", tag, value, ident))
    if not found:
        entries.insert(1, struct.pack("<HHI", 2, perm, uid))
    os.setxattr(path, "system.posix_acl_access", b"".join(entries))


def _acl_mentions(path, uid: int) -> bool:
    import struct

    if not hasattr(os, "getxattr"):
        return False
    try:
        raw = os.getxattr(path, "system.posix_acl_access")
    except OSError:
        return False
    for i in range(4, len(raw), 8):
        tag, _value, ident = struct.unpack("<HHI", raw[i : i + 8])
        if tag == 2 and ident == uid:
            return True
    return False


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="POSIX ACL 只有 Linux 文件系统支持"
)
def test_staged_copy_drops_source_acl(tmp_path, runtime_root) -> None:
    """源目录带 ACL 时，复制出的运行目录不能跟着带（否则点名 65534 的禁止条目会锁死插件）。

    这是 NAS 上的真实故障：QNAP 的 data 目录带 ``user:guest:---``（guest 就是 65534），
    ``copytree`` 默认的 ``copy2`` 把它一起复制过去，插件进程连自己的目录都列不出来。
    """
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "mod.py").write_text("X = 1\n")
    if not _acl_supported(src):
        pytest.skip("这个文件系统不支持 POSIX ACL")

    nobody = plugin_isolation.DEFAULT_UID
    _grant_acl(src, nobody, 0)  # u:65534 = --- ：禁止

    staged = plugin_isolation.stage_plugin("acme.acl", src, "pkg")
    assert not _acl_mentions(staged, nobody)
    # 去掉 ACL 之后 chmod 才真的说了算：别人能读、不能写
    mode = stat.S_IMODE(staged.stat().st_mode)
    assert mode == 0o755
    assert not os.access(staged, os.W_OK, effective_ids=False) or os.geteuid() == 0


def test_readable_strips_acl_even_if_copy_keeps_it(tmp_path) -> None:
    """兜底：即使副本上已经有 ACL，_readable() 也要把它清掉（幂等，可重复调用）。"""
    target = tmp_path / "code"
    (target / "a").mkdir(parents=True)
    (target / "a" / "x.py").write_text("X = 1\n")
    if _acl_supported(target):
        _grant_acl(target, plugin_isolation.DEFAULT_UID, 0)
        assert _acl_mentions(target, plugin_isolation.DEFAULT_UID)
    plugin_isolation._readable(target)
    assert not _acl_mentions(target, plugin_isolation.DEFAULT_UID)
    if sys.platform.startswith("linux"):  # Windows 上 chmod 不落权限位，只有 Linux 可断言
        assert stat.S_IMODE(target.stat().st_mode) == 0o755
        assert stat.S_IMODE((target / "a" / "x.py").stat().st_mode) == 0o644


PLUGIN = """
import os
import sys

import movieclaw_api
from movieclaw_api import hooks
from movieclaw_sdk import plugin


@plugin("acme.jail", title="隔离验收")
async def apply(ctx) -> None:
    def pick(query):
        where = f"{os.getuid()}|{movieclaw_api.__file__}|{os.environ.get('HOME')}|{os.getcwd()}"
        return hooks.DownloaderChoice(downloader_id=1, reason=where)

    ctx.on(hooks.DOWNLOADER_SELECT, pick, id="pick")
"""


@pytest.fixture
def jailed(tmp_path, runtime_root, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'jail.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SITE_CONFIGS_DIR", str(tmp_path / "site-configs"))
    monkeypatch.setenv("MOVIECLAW_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    if os.geteuid() != 0:
        # 不是 root：强制开启隔离，但「插件用户」就是自己（只验证运行目录与环境）
        monkeypatch.setattr(
            plugin_isolation,
            "isolation",
            lambda: plugin_isolation.Isolation(uid=os.getuid(), gid=os.getgid()),
        )
    get_settings.cache_clear()
    durable_events.reset_state()
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "jail.py").write_text(PLUGIN, encoding="utf-8")
    (tmp_path / "plugins.yaml").write_text(
        textwrap.dedent(
            """
            - id: acme.jail
              local: true
              module: jail
              runtime: process
            """
        ),
        encoding="utf-8",
    )
    from movieclaw_api.app import create_app

    app = create_app()
    with TestClient(app) as client:
        yield app, client, runtime_root
    durable_events.reset_state()
    get_settings.cache_clear()


def where(client) -> list[str]:
    from movieclaw_api import hooks

    async def ask():
        choice = await hooks.bail(
            hooks.DOWNLOADER_SELECT, hooks.DownloaderQuery(title="x", media_kind="tv")
        )
        return choice.reason

    return client.portal.call(ask).split("|")


def test_process_plugin_runs_from_the_staged_runtime(jailed) -> None:
    app, client, root = jailed
    assert app.state.kernel.fiber("acme.jail").state.value == "active"
    uid, module_file, home, cwd = where(client)
    # 代码来自复制出的运行目录，不是宿主的源码目录；HOME 是它自己的临时目录
    assert module_file.startswith(str(root.resolve())) or module_file.startswith(str(root))
    assert os.path.realpath(home) == os.path.realpath(root / "run" / "acme.jail")
    assert os.path.realpath(cwd) == os.path.realpath(root / "code" / "acme.jail")
    expected_uid = plugin_isolation.DEFAULT_UID if os.geteuid() == 0 else os.getuid()
    assert int(uid) == expected_uid
    assert plugin_runtime.sessions["acme.jail"].pid != os.getpid()


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() != 0 or not sys.platform.startswith("linux"),
    reason="真正切换用户需要以 root 运行（Docker 镜像里就是）",
)
def test_root_host_switches_the_plugin_to_nobody(jailed) -> None:
    app, client, _root = jailed
    uid, *_ = where(client)
    assert int(uid) == plugin_isolation.DEFAULT_UID
