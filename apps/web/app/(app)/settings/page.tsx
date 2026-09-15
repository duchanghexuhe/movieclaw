"use client";

import { useRouter } from "next/navigation";
import { useEffect } from "react";

import { NetflixSettingsIndex } from "@/components/netflix/settings-index";
import { settingsSections } from "@/lib/mock-data";
import { useIsMobile } from "@/lib/use-media-query";
import { useTheme } from "@/lib/ui-prefs";

/**
 * /settings 裸地址按形态分支：
 *   - Netflix 主题移动端：设置分区列表页（2026-09 修订——原分区下拉浮层在
 *     长清单上滑不动，见 components/netflix/settings-index.tsx）；
 *   - 其余形态（银玻璃全部 + Netflix 桌面）：重定向到首个分区，保证设置页
 *     始终有明确的分区地址。首个分区即「概览」落地页——管理员进设置先看到
 *     配置状态与下一步；成员没有概览（见 MEMBER_SECTION_IDS），SettingsPanel
 *     会兜底到个人信息。桌面端分区菜单在常驻侧栏，列表页反而多一跳。
 * 主题只存在于客户端偏好 Context（服务端无值），因此这里是客户端组件；
 * 首帧即真实形态：AppShell 整棵树在 AuthGate 确认登录后纯客户端渲染，
 * useIsMobile 首帧读真实 matchMedia、主题取 localStorage 缓存（ui-prefs）。
 */
export default function SettingsIndexPage() {
  const router = useRouter();
  const isNetflix = useTheme().id === "netflix";
  const isMobile = useIsMobile();
  const isNetflixMobile = isNetflix && isMobile;

  useEffect(() => {
    if (!isNetflixMobile) router.replace(`/settings/${settingsSections[0].id}`);
  }, [isNetflixMobile, router]);

  return isNetflixMobile ? <NetflixSettingsIndex /> : null;
}
