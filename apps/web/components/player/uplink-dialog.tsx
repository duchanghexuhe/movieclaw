"use client";

/**
 * 外网画质确认弹窗（起播前）。
 *
 * 服务端判定「源片码率超出家宽上行/外网上限」后**不静默降档**：装得下的低档
 * 计划照常返回，并带 ``uplink_step_down`` 标记。网页端拿这个标记拦下起播、
 * 在这里问一次——推荐档会话已就绪，点「就这样播」零等待；要原画质则带画质
 * 上限重开（服务端会停掉这路转码）。App 不认这个标记，拿到计划直接播，等于
 * 维持自动降档（移动场景下也是合理默认）。
 *
 * 外观与交互照抄 ConsentDialog（components/player/consent-dialog.tsx）：同一
 * 套 role=dialog / pointer-events-auto 的 media-chrome 豁免与 Modal 面板样式，
 * 不另起一套系统。
 */
export function UplinkDialog({
  recommendedHeight,
  reason,
  onUseRecommended,
  onUseOriginal,
}: {
  /** 服务端挑好的档（计划的目标高度）；取不到时按钮退化为「用推荐画质」 */
  recommendedHeight: number | null;
  reason: string;
  onUseRecommended: () => void;
  onUseOriginal: () => void;
}) {
  // 与服务端转码阶梯同一组数（quality-suggestion.ts 的 TRANSCODE_LADDER 前三档），
  // Mbps → GB/小时 = ×3600/8000：480p≈0.7、720p≈1.4、1080p≈2.7
  const RUNG_MBPS: Record<number, number> = { 480: 1.5, 720: 3, 1080: 6 };
  const gbPerHour =
    recommendedHeight !== null && RUNG_MBPS[recommendedHeight] !== undefined
      ? (RUNG_MBPS[recommendedHeight] * 3600) / 8000
      : null;
  return (
    // role=dialog 兼具语义与 media-controller 的自动淡出豁免；pointer-events-auto
    // 必须显式写——media-chrome 浮层容器是 pointer-events:none，豁免规则跳过
    // [role=dialog]，不自己声明的话看得见点不中（详见 consent-dialog 同一注释）。
    <div
      role="dialog"
      aria-modal="true"
      className="pointer-events-auto absolute inset-0 z-30 flex items-center justify-center bg-black/75 px-6"
    >
      <div className="solid-popover w-full max-w-[460px] rounded-2xl border border-white/10 bg-[rgba(16,18,26,0.92)] p-7 text-white shadow-[0_32px_90px_rgba(0,0,0,0.7)] backdrop-blur-2xl">
        <h2 className="text-[19px] font-semibold leading-snug">外网播放，建议降低画质</h2>

        <dl className="mt-4 space-y-3 text-[13px] leading-relaxed text-white/70">
          <div>
            <dt className="text-white/45">原因</dt>
            <dd className="mt-0.5 text-white/85">{reason}</dd>
          </div>
          <div>
            <dt className="text-white/45">流量</dt>
            <dd className="mt-0.5 text-white/85">
              推荐档约 {gbPerHour !== null ? `${gbPerHour.toFixed(1)} GB` : "几十 GB 以下"}/小时；
              原画质按片源码率算，高码率原盘可达数十 GB/小时
            </dd>
          </div>
        </dl>

        <div className="mt-5 flex justify-end gap-2">
          <button
            type="button"
            onClick={onUseOriginal}
            className="rounded-full px-5 py-2.5 text-[14px] text-white/70 transition-colors hover:bg-white/10"
          >
            仍用原画质
          </button>
          <button
            type="button"
            onClick={onUseRecommended}
            className="rounded-full bg-[var(--player-accent)] px-5 py-2.5 text-[14px] font-semibold text-black transition-colors hover:bg-[var(--player-accent-hover)]"
          >
            {recommendedHeight !== null ? `用 ${recommendedHeight}p 播放` : "用推荐画质播放"}
          </button>
        </div>
      </div>
    </div>
  );
}
