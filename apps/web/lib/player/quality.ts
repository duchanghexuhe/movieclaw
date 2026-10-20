/**
 * 画质选择（docs/design/web-player.md §10 预案的「手动选清晰度」）。
 *
 * 语义是**上限而不是目标**：源分辨率不超所选档就照常直通——直通是无损的，
 * 比转出来的同分辨率画质好、还零转码开销；超了才转码降到该档。所以选
 * 「1080p」看一部 1080p H.264 片依然是原画直通，不会画蛇添足地重编码。
 *
 * 「自动」= 不设上限，交给服务端决策引擎（直通优先），也是默认值。
 * 弱网场景用户手选低档，换来的是码率阶梯里对应的低带宽（720p→3M、480p→1.5M）。
 */

export interface QualityOption {
  /** 传给服务端的 max_height；null = 自动（不限制） */
  maxHeight: number | null;
  label: string;
  /** 菜单里的补充说明（带宽预期） */
  hint: string | null;
}

export const QUALITY_OPTIONS: readonly QualityOption[] = [
  { maxHeight: null, label: "自动", hint: "原画质优先，能直通不转码" },
  { maxHeight: 1080, label: "1080p", hint: "约 6 Mbps" },
  { maxHeight: 720, label: "720p", hint: "约 3 Mbps，网络一般时选它" },
  { maxHeight: 480, label: "480p", hint: "约 1.5 Mbps，弱网救急" },
] as const;

/**
 * 画质按「影片 × 在哪看」记（与 iOS App 的 `QualityMemory` 同一口径，apps/apple/MovieClaw/Features/Player/PlayerPreferences.swift）。
 *
 * 原来网页的画质是**全局一个值**：在外面给某部片选一次 720p，之后所有片子、回到家也都在转码。
 * 现在每部片各记各的，剧集整部剧共用一份（按条目 id）；只存限了画质的选择，选回「自动」就删掉
 * 这一条；上限不低于片源等于没限，也记成「自动」（由调用方判）。最多记 300 条，超出按最久没用的先丢。
 *
 * 在家与外网分开记（iOS 同样分）：外网点过一次「改用 720p」，回家照样放原画。网页拿不到网卡与网段，
 * 「在哪看」由服务端按连接来源判定、随会话带回（`PlaybackSession.external`）。开会话之前还不知道，
 * 播放器按上一次会话的网络（`lastPlaybackNetwork`）先猜，猜错了再纠正。
 *
 * 分网络之前记下的条目（键是裸条目 id）一律按外网读：换低画质的提示只在线路跟不上时出现，
 * 那几乎都是在外面。
 */
const MEMORY_KEY = "movieclaw.player.quality-by-title";
/** 旧版的全局画质键：读到就删，不再沿用（理由见上） */
const LEGACY_KEY = "movieclaw.player.quality";
/** 上一次会话的网络（开会话前猜「在哪看」用） */
const NETWORK_KEY = "movieclaw.player.last-network";
export const QUALITY_MEMORY_LIMIT = 300;

/** 在哪看：在家（局域网来源）/ 外网。服务端按连接来源判定 */
export type PlaybackNetwork = "lan" | "external";

/** `<网络>:<条目 id>` → [画质上限, 记下的时刻（毫秒）] */
type QualityEntries = Record<string, [number, number]>;

function entryKey(mediaItemId: number, network: PlaybackNetwork): string {
  return `${network}:${mediaItemId}`;
}

function isValidHeight(value: unknown): value is number {
  return QUALITY_OPTIONS.some((o) => o.maxHeight !== null && o.maxHeight === value);
}

function readEntries(): QualityEntries {
  try {
    const storage = window.localStorage;
    if (storage.getItem(LEGACY_KEY) !== null) storage.removeItem(LEGACY_KEY);
    const raw = storage.getItem(MEMORY_KEY);
    if (!raw) return {};
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return {};
    const entries: QualityEntries = {};
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (Array.isArray(value) && isValidHeight(value[0]) && typeof value[1] === "number") {
        // 分网络之前的条目（裸条目 id）按外网读；同一部片已有新记法的以新的为准
        const normalized = /^\d+$/.test(key) ? `external:${key}` : key;
        if (normalized !== key && normalized in entries) continue;
        entries[normalized] = [value[0], value[1]];
      }
    }
    return entries;
  } catch {
    return {};
  }
}

/** 这部片在这个网络下记着的画质上限；没记过 / 记的值不合法 / 没有 localStorage（SSR、隐私模式）都回「自动」。 */
export function loadQualityFor(mediaItemId: number, network: PlaybackNetwork): number | null {
  return readEntries()[entryKey(mediaItemId, network)]?.[0] ?? null;
}

/**
 * 记下这部片在这个网络下的画质上限；null（自动）删掉这一条。
 *
 * `now` 只给测试用。超过上限按记下时刻从旧到新丢。
 */
export function rememberQualityFor(
  mediaItemId: number,
  network: PlaybackNetwork,
  maxHeight: number | null,
  now: number = Date.now(),
): void {
  const entries = readEntries();
  const key = entryKey(mediaItemId, network);
  if (maxHeight === null || !isValidHeight(maxHeight)) {
    delete entries[key];
  } else {
    entries[key] = [maxHeight, now];
    const keys = Object.keys(entries);
    if (keys.length > QUALITY_MEMORY_LIMIT) {
      keys
        .sort((a, b) => entries[a][1] - entries[b][1])
        .slice(0, keys.length - QUALITY_MEMORY_LIMIT)
        .forEach((old) => delete entries[old]);
    }
  }
  try {
    if (Object.keys(entries).length === 0) window.localStorage.removeItem(MEMORY_KEY);
    else window.localStorage.setItem(MEMORY_KEY, JSON.stringify(entries));
  } catch {
    // 隐私模式下写不进：本次会话内仍生效，只是不记住
  }
}

/** 上一次会话在哪看（开会话前按它取画质记忆）；没记过、读不到按在家。 */
export function lastPlaybackNetwork(): PlaybackNetwork {
  try {
    return window.localStorage.getItem(NETWORK_KEY) === "external" ? "external" : "lan";
  } catch {
    return "lan";
  }
}

export function rememberPlaybackNetwork(network: PlaybackNetwork): void {
  try {
    window.localStorage.setItem(NETWORK_KEY, network);
  } catch {
    // 写不进只是下次可能多猜错一次
  }
}

/** 台账分辨率（"2160p" / "1080p" / "1080i"）→ 像素高度；认不出返回 null。 */
export function sourceHeight(resolution: string | null | undefined): number | null {
  const match = resolution?.match(/^(\d{3,4})[pi]$/i);
  return match ? Number(match[1]) : null;
}

/**
 * 画质上限有没有真的限住片子：没限、或上限不低于片源，都等于「自动」——
 * 记忆里不存它，开播提示里也不提它。片源高度未知时按限住了算。
 */
export function qualityLimits(maxHeight: number | null, sourceHeightPx: number | null): boolean {
  if (maxHeight === null) return false;
  return sourceHeightPx === null || maxHeight < sourceHeightPx;
}

/**
 * 换画质上限要不要重开会话。只有一种情况不用：视频直通、且新上限没限住片源——服务端会给出一模一样的
 * 计划，重开纯属白断一次。
 *
 * 片源高度先看会话给的规格，拿不到才看 `<video>` 的 `videoHeight`（直通档里它就是片源高度），而且只认
 * 非零值：起播还没出画时它是 0，拿 0 判会把「改用 720p」当成「片源本来就不超 720p」、不重开——
 * 慢线路起播时弹出的换画质卡点了没反应（NAS 实测）。两头都拿不到按限住了算，重开。
 */
export function qualityChangeNeedsRestart(input: {
  copying: boolean;
  maxHeight: number | null;
  sourceResolution: string | null | undefined;
  videoHeight: number;
}): boolean {
  if (!input.copying) return true;
  const sourceHeightPx = sourceHeight(input.sourceResolution) ?? (input.videoHeight > 0 ? input.videoHeight : null);
  return qualityLimits(input.maxHeight, sourceHeightPx);
}

export function qualityLabel(maxHeight: number | null): string {
  return QUALITY_OPTIONS.find((o) => o.maxHeight === maxHeight)?.label ?? "自动";
}
