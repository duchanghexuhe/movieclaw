/**
 * 网速跟不上时的「换低画质」提示（只提示、从不自动切；与 iOS App 的 `QualitySuggestion` 同一套规则与
 * 常量，apps/apple/MovieClaw/Features/Player/PlaybackRouting.swift，设计见 docs/design/player-engine.md §3.3）。
 *
 * 为什么要它：网页原来在缓冲耗尽且实测带宽低于码率时**自动**把直通档改成转码（画质直接被降），
 * 直出档还会弹一句「线路速度低于片源码率」的小字。iOS 拍板换不换码率由用户决定之后，两端对齐成
 * 同一张卡：只在真有必要时打扰，条件全部满足才给，每个播放单元最多一次——
 *
 * 1. 只算用户想看的时候：暂停期间不计；
 * 2. 触发（满足其一）：
 *    - **一次等太久**：等首帧、等跳转落点、播放中卡住，连续等满 8 秒。外网放高码率原片时用户常常
 *      一上来就等十几秒，人早退出了，只数「开播后卡了几次」永远等不到提示；
 *    - **反复卡**：开播后最近 5 分钟里卡了 2 次（起播、跳转、从暂停恢复后 10 秒内的缓冲不计，
 *      跳转的等待也不计）；
 * 3. 等待期间实测加载速度低于这条流码率的 90%（一次长等看这段里最快的一秒，反复卡看中位数）——
 *    速度够还卡，不是线路问题，提示了也没用。
 *
 * 缓冲攒满后播放器会暂停下载，平时的速度读数不可信；等待时缓冲是空的、播放器在全力下载，这时的
 * 读数才代表线路。
 */

export const GRACE_SECONDS = 10;
export const WINDOW_SECONDS = 300;
export const MIN_STALLS = 2;
export const LONG_WAIT_SECONDS = 8;
export const LINK_MARGIN = 0.9;

/** 给用户的提议：实测速度、这条流要的码率、推荐的画质上限 */
export interface QualityOffer {
  measuredBps: number;
  requiredBps: number;
  maxHeight: number;
}

interface Stall {
  start: number;
  seconds: number;
  speeds: number[];
}

/** 画质阶梯（与服务端转码目标、画质菜单的说明一致）：1080p 约 6、720p 约 3、480p 约 1.5 Mbit/s */
const LADDER: readonly { height: number; bps: number }[] = [
  { height: 1080, bps: 6_000_000 },
  { height: 720, bps: 3_000_000 },
  { height: 480, bps: 1_500_000 },
];

/**
 * 推荐档位：比当前低、码率留两成余量装得下实测速度的最高一档；都装不下就给最低档；
 * 已经在最低档（没有更低的可换）返回 null。
 */
export function recommendedHeight(bps: number, currentHeight: number | null): number | null {
  const lower = LADDER.filter((rung) => currentHeight === null || rung.height < currentHeight);
  if (lower.length === 0) return null;
  return (lower.find((rung) => rung.bps <= bps * 0.8) ?? lower[lower.length - 1]).height;
}

/** 服务端转码的码率阶梯（高度 → maxrate），与 services/playback/ffmpeg_args.py 的 BITRATE_LADDER 同一组数 */
const TRANSCODE_LADDER: readonly { height: number; bps: number }[] = [
  { height: 480, bps: 1_500_000 },
  { height: 720, bps: 3_000_000 },
  { height: 1080, bps: 6_000_000 },
  { height: 1440, bps: 10_000_000 },
  { height: 2160, bps: 16_000_000 },
];

/**
 * 转码流的码率：服务端给 ffmpeg 的 maxrate——不小于目标高度的最近一档（没有高度按 1080p），再与按线路
 * 定的上限取小（ffmpeg_args.maxrate_for_video 同一规则）。
 *
 * 判「线路跟不跟得上」要拿这条流的码率比。hls.js 量出第一个分片之前只能靠它：拿片源码率顶替会把
 * 4K HDR 转 SDR 的片子（片源 90 Mbps、转出来最多 16 Mbps）说成「这一版需要约 10.7 MB/s」（NAS 实测）。
 */
export function transcodeBitrateBps(video: { height: number | null; bitrate_cap_bps?: number | null }): number {
  const { height } = video;
  const rung = height === null ? undefined : TRANSCODE_LADDER.find((r) => height <= r.height);
  const ladder = height === null ? 6_000_000 : (rung ?? TRANSCODE_LADDER[TRANSCODE_LADDER.length - 1]).bps;
  const cap = video.bitrate_cap_bps;
  return cap !== null && cap !== undefined && cap > 0 ? Math.min(ladder, cap) : ladder;
}

/**
 * 正在转码时线路跟不上，播放器不出提示卡、直接按实测带宽同档重开（服务端顺带可能降分辨率）。
 * 自动调整也要看得见：重开后的一句提示——分辨率降了说降到几 p，只压了码率就说压了码率。
 */
export function bandwidthRestartNotice(fromHeight: number | null, toHeight: number | null): string {
  if (fromHeight !== null && toHeight !== null && toHeight < fromHeight) {
    return `线路较慢，已调到 ${toHeight}p`;
  }
  return "线路较慢，已调低码率";
}

export class QualitySuggestion {
  /** 观看秒数：只在用户想看（没暂停）的时候走 */
  private clock = 0;
  private graceUntil = GRACE_SECONDS;
  private stalls: Stall[] = [];
  private stalling = false;
  /** 当前这一段连续等待（不管宽限、不管是不是跳转）的秒数与期间的速度读数 */
  private wait = 0;
  private waitSpeeds: number[] = [];
  private given = false;

  get waitSeconds(): number {
    return this.wait;
  }

  get offered(): boolean {
    return this.given;
  }

  /** 起播、跳转、从暂停恢复：接下来 10 秒的缓冲不算「卡」；新的一段等待从这一刻算起 */
  restartGrace(): void {
    this.graceUntil = this.clock + GRACE_SECONDS;
    this.stalling = false;
    this.wait = 0;
    this.waitSpeeds = [];
  }

  /** 每秒一次，只在用户想看时调用。stalled：正在等（缓冲中，含起播）；seeking：这段等待是跳转造成的 */
  tick(input: { stalled: boolean; seeking?: boolean; loadingBps: number | null }): void {
    this.clock += 1;
    this.stalls = this.stalls.filter((s) => s.start + s.seconds >= this.clock - WINDOW_SECONDS);
    const speed = input.loadingBps !== null && input.loadingBps > 0 ? input.loadingBps : null;
    if (input.stalled) {
      this.wait += 1;
      if (speed !== null) this.waitSpeeds.push(speed);
    } else {
      this.wait = 0;
      this.waitSpeeds = [];
    }
    if (!input.stalled || input.seeking || this.clock <= this.graceUntil) {
      this.stalling = false;
      return;
    }
    if (!this.stalling) {
      this.stalls.push({ start: this.clock, seconds: 0, speeds: [] });
      this.stalling = true;
    }
    const current = this.stalls[this.stalls.length - 1];
    current.seconds += 1;
    if (speed !== null) current.speeds.push(speed);
  }

  /** 现在该不该提议；给出一次后本单元不再给 */
  offer(input: { streamBitrateBps: number | null; currentHeight: number | null }): QualityOffer | null {
    const bitrate = input.streamBitrateBps;
    if (this.given || bitrate === null || !(bitrate > 0)) return null;
    let measured: number;
    if (this.wait >= LONG_WAIT_SECONDS) {
      // 一次长等取这段里最快的一秒：冷起播时播放器一段一段地取（索引、文件头、首个分片分头取），
      // 逐秒读数时有时无；最快那秒最接近线路能力，它都跟不上码率才算线路问题
      if (this.waitSpeeds.length === 0) return null;
      measured = Math.max(...this.waitSpeeds);
    } else if (this.stalls.length >= MIN_STALLS) {
      const speeds = this.stalls.flatMap((s) => s.speeds).sort((a, b) => a - b);
      if (speeds.length === 0) return null;
      measured = speeds[Math.floor(speeds.length / 2)];
    } else {
      return null;
    }
    if (!(measured < bitrate * LINK_MARGIN)) return null;
    const height = recommendedHeight(measured, input.currentHeight);
    if (height === null) return null;
    this.given = true;
    return { measuredBps: measured, requiredBps: bitrate, maxHeight: height };
  }
}
