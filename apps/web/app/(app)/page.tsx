"use client";

import { NewTask } from "@/components/new-task";
import { NetflixHome } from "@/components/netflix/home";
import { useTheme } from "@/lib/ui-prefs";

/**
 * 首页（/）按主题分支（docs/design/web-themes.md §0 决策 3）：
 *   - 银玻璃（默认）：AI 新任务输入台——全站唯一不铺蒙版的「氛围页」门面；
 *   - Netflix：内容首页（billboard + 行），AI 新任务收进顶栏「＋ 新任务」
 *     与独立路由 /new。
 * 主题只存在于登录后的偏好 Context 里（服务端无值），因此这里是客户端组件。
 */
export default function HomePage() {
  const theme = useTheme();
  return theme.id === "netflix" ? <NetflixHome /> : <NewTask />;
}
