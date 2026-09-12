# 多主题 Web UI 框架与「仿 Netflix」主题设计

> 状态：v2 —— 三轮子代理调研（桌面 Web / 移动端 / 工程技法）已完成并仲裁并入本文
> （仲裁记录见 §11）；关键取舍已与需求方确认（§0），待评审后实施。
>
> 范围：主题框架（注册 / 切换 / 存储 / 防闪烁）+ Netflix 主题的全量视觉与结构规格，
> 覆盖 PC 与移动端。Netflix 主题是本框架的第一个消费者，框架设计以「后续还有第三、
> 第四个主题」为前提，但不预做任何用不上的抽象。
>
> **置信度标注约定**：【官方】= Netflix 自有资产（官网 CSS 抓取、技术博客、帮助中心、
> 品牌站）；【媒体】= 专业媒体评测/实测；【社区】= 克隆项目、聚合站、抓取转述。
> 凡标【社区】的数值只作实现基准，P6 验收以与 Netflix 实机并排比对为准。

## 0. 已确认的四个关键决策

需求方在方案前拍板，本文全部展开以此为准：

| # | 问题 | 决策 |
|---|------|------|
| 1 | 还原深度 | **结构级还原**：顶栏、billboard 首屏、横向卡片行、hover 展开预览卡、移动端底部标签栏，全做 |
| 2 | 控制台页（设置 / AI 会话 / 任务 / 订阅管理） | **全站跟随 Netflix 语言**：保留信息架构，只换视觉皮肤 |
| 3 | 首页（/） | **变成 Netflix 式内容首页**（billboard + 行），AI 新任务收进顶栏与 /new |
| 4 | 主题存储 | **跟随账号**，存 `ui.preferences.theme`，复用现有偏好通道，全设备同步 |

## 1. 目标与非目标

**目标**

1. 主题框架：主题注册制、账号级存储、设置→外观一键切换、实时预览、刷新无闪（FOUC）。
2. Netflix 主题 1:1：浏览主路径（首页 / 发现 / 媒体库 / 详情 / 播放）在 PC 与移动端对
   Netflix 官方规格逐项对齐。**参照基准已锁定**：PC = 桌面 Web 端；移动端 = Netflix
   **App**（官方不支持手机浏览器播放，不存在「Netflix 手机 Web 版」可供参照，
   见 §2.6），我们的移动 Web / PWA 以 App 形态为目标自实现。
3. 播放器只换强调色：现有网页播放器（见 docs/design/web-player.md）的控制语言本就
   对齐 YouTube / Netflix（三段半透白进度条、黑底、图标按钮），Netflix 主题下仅把
   强调色换成品牌红。

**非目标（明确不做，避免范围膨胀）**

- 不做用户自定义配色 / 任意主题混搭——只有「主题级」的整档切换。
- 不复制需要内容运营或 App 平台支撑的元素：hover 卡片的**预览视频自动播放**（官方
  功能，但本站无预览片源，用剧照 + 信息层替代）、Top 10 的每日排名语义（本站榜单
  行已有自己的语义）、离线下载 / Clips 竖屏流 / Moments（App 独占概念）。
- 不重做播放器交互，不动 AI 会话的对话交互，控制台页不重排信息架构。

## 2. Netflix 设计语言调研（v2 仲裁后）

### 2.1 色板

| 角色 | 值 | 置信度 | 说明 |
|---|---|---|---|
| 品牌红 | `#E50914` | 【官方】brand.netflix.com | 字标 / 进度条 / 高亮，**不是**主按钮色 |
| 红 hover（网页按钮） | `#C11119` | 【媒体】生产 CSS 抓取 | 营销页/按钮 hover 红；Netflix 的红是 hover **变暗**不是变亮 |
| 红 pressed / 品牌暗红 | `#B20710` | 【官方】品牌站 Symbol Dark Red | 「按下」语义为社区引申 |
| 画布 | `#141414` | 【社区】强共识 | 全站底色；播放器与最深沉浸层用纯黑 `#000` |
| 卡片面 | `#181818`（Grey 850） | 【社区】强共识 | 卡片、弹层、信息面板 |
| 悬停行 | `#2F2F2F` | 【社区】存疑 | 聚合站值；另一说 hover 行 `#333333`，P6 对照定稿 |
| 描边 | `#404040`（Grey 700） | 【社区】存疑 | **实线**边框；DESIGN.md 一说 `#333333`，P6 定稿 |
| 次要文字 | `#B3B3B3` | 【社区】强共识 | 正文、元数据 |
| 弱文字 | `#808080` | 【社区】存疑 | 说明、占位 |
| 强调文字 | `#E5E5E5` | 【社区】 | 导航链接未激活态 |
| 主文字 | `#FFFFFF` | 【社区】强共识 | 标题、激活导航、按钮文字 |
| 匹配绿 | `#46D369` | 【社区】聚合站 | 匹配度绿；P6 对照定稿 |
| 警示橙 | `#E87C03` | 【社区】存疑 | 仅见第三方扩展色板，P6 定稿 |
| 成功绿 | `#2A9D3C` | 【社区】 | 实底成功态 |

> 生产 CSS 已证实的关键值（直接抓取 netflix.com / 播放器样式）：次级按钮
> `rgba(109,109,110,0.7)`（hover 0.4）；主按钮白底，三态透明度 1 / hover 0.75 /
> 按下 0.5；按钮圆角 4px；焦点环 = 2px 白描边、8px 圆角、-4px 偏移。

### 2.2 字体

Netflix Sans 为专有字体（Dalton Maag 2018），**不可自托管**。netflix.com 生产 CSS
的字体栈为 `Netflix Sans, Helvetica Neue, Segoe UI, Roboto, Ubuntu, sans-serif`
（Arial 只出现在播放器栈；官方 @font-face 覆盖 100/300/400/500/700/900 六档字重）
【官方】。

替代方案（仲裁结论）：正文与 UI 继续用项目已加载的 **Inter**——同为「为屏幕设计的
grotesque + humanist」，是社区公认 Netflix Sans 最接近的开源替代，且已在构建产物里、
零增量。字重对位 400/500/700；标题收紧字距（-0.01 ~ -0.025em）、行高约 1.1，
正文行高 1.5。中文由系统黑体补位（Netflix 中文界面同样如此），中文界面不引入
Noto Sans SC 新字体文件（保持现有 PingFang/系统栈）。

字标（logo wordmark）：如需更贴近 Netflix 字标气质，可选 **Bebas Neue**（Netflix
早期字标的定制血缘、克隆项目通行做法），仅用于字标展示、绝不做正文。是否引入属
可选优化（新增一个静态字体文件，不动运行时依赖），列为开放问题（§9）。

字阶（web 端关键档）：billboard 标题 56px/700【社区】、行标题 20px/700【社区】、
正文 14px/400【社区·DESIGN.md】。中文没有 700 的 Inter，由系统黑体补位。

### 2.3 形状、间距与海拔

- **圆角极方**：4px（按钮、输入框、卡片，**生产 CSS 证实**）/ 6px（弹窗、展开卡，
  【社区】）/ 2px（徽章，【社区】存疑）；圆形按钮与开关除外（40px 圆钮 = 0.8rem
  内边距 + 24px 图标，生产 CSS 证实）。
  注意：**2026-06 网页端改版后磁贴趋圆**（【媒体】What's on Netflix 实测），本主题
  以经典 web 端 4px 为基准，改版趋势记录在案、P6 验收时再定是否跟进。
- 间距基尺 4px；页面左右内边距约 4vw；行内卡片间距 4~8px。
- **海拔靠缩放与层级表达，不靠阴影**：静息卡片无阴影；hover 卡
  `0 12px 24px rgba(0,0,0,0.8)`【社区】；弹窗 `0 8px 32px rgba(0,0,0,0.9)`【社区】。
  纯黑底上阴影几乎不可见，Netflix 的「浮起」主要靠 scale 与 z-index。

### 2.4 动效

| 档位 | 时长 | 置信度 | 用途 |
|---|---|---|---|
| hover | 150ms | 【社区】 | 颜色、透明度变化 |
| 卡片展开 | **400ms** | 【官方】技术博客《Delivering Meaning with Previews on Web》 | hover 展开画布动画 |
| 标准 | 400ms | 【社区】 | 弹窗、导航渐变 |
| billboard 信息渐入 | **600ms**（delay 200ms） | 【官方】生产 CSS `.info-wrapper-fade` | 首屏文案渐入 |

缓动 ease-out `cubic-bezier(0, 0, 0.2, 1)`【社区·通用值】。悬停到展开的触发延迟
官方未公布，社区实现普遍 ~500ms（本方案取 400ms，见 §5.4）。

### 2.5 关键交互模式（PC 桌面 Web）

- **顶栏**：高约 68px【社区共识，无官方数值】，页面顶端**透明**，滚动后渐变为
  `#141414` 实底；左侧字标 + 导航链接（激活 = 白 700，未激活 = `#E5E5E5`）；
  右侧搜索（放大镜展开输入框）、通知铃、方形圆角头像下拉。窄屏收敛为
  「浏览 ▾」下拉（断点 900–1100px，【社区】）。
- **Billboard**：全出血 hero，高度按 16:9 比例推导（≈56.25vw）并以视口约束收口
  ——「56vh」这一说法无出处（v2 仲裁修正）；底部渐隐入 `#141414`、左侧
  `rgba(0,0,0,0.6)→透明` 的可读性渐变【社区共识】；左下文案块（标题、匹配度绿字、
  元数据行、两行简介）+「▶ 播放」（白底黑字）与「ⓘ 更多信息」
  （`rgba(109,109,110,0.7)`，**生产 CSS 证实**）。官方 web 端有「自动播放预览」
  开关（billboard 静音播预告）【官方】——本站不复制（非目标）。
- **横向卡片行**：行标题在行上方、加粗；桌面每行 6 张、翻页**按整页翻（6 张/页）**
  且无限循环【媒体·Kevin Lobine 实测】；其余断点张数为克隆共识自定值。卡片
  16:9【证实】。hover：约 500ms 延迟后展开画布，400ms 动画放大（约 150%）并在
  下方展开信息面板（40px 圆形操作钮、匹配度、元数据、类型标签），展开方向随卡片
  在行中的位置翻转；预览视频起播后 10 秒信息淡出、交互即恢复【官方·技术博客】
  ——视频部分本站不做，信息面板保留。
- **Top 10 行**：描边大数字 + 2:3 竖版海报，数字被卡片圆角与 overflow 裁切
  （社区逆向证实构图）；本站仅在有真实榜单语义时选择性使用。
- **详情**：PC 是弹层（~850px 宽、`#181818`、6px 圆角、70% 黑遮罩，均为【社区】
  共识值），上半剧照 + 白播放钮，下半季下拉 + 横滚集卡 + 演职员 + More Like This。
- **进度条**：细红条【证实】；「3px 高、`#404040` 轨道」为社区值（克隆 3–6px 不一），
  按 3px 实现、P6 定稿。
- **头像下拉**（官方帮助中心证实）：档案列表 / 管理档案 / 账号 / 帮助中心 / 退出
  ——**没有「设置」项**。本站需在头像菜单放「设置」入口，这是有意的偏离（功能
  需要，非疏漏）。

### 2.6 移动端差异（参照基准 = Netflix App）

**基准事实（v2 仲裁确立）**：Netflix 官方支持列表只有电脑、iPad（iPadOS 14+ Safari）、
Meta Quest、Chromebook——**手机浏览器不在其中**，netflix.com 在手机上无专用 UI
（顶部布局 + 引导下载 App）。因此本主题移动端没有「Netflix 手机 Web 版」可抄，
1:1 的对象是 **App**，落地载体是我们的移动 Web / PWA。

- **导航**：底部标签栏。App 形态存在版本分叉：2026-04-30 大改版（四 tab
  Home / Clips / Search / My Netflix，New & Hot 降为首页顶部快捷入口，2026-08-25
  全球推送）之前是稳定多年的**三 tab：Home / New & Hot / My Netflix**。本主题
  **参照 2026-04 前的三 tab 形态**（素材多、认知度高、无 Clips 这类本站做不了的
  概念），底栏项按本站功能映射为 4 个（§5.2）；栏高 / 图标尺寸等数值无官方出处，
  按 iOS 惯例（49pt 栏高、44pt 目标、24pt 图标）取值，标注为**自家设计决策**。
- **内容行**：常规行用 2:3 竖版海报【证实·截图】；**「继续观看」行在移动端同样是
  16:9 横版卡 + 进度条**（v2 修正：不是竖版）【证实·截图】。
- **无 hover**：卡片点击直达详情**整页**（App 全屏详情；Web 端也是 `/title/<id>`
  整页跳转）【证实】。
- **详情页**：白色大播放钮、My List、季/集选择、下载钮（App）【官方】；全出血
  hero 与胶囊按钮构图属视觉观察，无官方规格。
- **播放器**：完整内容必须横屏观看（竖屏仅 Clips feed，改版后亦然）【媒体】；
  Screen Lock 锁屏防误触（App 独占）【官方】；Skip Intro 存在【官方】。
- **首页行序**：App 的行顺序是个性化动态的，本方案的固定行序是合理简化，不宣称
  「Netflix 官方顺序」。
- 移动端字号：无官方规格；沿用本站 HIG 换档机制（§7），不署名 Netflix。

## 3. 主题框架设计

### 3.1 数据模型

`ui.preferences` 新增顶层字段（前端 `lib/api/ui.ts` 的 `UiPreferences` 与后端
`settings.schemas` 同步对齐）：

```ts
export interface UiPreferences {
  /** 主题 id，取值来自 lib/themes.ts 的注册表；未知值兜底为 silver */
  theme: string;
  sidebar: SidebarUiPrefs;   // 不变
  scrim: ScrimUiPrefs;       // 不变
  nav: NavUiPrefs;           // 不变
}
```

- 默认值 `silver`（银玻璃，即现有主题）。`normalizeUiPreferences` 补齐缺项——老后端
  不认识该字段时返回缺省，前端自动兜底，**天然向前兼容**。
- 存储通道完全复用：该配置域本来就按主体分流（超管写全局配置域、成员写自己的
  `member.ui_prefs`），主题随之按账号隔离；localStorage 首帧缓存
  （`lib/ui-prefs-cache.ts`）同步带上 theme 字段。
- **不涉及数据库迁移**（JSON 配置域加字段），**不动 pyproject 依赖**，不触发
  runtime-version bump；后端改动仅 settings schema 一个字段。

### 3.2 主题注册表

新增 `apps/web/lib/themes.ts`：主题是这个功能里唯一会「全局生效」的东西，注册表
保持无组件依赖的纯数据，便于单测与后续扩展。

```ts
export interface ThemeMeta {
  id: string;            // "silver" | "netflix"
  label: string;         // 设置页展示名：「银玻璃」「Netflix」
  description: string;   // 一句话描述
  /** 外观选择卡的预览缩略：两个主色即可拼出观感 */
  preview: { bg: string; accent: string };
  /** 结构级差异标记：netflix 主题换外壳（顶栏/底栏/内容首页） */
  structural: boolean;
}
export const THEMES: ThemeMeta[] = [ /* silver, netflix */ ];
```

### 3.3 应用机制：`data-theme` 属性 + 三层覆盖

主题生效分三层，由浅入深，**Netflix 主题三层全用**；后续纯换肤主题只用到第一层。

1. **token 层（CSS 变量）**：`globals.css` 里所有语义变量（`--bg` / `--surface-*` /
   `--text-*` / `--accent*` / 状态色）在 `html[data-theme="netflix"]` 作用域下整组
   覆盖。全站组件消费的是变量，皮肤自动跟随。`html[data-theme]`（0,1,1）位于
   无 layer 的普通规则，稳定胜过 Tailwind v4 `@layer theme` 里的 `:root` 声明。
2. **Tailwind v4 主题变量层（圆角的全局换档）**——**v2 仲裁：机制已核验【可行】**
   （官方文档 + 源码证实）：v4 的 `rounded-*` 工具类编译产物是
   `border-radius: var(--radius-lg)` 这类变量引用，且 `@theme` 变量以真实 CSS 变量
   输出到 `:root`、运行时可用 CSS 覆盖。因此在根节点覆盖 `--radius-*` 即可全站
   换圆角档，无需重编译。v4 默认值表（源码 theme.css）：`xs` 2px / `sm` 4px /
   `md` 6px / `lg` 8px / `xl` 12px / `2xl` 16px / `3xl` 24px / `4xl` 32px。
   Netflix 主题的覆盖映射（把全站压到 Netflix 的 2/4/6 档）：

   ```css
   html[data-theme="netflix"] {
     --radius-xs: 2px;   /* 徽章 */
     --radius-sm: 4px;   /* 按钮、输入框、卡片、磁贴（生产 CSS 证实 4px） */
     --radius-md: 4px;
     --radius-lg: 4px;
     --radius-xl: 6px;   /* 弹窗、展开卡 */
     --radius-2xl: 6px;
     --radius-3xl: 8px;  /* 个别大面留一档 */
   }
   ```

   已核验的四个边界（照办即可避开）：
   - `rounded-full`（= `calc(infinity * 1px)`）与 `rounded-none` 是**静态值不走变量**
     ——不影响本方案（Netflix 的圆形按钮本来就要保留圆）；
   - 任意值 `rounded-[10px]` 不走 `--radius-*`，覆盖不到——P1 审计分布，验收清单内
     组件替换为档位类或 `var(--radius-*)`，深水区接受偏差（§9）；
   - 本项目不得使用 `@theme inline`（会把变量值内联进工具类、令运行时覆盖失效），
     现有代码未使用，P1 时构建产物抽查确认一次；
   - 默认只输出**被用到**的变量，主题 CSS 若引用了未被工具类用到的档位，用
     `@theme static` 强制输出。
3. **结构层（React 条件渲染）**：`useTheme()` 从 `UiPrefsProvider` 读当前主题，
   外壳按主题分支渲染（§5）。本站 AppShell 仅在 AuthGate 确认登录后于客户端渲染
   （不参与 SSR），不存在水合不一致；`<html>` 上加 `suppressHydrationWarning`
   （next-themes 同款约定），主题状态在 React 之外以 DOM 属性承载。

### 3.4 首帧防闪烁

`app/layout.tsx` 已有同型先例（`immersive-route` 内联脚本）。新增一段：首帧绘制前读
localStorage 偏好缓存，把 `data-theme` 写上 `<html>`（与 next-themes 的标准做法
同构：内联阻塞脚本 + `documentElement.setAttribute`）。`UiPrefsProvider` 拉到服务端
值后再同步一次（含设置页实时预览：`setPreview` 草稿直接驱动 `data-theme`）。
SSR 首屏无属性 = 银玻璃默认；登录后才渲染的结构层在客户端拿 Context 值渲染，
首帧即正确主题。

### 3.5 与现有氛围体系的兼容：Netflix 主题下停用「玻璃三件套」

Netflix 主题是**纯色平铺**设计，现有的三大氛围机制整体停用（不是隐藏——是不渲染）：

| 机制 | 银玻璃 | Netflix |
|---|---|---|
| 背景大图（`body::before` + BackdropProvider） | 渲染 | **不渲染**，纯 `#141414` |
| 全站蒙版（`.page-scrim`，外观可调） | 渲染 | **不渲染**——底是纯色，蒙版无意义 |
| WebGL 液态玻璃（GlassPanel / lib/glass.ts） | 渲染 | **flat 形态**：实色 `#181818`、发丝实线描边、不启动 WebGL（顺带省 GPU 与移动端上下文限额） |

- `GlassPanel` 内部读主题：`netflix` 下不挂 canvas、按 flat 预设渲染。调用方零改动。
- 设置→外观的「背景图 / 蒙版质感 / 侧栏玻璃」三个分区在 Netflix 主题下置灰并标注
  「仅银玻璃主题生效」；对应 prefs 字段保留不丢。
- 播放器、详情页自带的 hero 剧照体系照常工作（那本来就是 Netflix 的构图方式）。

### 3.6 设置入口

设置→外观（`appearance` 分区，成员与管理员均可见）最顶部新增「主题」组：两张可选卡
（缩略预览 = 预览色块 + 主题名），点击即走现有 `setPreview` 实时预览 + 保存回执流程，
与外观分区的既有交互语言完全一致。

## 4. Netflix 主题 token 全表（`globals.css` 的 `html[data-theme="netflix"]` 覆盖组）

| 现有 token | 银玻璃值 | Netflix 值 | 备注 |
|---|---|---|---|
| `--bg` | `#0a0b10` | `#141414` | 播放器 / 沉浸页另有 `#000`，见 §5.6 |
| `--surface-main` | 玻璃 | `#141414` | 纯色 |
| `--surface-raised` | 玻璃 | `#181818` | 卡片 / 弹层 |
| `--surface-inset` | 白 5% | `#2F2F2F` | 输入框内嵌（存疑值，P6 定稿） |
| `--text` | 冷白 | `#FFFFFF` | |
| `--text-muted` | 白 62% | `#B3B3B3` | |
| `--text-faint` | 白 36% | `#808080` | |
| `--line` | 白 8% | `#404040` | Netflix 用实线；存疑值，P6 定稿 |
| `--line-strong` | 白 15% | `#808080` | |
| `--accent` | 冷银 `#cdd6e6` | `#E50914` | 品牌红：进度条 / 高亮文字 / 激活指示 |
| `--accent-strong` | 亮银 | `#C11119` | **hover 红**（网页按钮 hover 变暗变深，不是变亮） |
| `--accent-2` | 银蓝 | `#B20710` | pressed / 品牌暗红侧 |
| `--accent-soft` | 银 14% | `rgba(229,9,20,0.15)` | |
| `--accent-ring` | 冷光 | `rgba(255,255,255,0.7)` | Netflix 焦点环 = 2px 白描边、8px 圆角、-4px 偏移（生产 CSS 证实） |
| `--glass-fill-hover` | 白 7.5% | `rgba(255,255,255,0.12)` | ≈ `#2F2F2F` on `#141414` |
| `--glass-fill-active` | 白 12% | `rgba(255,255,255,0.2)` | |
| `--ok` | `#4ade80` | `#46D369` | Netflix 匹配绿 |
| `--info` | `#7fb0ff` | `#6BA6FF` | 微调贴合，验收时定稿 |
| `--warn` | `#f5c451` | `#E87C03` | Netflix 警示橙（存疑值，P6 定稿） |
| `--danger` | `#ff6b6b` | `#EB3942` | 故意不等于品牌红：红点/红字是状态信号，`#E50914` 是品牌，两者混用会让满屏红失去语义（P6 定稿） |
| `--danger-solid` | `#c73838` | `#B20710` | 实底红 = 品牌暗红，白字对比达标 |

**Netflix 主题下「强调色」的语义纪律**（这是 1:1 观感的成败点）：红不是主按钮色。
主操作 = 白底黑字（三态：1 / hover 0.75 / 按下 0.5，生产 CSS 证实）；次级 =
`rgba(109,109,110,0.7)` 灰底白字（hover 0.4）；红只出现在字标、进度条、匹配度式
高亮与激活指示上。中心化类同步覆盖：`.btn-accent` → 白底黑字（hover 压暗）、
`.btn-glass` → 灰半透方角，`.menu-surface` / `.surface-raised` → `#181818` 实底 +
Netflix 阴影，`.brand-badge` / `.nav-item` 选中胶囊 → 白系，播放器
`--player-accent` → `#E50914`。

## 5. 结构级组件规格

### 5.1 桌面顶栏 `NetflixTopNav`（≥768px）

- 高 68px【社区共识值】，`fixed`，z 高于内容；顶部透明，滚动过阈值后过渡到
  `#141414` 实底（不引入模糊——Netflix 顶栏是实底不是毛玻璃）。
- 左：品牌字标（红色 wordmark，复用现有 logo 资源的单色红版；可选 Bebas Neue，
  见 §2.2），点击回 `/`。
- 导航链接：首页 · 电影 · 剧集 · 媒体库 · 我的订阅（权限过滤复用 `session.role` 的
  `memberNavItems` 规则；激活 = 白 700，未激活 = `#E5E5E5`）。`<1100px` 收敛为
  「浏览 ▾」下拉（Netflix 同款，断点取社区共识区间中值）。**侧栏的 `nav.order`
  个人排序不作用于顶栏**——顶栏链接是全局固定五项（记录为已知取舍）。
- 右：`＋ 新任务`（白底黑字，跳 `/new`）· 搜索放大镜（复用 `SearchCommand` 命令
  面板，浮层换 Netflix 皮）· 通知铃（现有 notice-center 入口）· 方角小头像下拉
  （现有 user-menu 内容：设置 / 退出等；Netflix 原版下拉无「设置」项，此处为有意
  偏离，见 §2.5）。
- AI 会话的侧栏列表（最近会话）：顶栏放不下，收敛进头像下拉的「AI 会话」二级菜单。
  会话页内部已有会话间导航，不因此回退。

### 5.2 移动端底部标签栏 `NetflixTabBar`（<768px）

- 参照基准：Netflix App 2026-04 改版前的三 tab 形态（§2.6）；按本站功能映射为
  4 tab：**首页 / 发现 / 媒体库 / 我的**。
- 高 `49px + safe-bottom`（iOS 惯例，自家决策值）、背景 `rgba(10,10,10,0.95)` +
  轻模糊；激活白、未激活 `#808080`，图标 24px。
- 「我的」承载原抽屉内容：用户信息、新任务、我的订阅、活动、设置、AI 会话列表。
  原 `mobile-topbar` + 抽屉体系在 Netflix 主题下退役；详情页 `PageNav`（返回键 +
  吸顶雾）保留——App 详情页同样有返回。
- 现有页面级滚动容器的 `.scroll-safe` 让位机制照用；主区底部为标签栏让位的收口
  写在一条 `[data-theme="netflix"] .app-shell > main` 规则里，页面不逐个登记。

### 5.3 内容首页 `NetflixHome`（路由 `/`）

- **Billboard 选片规则**（确定性、无后端改动）：「接下来继续」第一项
  （`/playback/up-next`，见 library-home-up-next.md）→ 否则「最近入库」第一项 →
  否则显示空态（全新部署：添加媒体库 / 发起任务的引导卡，直接复用现有空态组件换皮）。
- 高度：PC 全出血 16:9 推导，`clamp(480px, 56.25vw, 80vh)`（v2 仲裁修正：56vh 无
  出处，按 Netflix 全出血构图推导）；移动约 40vh（自家决策值）。构图与渐变按
  §2.5；移动端构图转竖向（标题 / 按钮纵向堆叠）。
- 按钮：`▶ 播放`（白底黑字，直达播放页）· `ⓘ 详情`（灰底）· `✦ 问 AI`
  （ghost，跳 `/new` 并预填该片上下文——AI 是本站差异能力，给一个 Netflix 没有但
  不破坏画面的入口）。
- **行**（全部来自现有数据源，无新后端接口）：
  1. 继续观看（up-next，卡片带红色进度条；**双端都用 16:9 横版卡**，与 App 同构）
  2. 最近入库（recent-addition）
  3. 我的订阅（subscription-overview）
  4. 媒体库各库一行（行标题 = 库名，取前 N 个库，点标题进库页）
  5. 高分 / Top 250（discover 榜单，可按需取舍）
- 行标题 20px/700【社区值】，整行可点进对应页面。空行不渲染（不出现空标题）。
- `isHome`（不铺蒙版的氛围页判定）保持不变：Netflix 首页同样大图直出。

### 5.4 卡片行 `NetflixRow` 与卡片

- **PC（hover:hover 设备）**：16:9 横版剧照（`backdropUrl`，数据层已在
  discover / libraries / playback DTO 中返回）；无横版图的条目复用 `PosterImage`
  已有的「主图模糊铺底 + 居中完整显示」机制兜底。每行张数：桌面 6（>1400）/
  5（950–1400）/ 4（768–950）/ 平板 4 → 移动竖版（§7）；桌面 6/页为实测基准，
  其余档为自定值。
- **hover 展开卡——v2 仲裁：实现路线已定，Portal + fixed 定位**。
  CSS 规范硬约束（W3C Overflow L3 原文）：`overflow-x` 为滚动值时 `overflow-y`
  必然计算为 `auto/hidden`——**原生横滚行在 CSS 层面无法不裁切纵向溢出**， Netflix
  官方同样采用「把展开画布拎出文档流」的路线（其技术博客描述的扩展画布即如此）。
  实现：hover 意图延迟 400ms 后 `createPortal` 到 body 渲染 `position: fixed`
  展开卡——按卡片 `getBoundingClientRect` 定位、水平 `clamp` 于视口、垂直空间
  不足向上翻；关闭给 80ms 宽限让鼠标能移进面板；`scroll`/`wheel` 在 capture 阶段
  监听并立即关闭（防滚动后 hover 残留，社区共识的经典坑）；z-index 取 500 档
  （高于内容行、低于全局浮层）；展开卡样式全部走 `:root` token——Portal 会切断
  CSS 继承，但本站 token 挂在根节点，天然不受影响。生产级参考：Radix HoverCard
  的 openDelay/closeDelay/portal/collision 语义。
  展开内容：scale ~1.5、阴影 `0 12px 24px rgba(0,0,0,0.8)`、`#181818` 信息面板
  （40px 圆形操作钮、匹配度、元数据、类型标签），操作语义复用 `PosterCard` 现有
  五态（subscribe/follow/backfill/owned/none）。
- **移动 / 平板（无 hover）**：常规行 2:3 竖版海报行（App 同构），tap 直达详情，
  复用现有 `revealInfoOnTouch` 取舍；露出半张卡暗示可滑的既有约定保留。
  **「继续观看」行双端都用 16:9 横版 + 底部 3px 红色进度条**（App 截图证实）。
- 行翻页：桌面按整页翻（每页 = 当前行张数）、行 hover 时两侧箭头浮现，复用
  HScroller 的翻页框架（按 85% 翻的现值改为整页翻）；移动端无箭头、靠触摸滑动。

### 5.5 详情页（`/media/[type]/[id]` 与库内条目页）

- 保留**路由页**形态（可分享、可刷新、前进后退——本站强约定），不做 Netflix PC 的
  弹层；视觉按 Netflix 详情版式做：hero `clamp(480px, 56.25vw, 80vh)`（移动 40vh）、
  左下白色 `▶ 播放` 大按钮 + 灰底圆钮组（收藏 / 下载 / 分享）、meta 行（匹配度绿 /
  年份 / 分级 / 集数）、剧集季下拉 + 16:9 横滚集卡、演职员横滚（cast-row 已有）。
- 现有 `detail-ambient`（剧照 → 纯黑渐变板）机制完全契合，只在 Netflix 主题下把
  渐变终点从黑改为 `#141414`。

### 5.6 播放器（`/play/*`）

token 层换肤即可：`--player-accent: #E50914`（进度红），轨道 / 缓冲三段半透白与
Netflix 完全同款、已对齐，不再动。分享页（`/s/[slug]`）与播放页不套 AppShell，
`data-theme` 由 layout 内联脚本全局生效，它们只吃 token 层、不吃结构层。

### 5.7 控制台页（设置 / AI 会话 / 活动 / 订阅管理）

信息架构与布局骨架**全部保留**，按 §4 token 换皮：玻璃面板 → `#181818` 实色卡、
实线描边、方角、白底主按钮；AI 会话页 `.page-solid` 在 Netflix 主题下取 `#000`
（Netflix 播放/沉浸层语言）。深度控件（表格、开关、滑杆）沿用现有组件，颜色变量
自动跟随，圆角接受偏差（§9）。

## 6. 页面映射总表

| 路由 | 银玻璃（现状） | Netflix 主题 | 改动 |
|---|---|---|---|
| `/` | AI 新任务输入台 | **NetflixHome**：billboard + 行 | 结构层，新组件 |
| `/new`（新增） | — | AI 新任务页（复用 NewTask 组件） | 新路由；银玻璃主题下侧栏「新会话」仍指 `/` |
| `/discover/movie|tv` | 海报墙 + 榜单行 | Netflix 行/墙 + Netflix 卡 | 卡片层 |
| `/library…` | 卡片墙 | Netflix 行/墙 + Netflix 卡 | 卡片层 |
| `/media/…`、库内详情 | hero 详情页 | Netflix 详情版式 | 视觉层 |
| `/play/*` | 播放器 | token 换肤（进度红） | 仅 CSS |
| `/search` | 结果列表 | 换皮，结构不变 | token 层 |
| `/sessions/[id]` | 沉浸对话页 | 换皮（`.page-solid` → `#000`） | token 层 |
| `/settings…`、`/activity`、`/subscriptions` | 控制台页 | 换皮，信息架构不动 | token 层 |
| `/s/[slug]` | 分享页 | token 层跟随 | 仅 CSS |
| 外壳 | 侧栏 + 抽屉 | **NetflixTopNav / NetflixTabBar** | 结构层，新组件 |

## 7. 双端断点与规格对照

| 断点 | 每行卡片 | 卡片形态 | hover 展开 | 导航 |
|---|---|---|---|---|
| ≥1400px | 6（实测基准） | 16:9 横版 | 是 | 顶栏（全部链接） |
| 950–1400px | 5 | 16:9 横版 | 是 | 顶栏（≥1100 全链接，以下「浏览 ▾」） |
| 768–950px（平板竖） | 4 | 16:9 横版 | 否 | 顶栏「浏览 ▾」 |
| 500–767px（手机） | 3 | 2:3 竖版 | 否 | **底部标签栏** |
| <500px | 2~3 | 2:3 竖版 | 否 | 底部标签栏 |

> 桌面 6/页与整页翻页有实测依据；其余张数为自定基准（Netflix 未公布断点表），
> 验收以观感定稿。触控目标 44px 全线满足（沿用全站 `.touch-target` / 44pt 约定）；
> 字号移动端沿用现有 HIG 换档机制（`--text-*` 移动档），Netflix 字阶只作用在
> 新增结构组件上。

## 8. 实施拆分与验证

每阶段独立可合、可回退（主题开关一键回银玻璃）：

| 阶段 | 内容 | 验证 |
|---|---|---|
| P1 框架 | themes.ts、`ui.preferences.theme`（前后端）、`data-theme` 应用 + 防闪烁、设置主题卡、radius 覆盖组 | 切换主题全站色板/圆角即时生效；构建产物抽查工具类确为 `var(--radius-*)` 引用；刷新无闪；老后端数据兜底为 silver |
| P2 外壳 | NetflixTopNav、NetflixTabBar、搜索/通知/头像接线、玻璃体系停用 | 全路由可达性不回退（对齐侧栏权限矩阵）；WebGL 上下文数不增 |
| P3 首页 | NetflixHome（billboard + 行 + 空态） | 选片规则三档；双端构图对照 |
| P4 卡片 | NetflixRow / 横版卡 / **Portal hover 展开卡**（§5.4）/ 进度条 / 竖版移动行 | 展开卡不被行裁切、滚动即收、开合延迟手感正确；断点张数对照表逐档过 |
| P5 换肤 | 详情页、播放器、控制台页 token 收尾 | 与 Netflix 实机截图并排比对 |
| P6 验收 | PC 1440/1280/950 + 移动 390/768 双主题往返 | §2 规格逐项 checklist；存疑色值定稿；主题来回切换无状态残留 |

## 9. 风险与开放问题

**风险**

1. **hover 展开卡**：路线已裁定为 Portal + fixed（§5.4，规范层面是唯一能同时保住
   原生滚动与 1.5x 展开的方案）。剩余实现细节：滚动残留 hover（capture 监听已列）、
   打开后内容异步加载导致的定位漂移（定位取卡片矩形、加载完成不重排）、性能
   （展开卡懒挂载，收起即卸载）。P4 先做最小可用展开卡再铺行。
2. **任意值圆角不跟随 token**：`rounded-[10px]` 与内联 `border-radius` 散布待审计；
   策略是「验收清单内替换、清单外接受」，不追求一次性全量替换（避免超大 diff 淹没
   真实改动）。
3. **横版剧照缺失**：模糊铺底兜底已有机制，但「行内横版图忽有忽无」的观感节奏需要
   P4 验收时专门看一眼。
4. **顶栏横向空间**：1100px 以下链接收敛下拉；「新任务 + 搜索 + 铃 + 头像」四个右侧
   控件在 ~768px 档需要压成图标态。
5. **Netflix 改版漂移**：2026-06 网页端磁贴趋圆、2026-04 App 四 tab 改版均在演进；
   本主题已锁定基准版本（经典 web + 三 tab 期 App），不受其后续漂移影响，token
   集中定义便于单点调整。

**开放问题（不阻塞开工，实施中拍板）**

1. PC 详情页保留路由页（本方案推荐）还是做 Netflix 式弹层——涉及分享/刷新语义，
   若要弹层则需路由侧方案，另行评审。
2. 字标是否引入 Bebas Neue（新增静态字体文件，观感收益 vs 资产增量的权衡）。
3. Billboard 是否需要「换一部」手动入口（Netflix 无此交互，倾向不加）。
4. `--info` / `--danger` / `--warn` 与 `--surface-inset` / `--line` 的存疑色值定稿
   （P6 与实机对照）。
5. Netflix 主题下首页各行的取舍与排序（§5.3 的 1~5 是初排，P3 验收时按内容密度调）。

## 10. 调研来源

**官方（Netflix 自有资产）**

- 技术博客《Delivering Meaning with Previews on Web》（hover 展开画布 400ms、预览行为）：<https://netflixtechblog.com/delivering-meaning-with-previews-on-web-3cedc0341b9e>
- netflix.com 生产 CSS 抓取（字体栈、按钮三态、4px 圆角、焦点环、40px 圆钮、次按钮 `rgba(109,109,110,0.7)`、600ms 信息渐入）：<https://assets.nflxext.com/web/ffe/wp/@nf-web-ui/ui-shared/dist/less/pages/clcs/shared.fd4b86a52de5dc09baaa.css>
- 官方品牌色（#E50914 / #B20710）：<https://brand.netflix.com/en/assets/logos/>
- 帮助中心：支持的浏览器（无手机）、自动播放预览、头像菜单、Screen Lock、My Netflix：<https://help.netflix.com/en/node/30081> · <https://help.netflix.com/en/node/2102> · <https://help.netflix.com/en/node/322532375336036> · <https://help.netflix.com/en/node/115018> · <https://help.netflix.com/en/node/321880164349028>
- 2026 移动 App 改版（四 tab / Clips / New & Hot 降级，2026-04-30 起、08-25 全球）：<http://about.netflix.com/en/news/introducing-exciting-new-ways-to-find-and-enjoy-your-next-favorite-on-mobile> · <https://help.netflix.com/en/node/575087423404644>
- Top 10 官方说明：<https://about.netflix.com/news/top-10-things-about-netflix-top-10>
- 2025 TV 改版（仅 TV 端，与 web 无关的背景信息）：<https://www.netflix.com/tudum/articles/netflix-new-tv-layout>

**媒体 / 技术**

- Kevin Lobine《Debunking the Netflix slider》（6 张/页、整页翻、DOM 回收）：<https://kevinlobine.dev/read/debunking-the-netflix-slider>
- CSS-Tricks：hover 展开 150% 缩放与邻卡让位：<https://css-tricks.com/how-to-re-create-a-nifty-netflix-animation-in-css/>
- What's on Netflix：2026-06 网页端改版实测（磁贴趋圆 / 徽章 / hero 渐变）：<https://www.whats-on-netflix.com/news/netflix-website-redesign-testing/>；2026-04 移动改版实测：<https://www.whats-on-netflix.com/news/netflix-mobile-updates-clips-new-navigation/>
- Variety：移动 App 改版与 Clips（2026-04-30）、My Netflix 上线（2023-07）：<https://variety.com/2026/digital/news/netflix-new-mobile-app-vertical-video-feed-clips-1236734258/> · <https://variety.com/2023/digital/news/my-netflix-tab-mobile-download-shortcuts-1235678561/>

**工程参考（一手实现 / 规范）**

- Tailwind v4 border-radius / theme 文档与源码（`--radius-*` 变量引用机制、默认值表、`rounded-full` 静态值）： <https://tailwindcss.com/docs/border-radius> · <https://tailwindcss.com/docs/theme> · <https://github.com/tailwindlabs/tailwindcss>
- W3C CSS Overflow L3（overflow-x 滚动值强制 overflow-y 计算为 auto/hidden）：<https://www.w3.org/TR/css-overflow-3/>
- next-themes（data-theme 内联脚本 / suppressHydrationWarning / 双树渲染约定）：<https://github.com/pacocoursey/next-themes>
- Radix HoverCard（openDelay/closeDelay/portal/collision 语义）：<https://www.radix-ui.com/primitives/docs/components/hover-card>
- Portal 定位展开卡的一手实现（450ms/80ms/clamp/上翻）：<https://github.com/supriyaguess/jiohotstar-clone>
- Netflix DOM 复刻（sliderMask/showPeek、per-item transform-origin）：<https://github.com/zygisS22/react-netflix>
- Netflix Sans 替代字体讨论（Inter 为第一替代、Bebas Neue 与字标）：<https://madegooddesigns.com/netflix-font/>
- 设计 token 拆解（v1 引用，多项数值置信度在 v2 仲裁中重标）：<https://oh-my-design.kr/design-systems/netflix>

## 11. 调研仲裁记录（v2，2026-09-12）

三个子代理并行调研（桌面 Web 规格 / 移动端规格 / 工程技法），交叉比对后由主代理
裁定如下冲突与修正，已并入上文正文：

| # | 分歧 / 空白 | 仲裁结论 | 依据 |
|---|---|---|---|
| 1 | hover 展开动画 300ms（v1 拆解站） vs 400ms | **取 400ms** | 官方技术博客 |
| 2 | billboard 高度「56vh」无任何出处 | 改为全出血 16:9 推导 `clamp(480px, 56.25vw, 80vh)`；移动 40vh 标注为自家决策 | 官方构图 + 克隆实现 |
| 3 | 移动端「Netflix 手机 Web 版」是否存在 | **不存在**——官方不支持手机浏览器播放；移动端 1:1 对象改为 **App** | 官方帮助中心 |
| 4 | 底栏三 tab vs 四 tab | 参照 2026-04-30 改版前的三 tab 形态（稳定期），底栏项按本站功能映射 4 个；栏高/图标为 iOS 惯例自家值 | 官方改版公告 + 媒体实测 |
| 5 | Tailwind v4 圆角变量覆盖是否可行 | **可行**（工具类编译为 `var(--radius-*)` 引用）；边界：`rounded-full/none` 静态、任意值绕过、禁用 `@theme inline` | 官方文档 + 源码 |
| 6 | hover 展开卡防裁切路线（v1 为「待 spike」） | **裁定 Portal + fixed**：CSS 规范证明原生滚动行必裁切纵向溢出；400ms 开 / 80ms 关 / capture 关滚 | W3C Overflow L3 + 一手实现 |
| 7 | 红 hover 色值（#B20710 vs #C11119） | 二者并存分职：`--accent-strong` = #C11119（网页按钮 hover）、`--accent-2` = #B20710（pressed/品牌暗红） | 官方品牌站 + 生产 CSS |
| 8 | 色板中 #2F2F2F / #404040 / #808080 / #E87C03 / #46D369 的可信度 | 标注【社区/存疑】，保留为实现基准，P6 与实机对照定稿 | 三源交叉 |
| 9 | 字体回退链 | 修正为 `Netflix Sans, Helvetica Neue, Segoe UI, Roboto, Ubuntu, sans-serif`（Arial 仅播放器栈）；替代字体确认 Inter，字标可选 Bebas Neue | 生产 CSS 抓取 |
| 10 | 头像下拉内容 | Netflix 原版无「设置」项；本站保留设置入口，记为有意偏离 | 官方帮助中心 |
| 11 | 移动端「继续观看」行卡片形态 | **16:9 横版 + 进度条**（v1 误写竖版） | App 截图（媒体实测） |
| 12 | 行翻页行为 | 整页翻（6 张/页）+ 无限循环 + 行 hover 浮现箭头 | 媒体实测 + 官方 DOM 类名 |
