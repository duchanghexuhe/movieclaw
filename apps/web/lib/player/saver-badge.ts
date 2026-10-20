/**
 * 顶栏「放的不是原画」标识：外网省流一律自动生效、做了就标出来，原画放得了的给一键切回
 * （AGENTS.md §5：默认值可以替用户选，但不能替用户瞒着）。三种来路：
 *
 * - **便携版**（预转码的省流副本，服务端外网择优选中）：「便携版 · 720p」，切回 = 带原片文件 id 重开；
 * - **上行省流**（原片能直连，按家宽上行 / 外网上限转成低档，`uplink_step_down`）：「省流 · 1080p」，
 *   切回 = 带画质上限重开（服务端据此跳过上行判定）；
 * - **外网线路压低了转码**（原片本来就放不了，如浏览器不支持的 HEVC；转码被上行或实测带宽收紧，
 *   `bitrate_cap_bps` 非空）：「省流 · 720p」，原画放不了，不给切回。
 *
 * 内网的转码（编码不支持、HDR）不标：那不是省流，是这台设备放不了原画。
 */

import type { PlaybackSession } from "@/lib/api/playback";

export interface SaverBadge {
  label: "便携版" | "省流";
  /** 画质说明（如 720p）；取不到为 null */
  detail: string | null;
  /** 一键切回原画的做法；null = 原画在这台设备上放不了 */
  toOriginal: { kind: "portable"; fileId: number } | { kind: "uplink" } | null;
}

export function saverBadge(
  session: Pick<PlaybackSession, "decision" | "external"> | null,
): SaverBadge | null {
  if (!session) return null;
  const { decision } = session;
  if (decision.portable_source_file_id !== null && decision.portable_source_file_id !== undefined) {
    return {
      label: "便携版",
      detail: decision.portable_resolution ?? null,
      toOriginal: { kind: "portable", fileId: decision.portable_source_file_id },
    };
  }
  const video = decision.video;
  const detail = video?.height ? `${video.height}p` : null;
  if (decision.uplink_step_down) return { label: "省流", detail, toOriginal: { kind: "uplink" } };
  if (session.external && video?.action === "transcode" && video.bitrate_cap_bps) {
    return { label: "省流", detail, toOriginal: null };
  }
  return null;
}
