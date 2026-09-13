"use client";

import { useEffect, useMemo, useState } from "react";

import { listLibraries } from "@/lib/api/libraries";
import type { SearchVertical } from "@/lib/categories";
import { usePermissions } from "@/lib/permissions";

const ORDERED_SEARCH_VERTICALS: SearchVertical[] = ["media", "torrent", "library"];

export interface SearchAccess {
  canMedia: boolean;
  canTorrent: boolean;
  canLibrary: boolean;
  ready: boolean;
  available: SearchVertical[];
  firstAvailable: SearchVertical | null;
}

/**
 * 搜索入口的前端权限快照。
 *
 * - 影视：对应成员「订阅」能力，能订阅才需要查影视条目；
 * - 站点资源：对应成员「PT 站资源搜索」能力；
 * - 媒体库：对应成员可见媒体库白名单，至少有一个可见库才展示。
 *
 * 后端仍是最终边界；这里负责菜单/按钮不展示不可用入口，减少误点。
 */
export function useSearchAccess(): SearchAccess {
  const permissions = usePermissions();
  const [libraryAvailable, setLibraryAvailable] = useState<boolean | null>(
    permissions.isAdmin ? true : null,
  );

  useEffect(() => {
    if (permissions.isAdmin) {
      setLibraryAvailable(true);
      return;
    }
    let cancelled = false;
    setLibraryAvailable(null);
    listLibraries()
      .then((libraries) => {
        if (!cancelled) setLibraryAvailable(libraries.length > 0);
      })
      .catch(() => {
        if (!cancelled) setLibraryAvailable(false);
      });
    return () => {
      cancelled = true;
    };
  }, [permissions.isAdmin]);

  const canMedia = permissions.canSubscribe;
  const canTorrent = permissions.canSearch;
  const canLibrary = permissions.isAdmin || libraryAvailable === true;
  const ready = permissions.isAdmin || libraryAvailable !== null;

  return useMemo(() => {
    const available = ORDERED_SEARCH_VERTICALS.filter((vertical) => {
      if (vertical === "media") return canMedia;
      if (vertical === "torrent") return canTorrent;
      return canLibrary;
    });
    return {
      canMedia,
      canTorrent,
      canLibrary,
      ready,
      available,
      firstAvailable: available[0] ?? null,
    };
  }, [canLibrary, canMedia, canTorrent, ready]);
}
