"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import {
  BellIcon,
  CheckIcon,
  DownloadIcon,
  InfoIcon,
  PlayIcon,
  PlusIcon,
} from "@/components/icons";
import { HScroller } from "@/components/h-scroller";
import { MediaRow } from "@/components/media-row";
import { PosterImage } from "@/components/poster-image";
import { useSubscribeEntry } from "@/components/subscribe-entry";
import { useMediaDetail } from "@/lib/media-detail";
import { useTapGuard } from "@/lib/use-tap-guard";
import type { PosterCardAction } from "@/components/poster-card";
import type { MediaItem } from "@/lib/media-types";
import { imageUrl } from "@/lib/image-proxy";
import { useIsMobile, useMediaQuery } from "@/lib/use-media-query";
import type { Route } from "next";

/**
 * Netflix 主题的内容行与卡片（docs/design/web-themes.md §5.4）。
 *
 * - PC（hover 设备）：16:9 横版剧照卡，hover 400ms 延迟后展开预览卡——
 *   原生横滚行的 overflow 必然裁切纵向溢出（CSS Overflow L3），展开画布
 *   因此 Portal 到 body、按卡片矩形 fixed 定位，scale 1.5 / 400ms 展开，
 *   滚动与滚轮即收（capture 监听防 hover 残留），行尾整页翻（HScroller
 *   pageFraction=1）。
 * - 移动 / 触屏：常规行回落到既有的 2:3 海报行（MediaRow，App 同构）；
 *   「继续观看」行双端保持 16:9 横版 + 3px 红色进度条（App 截图证实）。
 *
 * 行数据是 MediaItem 的薄包装：媒体身份、图片与订阅语义直接复用既有
 * 体系，行自己只补「继续观看」才有的播放落点与进度。
 */
export interface NetflixRowItem {
  /** 基础身份：标题 / 海报 / 详情跳转 / 订阅语义都从这里来 */
  media: MediaItem;
  /** 详情落点覆盖（库内条目跳库内详情页）；缺省走 useMediaDetail 的发现详情 */
  href?: Route;
  /** 直接播放落点（继续观看行）；非空时整卡点击即起播（Netflix 同款） */
  playHref?: Route;
  /** 继续观看进度 0~100（有则卡片底部画 3px 红条） */
  progress?: number | null;
  /** 卡片下方的副标题（继续观看行是「S01E02 · 集名」） */
  context?: string;
  /** 悬浮层操作语义：与 PosterCard 五态对齐；缺省按库存状态给 owned / subscribe */
  action?: PosterCardAction;
}

export interface NetflixRowProps {
  id: string;
  title: string;
  items: NetflixRowItem[];
  /** 行标题的落点（整行可点进对应页面，空行不渲染由调用方保证） */
  moreHref?: Route;
  moreLabel?: string;
  /** 移动端保持 16:9 横版卡（继续观看行；默认移动端回落 2:3 海报行） */
  landscapeOnMobile?: boolean;
  /** 移动端海报行的操作语义（透传 MediaRow / PosterCard） */
  cardAction?: PosterCardAction | ((item: MediaItem) => PosterCardAction);
  /** 触摸端海报行首次点按先展开信息层 */
  cardRevealInfoOnTouch?: boolean;
}

export function NetflixRow({
  id,
  title,
  items,
  moreHref,
  moreLabel = "查看全部",
  landscapeOnMobile = false,
  cardAction,
  cardRevealInfoOnTouch = false,
}: NetflixRowProps) {
  const isMobile = useIsMobile();
  const touch = useMediaQuery("(hover: none)");

  const titleNode = (
    <div className="mb-2 flex items-baseline justify-between gap-4 px-[var(--nf-inset)]">
      {moreHref ? (
        <Link
          href={moreHref}
          className="text-[20px] font-bold leading-tight tracking-[-0.01em] text-white transition-opacity hover:opacity-80"
        >
          {title}
        </Link>
      ) : (
        <h3 className="text-[20px] font-bold leading-tight tracking-[-0.01em] text-white">
          {title}
        </h3>
      )}
      {moreHref && (
        <Link
          href={moreHref}
          className="shrink-0 text-[12px] font-medium text-[var(--text-muted)] transition-colors hover:text-white"
        >
          {moreLabel} ›
        </Link>
      )}
    </div>
  );

  // —— 移动 / 触屏：无 hover，tap 直达详情整页（App 同构） ——
  if (touch || isMobile) {
    if (landscapeOnMobile) {
      // 继续观看行：双端 16:9 横版 + 进度条；触屏没有展开卡，整卡即播放
      return (
        <section aria-label={title} className="nf-row [content-visibility:auto] [contain-intrinsic-size:auto_240px]">
          {titleNode}
          <HScroller className="gap-[var(--nf-gap)] px-[var(--nf-inset)] pb-1 pt-1">
            {items.map((item) => (
              <NetflixLandscapeCard
                key={`${id}-${item.media.id}`}
                item={item}
                className="w-[224px] max-md:w-[168px]"
              />
            ))}
          </HScroller>
        </section>
      );
    }
    // 常规行：复用既有 2:3 海报行（PosterCard 的触屏首点展开等约定全部保留）。
    // cardHref：NetflixRowItem.href（库内条目 → 库内详情页）必须映射回
    // MediaRow——否则移动端回落 useMediaDetail 的发现详情，无 tmdb_id 的
    // 本地条目（id 形如 local:123）会跳进解析必败的无效路由
    const hrefByMediaId = new Map(
      items.flatMap((item) => (item.href ? [[item.media.id, item.href] as const] : [])),
    );
    return (
      <MediaRow
        row={{ id, title, items: items.map((item) => item.media) }}
        moreHref={moreHref}
        moreLabel={moreLabel}
        cardAction={cardAction}
        cardHref={(m) => hrefByMediaId.get(m.id)}
        cardRevealInfoOnTouch={cardRevealInfoOnTouch}
        insetClassName="px-[var(--nf-inset)]"
      />
    );
  }

  // —— PC：横版卡 + hover 展开卡（Portal） ——
  return <DesktopRow id={id} title={title} items={items} moreHref={moreHref} moreLabel={moreLabel} titleNode={titleNode} />;
}

/** PC 端横版行：展开卡状态与生命周期集中在这里（一行同时最多一张展开卡）。 */
function DesktopRow({
  id,
  title,
  items,
  moreHref,
  moreLabel,
  titleNode,
}: {
  id: string;
  title: string;
  items: NetflixRowItem[];
  moreHref?: Route;
  moreLabel: string;
  titleNode: React.ReactNode;
}) {
  // 展开状态：矩形在「打开那一刻」测量，之后内容异步加载不重排（定位取卡片矩形）
  const [hover, setHover] = useState<{
    item: NetflixRowItem;
    rect: { left: number; top: number; width: number; height: number };
    flipUp: boolean;
  } | null>(null);
  const openTimer = useRef<number | null>(null);
  const closeTimer = useRef<number | null>(null);
  const router = useRouter();

  const clearTimers = () => {
    if (openTimer.current) window.clearTimeout(openTimer.current);
    if (closeTimer.current) window.clearTimeout(closeTimer.current);
    openTimer.current = null;
    closeTimer.current = null;
  };

  /** hover 400ms 延迟后展开（官方触发延迟未公布，社区普遍 ~500ms，取 400ms） */
  const scheduleOpen = (item: NetflixRowItem, el: HTMLElement) => {
    if (closeTimer.current) {
      window.clearTimeout(closeTimer.current);
      closeTimer.current = null;
    }
    if (openTimer.current) window.clearTimeout(openTimer.current);
    openTimer.current = window.setTimeout(() => {
      openTimer.current = null;
      const rect = el.getBoundingClientRect();
      // 展开总高 ≈ (卡高 + 信息面板) × 1.5；下方放不下且上方更宽裕时向上翻
      const panelH = 96;
      const expandedH = (rect.height + panelH) * 1.5;
      const spaceBelow = window.innerHeight - rect.bottom;
      const flipUp = spaceBelow < expandedH && rect.top > spaceBelow;
      setHover({
        item,
        rect: { left: rect.left, top: rect.top, width: rect.width, height: rect.height },
        flipUp,
      });
    }, 400);
  };

  /** 移出即安排关闭（80ms 宽限：滑过卡缝时不闪烁） */
  const scheduleClose = () => {
    if (openTimer.current) {
      window.clearTimeout(openTimer.current);
      openTimer.current = null;
    }
    if (closeTimer.current) window.clearTimeout(closeTimer.current);
    closeTimer.current = window.setTimeout(() => {
      closeTimer.current = null;
      setHover(null);
    }, 80);
  };

  // 滚动即收：展开卡挂在 body 上，行在底下滚动时 hover 残留是最经典的坑；
  // capture 才能收到内部滚动容器的事件，wheel 覆盖悬停不动滚轮的场景
  useEffect(() => {
    if (!hover) return;
    const close = () => setHover(null);
    document.addEventListener("scroll", close, { capture: true, passive: true });
    window.addEventListener("wheel", close, { passive: true });
    return () => {
      document.removeEventListener("scroll", close, { capture: true });
      window.removeEventListener("wheel", close);
    };
  }, [hover]);

  // 卸载兜底清理计时器
  useEffect(() => clearTimers, []);

  // 详情落点：显式 href 优先，否则走全站详情入口（含 seed 缓存零白屏）
  const { open: openDetail } = useMediaDetail();
  const openItem = (item: NetflixRowItem) => {
    if (item.href) router.push(item.href);
    else openDetail(item.media);
  };

  return (
    <section
      aria-label={title}
      className="nf-row group/row relative [content-visibility:auto] [contain-intrinsic-size:auto_300px]"
    >
      {titleNode}
      <HScroller
        className="gap-[var(--nf-gap)] px-[var(--nf-inset)] pb-1 pt-1"
        pageFraction={1}
      >
        {items.map((item) => (
          <div key={`${id}-${item.media.id}`} className="nf-card-land shrink-0">
            <NetflixLandscapeCard
              item={item}
              onHoverEnter={(el) => scheduleOpen(item, el)}
              onHoverLeave={scheduleClose}
            />
          </div>
        ))}
      </HScroller>

      {/* 展开卡：Portal 到 body——原生横滚行的 overflow 必然裁切纵向溢出，
          拎出文档流是规范层面唯一能同时保住滚动与展开的路线（§5.4） */}
      {hover &&
        createPortal(
          <div
            className="nf-hovercard pointer-events-auto fixed"
            style={{
              left: clampLeft(hover.rect),
              top: hover.flipUp
                ? hover.rect.top + hover.rect.height - hover.rect.height * 1.5
                : hover.rect.top,
              width: hover.rect.width,
              transformOrigin: "center top",
            }}
            onPointerEnter={() => {
              if (closeTimer.current) {
                window.clearTimeout(closeTimer.current);
                closeTimer.current = null;
              }
            }}
            onPointerLeave={scheduleClose}
          >
            {/* 展开画布：图 + 信息面板，整体 scale 1.5（动画见 globals.css）。
                内层再包一层容器：scale 动画挂外层，内层负责圆角裁切与阴影 */}
            <div
              className="overflow-hidden rounded-[6px] bg-[#181818] shadow-[0_12px_24px_rgba(0,0,0,0.8)] ring-1 ring-white/[0.08]"
              style={{ width: hover.rect.width }}
            >
              <button
                type="button"
                onClick={() => openItem(hover.item)}
                className="relative block aspect-video w-full cursor-pointer outline-none"
                aria-label={`查看《${hover.item.media.title}》详情`}
              >
                <LandscapeArtwork media={hover.item.media} />
                {hover.item.progress != null && hover.item.progress > 0 && (
                  <span className="absolute inset-x-0 bottom-0 h-[3px] bg-white/25">
                    <span
                      className="block h-full bg-[var(--accent)]"
                      style={{ width: `${hover.item.progress}%` }}
                    />
                  </span>
                )}
              </button>
              <HoverPanel item={hover.item} onDetail={() => openItem(hover.item)} />
            </div>
          </div>,
          document.body,
        )}
      {moreHref && <span className="sr-only">{title} · {moreLabel}</span>}
    </section>
  );
}

/** 展开卡横向定位：scale 1.5 后左右各外扩 1/4 卡宽，clamp 在视口内。 */
function clampLeft(rect: { left: number; width: number }): number {
  const min = 8 + rect.width * 0.25;
  const max = window.innerWidth - 8 - rect.width * 1.25;
  if (min > max) return (window.innerWidth - rect.width) / 2;
  return Math.min(Math.max(rect.left, min), max);
}

/** 信息面板：40px 圆形操作钮 + 元数据 + 类型标签（预览视频不在非目标内，无画面层） */
function HoverPanel({ item, onDetail }: { item: NetflixRowItem; onDetail: () => void }) {
  const media = item.media;
  const meta = [
    media.year > 0 ? String(media.year) : null,
    media.type === "tv" ? "剧集" : "电影",
    item.context?.split(" · ")[0] ?? null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <div className="p-3">
      <div className="flex items-center gap-2">
        {item.playHref ? <PlayButton href={item.playHref} /> : <DetailButton onClick={onDetail} />}
        <HoverSubscribeButton item={item} />
      </div>
      <p className="tnum mt-2.5 flex flex-wrap items-center gap-x-2 text-caption text-[var(--text-muted)]">
        {media.libraryStatus && <span className="font-semibold text-[var(--ok)]">在库</span>}
        {meta && <span>{meta}</span>}
      </p>
      {media.genres.length > 0 && (
        <p className="mt-1 truncate text-caption text-[var(--text-muted)]">
          {media.genres.slice(0, 3).join(" · ")}
        </p>
      )}
    </div>
  );
}

/** 40px 圆形操作钮的公共底座（生产 CSS：40px 圆钮 = 0.8rem 内边距 + 24px 图标） */
function CircleButton({
  label,
  onClick,
  href,
  tone = "white",
  children,
}: {
  label: string;
  onClick?: () => void;
  href?: Route;
  tone?: "white" | "grey";
  children: React.ReactNode;
}) {
  const cls = `flex size-10 items-center justify-center rounded-full transition-colors ${
    tone === "white"
      ? "bg-white text-black hover:bg-white/75"
      : "border-2 border-white/40 bg-[rgba(109,109,110,0.7)] text-white hover:border-white"
  }`;
  if (href) {
    return (
      <Link href={href} aria-label={label} title={label} className={cls}>
        {children}
      </Link>
    );
  }
  return (
    <button type="button" aria-label={label} title={label} onClick={onClick} className={cls}>
      {children}
    </button>
  );
}

function PlayButton({ href }: { href: Route }) {
  return (
    <CircleButton label="播放" href={href} tone="white">
      <PlayIcon className="size-5" fill="currentColor" />
    </CircleButton>
  );
}

function DetailButton({ onClick }: { onClick: () => void }) {
  return (
    <CircleButton label="更多信息" onClick={onClick} tone="grey">
      <InfoIcon className="size-5" />
    </CircleButton>
  );
}

/**
 * 展开卡里的订阅动作：语义与 PosterCard 五态一致（subscribe / follow /
 * backfill / owned / none），按钮是 grey 圆钮、已订阅画对勾。
 */
function HoverSubscribeButton({ item }: { item: NetflixRowItem }) {
  const { canSubscribe, open: openSubscribe, subscriptionOf } = useSubscribeEntry();
  const action =
    item.action ?? (item.media.libraryStatus ? ("owned" as const) : ("subscribe" as const));
  const meta =
    action === "subscribe" || action === "follow" || action === "backfill"
      ? { subscribe: PlusIcon, follow: BellIcon, backfill: DownloadIcon }[action]
      : null;
  if (!meta) return null;
  if (!canSubscribe) return null;
  const subscribed = Boolean(subscriptionOf(item.media));
  const Icon = subscribed ? CheckIcon : meta;
  return (
    <CircleButton
      label={subscribed ? "已订阅" : "订阅影片"}
      onClick={() => void openSubscribe(item.media)}
      tone="grey"
    >
      <Icon className="size-5" />
    </CircleButton>
  );
}

/**
 * 行内横版卡：16:9 剧照 + 底部 3px 红色进度条。无横版图的条目复用
 * PosterImage 的「主图模糊铺底 + 居中完整显示」机制兜底。
 * 落点：playHref（继续观看，整卡即播）→ href → 全站详情入口。
 * 触屏误触保护：卡在横滚行里，滑动/刹车手势会派发 click（use-tap-guard
 * 三重判定），Link 分支只拦截不传动作——放行的点击走默认跳转。
 */
function NetflixLandscapeCard({
  item,
  className = "",
  onHoverEnter,
  onHoverLeave,
}: {
  item: NetflixRowItem;
  className?: string;
  onHoverEnter?: (el: HTMLElement) => void;
  onHoverLeave?: () => void;
}) {
  const { open: openDetail } = useMediaDetail();
  // playHref 优先（接口注释与「继续观看」行的设计一致：整卡点击即起播，
  // 不该先进详情再找播放键）；仅有 href 时落详情页
  const playFirst = Boolean(item.playHref);
  const target = item.playHref ?? item.href;
  // Link 分支不传动作：guard 只负责拦下误触（preventDefault 掉跳转），
  // 放行的点击走 Link 默认导航；button 分支把 openDetail 交给 onTap
  const tapGuard = useTapGuard(target ? undefined : () => openDetail(item.media));
  const artwork = (
    <>
      <div className="relative aspect-video overflow-hidden rounded-[4px] bg-[#181818] ring-1 ring-white/[0.06] transition-all duration-150 group-hover/nfcard:ring-white/30">
        <LandscapeArtwork media={item.media} />
        {item.progress != null && (
          <span className="absolute inset-x-0 bottom-0 h-[3px] bg-white/25">
            <span
              className="block h-full bg-[var(--accent)]"
              style={{ width: `${Math.max(0, Math.min(100, item.progress))}%` }}
            />
          </span>
        )}
      </div>
      {item.context && (
        <p className="tnum mt-1.5 truncate text-caption text-[var(--text-muted)]">
          {item.context}
        </p>
      )}
    </>
  );
  const shared = "group/nfcard block cursor-pointer outline-none";
  return (
    <div
      className={`relative ${className}`}
      onPointerEnter={onHoverEnter ? (e) => onHoverEnter(e.currentTarget) : undefined}
      onPointerLeave={onHoverLeave}
    >
      {target ? (
        <Link
          href={target}
          {...tapGuard}
          className={shared}
          aria-label={`${playFirst ? "播放" : "查看"}《${item.media.title}》${playFirst ? "" : "详情"}${item.context ? `，${item.context}` : ""}`}
        >
          {artwork}
        </Link>
      ) : (
        <button
          type="button"
          {...tapGuard}
          className={shared}
          aria-label={`查看《${item.media.title}》详情${item.context ? `，${item.context}` : ""}`}
        >
          {artwork}
        </button>
      )}
    </div>
  );
}

/** 横版卡画面：优先 backdrop；缺失时海报模糊铺底 + 中央完整显示（既有机制） */
function LandscapeArtwork({ media }: { media: MediaItem }) {
  const CARD_ASPECT = 16 / 9;
  const backdrop = media.backdropUrl;
  if (backdrop) {
    return (
      <PosterImage
        src={imageUrl(backdrop, "landscape-card")}
        alt={`${media.title} 剧照`}
        className="size-full transition duration-500 group-hover/nfcard:scale-[1.03]"
        fallback={
          <PosterFallback media={media} cardAspect={CARD_ASPECT} />
        }
      />
    );
  }
  return <PosterFallback media={media} cardAspect={CARD_ASPECT} />;
}

/** 海报兜底：同一张图放大模糊做底，中央按真实比例完整显示 */
function PosterFallback({ media, cardAspect }: { media: MediaItem; cardAspect: number }) {
  const posterAspect = media.imageAspect ?? media.aspect ?? 2 / 3;
  if (!media.posterUrl) {
    return (
      <span className="flex size-full items-center justify-center px-4 text-center text-ui font-semibold text-white/25">
        {media.title}
      </span>
    );
  }
  if (Math.abs(posterAspect - cardAspect) <= 0.05) {
    return <PosterImage src={media.posterUrl} alt={`${media.title} 海报`} className="size-full" />;
  }
  return (
    <div className="relative size-full overflow-hidden bg-[#10131c]">
      <PosterImage
        src={media.posterUrl}
        alt=""
        className="absolute inset-0 size-full scale-125 blur-xl opacity-45"
      />
      <div className="absolute inset-0 bg-black/25" />
      <div className="absolute inset-0 flex items-center justify-center">
        <div
          style={{ aspectRatio: posterAspect }}
          className={`${posterAspect > cardAspect ? "w-full" : "h-full"}`}
        >
          <PosterImage src={media.posterUrl} alt={`${media.title} 海报`} className="size-full" />
        </div>
      </div>
    </div>
  );
}
