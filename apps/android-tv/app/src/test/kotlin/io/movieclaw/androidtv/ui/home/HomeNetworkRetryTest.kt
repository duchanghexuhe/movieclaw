package io.movieclaw.androidtv.ui.home

import android.app.Application
import android.net.ConnectivityManager
import androidx.activity.ComponentActivity
import androidx.compose.ui.test.junit4.v2.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import io.movieclaw.androidtv.ui.LaunchArgs
import io.movieclaw.androidtv.ui.TV_QUALIFIERS
import io.movieclaw.androidtv.ui.focusedText
import io.movieclaw.androidtv.ui.setTvContent
import io.movieclaw.androidtv.ui.shell.MainShell
import io.movieclaw.core.model.generated.LibraryView
import io.movieclaw.core.model.generated.UpNextItemView
import io.movieclaw.core.model.generated.UpNextView
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowNetwork

/**
 * 开机直达 MovieClaw 时 Wi-Fi 比 App 晚连上（androidtv-home.md §7；用户真机：重启电视后停在「连不上服务器」，要手动点重试）：
 * 首页第一次加载失败后，网络一连上就自己重试，不等每分钟的定时刷新。
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class, sdk = [34], qualifiers = TV_QUALIFIERS)
class HomeNetworkRetryTest {
    @get:Rule val rule = createAndroidComposeRule<ComponentActivity>()

    private fun errorCount() = rule.onAllNodesWithText("连不上服务器").fetchSemanticsNodes().size

    @Test
    fun firstLoadRetriesWhenNetworkComesUp() {
        // 刚开机还没网：假服务端什么都不答
        val routes = mutableMapOf<String, Any>()
        rule.setTvContent(routes) { MainShell(LaunchArgs()) }
        rule.mainClock.advanceTimeBy(2_000)
        rule.waitForIdle()
        assertEquals("没网时首页报错", 1, errorCount())

        // 服务端已经能答，但系统还没报网络连上：不自己轮询
        routes["/libraries"] = listOf(LibraryView(id = 1, name = "剧集", kind = "tv", viewerAccess = true))
        routes["/playback/up-next"] = UpNextView(
            listOf(
                UpNextItemView(
                    mediaItemId = 11, libraryId = 1, kind = "tv", title = "甲剧", seasonNumber = 1, episodeNumber = 2,
                    positionMs = 60_000, durationMs = 1_800_000,
                ),
            ),
        )
        rule.mainClock.advanceTimeBy(5_000)
        rule.waitForIdle()
        assertEquals(1, errorCount())

        // Wi-Fi 连上：系统发来网络回调，首页立即重试（远不到 60 秒的定时刷新）
        val connectivity = rule.activity.application.getSystemService(ConnectivityManager::class.java)
        shadowOf(connectivity).networkCallbacks.forEach { it.onAvailable(ShadowNetwork.newInstance(100)) }
        rule.mainClock.advanceTimeBy(2_000)
        rule.waitForIdle()
        assertEquals("网络一来首页就加载出来", 0, errorCount())
        assertEquals("继续播放", rule.focusedText())
        // 加载成功后退订网络回调
        assertEquals(0, shadowOf(connectivity).networkCallbacks.size)
    }
}
