"use client";

import type { Route } from "next";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import { AccountSwitcherDialog } from "@/components/account-switcher-dialog";
import { AvatarBadge } from "@/components/avatar-badge";
import { MovieclawMark } from "@/components/netflix/brand";
import {
  ActivityIcon,
  ChevronRightIcon,
  GearIcon,
  LogoutIcon,
  PlusIcon,
  BookmarkIcon,
  UserIcon,
} from "@/components/icons";
import { logout } from "@/lib/api/auth";
import { clearBackdropCache } from "@/lib/backdrop-cache";
import { useAgentConversations } from "@/lib/agent-conversations";
import { settingsSectionGroupsFor, settingsSections } from "@/lib/mock-data";
import { accessiblePathFor, usePermissions } from "@/lib/permissions";
import { useSession } from "@/lib/session";
import { clearUiPrefsCache } from "@/lib/ui-prefs-cache";
import { useTheme } from "@/lib/ui-prefs";

/**
 * Netflix 主题的「我的」页面（路由 /my，2026-09 修订）。
 *
 * 原实现是右侧滑出的 NetflixMySheet 面板——浮层形态承载不了还在生长的
 * 账号/设置动线（面板里点设置又要跳路由），且与「每个入口都是真实路由、
 * 可刷新可分享」的全站导航原则相悖。改为独立页面后：
 *   - 底栏「我的」页签直接路由到 /my（不再开关面板）；
 *   - 设置成为本页的二级页面（顶栏返回键回来，见 NetflixSettingsNav）；
 *   - 银玻璃主题不使用本页（它的同等入口在抽屉侧栏），直达时跳回首页。
 *
 * 内容分区对齐 Netflix App 的 My Netflix：用户头 + 快捷入口 + AI 会话 +
 * 账号操作。行皮肤用 glass-row（Netflix 主题下自动换实色卡 + 4px 方角）。
 */
export function NetflixMyPage() {
  const router = useRouter();
  const theme = useTheme();
  const { session } = useSession();
  const { isAdmin, canSubscribe } = usePermissions();
  const { conversations } = useAgentConversations();
  const [switcherOpen, setSwitcherOpen] = useState(false);

  // 银玻璃主题的同等入口在抽屉侧栏里，本页只在 Netflix 结构下存在
  useEffect(() => {
    if (theme.id !== "netflix") router.replace("/");
  }, [theme.id, router]);

  /**
   * 退出登录：只退当前账号，本浏览器还有别的账号时后端自动切过去，
   * 没有了才去登录页；整页跳转重置全部前端状态。
   */
  const handleLogout = async () => {
    let next: Awaited<ReturnType<typeof logout>> = null;
    try {
      next = await logout();
    } catch {
      // 即使请求失败（网络断开），也照常跳登录页；会话在后端仍会自然过期
    }
    clearBackdropCache();
    clearUiPrefsCache();
    window.location.href = next ? accessiblePathFor(next, "/") : "/login";
  };

  if (theme.id !== "netflix") return null;

  // 进设置的默认分区按角色取可见清单第一项：管理员落「概览」，成员落
  // 「个人信息」——避免把成员送进一个 403 分区（与外壳 openSettings 同口径）
  const defaultSettingsSection =
    settingsSectionGroupsFor(session.role)[0]?.items[0]?.id ?? settingsSections[0].id;

  return (
    <div className="scroll-thin scroll-safe h-full overflow-y-auto">
      <div className="mx-auto w-full max-w-2xl px-4 pb-16 pt-6 md:px-6 md:pt-10">
        {/* 用户头：头像 + 昵称 + 用户名（My Netflix 的门面） */}
        <header className="flex items-center gap-4 px-1">
          <AvatarBadge
            nickname={session.nickname}
            avatarUrl={session.avatar_url}
            className="size-14 rounded-[4px] text-title-lg"
          />
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-title-lg font-bold tracking-[-0.01em] text-[var(--text)]">
              {session.nickname}
            </h1>
            <p className="mt-0.5 truncate text-ui text-[var(--text-muted)]">
              @{session.username}
            </p>
          </div>
          <MovieclawMark className="h-6 w-auto shrink-0 opacity-90" aria-hidden="true" />
        </header>

        {/* 快捷入口 */}
        <nav aria-label="我的入口" className="mt-6 space-y-0.5">
          <MyRow Icon={PlusIcon} label="新任务" onClick={() => router.push("/new" as Route)} />
          {canSubscribe && (
            <MyRow
              Icon={BookmarkIcon}
              label="我的订阅"
              onClick={() => router.push("/subscriptions" as Route)}
            />
          )}
          {isAdmin && (
            <MyRow Icon={ActivityIcon} label="活动" onClick={() => router.push("/activity" as Route)} />
          )}
          <MyRow
            Icon={GearIcon}
            label="设置"
            onClick={() => router.push(`/settings/${defaultSettingsSection}` as Route)}
          />
        </nav>

        {/* AI 会话：与原「我的」面板同源（管理员可见），点击直达会话页 */}
        {isAdmin && (
          <nav aria-label="AI 会话" className="mt-6 space-y-0.5">
            <p className="group-label px-3 pb-1.5">AI 会话</p>
            {conversations.length === 0 ? (
              <p className="px-3 py-1.5 text-caption leading-5 text-[var(--text-faint)]">
                还没有会话，从「新任务」开始。
              </p>
            ) : (
              conversations.map((c) => (
                <MyRow
                  key={c.id}
                  label={c.title}
                  running={c.running}
                  onClick={() => router.push(`/sessions/${c.id}` as Route)}
                />
              ))
            )}
          </nav>
        )}

        {/* 账号操作 */}
        <nav aria-label="账号操作" className="mt-6 space-y-0.5">
          <MyRow Icon={UserIcon} label="切换账号" onClick={() => setSwitcherOpen(true)} />
          <MyRow Icon={LogoutIcon} label="退出登录" danger onClick={() => void handleLogout()} />
        </nav>
      </div>

      <AccountSwitcherDialog open={switcherOpen} onClose={() => setSwitcherOpen(false)} />
    </div>
  );
}

/** 页面行：glass-row 皮肤 + 右缘 chevron 表达「点进去」的可点性。 */
function MyRow({
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
      className={`glass-row w-full px-3 py-3 text-ui font-medium ${
        danger ? "!text-[var(--danger)] hover:!bg-[rgba(255,107,107,0.12)]" : ""
      }`}
    >
      {running && (
        <span aria-hidden="true" className="size-1.5 shrink-0 animate-pulse rounded-full bg-[#6aa7ff]" />
      )}
      {Icon && <Icon className="size-[20px] shrink-0" />}
      <span className="min-w-0 flex-1 truncate text-left">{label}</span>
      <ChevronRightIcon className="size-4 shrink-0 text-[var(--text-faint)]" />
    </button>
  );
}
