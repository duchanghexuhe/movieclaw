/**
 * 思维链强度控件的纯逻辑（无 React、无 API 依赖，供 node --test 直接导入）。
 *
 * 服务端只下发一个字符串菜单（thinking_levels），前端不理解方言，但要按
 * 菜单形状决定控件长什么样：
 *   - 空菜单            → 隐藏（模型强度不可控）
 *   - 只有一档          → 列表（默认 / 该档）。toggle 方言（kimi-k2.6、glm-5.x）
 *                         的菜单就是单项「关」，画成一根只有一个刻度的滑杆既
 *                         没有可拖的距离，点了刻度也再点不回默认
 *   - 两档及以上        → 横向离散滑杆，刻度按统一词汇表排序
 *
 * 「默认」= 不发任何参数、用模型自身行为，不是强度轴上的一点，所以滑杆上
 * 不占刻度（默认态无滑块），靠「恢复默认」回去；列表里则是普通一项。
 */

/** 统一词汇表的强度顺序（越靠后越深）：滑杆按它排刻度，服务端下发的菜单也按它归一。 */
export const THINKING_LEVEL_ORDER = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];

/** 档位文案（对齐 maka 的短单词标签）；「默认」由选择器的空值表达。 */
export const THINKING_LEVEL_LABELS: Record<string, string> = {
  off: "关",
  minimal: "最少",
  low: "低",
  medium: "中",
  high: "高",
  xhigh: "超高",
  max: "最高",
};

export type ThinkingControlShape = "hidden" | "list" | "slider";

/** 服务端菜单 → 按词汇表排序的刻度；词汇表外的值丢弃（声明不是有序集合）。 */
export function thinkingStops(levels: readonly string[]): string[] {
  return THINKING_LEVEL_ORDER.filter((level) => levels.includes(level));
}

/** 按刻度数决定控件形态（见文件头注释）。 */
export function thinkingControlShape(stops: readonly string[]): ThinkingControlShape {
  if (stops.length === 0) return "hidden";
  return stops.length === 1 ? "list" : "slider";
}

/** 刻度圆心在轨道可用长度上的位置（0..100）：刻度均布，只有一个时居中。 */
export function stopPercent(index: number, count: number): number {
  return count > 1 ? (index / (count - 1)) * 100 : 50;
}

/** 轨道可用长度上 0..1 的位置 → 最近的刻度下标；越界钳到两端。 */
export function nearestStopIndex(ratio: number, count: number): number {
  if (count <= 1) return 0;
  const clamped = Math.min(1, Math.max(0, ratio));
  return Math.round(clamped * (count - 1));
}

/**
 * 指针横坐标 → 刻度下标。轨道两端各留 inset 给刻度圆心（圆心不能贴边，
 * 否则滑块会溢出轨道），可用长度 = 轨道宽 − 2·inset；点在留白上按端点算。
 */
export function stopIndexAtPointer(
  clientX: number,
  trackLeft: number,
  trackWidth: number,
  count: number,
  inset: number,
): number {
  const usable = trackWidth - inset * 2;
  if (usable <= 0) return 0;
  return nearestStopIndex((clientX - trackLeft - inset) / usable, count);
}

/**
 * 键盘步进：←/→（↓/↑）相邻刻度，Home/End 两端；默认态（index=-1）按任一
 * 方向都先落到最浅一档。不是导航键返回 null（调用方不拦截该按键）。
 */
export function steppedStopIndex(index: number, count: number, key: string): number | null {
  if (count === 0) return null;
  const last = count - 1;
  switch (key) {
    case "ArrowRight":
    case "ArrowUp":
      return Math.min(index + 1, last);
    case "ArrowLeft":
    case "ArrowDown":
      return Math.max(index - 1, 0);
    case "Home":
      return 0;
    case "End":
      return last;
    default:
      return null;
  }
}
