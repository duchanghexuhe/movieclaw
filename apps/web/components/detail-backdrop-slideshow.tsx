"use client";

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import type { MediaImage } from "@/lib/api/discover";

const ROTATE_INTERVAL_MS = 9000;
const FADE_MS = 1600;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** 预加载并解码一张图；失败返回 false（该张本轮跳过）。 */
function load(url: string): Promise<boolean> {
  return new Promise((resolve) => {
    const img = new Image();
    const done = (ok: boolean) => {
      if (!ok) {
        resolve(false);
        return;
      }
      // 与沉浸覆盖层同款：解码完成才算就位，避免切上去那一帧空白
      img
        .decode()
        .catch(() => {})
        .then(() => resolve(true));
    };
    img.onload = () => done(true);
    img.onerror = () => done(false);
    img.src = url;
  });
}

/**
 * Netflix 详情页的背景轮换：剧照 original 原图按序做「叠变」——交叉溶解
 * （1.6s 双向淡入淡出）+ Ken Burns 缓推（globals.css 的 nf-kenburns：每张图
 * 在显示期间缓慢放大，溶解时两层各自在动，观感是「融接」而非「切换」）。
 * portal 到 body、z-2——**必须 portal**：放在详情树里会画在滚动容器的
 * 渐变板之上，横幅以下的本该全黑的区域会漏出图片。构图与覆盖层保持一致
 * （globals.css 的 .detail-slideshow，改构图两处必须一起改）。
 *
 * 规矩：
 *   - 首帧用 initialUrl（与沉浸覆盖层同源的主图，衔接零跳变），轮换从清单
 *     第一张起循环（与首帧相同的图再放一次无妨，观众不会对照编号）；
 *   - 下一张预加载并解码**就位才切**，加载失败跳过该张，绝不闪黑；
 *   - 页面不可见（切标签页）时跳过该轮，回来自动继续；
 *   - 系统开启「减弱动态效果」时不轮（本组件直接不挂载，见调用方）。
 *
 * 时序模型：轮换循环只跑**一份实例**（effect 依赖空数组 + refs 读参）。
 * 此前依赖 `urls` 数组——每次渲染都是新引用，任何一次重渲染都会重启 effect
 * 并取消进行中的循环，而循环第一步 setTop 本身就触发渲染：循环刚起步即被
 * 自己掐死，top 层永远停在 opacity-0（轮换「消失」的实测根因）。
 *
 * 双层交叉淡入：A/B 两个 <img> 叠放，B 淡入完成后把同一张图同步给 A、再把
 * B 摘掉——下一轮继续用 B 换图，任何时刻至多一层在过渡。
 */
export function DetailBackdropSlideshow({
  images,
  initialUrl,
}: {
  images: MediaImage[];
  /** 首帧：与沉浸覆盖层当前显示的主图同源（主 backdrop 原图），衔接零跳变 */
  initialUrl?: string;
}) {
  const [bottom, setBottom] = useState<string | null>(
    initialUrl ?? images[0]?.fullUrl ?? null,
  );
  const [top, setTop] = useState<string | null>(null);
  const [topOn, setTopOn] = useState(false);
  // 首图预加载解码完成才把整层显示出来：挂载即渲染会先透黑、图到了再突然
  // 出现，加上覆盖层同步被隐藏——两次硬切就是闪烁。
  const [ready, setReady] = useState(false);
  // 轮换清单走 ref：effect 只挂载时起一份循环，不随渲染重启（见时序模型）
  const urlsRef = useRef<string[]>(
    images.map((img) => img.fullUrl).slice(0, 5),
  );
  const indexRef = useRef(0);

  useEffect(() => {
    if (!bottom) return;
    let cancelled = false;
    load(bottom).then((ok) => {
      if (!cancelled && ok) setReady(true);
    });
    return () => {
      cancelled = true;
    };
  }, [bottom]);

  useEffect(() => {
    if (urlsRef.current.length < 2) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    let cancelled = false;

    const cycle = async () => {
      await sleep(ROTATE_INTERVAL_MS);
      while (!cancelled) {
        if (!document.hidden) {
          const urls = urlsRef.current;
          const url = urls[indexRef.current % urls.length];
          indexRef.current += 1;
          const ok = await load(url);
          if (cancelled) return;
          if (ok) {
            setTop(url);
            setTopOn(false);
            // 等 60ms 让浏览器把新 src（opacity-0）提交后再触发过渡，否则淡入
            // 会被吞掉。不能用 requestAnimationFrame：宿主面板被遮挡/后台时
            // rAF 被节流甚至暂停，await 会永远挂起，轮换整个卡死（实测）。
            await sleep(60);
            if (cancelled) return;
            setTopOn(true);
            await sleep(FADE_MS + 100);
            if (cancelled) return;
            // 顶层已完全不透明：把同一张图落到底层、顶层摘掉，回到初始态
            setBottom(url);
            setTopOn(false);
            await sleep(FADE_MS + 100);
            if (cancelled) return;
            setTop(null);
          }
        } else {
          indexRef.current += 1;
        }
        await sleep(ROTATE_INTERVAL_MS);
      }
    };
    void cycle();
    return () => {
      cancelled = true;
    };
  }, []);

  if (!bottom || typeof document === "undefined") return null;
  return createPortal(
    <div
      aria-hidden="true"
      data-ready={ready}
      className="detail-slideshow pointer-events-none fixed inset-0 [bottom:calc(-1*var(--vp-overshoot))]"
    >
      {/* key=src（加层前缀）：换图即新元素，Ken Burns 动画从头起播。
          前缀必须有——溶解收尾时 bottom 会短暂与 top 同 URL，裸 URL 作 key
          会触发 React 重复 key 报错（每次轮换一条）。 */}
      <img
        key={`b:${bottom}`}
        src={bottom}
        alt=""
        draggable={false}
        className="absolute inset-0 size-full object-cover"
      />
      {top && (
        <img
          key={`t:${top}`}
          src={top}
          alt=""
          draggable={false}
          className={`absolute inset-0 size-full object-cover ${
            topOn ? "opacity-100" : "opacity-0"
          }`}
        />
      )}
    </div>,
    document.body,
  );
}
