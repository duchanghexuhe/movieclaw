"use client";

import type { SessionView } from "@/lib/api/auth";
import { useSession } from "@/lib/session";

/**
 * 前端只消费业务权限，不在各页面散落角色判断。这里负责把管理员角色和成员
 * 能力快照收敛为稳定语义；后端鉴权仍是最终安全边界。
 */
export interface AppPermissions {
  isAdmin: boolean;
  canSubscribe: boolean;
  canSearch: boolean;
  canDirectDownload: boolean;
  canManageLibraries: boolean;
  canManageSubscriptions: boolean;
  /** 网页媒体库开关（系统级，随会话快照下发）：浏览海报墙/详情/网页播放是否可用 */
  canUseLibrary: boolean;
}

export function permissionsFor(session: SessionView): AppPermissions {
  const isAdmin = session.role === "admin";
  return {
    isAdmin,
    canSubscribe: isAdmin || session.capabilities.allow_subscribe,
    canSearch: isAdmin || session.capabilities.allow_search,
    canDirectDownload: isAdmin || session.capabilities.allow_direct_download,
    canManageLibraries: isAdmin,
    canManageSubscriptions: isAdmin,
    // !== false：老后端的会话快照没有这个字段（滚动更新的瞬间），缺省按开着算，
    // 退化到后端 404 兜底，而不是把所有人的媒体库藏掉
    canUseLibrary: session.library_enabled !== false,
  };
}

/** 当前会话的语义权限；权限变化后随 SessionProvider 快照立即更新。 */
export function usePermissions(): AppPermissions {
  return permissionsFor(useSession().session);
}

export function roleLabel(session: SessionView): string {
  return session.role === "admin" ? "超级管理员" : "成员";
}

/**
 * 网页媒体库关闭后的落点：管理员还有库要管（扫描/整理），去库管理页；
 * 成员没有管理面，能去的第一个功能页是订阅（有权限时）或发现页。
 */
export function libraryDisabledHomePath(session: SessionView): string {
  if (session.role === "admin") return "/library/manage";
  return permissionsFor(session).canSubscribe ? "/subscriptions" : "/discover/movies";
}

/**
 * 把登录后的目标地址收敛到当前身份可进入的页面。除了登录落点，AuthGate
 * 也复用它拦截手输 URL，避免成员短暂看到 Agent 页面再收到后端 403。
 *
 * 网页媒体库关闭时：/library 浏览页、/play 播放页整体下线（管理员的
 * /library/manage 保留——整理链路的入口）。
 */
export function accessiblePathFor(session: SessionView, requestedPath: string): string {
  const libraryOpen = session.library_enabled !== false;
  if (!libraryOpen) {
    const manageAllowed = session.role === "admin" && requestedPath.startsWith("/library/manage");
    if (
      (requestedPath.startsWith("/library") || requestedPath.startsWith("/play")) &&
      !manageAllowed
    ) {
      return libraryDisabledHomePath(session);
    }
  }
  // 媒体库不可用时的成员回落目标（原本统一回落 /library）
  const memberFallback = libraryOpen ? "/library" : libraryDisabledHomePath(session);
  if (session.role === "member") {
    if (requestedPath === "/" || requestedPath.startsWith("/sessions/")) return memberFallback;
    if (!session.capabilities.allow_subscribe && requestedPath.startsWith("/subscriptions")) {
      return memberFallback === "/subscriptions" ? "/discover/movies" : memberFallback;
    }
    if (!session.capabilities.allow_search && requestedPath.startsWith("/search")) {
      return memberFallback;
    }
  }
  return requestedPath;
}
