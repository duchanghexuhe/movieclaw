package io.movieclaw.android.core.designsystem

import androidx.compose.runtime.Composable
import androidx.compose.runtime.staticCompositionLocalOf
import androidx.compose.ui.Modifier
import dev.chrisbanes.haze.HazeState
import dev.chrisbanes.haze.hazeSource

/**
 * 液态底栏的模糊源（glass-kit 同款结构）：MainTabScreen 在**开关打开时**提供，
 * 各页签根页的内容层用 [tabGlassSource] 标记。开关关闭时 Local 为 null，标记原样返回，
 * 不付任何图层捕获的代价。
 */
val LocalTabHazeState = staticCompositionLocalOf<HazeState?> { null }

/**
 * 本页签此刻是否在前台。主页签切换时**不再拆掉重建**页面（每次切都从头组装列表、图片、轮播，
 * 那一帧很重，底栏胶囊会顿一下——实机反馈「切 tab 不跟手」），而是把去过的页签留在组合里、
 * 只是不摆放；后台页签据此停掉轮播计时、滚动收起追踪这类只该在前台跑的东西。
 * 不在主页签里的页面（二级页）恒为 true。
 */
val LocalTabActive = androidx.compose.runtime.compositionLocalOf { true }

/** 内容层标记：把本节点的绘制喂给玻璃件的背景模糊（`hazeSource`） */
@Composable
fun Modifier.tabGlassSource(): Modifier {
    val state = LocalTabHazeState.current ?: return this
    return this.hazeSource(state)
}
