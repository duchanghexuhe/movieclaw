"use client";

import { useCallback, useEffect, useState } from "react";

import { BrandLoader } from "@/components/brand-loader";
import { Modal } from "@/components/modal";
import {
  type ArtworkCandidate,
  type ArtworkCandidates,
  listArtworkCandidates,
  selectArtwork,
} from "@/lib/api/libraries";
import { cachedImageUrl } from "@/lib/image-proxy";

/**
 * 「更换图片」弹层（docs/design/metadata.md 6.3）——自动选图挑不中口味时的
 * 人工通道。
 *
 * 两个 tab（海报 / 背景），网格铺候选缩略图，点选即落盘 + 覆盖媒体目录 +
 * 加锁（此后刷新不再覆盖）。候选顺序与自动选图规则一致，所以**第一张就是
 * 系统默认给的那张**，用户一眼看出"我现在用的是哪张、还有什么可选"。
 * 背景候选里"无文字"的排在前面（干净的图才适合铺全屏）。
 */
export function ArtworkPickerDialog({
  open,
  libraryId,
  mediaItemId,
  onClose,
  onChanged,
}: {
  open: boolean;
  libraryId: number;
  mediaItemId: number;
  onClose: () => void;
  /** 选定/恢复后回调：调用方重拉详情呈现新图 */
  onChanged: () => void;
}) {
  const [tab, setTab] = useState<"poster" | "backdrop">("backdrop");
  const [data, setData] = useState<ArtworkCandidates | null>(null);
  const [failed, setFailed] = useState(false);
  // 正在应用的候选 file_path（"" 表示正在恢复自动）；null = 空闲
  const [applying, setApplying] = useState<string | null>(null);

  const load = useCallback(() => {
    setFailed(false);
    setData(null);
    listArtworkCandidates(libraryId, mediaItemId)
      .then(setData)
      .catch(() => setFailed(true));
  }, [libraryId, mediaItemId]);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  const apply = async (filePath: string | null) => {
    setApplying(filePath ?? "");
    try {
      await selectArtwork(libraryId, mediaItemId, tab, filePath);
      onChanged();
      load();
    } catch {
      setFailed(true);
    } finally {
      setApplying(null);
    }
  };

  const candidates: ArtworkCandidate[] =
    (tab === "poster" ? data?.posters : data?.backdrops) ?? [];
  const locked = tab === "poster" ? data?.poster_locked : data?.backdrop_locked;
  // 「当前」按**实际在用的路径**比对（后端给），不能用"列表第一张"——
  // 策略升级前刮的条目、锁定的条目、TMDB 新增更高票的图都会对不上
  const current = tab === "poster" ? data?.current_poster : data?.current_backdrop;

  return (
    <Modal
      open={open}
      onClose={applying === null ? onClose : () => {}}
      label="更换图片"
      width="2xl"
      panelClassName="flex max-h-[80dvh] flex-col"
    >
      <div className="flex items-center justify-between gap-4 border-b border-white/[0.08] px-6 py-4 max-md:flex-col max-md:items-stretch max-md:gap-3 max-md:px-5 max-md:py-3.5">
        <div className="min-w-0">
          <h3 className="text-title-sm font-semibold text-[var(--text)]">更换图片</h3>
          <p className="mt-1 text-sub leading-5 text-[var(--text-muted)]">
            选中即生效，并同步写入媒体目录；此后刷新元数据不会覆盖你选的图
          </p>
        </div>
        <div className="flex shrink-0 gap-1 self-start rounded-full bg-white/[0.06] p-1">
          {(
            [
              ["backdrop", "背景"],
              ["poster", "海报"],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              type="button"
              onClick={() => setTab(key)}
              className={`rounded-full px-3.5 py-1.5 text-sub font-medium transition ${
                tab === key ? "bg-white/[0.14] text-white" : "text-white/60 hover:text-white/85"
              }`}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {locked && (
        <div className="flex items-center justify-between gap-3 border-b border-white/[0.08] bg-[var(--info)]/[0.07] px-6 py-2.5 max-md:px-5">
          <span className="text-sub text-[var(--info)]">
            当前{tab === "poster" ? "海报" : "背景"}由你手动选定，刷新元数据不会覆盖
          </span>
          <button
            type="button"
            disabled={applying !== null}
            onClick={() => void apply(null)}
            className="shrink-0 text-sub font-medium text-[var(--accent-2)] hover:underline disabled:opacity-50"
          >
            恢复自动选图
          </button>
        </div>
      )}

      <div className="scroll-thin flex-1 overflow-y-auto p-6">
        {failed && (
          <p className="py-10 text-center text-ui text-[#ff9f9f]">
            候选图加载失败（TMDB 可能不可达），
            <button type="button" onClick={load} className="ml-1 underline">
              重试
            </button>
          </p>
        )}
        {!failed && data === null && (
          <div className="flex items-center justify-center gap-2 py-14 text-ui text-[var(--text-muted)]">
            <BrandLoader className="size-5" />
            正在拉取候选图…
          </div>
        )}
        {data !== null && candidates.length === 0 && (
          <p className="py-14 text-center text-ui text-[var(--text-muted)]">
            TMDB 上没有这个条目的{tab === "poster" ? "海报" : "背景图"}
          </p>
        )}
        {candidates.length > 0 && (
          <div
            className={`grid gap-3 ${
              tab === "poster"
                ? "[grid-template-columns:repeat(auto-fill,minmax(116px,1fr))]"
                : "[grid-template-columns:repeat(auto-fill,minmax(220px,1fr))]"
            }`}
          >
            {candidates.map((c) => (
              <button
                key={c.file_path}
                type="button"
                disabled={applying !== null}
                onClick={() => void apply(c.file_path)}
                className={`group relative overflow-hidden rounded-lg ring-1 transition disabled:opacity-60 ${
                  c.file_path === current
                    ? "ring-2 ring-[var(--accent-2)]"
                    : "ring-white/10 hover:ring-[var(--accent-2)]"
                }`}
              >
                <img
                  src={cachedImageUrl(c.preview_url)}
                  alt=""
                  loading="lazy"
                  referrerPolicy="no-referrer"
                  className={`w-full object-cover ${tab === "poster" ? "aspect-[2/3]" : "aspect-video"}`}
                />
                {/* 标出正在用的那张，消除"我现在用的是哪张"的疑问 */}
                {c.file_path === current && (
                  <span className="absolute left-1.5 top-1.5 rounded bg-[var(--accent-2)] px-1.5 py-0.5 text-micro font-semibold text-black/85">
                    当前
                  </span>
                )}
                {tab === "backdrop" && c.language === null && (
                  <span className="absolute right-1.5 top-1.5 rounded bg-black/70 px-1.5 py-0.5 text-micro text-white/85">
                    无文字
                  </span>
                )}
                <span className="tnum absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/80 to-transparent px-1.5 pb-1 pt-4 text-left text-micro text-white/75">
                  {c.width}×{c.height}
                  {c.language ? ` · ${c.language}` : ""}
                </span>
                {applying === c.file_path && (
                  <span className="absolute inset-0 flex items-center justify-center bg-black/55">
                    <BrandLoader className="size-6" />
                  </span>
                )}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="flex justify-end border-t border-white/[0.08] px-6 py-3.5">
        <button
          type="button"
          onClick={onClose}
          disabled={applying !== null}
          className="btn-glass px-4 py-2 text-ui font-medium disabled:opacity-50"
        >
          完成
        </button>
      </div>
    </Modal>
  );
}
