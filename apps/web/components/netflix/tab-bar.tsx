"use client";

import Link from "next/link";
import type { Route } from "next";
import { usePathname, useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";

import { AccountSwitcherDialog } from "@/components/account-switcher-dialog";
import { AvatarBadge } from "@/components/avatar-badge";
import { MovieclawMark } from "@/components/netflix/brand";
import {
  ActivityIcon,
  BookmarkIcon,
  ChevronDownIcon,
  CompassIcon,
  GearIcon,
  HouseIcon,
  LibraryIcon,
  LogoutIcon,
  PlusIcon,
  UserIcon,
} from "@/components/icons";
import { logout } from "@/lib/api/auth";
import { clearBackdropCache } from "@/lib/backdrop-cache";
import { useAgentConversations } from "@/lib/agent-conversations";
import { settingsSectionGroupsFor } from "@/lib/mock-data";
import { accessiblePathFor, usePermissions } from "@/lib/permissions";
import { useSession } from "@/lib/session";
import { clearUiPrefsCache } from "@/lib/ui-prefs-cache";

/**
 * Netflix 主题的移动端底部标签栏（<768px，docs/design/web-themes.md §5.2）。
 *
 * 参照基准：Netflix App 2026-04 改版前的三 tab 稳定期形态，按本站功能映射为
 * 4 个页签：首页 / 发现 / 媒体库 / 我的。「我的」承载原抽屉内容（原 mobile
 * 抽屉在 Netflix 主题下退役，openDrawer 由外壳改接到这个面板上），栏高
 * 49px + 底部安全区、激活白、未激活 #808080、图标 24px——这些数值无官方
 * 出处，按 iOS 惯例取值（设计文档标注的自家设计决策）。
 */

const TABS = [
  { id: "home", label: "首页", href: "/", Icon: HouseIcon },
  { id: "discover", label: "发现", href: "/discover/movie", Icon: CompassIcon },
  { id: "library", label: "媒体库", href: "/library", Icon: LibraryIcon },
] as const;

/** pathname → 当前页签 id（详情等子页落在所属的顶层页签上）。 */
function activeTabId(pathname: string): string {
  if (pathname === "/" || pathname.startsWith("/new") || pathname.startsWith("/sessions/")) {
    return "home";
  }
  if (pathname.startsWith("/discover")) return "discover";
  if (pathname.startsWith("/library") || pathname.startsWith("/media")) return "library";
  return "";
}

export function NetflixTabBar({ onOpenMy }: { onOpenMy: () => void }) {
  const pathname = usePathname();
  const active = activeTabId(pathname);

  return (
    <nav
      aria-label="主导航"
      className="nf-tabbar fixed inset-x-0 bottom-0 z-40 flex h-[49px] items-stretch border-t border-white/[0.06] pb-[var(--safe-bottom)]"
    >
      {TABS.map(({ id, label, href, Icon }) => (
        <Link
          key={id}
          href={href}
          aria-current={active === id ? "page" : undefined}
          className={`flex flex-1 flex-col items-center justify-center gap-0.5 ${
            active === id ? "text-white" : "text-[#808080]"
          }`}
        >
          <Icon className="size-6" />
          <span className="text-[10px] font-medium leading-none">{label}</span>
        </Link>
      ))}
      {/* 「我的」不是路由而是面板：面板开着时保持高亮 */}
      <button
        type="button"
        onClick={onOpenMy}
        aria-expanded="true"
        className={`flex flex-1 flex-col items-center justify-center gap-0.5 ${
          active === "my" ? "text-white" : "text-[#808080]"
        }`}
      >
        <UserIcon className="size-6" />
        <span className="text-[10px] font-medium leading-none">我的</span>
      </button>
    </nav>
  );
}

/**
 * 「我的」面板：原移动抽屉的内容在 Netflix 主题下的新家——用户信息、新任务、
 * 我的订阅、活动、设置、AI 会话列表、切换账号、退出登录。
 *
 * 复用原抽屉的交互契约：路由切换自动收起由外壳负责；Esc 关闭；面板挂在
 * .app-shell 之外不会被外壳缩放裁掉（.nf-mysheet 是 fixed 定位）。
 */
export function NetflixMySheet({
  open,
  onClose,
  onOpenSettings,
}: {
  open: boolean;
  onClose: () => void;
  onOpenSettings: (sectionId?: string) => void;
}) {
  const router = useRouter();
  const { session } = useSession();
  const { isAdmin, canSubscribe } = usePermissions();
  const { conversations } = useAgentConversations();
  const [switcherOpen, setSwitcherOpen] = useState(false);

  // 面板打开时按 Esc 关闭（外接键盘 / 平板场景）
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const go = (href: Route) => {
    onClose();
    router.push(href);
  };

  const handleLogout = async () => {
    onClose();
    let next: Awaited<ReturnType<typeof logout>> = null;
    try {
      next = await logout();
    } catch {
      // 即使请求失败，也照常跳登录页；会话在后端仍会自然过期
    }
    clearBackdropCache();
    clearUiPrefsCache();
    window.location.href = next ? accessiblePathFor(next, "/") : "/login";
  };

  return (
    <>
      <AccountSwitcherDialog open={switcherOpen} onClose={() => setSwitcherOpen(false)} />
      {open && (
        <button
          type="button"
          aria-label="关闭我的面板"
          onClick={onClose}
          className="mobile-drawer-scrim cursor-default"
        />
      )}
      <div className="nf-mysheet" data-open={open} aria-hidden={!open} inert={!open}>
        <div className="menu-surface flex h-full flex-col overflow-hidden border-0 p-1.5">
          {/* 头部：品牌 + 用户信息 */}
          <div className="flex items-center gap-3 px-2 pb-3 pt-1">
            <MovieclawMark className="h-8 w-auto" />
            <div className="ml-auto flex min-w-0 items-center gap-2">
              <AvatarBadge
                nickname={session.nickname}
                avatarUrl={session.avatar_url}
                className="size-8 text-ui"
              />
              <p className="min-w-0 truncate text-ui font-semibold text-[var(--text)]">
                {session.nickname}
              </p>
            </div>
          </div>
          <div className="h-px bg-white/[0.08]" />

          <div className="scroll-thin min-h-0 flex-1 overflow-y-auto pt-1.5">
            <SheetRow
              Icon={PlusIcon}
              label="新任务"
              onClick={() => go("/new" as Route)}
            />
            {canSubscribe && (
              <SheetRow
                Icon={BookmarkIcon}
                label="我的订阅"
                onClick={() => go("/subscriptions" as Route)}
              />
            )}
            {isAdmin && <SheetRow Icon={ActivityIcon} label="活动" onClick={() => go("/activity" as Route)} />}
            <SheetRow Icon={GearIcon} label="设置" onClick={() => { onClose(); onOpenSettings(); }} />

            {isAdmin && (
              <>
                <p className="group-label px-3 pb-1 pt-4">AI 会话</p>
                {conversations.length === 0 ? (
                  <p className="px-3 py-1 text-caption leading-5 text-[var(--text-faint)]">
                    还没有会话，从「新任务」开始。
                  </p>
                ) : (
                  conversations.map((c) => (
                    <SheetRow
                      key={c.id}
                      label={c.title}
                      running={c.running}
                      onClick={() => go(`/sessions/${c.id}` as Route)}
                    />
                  ))
                )}
              </>
            )}
          </div>

          <div className="h-px bg-white/[0.08]" />
          <SheetRow
            Icon={UserIcon}
            label="切换账号"
            onClick={() => {
              onClose();
              setSwitcherOpen(true);
            }}
          />
          <SheetRow
            Icon={LogoutIcon}
            label="退出登录"
            danger
            onClick={() => void handleLogout()}
          />
        </div>
      </div>
    </>
  );
}

/** 面板里的一行（glass-row 皮肤，Netflix 主题下自动跟随 token 换肤）。 */
function SheetRow({
  Icon,
  label,
  onClick,
  danger = false,
  running = false,
}: {
  Icon?: React.ComponentType<React.SVGProps<SVGSVGElement>>;
  label: string;
  onClick: () => void;
  danger?: boolean;
  running?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`glass-row px-2.5 py-2.5 text-ui font-medium ${
        danger ? "!text-[var(--danger)] hover:!bg-[rgba(255,107,107,0.12)]" : ""
      }`}
    >
      {running && (
        <span aria-hidden="true" className="size-1.5 shrink-0 animate-pulse rounded-full bg-[#6aa7ff]" />
      )}
      {Icon && <Icon className="size-[20px] shrink-0" />}
      <span className="min-w-0 flex-1 truncate">{label}</span>
    </button>
  );
}

/**
 * 移动端设置页的分区下拉：银玻璃主题下分区列表装在抽屉侧栏里，Netflix
 * 主题抽屉退役后由这条下拉承接同样的导航能力（/settings/* 可达性不回退）。
 * 挂在页面内容顶部（外壳在 settings 路由下渲染），数据与桌面分区菜单同源。
 */
export function NetflixSettingsNav({
  active,
  onSelect,
}: {
  active: string;
  onSelect: (id: string) => void;
}) {
  const { session } = useSession();
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
    return () => document.removeEventListener("mousedown", onPointer);
  }, [open]);

  if (!current) return null;

  return (
    <div ref={rootRef} className="relative shrink-0 border-b border-[var(--line)] bg-[var(--bg)] px-4 py-2.5">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex items-center gap-1.5 text-title font-semibold text-[var(--text)]"
      >
        {current.label}
        <ChevronDownIcon className={`size-4 transition-transform ${open ? "rotate-180" : ""}`} />
      </button>
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
