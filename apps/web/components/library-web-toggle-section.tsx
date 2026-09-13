"use client";

import { useCallback, useEffect, useState } from "react";

import { fetchLibraryWebFeature, saveLibraryWebFeature } from "@/lib/api/libraries";
import { getSession } from "@/lib/api/auth";
import { useSession } from "@/lib/session";

/**
 * 「播放与媒体库」分区的总开关：是否启用 movieclaw 自带的网页媒体库
 * （海报墙浏览、条目详情、网页播放、影片分享）。
 *
 * 面向「看片用 Plex / VidHub / Infuse 等外部播放器」的部署：关掉后全站
 * 隐藏浏览与播放入口、相关接口下线；订阅、寻种、下载、整理、刮削照常，
 * 管理员的库管理页保留。观看进度/收藏等数据不删，重新打开即恢复。
 *
 * 改成即存、失败回滚（同进度条预览开关）；开关是系统级的，保存成功后
 * 顺手刷新会话快照（/auth/me 的 library_enabled），导航等入口随新会话
 * 立即增减，不用等下一次登录。
 */
export function LibraryWebToggleSection() {
  const { setSession } = useSession();
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchLibraryWebFeature()
      .then((feature) => {
        if (alive) setEnabled(feature.enabled);
      })
      .catch((e: Error) => {
        if (alive) setError(e.message);
      });
    return () => {
      alive = false;
    };
  }, []);

  const toggle = useCallback(
    async (next: boolean) => {
      const previous = enabled;
      setEnabled(next); // 乐观更新：失败回滚，成功时省一次等待
      setBusy(true);
      setError(null);
      try {
        const feature = await saveLibraryWebFeature(next);
        setEnabled(feature.enabled);
        // 会话快照带的是开关的镜像，刷新它让全站导航立刻跟上
        setSession(await getSession());
      } catch (e) {
        setEnabled(previous);
        setError((e as Error).message);
      } finally {
        setBusy(false);
      }
    },
    [enabled, setSession],
  );

  return (
    <section>
      <h3 className="group-label mb-2.5 px-1">网页媒体库</h3>
      <div className="css-glass space-y-4 !rounded-2xl p-5 max-sm:p-4">
        {error && (
          <div
            role="alert"
            className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff9b9b]"
          >
            {error}
          </div>
        )}
        <label className="flex cursor-pointer items-center justify-between gap-4">
          <span>
            <span className="block text-body font-medium text-[var(--text)]">
              启用网页媒体库与网页播放
            </span>
            <span className="mt-0.5 block text-caption leading-5 text-[var(--text-faint)]">
              {enabled
                ? "提供海报墙、条目详情与网页播放；关闭后看片请用 Plex / VidHub / Infuse 等外部播放器"
                : "已关闭：浏览与播放页面下线，订阅、下载、整理、刮削不受影响；库管理页对管理员保留"}
            </span>
          </span>
          {enabled !== null && (
            <input
              type="checkbox"
              checked={enabled}
              disabled={busy}
              onChange={(event) => void toggle(event.target.checked)}
              className="size-5 shrink-0 accent-[var(--accent)]"
            />
          )}
        </label>
      </div>
    </section>
  );
}
