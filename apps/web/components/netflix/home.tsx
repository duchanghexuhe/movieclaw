"use client";

import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";

import { ContentEmptyState } from "@/components/content-empty-state";
import { InfoIcon, PlayIcon, SparkIcon } from "@/components/icons";
import { PosterImage } from "@/components/poster-image";
import {
  NetflixRow,
  type NetflixRowItem,
} from "@/components/netflix/row";
import {
  type LibraryItem,
  type MediaLibrary,
  listLibraries,
  listLibraryItems,
} from "@/lib/api/libraries";
import { listUpNext, type UpNextItem } from "@/lib/api/playback";
import { listSubscriptions, type Subscription } from "@/lib/api/subscriptions";
import { imageUrl, cardVariantFor } from "@/lib/image-proxy";
import { usePermissions } from "@/lib/permissions";
import { formatRelativeTime } from "@/lib/time";

/**
 * Netflix 主题的内容首页（路由 /，docs/design/web-themes.md §5.3）。
 *
 * 构图 = Billboard 全出血 hero + 横版卡片行；行数据全部来自现有接口
 * （up-next / libraries / subscriptions），无新后端依赖。Billboard 选片
 * 规则是确定性的：「接下来继续」第一项 → 否则「最近入库」第一项 →
 * 否则空态（全新部署的引导卡）。空行不渲染（不出现空标题）。
 *
 * 首页是氛围页（外壳 isHome）：无蒙版、无顶栏让位，billboard 从透明
 * 顶栏底下直出——这正是 Netflix 的首屏构图。
 */

/** 每行卡数上限（服务端拉取上限与行内展示共用）。 */
const ROW_COUNT = 20;
/** 首页最多给几个库各开一行「库名」行。 */
const MAX_LIBRARY_ROWS = 4;

export function NetflixHome() {
  const router = useRouter();
  const { canSubscribe } = usePermissions();
  const [upNext, setUpNext] = useState<UpNextItem[] | null>(null);
  const [libraries, setLibraries] = useState<MediaLibrary[] | null>(null);
  const [itemsByLibrary, setItemsByLibrary] = useState<Map<number, LibraryItem[]>>(new Map());
  const [subscriptions, setSubscriptions] = useState<Subscription[] | null>(null);

  const reload = useCallback(() => {
    // 与媒体库首页同一套失败策略：单路失败不拖垮整页，保留旧数据
    listUpNext(ROW_COUNT)
      .then((items) => setUpNext(items))
      .catch(() => setUpNext((prev) => prev ?? []));
    listLibraries()
      .then(async (libs) => {
        setLibraries(libs);
        const visible = libs.filter((l) => l.viewer_access && !l.exclude_from_home);
        const entries = await Promise.all(
          visible.map(async (lib) => {
            try {
              return [lib.id, await listLibraryItems(lib.id, { sort: "added_at", limit: ROW_COUNT })] as const;
            } catch {
              return [lib.id, [] as LibraryItem[]] as const;
            }
          }),
        );
        setItemsByLibrary(new Map(entries));
      })
      .catch(() => setLibraries((prev) => prev ?? []));
    if (canSubscribe) {
      listSubscriptions().catch(() => null).then((subs) => {
        if (subs) setSubscriptions(subs);
      });
    }
  }, [canSubscribe]);

  useEffect(() => {
    reload();
  }, [reload]);

  // —— Billboard 选片（确定性三档） ——
  const recentSorted = useMemo(() => {
    const all: LibraryItem[] = [];
    for (const items of itemsByLibrary.values()) all.push(...items);
    return all
      .sort((a, b) => (b.added_at ?? "").localeCompare(a.added_at ?? ""))
      .slice(0, ROW_COUNT);
  }, [itemsByLibrary]);

  const billboard = useMemo(() => {
    if (upNext?.length) return { kind: "upnext" as const, item: upNext[0] };
    if (recentSorted.length) return { kind: "library" as const, item: recentSorted[0] };
    return null;
  }, [upNext, recentSorted]);

  // —— 行装配（空行不渲染） ——
  const upNextRowItems = useMemo<NetflixRowItem[] | null>(() => {
    if (!upNext?.length) return null;
    return upNext.map((item) => ({
      media: upNextToMediaItem(item),
      playHref: playHrefOf(item),
      href: itemHrefOf(item),
      progress: item.progress_percent,
      context: upNextContext(item),
    }));
  }, [upNext]);

  const recentRowItems = useMemo<NetflixRowItem[] | null>(() => {
    if (!recentSorted.length) return null;
    return recentSorted.map((item) => ({
      media: libraryItemToMediaItem(item),
      href: `/library/${item.library_id}/item/${item.media_item_id}` as Route,
    }));
  }, [recentSorted]);

  const subscriptionRowItems = useMemo<NetflixRowItem[] | null>(() => {
    if (!subscriptions?.length) return null;
    return subscriptions.slice(0, ROW_COUNT).map((sub) => ({
      media: subscriptionToMediaItem(sub),
    }));
  }, [subscriptions]);

  const libraryRows = useMemo(() => {
    if (!libraries) return [];
    return libraries
      .filter((lib) => lib.viewer_access && !lib.exclude_from_home)
      .map((lib) => ({
        library: lib,
        items: itemsByLibrary.get(lib.id) ?? [],
      }))
      .filter(({ items }) => items.length > 0)
      .slice(0, MAX_LIBRARY_ROWS);
  }, [libraries, itemsByLibrary]);

  const loading = upNext === null && libraries === null;

  return (
    <div className="scroll-thin scroll-safe h-full overflow-y-auto">
      {billboard ? (
        <NetflixBillboard
          upNextItem={billboard.kind === "upnext" ? billboard.item : null}
          libraryItem={billboard.kind === "library" ? billboard.item : null}
        />
      ) : loading ? (
        <div className="flex h-[56vh] items-center justify-center text-ui text-[var(--text-muted)]">
          <span className="size-5 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
        </div>
      ) : (
        /* 全新部署空态：添加媒体库 / 发起任务的引导（复用现有空态组件） */
        <div className="mx-auto flex min-h-[70vh] max-w-2xl flex-col justify-center px-6">
          <ContentEmptyState
            variant="library"
            title="为收藏准备一个家"
            description="创建电影库或剧集库，订阅完成的内容会自动整理到这里；也可以直接交给 AI 追一部新片。"
            action={
              <button
                type="button"
                onClick={() => router.push("/new")}
                className="btn-accent flex items-center gap-1.5 rounded-[4px] px-4 py-2 text-ui font-semibold"
              >
                <SparkIcon className="size-4" />
                让 AI 帮你找片
              </button>
            }
          />
        </div>
      )}

      <div className="relative z-10 space-y-8 pb-12">
        {/* 1. 继续观看：双端 16:9 横版卡 + 红色进度条（App 同构） */}
        {upNextRowItems && (
          <NetflixRow
            id="continue"
            title="继续观看"
            items={upNextRowItems}
            landscapeOnMobile
          />
        )}
        {/* 2. 最近入库（跨库聚合，按最近入账倒序） */}
        {recentRowItems && (
          <NetflixRow id="recent" title="最近入库" items={recentRowItems} />
        )}
        {/* 3. 我的订阅 */}
        {subscriptionRowItems && (
          <NetflixRow
            id="subscriptions"
            title="我的订阅"
            items={subscriptionRowItems}
            moreHref={canSubscribe ? ("/subscriptions" as Route) : undefined}
            cardAction="none"
          />
        )}
        {/* 4. 媒体库各库一行（行标题 = 库名，点标题进库页） */}
        {libraryRows.map(({ library, items }) => (
          <NetflixRow
            key={library.id}
            id={`library-${library.id}`}
            title={library.name}
            items={items.map((item) => ({
              media: libraryItemToMediaItem(item),
              href: `/library/${library.id}/item/${item.media_item_id}` as Route,
            }))}
            moreHref={`/library/${library.id}` as Route}
            moreLabel="进入媒体库"
            cardAction="none"
            cardRevealInfoOnTouch
          />
        ))}
      </div>
    </div>
  );
}

/* ================================ Billboard ================================ */

/**
 * 全出血 hero：高度按 16:9 推导并以视口收口 `clamp(480px, 56.25vw, 80vh)`
 * （移动 40vh，自家决策值）；底部渐隐入 #141414、左侧可读性渐变；左下文案块
 * （标题 / 元数据 / 按钮）与「▶ 播放（白底黑字）· ⓘ 详情（灰底）· ✦ 问 AI」。
 */
function NetflixBillboard({
  upNextItem,
  libraryItem,
}: {
  upNextItem: UpNextItem | null;
  libraryItem: LibraryItem | null;
}) {
  const router = useRouter();
  const title = upNextItem?.title ?? libraryItem?.title ?? "";
  const meta = upNextItem
    ? [
        upNextItem.year && upNextItem.year > 0 ? String(upNextItem.year) : null,
        upNextItem.kind === "tv" ? "剧集" : "电影",
        upNextContext(upNextItem),
      ]
    : [
        libraryItem && libraryItem.year ? String(libraryItem.year) : null,
        libraryItem ? kindLabel(libraryItem.kind) : null,
      ]
  const metaText = meta.filter(Boolean).join(" · ");
  const artworkUrl = upNextItem
    ? (upNextItem.episode_still_url ?? upNextItem.backdrop_url)
    : null;
  const playHref = upNextItem ? playHrefOf(upNextItem) : null;
  const detailHref = upNextItem ? itemHrefOf(upNextItem) : libraryItem && libraryItem.library_id != null
    ? (`/library/${libraryItem.library_id}/item/${libraryItem.media_item_id}` as Route)
    : null;
  const progress = upNextItem?.progress_percent ?? null;
  // 入库时间做副文案（最近入库兜底时回答「为什么它在这儿」）
  const addedLabel =
    !upNextItem && libraryItem?.added_at ? `${formatRelativeTime(libraryItem.added_at)}入库` : null;

  return (
    <section
      aria-label={`正在展示《${title}》`}
      className="relative h-[40vh] min-h-[320px] w-full max-md:min-h-[300px] md:h-[clamp(480px,56.25vw,80vh)]"
    >
      {/* 画面：剧照直出；无剧照（库内条目）用海报模糊铺底兜底 */}
      <div className="absolute inset-0 overflow-hidden bg-[#141414]">
        {artworkUrl ? (
          <img
            src={imageUrl(artworkUrl, "landscape-card")}
            alt=""
            className="size-full object-cover"
          />
        ) : libraryItem?.poster_url ? (
          <PosterFallbackFill url={libraryItem.poster_url} aspect={libraryItem.primary_aspect} />
        ) : null}
        {/* 底部渐隐入画布色 + 左侧可读性渐变（§2.5 构图）。渐变终点必须是
            纯黑 #000（= 画布 --bg）：取卡片灰 #141414 会在图与下方内容的
            交界处显出一道色差缝（globals.css 修 library hero 时的同一结论） */}
        <div className="absolute inset-0 bg-[linear-gradient(180deg,rgba(20,20,20,0.45)_0%,rgba(20,20,20,0)_32%,rgba(20,20,20,0)_60%,#000_100%)]" />
        <div className="absolute inset-0 max-md:bg-[linear-gradient(180deg,rgba(0,0,0,0.5)_0%,transparent_40%,rgba(0,0,0,0.92)_100%)] md:bg-[linear-gradient(90deg,rgba(0,0,0,0.72)_0%,rgba(0,0,0,0.35)_42%,transparent_68%)]" />
      </div>

      {/* 左下文案块 */}
      <div className="absolute inset-x-0 bottom-[12%] px-[4vw] max-md:bottom-[10%]">
        <h1 className="max-w-[80%] text-[clamp(28px,4.6vw,56px)] font-bold leading-[1.12] tracking-[-0.02em] text-white text-on-image max-md:max-w-full">
          {title}
        </h1>
        {metaText && (
          <p className="tnum text-on-image mt-2.5 flex flex-wrap items-center gap-x-2.5 text-[14px] font-medium text-[#e5e5e5] max-md:mt-2">
            <span className="font-bold text-[var(--ok)]">在库</span>
            {metaText}
          </p>
        )}
        {addedLabel && (
          <p className="text-on-image mt-1 text-caption text-[var(--text-muted)]">{addedLabel}</p>
        )}
        {/* 按钮组：▶ 播放（白底黑字）· ⓘ 详情（灰底）· ✦ 问 AI（ghost）。
            移动端可换行（320px 视口三颗排不下）且保持 44px 触控高度——
            billboard 按钮组是首页最高频的操作区，max-md:h-9 的 36px 偏小 */}
        <div className="mt-4 flex items-center gap-2.5 max-md:mt-3.5 max-md:flex-wrap">
          {playHref && (
            <button
              type="button"
              onClick={() => router.push(playHref)}
              className="flex h-10 items-center gap-2 rounded-[4px] bg-white px-5 text-[15px] font-bold text-black transition-colors hover:bg-white/75 max-md:h-11 max-md:px-4"
            >
              <PlayIcon className="size-5" fill="currentColor" />
              播放
            </button>
          )}
          {detailHref && (
            <button
              type="button"
              onClick={() => router.push(detailHref)}
              className="flex h-10 items-center gap-2 rounded-[4px] bg-[rgba(109,109,110,0.7)] px-5 text-[15px] font-semibold text-white transition-colors hover:bg-[rgba(109,109,110,0.4)] max-md:h-11 max-md:px-4"
            >
              <InfoIcon className="size-5" />
              更多信息
            </button>
          )}
          {/* AI 是本站差异能力：Netflix 没有但至少不破坏画面的第三个入口 */}
          <button
            type="button"
            onClick={() => router.push("/new")}
            className="text-on-image flex h-10 items-center gap-1.5 rounded-[4px] px-3 text-[15px] font-medium text-[var(--text-muted)] transition-colors hover:text-white max-md:h-11"
          >
            <SparkIcon className="size-4" />
            问 AI
          </button>
        </div>
        {/* 继续观看的进度：billboard 底部细红条（与播放器进度同语言） */}
        {progress != null && progress > 0 && (
          <div className="mt-4 h-[3px] w-[min(320px,60%)] bg-white/25 max-md:mt-3">
            <div className="h-full bg-[var(--accent)]" style={{ width: `${progress}%` }} />
          </div>
        )}
      </div>
    </section>
  );
}

/** 无横版剧照时的 billboard 兜底：海报放大模糊铺底 + 中央完整显示 */
function PosterFallbackFill({ url, aspect }: { url: string; aspect: number }) {
  const src = imageUrl(url, cardVariantFor(aspect));
  return (
    <>
      <img src={src} alt="" className="absolute inset-0 size-full scale-110 object-cover opacity-40 blur-2xl" />
      <div className="absolute inset-0 flex items-center justify-center">
        <div
          style={{ aspectRatio: aspect, height: "86%" }}
          className="overflow-hidden rounded-[4px] shadow-[0_0_40px_rgba(0,0,0,0.6)]"
        >
          <PosterImage src={src} alt="" className="size-full" />
        </div>
      </div>
    </>
  );
}

/* ============================== 数据适配层 ============================== */
/* 三路数据源（up-next / 库内条目 / 订阅媒体）各自映射成行与 billboard 共用的
   MediaItem 形态；继续观看的播放/详情落点沿用最近观看行的语义。 */

function upNextToMediaItem(item: UpNextItem) {
  return {
    id: String(item.media_item_id),
    source: "tmdb" as const,
    type: (item.kind === "tv" ? "tv" : "movie") as "tv" | "movie",
    title: item.title,
    originalTitle: "",
    year: item.year ?? 0,
    rating: 0,
    genres: [],
    extent: "",
    badges: [],
    overview: "",
    // 在库身份：hover 卡的「在库」徽标与移动端海报行的「已入库」斜标都由
    // libraryStatus 派生（消费方只做真值判断，计数字段给保守值即可）
    libraryStatus: { mediaItemId: item.media_item_id, libraryCount: 1, fileCount: 0 },
    // 行内卡是 16:9 框，海报按真实比例兜底；剧照走 landscape-card 预设
    posterUrl: item.poster_url ? imageUrl(item.poster_url, cardVariantFor(item.poster_aspect)) : "",
    imageAspect: item.poster_aspect,
    backdropUrl: item.backdrop_url ?? undefined,
  };
}

function libraryItemToMediaItem(item: LibraryItem) {
  return {
    id: item.tmdb_id != null ? String(item.tmdb_id) : `local:${item.media_item_id}`,
    source: "tmdb" as const,
    type: (item.kind === "video" || item.kind === "photo" ? "movie" : item.kind) as "tv" | "movie",
    title: item.title,
    originalTitle: "",
    year: item.year ?? 0,
    rating: 0,
    genres: [],
    extent: "",
    badges: [],
    libraryStatus: { mediaItemId: item.media_item_id, libraryCount: 1, fileCount: item.file_count },
    // 信息层的元信息：库行的 cardRevealInfoOnTouch 依赖信息层可渲染（无内容
    // 时不渲染、「首点展开看入库时间」的承诺落空）。走 overview 字段——
    // overlayMeta 在 PosterVisualItem 上、MediaItem 不带，悬层两者都渲染。
    overview: [
      item.added_at ? `${formatRelativeTime(item.added_at)}入库` : null,
      item.file_count > 0 ? `${item.file_count} 个文件` : null,
    ]
      .filter(Boolean)
      .join(" · "),
    posterUrl: item.poster_url ? imageUrl(item.poster_url, cardVariantFor(item.primary_aspect)) : "",
    imageAspect: item.primary_aspect,
    // 行内卡是 16:9 框：封面即 16:9 抓帧时直接铺满，竖海报模糊铺底
    aspect: 16 / 9,
  };
}

function subscriptionToMediaItem(sub: Subscription) {
  return {
    id: String(sub.media.tmdb_id),
    source: "tmdb" as const,
    type: sub.media.kind,
    title: sub.media.title,
    originalTitle: sub.media.original_title,
    year: sub.media.year ?? 0,
    rating: 0,
    genres: [],
    extent: "",
    badges: [],
    overview: "",
    posterUrl: sub.media.poster_url ? imageUrl(sub.media.poster_url, "poster-card") : "",
  };
}

/**
 * 继续观看的副文案：剧集给「S01E02 · 集名」；电影没有集数上下文、
 * 年份已由调用方的 meta 行给出，返回空串（否则年份会显示两遍）。
 */
function upNextContext(item: UpNextItem): string {
  if (item.kind !== "tv") return "";
  const code = `S${String(item.season_number).padStart(2, "0")}E${String(item.episode_number).padStart(2, "0")}`;
  return [code, item.episode_title].filter(Boolean).join(" · ");
}

/** 直接播放：续播点由服务端在开会话时解析（§6.10），前端不重复计算。 */
function playHrefOf(item: UpNextItem): Route {
  return (
    item.kind === "tv"
      ? `/play/${item.media_item_id}/s${String(item.season_number).padStart(2, "0")}e${String(item.episode_number).padStart(2, "0")}`
      : `/play/${item.media_item_id}`
  ) as Route;
}

function itemHrefOf(item: UpNextItem): Route {
  const base = `/library/${item.library_id}/item/${item.media_item_id}`;
  return (
    item.kind === "tv"
      ? `${base}?season=${item.season_number}&episode=${item.episode_number}&from=recent`
      : `${base}?from=recent`
  ) as Route;
}

function kindLabel(kind: LibraryItem["kind"]): string {
  return kind === "tv" ? "剧集" : kind === "movie" ? "电影" : "视频";
}
