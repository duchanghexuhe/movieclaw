"""默认轨策略的上下文（库语言、原始语言）的取数。

``movieclaw_playback.track_policy`` 是纯函数，要的两样东西在这里取好：

- **库语言**：文件所在媒体库的元数据主语言——库的刮削覆盖里配了语言优先级就用库的，
  否则跟全局设置（``effective_language``：未配置时跟 TMDB_LANGUAGE）；
- **原始语言**：文件所属条目刮削到的 TMDB original_language；
- **归属行**：便携版（预转码派生行）借用的外挂字幕来自源文件（``metadata_owner``），
  与取文件的同一条 SQL 外连源行取出。

两样都是一对一的关联（文件 → 库、条目 → 元数据），所以**跟取文件的 SQL 一起 LEFT JOIN
出来**（:func:`files_with_contexts`），不单独来回：起播决策、开会话、Jellyfin PlaybackInfo、
进度心跳本来就要取这个单元的文件，上下文连带取出，SQL 条数不变。手里已经有文件、不再取的
少数路径（按 file_id 起播、Jellyfin 转码列表没带音轨序号）才用 :func:`track_contexts`
按文件 id 补一条按主键的小查询。详情页手里有库行与条目元数据，直接拼、不查库
（:func:`library_track_context`）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from movieclaw_api.services.scrape_config import effective_language, merge_for_library
from movieclaw_db.models import Library, LibraryFile, MediaMetadata
from movieclaw_playback import state as playback_state
from movieclaw_playback.track_policy import NO_CONTEXT, TrackContext

Unit = tuple[int, int, int]

#: 只看库覆盖里的语言优先级：库只覆盖了选图、图片档位这些时，不必为它校验整份设置
_LANGUAGE_FIELDS = frozenset({"language_priority"})


def _with_context_columns(stmt: Select) -> Select:
    """给以 LibraryFile 为主的查询接上库的刮削覆盖、条目的原始语言，以及便携版的源文件行
    （都是一对一或零对一，文件行不会变多）。"""
    source = aliased(LibraryFile)
    return (
        stmt.add_columns(
            Library.name, Library.scrape_overrides, MediaMetadata.original_language, source
        )
        .outerjoin(Library, Library.id == LibraryFile.library_id)
        .outerjoin(MediaMetadata, MediaMetadata.media_item_id == LibraryFile.media_item_id)
        .outerjoin(source, source.id == LibraryFile.derives_from_file_id)
    )


def _context(
    library_name: str | None,
    overrides: Any,
    original_language: Any,
    metadata_owner: LibraryFile | None = None,
) -> TrackContext:
    library = SimpleNamespace(scrape_overrides=overrides, name=library_name or "?")
    language = effective_language(merge_for_library(library, fields=_LANGUAGE_FIELDS))
    return replace(
        TrackContext.build(language, original_language), metadata_owner=metadata_owner
    )


async def metadata_owner(session: AsyncSession, file: LibraryFile) -> LibraryFile:
    """外挂字幕与片头片尾标记的归属行：便携版没有自己的，借用源文件的；其余就是文件自己。

    便携版是源文件的转码副本（``derives_from_file_id``），同一部片的外挂字幕与片头区间都挂在
    源文件上——引用而不复制，源文件后补的字幕便携版下次起播就能用。源行已不在时退回自己，
    不下发借来的东西。
    """
    if file.derives_from_file_id is None:
        return file
    source = await session.get(LibraryFile, file.derives_from_file_id)
    return source or file


def library_track_context(library: Library, original_language: str | None) -> TrackContext:
    """手里已经有库行与条目元数据时（详情页）直接拼上下文，不查库。"""
    return _context(library.name, library.scrape_overrides, original_language)


async def track_contexts(
    session: AsyncSession, files: Iterable[LibraryFile]
) -> dict[int, TrackContext]:
    """每个文件（按 id）的默认轨上下文，一条 SQL 取完。没有 id 的文件不给。"""
    ids = sorted({f.id for f in files if f.id is not None})
    if not ids:
        return {}
    stmt = _with_context_columns(select(LibraryFile.id).where(LibraryFile.id.in_(ids)))
    rows = (await session.execute(stmt)).all()
    return {
        file_id: _context(name, overrides, original, owner)
        for file_id, name, overrides, original, owner in rows
    }


async def track_context(session: AsyncSession, file: LibraryFile) -> TrackContext:
    """单个文件的上下文（见 :func:`track_contexts`）。"""
    return (await track_contexts(session, [file])).get(file.id or 0, NO_CONTEXT)


async def files_with_contexts(
    session: AsyncSession, stmt: Select
) -> tuple[list[LibraryFile], dict[int, TrackContext]]:
    """执行一条 ``select(LibraryFile)`` 查询，连同各文件的上下文（按文件 id）——同一条 SQL。"""
    rows = (await session.execute(_with_context_columns(stmt))).all()
    files = [row[0] for row in rows]
    contexts = {
        file.id: _context(name, overrides, original, owner)
        for file, name, overrides, original, owner in rows
        if file.id is not None
    }
    return files, contexts


async def unit_files_with_contexts(
    session: AsyncSession, unit: Unit
) -> tuple[list[LibraryFile], dict[int, TrackContext]]:
    """一个播放单元的在位文件（同 ``unit_files``），连同各文件的上下文——同一条 SQL。"""
    return await files_with_contexts(session, playback_state.unit_files_statement(unit))
