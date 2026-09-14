"use client";

import Link from "next/link";
import type { Route } from "next";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import {
  ArrowLeftIcon,
  BookmarkIcon,
  ChevronDownIcon,
  CompassIcon,
  LibraryIcon,
  UserIcon,
} from "@/components/icons";
import { settingsSectionGroupsFor } from "@/lib/mock-data";
import { usePageChrome } from "@/lib/page-chrome";
import { usePermissions } from "@/lib/permissions";
import { useSession } from "@/lib/session";

/**
 * Netflix 主题的移动端底部标签栏（<768px，docs/design/web-themes.md §5.2）。
 *
 * 参照 Netflix App 的内容消费动线映射为 4 个**路由**页签：发现 / 媒体库 /
 * 订阅 / 我的（2026-09 修订：移除「首页」——内容首页 / 改由顶栏字标直达，
 * 底栏让位给高频的内容入口；「订阅」对齐桌面顶栏的「我的订阅」）。
 * 「订阅」按 canSubscribe 显隐，无权限时退化为 3 页签。
 * 栏高 49px + 底部安全区、激活白、未激活 #808080、图标 24px——这些数值
 * 无官方出处，按 iOS 惯例取值（设计文档标注的自家设计决策）。
 */

/** 页签基础清单（订阅由权限过滤补充，「我的」固定在末位）。 */
const BASE_TABS = [
  { id: "discover", label: "发现", href: "/discover/movie", Icon: CompassIcon },
  { id: "library", label: "媒体库", href: "/library", Icon: LibraryIcon },
] as const;

const SUBSCRIPTION_TAB = {
  id: "subscriptions",
  label: "订阅",
  href: "/subscriptions",
  Icon: BookmarkIcon,
} as const;

const MY_TAB = { id: "my", label: "我的", href: "/my", Icon: UserIcon } as const;

/** pathname → 当前页签 id（详情等子页落在所属的顶层页签上）。 */
function activeTabId(pathname: string): string {
  if (pathname.startsWith("/discover") || pathname.startsWith("/media")) return "discover";
  if (pathname.startsWith("/library")) return "library";
  if (pathname.startsWith("/subscriptions")) return "subscriptions";
  // 设置是「我的」的二级页面（返回键固定回 /my）：iOS 惯例是二级页保持
  // 父页签高亮，进设置后四个页签全部熄灭会让用户失去「我在哪」的位置感
  if (pathname.startsWith("/settings") || pathname === "/my") return "my";
  return "";
}

export function NetflixTabBar() {
  const pathname = usePathname();
  const { canSubscribe } = usePermissions();
  const active = activeTabId(pathname);
  // 订阅页签按权限插在媒体库与我的之间；tab 数组重建的代价可忽略（4 个字面量）
  const tabs = canSubscribe ? [...BASE_TABS, SUBSCRIPTION_TAB, MY_TAB] : [...BASE_TABS, MY_TAB];

  return (
    <nav
      aria-label="主导航"
      className="nf-tabbar fixed inset-x-0 bottom-0 z-40 flex h-[calc(49px+var(--safe-bottom))] items-stretch border-t border-white/[0.06] pb-[var(--safe-bottom)]"
    >
      {tabs.map(({ id, label, href, Icon }) => (
        <Link
          key={id}
          href={href}
          aria-current={active === id ? "page" : undefined}
          className={`flex flex-1 flex-col items-center justify-center gap-0.5 ${
            active === id ? "text-white" : "text-[#808080]"
          }`}
        >
          <Icon className="size-6" />
          <span className="text-micro font-medium leading-none">{label}</span>
        </Link>
      ))}
    </nav>
  );
}

/**
 * 移动端设置页的分区导航条：左侧返回键（回到「我的」/真实来路）+ 当前分区
 * 下拉。银玻璃主题下分区列表装在抽屉侧栏里，Netflix 主题抽屉退役后由这条
 * 下拉承接同样的导航能力（/settings/* 可达性不回退）。挂在页面内容顶部
 * （外壳在 settings 路由下渲染），数据与桌面分区菜单同源。
 *
 * 顶栏认领：挂载即 registerPageNav，让外壳撤掉全局顶栏（MobileTopBar）——
 * 否则设置页顶上摞两条顶栏（全局 52px + 本条），违背「窄屏永远只有一条
 * 顶栏」的收口原则（lib/page-chrome.tsx）。认领后 safe-top/left/right 由
 * 本条自己让出（原由全局顶栏承担）。
 */
export function NetflixSettingsNav({
  active,
  onSelect,
}: {
  active: string;
  onSelect: (id: string) => void;
}) {
  const { session } = useSession();
  const router = useRouter();
  const chrome = usePageChrome();
  // 认领移动端顶栏那一行（注销函数即 effect 清理）；桌面分支不渲染本组件，无副作用
  useEffect(() => chrome?.registerPageNav(), [chrome]);
  // 设置是「我的」的二级页面，返回键语义是「回上级」而不是「历史后退」：
  // 用户可能在分区间连续切换（历史里堆着一串 /settings/*），按后退语义要
  // 逐级回退每个分区才能离开设置，与 iOS 设置页的返回心智不符。固定
  // replace 回 /my，一次到位且不额外堆积历史。
  const back = () => router.replace("/my" as Route);
  const groups = settingsSectionGroupsFor(session.role);
  const all = groups.flatMap((group) => group.items);
  const current = all.find((section) => section.id === active) ?? all[0];
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointer = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointer);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointer);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (!current) return null;

  return (
    <div
      ref={rootRef}
      className="relative z-30 shrink-0 border-b border-[var(--line)] bg-[var(--bg)] py-2 pl-[max(0.5rem,var(--safe-left))] pr-[max(0.5rem,var(--safe-right))] pt-[calc(var(--safe-top)+0.5rem)]"
    >
      <div className="flex items-center gap-1">
        <button
          type="button"
          onClick={back}
          aria-label="返回"
          className="nf-icon-btn !size-11"
        >
          <ArrowLeftIcon className="size-[22px]" />
        </button>
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          className="flex min-w-0 items-center gap-1.5 px-2 text-title font-semibold text-[var(--text)]"
        >
          <span className="truncate">{current.label}</span>
          <ChevronDownIcon className={`size-4 shrink-0 transition-transform ${open ? "rotate-180" : ""}`} />
        </button>
      </div>
      {open && (
        <div
          className="menu-surface absolute inset-x-4 top-full z-50 p-1.5"
          // .menu-surface 的无层 CSS position:relative 会压过 absolute 工具类
          // （同 top-nav.tsx 两处下拉），内联覆盖才能锚在当前行下方
          style={{ position: "absolute" }}
        >
          {groups.map((group) => (
            <div key={group.label || group.items[0]?.id}>
              {group.label && <p className="group-label px-2.5 pb-1 pt-2.5">{group.label}</p>}
              {group.items.map((section) => (
                <button
                  key={section.id}
                  type="button"
                  onClick={() => {
                    setOpen(false);
                    onSelect(section.id);
                  }}
                  className={`glass-row px-2.5 py-2 text-ui font-medium ${
                    section.id === active ? "text-white" : ""
                  }`}
                >
                  <span className="flex-1 truncate">{section.label}</span>
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
