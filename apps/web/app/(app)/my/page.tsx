import type { Metadata } from "next";

import { NetflixMyPage } from "@/components/netflix/my-page";

/**
 * 「我的」页（Netflix 主题移动端底栏的第四个页签）：用户信息、快捷入口、
 * AI 会话与账号操作；设置页是它的二级页面。页面内容按主题客户端分支
 * （见组件说明），这里只做路由与元信息。
 */
export const metadata: Metadata = { title: "我的" };

export default function MyPage() {
  return <NetflixMyPage />;
}
