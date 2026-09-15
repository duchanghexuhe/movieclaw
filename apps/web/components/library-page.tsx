"use client";

import { LibraryView } from "@/components/library-view";
import { NetflixLibraryHero } from "@/components/netflix/library-hero";
import { useTheme } from "@/lib/ui-prefs";

/**
 * 媒体库页本体（/library 的客户端部分）。
 *
 * Netflix 主题把原「内容首页」的全出血 Billboard 插在本页行清单上方
 * （2026-09 修订：首页与媒体库合并，/ 在该主题下重定向到这里，
 * docs/design/web-themes.md §5.3）；银玻璃保持原版式、不渲染 hero。
 * 根部的 flex 列容器为 LibraryView 的滚动区提供确定高度。
 */
export function LibraryPageBody() {
  const isNetflix = useTheme().id === "netflix";
  return (
    <div className="flex h-full flex-col">
      <LibraryView hero={isNetflix ? <NetflixLibraryHero /> : null} />
    </div>
  );
}
