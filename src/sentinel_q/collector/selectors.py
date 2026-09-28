"""所有 CSS 选择器与文案标记，全项目唯一一处。

**为什么要单独一个文件**：知乎改版时唯一需要动的地方就是这里。散在四个
能力模块里的话，一次改版要满仓库找，而且必然会漏。

**每个选择器可以写多个候选，逗号隔开。**

## ⚠️ 候选的**顺序**就是优先级

`parse._pick()` / `drive.pick()` 按书写顺序逐个试，**第一个命中的就是它**。
（不能靠 CSS 分组本身的顺序——`query_selector(".A, .B")` 返回的是**文档里
靠前**的那个，不是选择器字面上靠前的那个。所以顺序必须由代码来管。）

`_pick_all()` 用的是"第一个**有命中**的候选，只返回它的结果"，
所以列表类选择器的**第一个候选必须是数量正确的那一个**。

## ⚠️ 这里**不能出现 `:has-text()`**

`:has-text()` 是 Playwright 专有伪类，BeautifulSoup **直接语法报错**。
而 `parse.py` 只吃 HTML 字符串、用 BeautifulSoup 解析（这个分工是刻意的，
见 parse.py 开头），所以凡是解析层会碰的分组，一个都不能有。

要按文案找元素，用下面的 `*_TEXTS` 常量 + `drive.click_text()`，
那是浏览器侧的活。

## 校准状态：2026-09-26

全部值已对着 `runtime/calib/` 下的 15 份真实快照逐条验证过（见文件末尾
`_REGISTRY` 与 `uncalibrated()`）。当年那些 `TODO-` 猜测值**错得很有代表性**，
错误记录保留在各常量下面——它们是这份文件最值钱的部分。

---
"""

from __future__ import annotations

# ── 页面状态：登录 / 验证码（session.py 用）──────────────────────────

AUTH_COOKIE_NAMES = "z_c0"
"""登录态 cookie 的名字。**判断有没有登录以这个为准，不以 DOM 为准。**

2026-09-26 实测确认：`z_c0` 在已登录的浏览器里存在且带值
（`2|1:0|10:…`，187 字符）。见 `probe --check` 的诊断输出。

## 为什么必须是 cookie（真事）

原来只看 LOGIN_INDICATOR 那个 DOM 选择器，结果用户手动登录成功了、
程序却一直判定"未登录"，卡在"等你登录"那一步死等到超时。

cookie 是**依据**，DOM 只是**表现**：浏览器带着它，服务器就认你；
页面上有没有那个头像 div，取决于知乎前端今天怎么写 class。
"""

LOGIN_INDICATOR = ".AppHeader-profile, .AppHeader-userInfo"
"""页面上"已登录"的视觉标志。**只当加分项用，不当判据。**

⚠️ 2026-09-26 实测：这一组在真实快照里**命中 0 个**——`.AppHeader-profile`
和 `.AppHeader-userInfo` 都不存在。它们来自参考仓库的旧版知乎，早就没了。

唯一还活着的候选是 `.Avatar`（首页信息流上 14 个、搜索页 2 个），
但它匹配的是**所有人的头像**，回答不了"我登录了吗"，已删除。

结论：**这一项在当前知乎上等于没有**。留着是为了将来有人看到它时，
能一眼知道"别指望它"。保留 `LOGOUT_INDICATOR` 同理。
"""

LOGOUT_INDICATOR = ".AppHeader-login, .SignFlow"
"""同上，实测也全是 0。

**判断没登录的真正依据是 `SIGNIN_PATH`**——URL 落在登录页上就一定是没登录，
这个判据不依赖任何猜测的选择器，比 DOM 标志可靠得多。
"""

SIGNIN_PATH = "/signin"

# ── 验证码 / 风控拦截页 ─────────────────────────────────────────────

CAPTCHA_URL_PATTERN = "/account/unhuman, /captcha, /signin/unhuman"
CAPTCHA_CONTAINER = "#captcha, .Captcha, .Unhuman"
"""⚠️ 三个候选都**未经实测**（采集快照那几次没撞上验证码）。

留着的原因是它宁可多查一次也不该漏——漏掉的代价是被当成"搜索无结果"，
正是最危险的那种静默失败。真撞上验证码时按实际 DOM 补。
"""

CAPTCHA_TEXT = "安全验证, 请完成安全验证, 拖动滑块"

RISK_INTERSTITIAL_TEXTS = ("网络环境存在异常", "开始验证")
"""知乎的风控拦截页。**这一组是实测观察到的。**

2026-09-26 实测：出口 IP 被标记时，无论真实 Chrome 还是自动化浏览器，
访问知乎都会先落到这一页，文案是

    系统监测到您的网络环境存在异常，为保证您的正常访问，
    请点击下方验证按钮进行验证。在您验证完成前，该提示将多次出现。

人工点「开始验证」过关后才到正常页面（和八爪鱼遇到的是同一页）。

⚠️ 记录它有两个用处：一是 `captcha_present()` 能认出这一页（否则会把它
当成"搜索没有结果"——正是最危险的那种静默失败）；二是**它说明 IP 被标记
这件事是网络层面的，换浏览器解决不了**，别在这上面白费功夫。
"""

# ── 通用文案标记 ────────────────────────────────────────────────────
# 用文案而不是 class 判断"到底了"更稳——class 会变，这句话不太会变。
# 但**不能只靠它**：文案真变了会让滚动循环一路空转到 max_rounds 才停。
# 所以 scrolling.py 同时用"连续 N 轮无新增"兜底。
#
# ⚠️ 2026-09-26 实测：**问题页滚到底不会出现这句话，只会滚不动了**。
#    所以下面的兜底不是"以防万一"，是主要机制。见 QUESTION_TOTAL。
END_OF_LIST_TEXTS = ("没有更多了", "没有更多内容", "已经到底了")
"""实测：搜索页滚到底会出现「没有更多了」（`search_bottom.html` 里 1 次）。
问题页**不会**（`question_bottom.html` 里 0 次）。
"""

# 「阅读全文」——**列表里**的卡片正文被折叠时，要点开才是全文
READ_MORE_TEXTS = ("阅读全文", "展开阅读全文")

CONTENT_MORE_BUTTON = "button.ContentItem-more"
"""被折叠卡片的那个「阅读全文」按钮。

⭐ **实测：它只出现在"列表"里，内容详情页一个都没有。** 六份快照数出来：

    快照                      ContentItem-more   .is-collapsed
    answer.html（回答详情）                 0               0
    article.html（文章详情）                0               0
    large_question.html（问题页 89 卡）      0               0
    question_bottom.html（问题页 15 卡）     0               0
    search_bottom.html（搜索列表）         198             198
    thought.html（想法搜索列表）           182             182

两张详情页是 0，所有列表页是"每张卡片一个"。所以：

  * **能力二（打开详情页取正文）不用点任何东西**——和问题页回答卡片
    （见 `answers.py` 模块开头）是同一个结论，两个能力各自实测过一遍。
  * **能力一（搜索列表）拿到的卡片正文是残缺的**，那不是详情页的正文，
    不能拿来当全文入库。这也正是能力一只产 URL、正文交给能力二的原因。

⚠️ 但 `/pin/<id>` **没有详情页快照**，所以 `content.py` 仍然会在取完正文后
检查这个按钮在不在：在就点开再取一次，点完还在就报「截断」。
没这一步的话，一个没实测过的页面形态会把残缺正文当全文存进证据库。
"""

CONTENT_COLLAPSED = ".is-collapsed"
"""折叠容器的标记，和 `CONTENT_MORE_BUTTON` 成对出现（实测两份列表里都是 182/198，
数字完全一致）。用作**交叉验证**：按钮点掉了但容器还带着这个类，就是没展开。"""

# 正文容器的语义化候选（第 2 档）。
#
# ⚠️ 实测：**回答页有 `[itemprop='text']`，文章页是 0**；
#    `[itemprop='articleBody']` **两页都是 0**（它只出现在搜索结果的
#    专栏卡片上，那是另一回事）。所以 CSS 类名兜底是必需的，
#    不是可选项——删了它文章正文就采空了。
#
# ⚠️ `.Post-RichText` 排在 `.RichText` **前面是刻意的**：实测文章页有
#    **3 个** `.RichText`——[0] 是正文（16454 字），[1] [2] 是评论输入框
#    （文案「理性发言，友善互动」）。取第一个碰巧对，但那又是运气。
#    `.Post-RichText` 是正文那个独有、且只有 1 个。
#    代价是它比 `.RichText`[0] 少约 500 字（正文外层包裹的部分），
#    外面那点内容不值得用"碰巧取对"去换。
RICH_TEXT = "[itemprop='text'], .Post-RichText, .RichText"

# ── 第 1 档：页面内嵌的结构化 JSON（架构文档 3.11）──────────────────
#
# ⚠️⚠️ 2026-09-26 实测，**这一档的适用范围比文档写的窄得多**。
#
# `js-initialData` 是**服务端渲染那一刻**的快照。点筛选、滚动、开弹窗之后
# 加载出来的东西进的是浏览器内存里的 Redux store，**永远不会写回那个 script**。
# 实测各页面的 `initialState.entities`：
#
#     正文页（回答/文章）   answers/articles: 1     ← 能用
#     问题页               answers: 5              ← 只是首屏那批，不是全部
#     搜索页               questions/answers/articles: 0   ← 完全是空的
#     评论                 comments/lineComments: 0        ← 完全是空的
#
# 结论：**第一档只适合"首屏就在的正文与元数据"**。凡是"点了才出来的"
# （搜索结果、评论、回答列表）只能走 DOM。文档 3.11 那句"最稳、拿得最全"
# 对搜索和评论不成立。
INITIAL_STATE_SCRIPT = "script#js-initialData"

# 在 JSON 树里找哪个键的提示，不是 JSONPath——`extract_json_state()` 会做一次
# 宽松的树搜索。注意这些**只对正文页有意义**（见上面的实测表）。
JSON_TEXT_KEYS = "content, excerpt, text"
JSON_TITLE_KEYS = "title, questionTitle, headline"
JSON_AUTHOR_NAME_KEYS = "name, authorName"
JSON_VOTEUP_KEYS = "voteupCount, voteUpCount, upvoteCount"
JSON_COMMENT_COUNT_KEYS = "commentCount, commentsCount"
JSON_CREATED_KEYS = "created, createdTime, updatedTime, dateCreated"

# ── 能力一：搜索页 ──────────────────────────────────────────────────

SEARCH_INPUT = ".SearchBar-input"
"""⚠️ 实测：`input[type='search']` 和 `input[name='q']` **都是 0**。
知乎的搜索框是个普通 input，只挂了 `.SearchBar-input` 这个类。
"""

SEARCH_RESULT_ITEM = ".SearchResult-Card .ContentItem, .ContentItem"
"""搜索结果里的一张内容卡片。

⚠️ **不能直接用 `.SearchResult-Card`**：实测 `search_bottom.html` 里有 220 张
`.SearchResult-Card`，但真正带内容的只有 **198** 张——多出来的 22 张是
**「相关搜索」块和推广位**，它们没有标题链接。

`.ContentItem` 实测 18（首屏）/ 198（滚到底），与真实内容数一致。
"""

SEARCH_EMPTY_TEXTS = ("内容发现", "精选内容")
"""⭐ **「搜不到东西」时页面的标志。这一条是本轮最值钱的发现之一。**

用户搜了一串乱码 `zzqqxx9988776qqzz`，知乎的反应是：
甩出一个 AI 直答（「完成回答，用时 3 秒」「全部来源 16」），
再把一批**推荐内容**塞进一个叫「**内容发现**」的板块。

也就是说——**"没有搜索结果"的页面并不是空的**。实测那份快照里照样有
**17 条能解析成内容、URL 完全合法的知乎条目**。

⚠️ 不知道这件事的话，采集会把这 17 条当成"搜索命中的结果"入库并显示成功。
这是文档 4.1 点名的那类失败：**看起来一切正常，采到的却是另一回事**。

实测判别力（5 份搜索快照里数出现次数）：

    标志         bottom  filter_open  filtered  initial  noresult
    内容发现          0          0          0        0      **2**
    完成回答          0          0          0        0      **1**
    精选内容          0          0          0        0      **3**
    "没有找到"        0          0          0        0        0

最后一行也是结论：**知乎不会说"没找到"**，所以别去找那句话。

处理方式（见 `search.py`）：**照常采，但必须响亮地报出来**——
"这个关键词没有搜索结果，采到的是「内容发现」推荐"。不丢数据，也不骗人。
"""

SEARCH_RESULT_LINK = ".ContentItem-title a[href]"
"""卡片里指向内容的链接。

⚠️ 实测 `a[itemprop='url']` 是 **0** —— `itemprop="url"` 挂在 `<meta>` 上，
不是 `<a>`，所以那个候选从来就选不中东西。

⚠️ 关于 href 的形态：**是协议相对的**（`//zhuanlan.zhihu.com/p/1943…`）
或者**站内相对路径**（`/question/22230085/answer/1594…`），两种都有。
调用方必须过 `urljoin` 再 `normalize`，别自己拼字符串。
"""

SEARCH_EXCERPT = ".RichText"
"""卡片上的**缩略信息**——不点「阅读全文」就能看到的那段正文。

⚠️ **不能用 `[itemprop='articleBody']`**，那是文章卡片专有的：

    实测 `search_bottom.html` 的 198 张卡（97 文章 + 99 回答）
        `[itemprop='articleBody']` → 只有 97 张命中，**99 张回答一张都选不中**
        `.RichText`                → **198 张全中**

    拿前者会把回答的缩略信息**整批静默丢掉**，而且丢得很有迷惑性：
    文章那半有值、回答那半全是空，看着像"知乎没给回答配摘要"。

⚠️ 三个存档（233 张卡）实测：每张卡**恰好一个** `.RichText`，
    **不会**匹配到标题，也不与标题重复。所以直接取第一个即可。

⚠️ 这段文字在页面上**被截断了**（每张卡都有「阅读全文」按钮）。
    它够用来判断"这条讲的是什么"，**但不能当正文用**。
"""

# ── 筛选面板 ────────────────────────────────────────────────────────
#
# 2026-09-26 实测 `search_filter_open.html`。这一整块原来全是猜的，
# 而且**猜错了三处**（见下面的注释），所以记得特别细。
#
# 面板的 DOM 结构（三组，顺序固定）：
#
#     <div class="SearchTabs-customFilter">
#       <ul class="SearchTabs-customFilter--group">   ← 组1 类型
#         <li><div class="SearchTabs-customFilter--tag tag-selected">不限类型</div></li>
#         <li><div class="SearchTabs-customFilter--tag">只看回答</div></li> …
#       <ul class="SearchTabs-customFilter--group">   ← 组2 排序
#         <li><div class="SearchTabs-customFilter--tag">最多赞同</div></li>
#         <li><div class="SearchTabs-customFilter--tag">最新发布</div></li>
#       <ul class="SearchTabs-customFilter--group">   ← 组3 时间
#         <li><div class="SearchTabs-customFilter--tag">一天内</div></li> …

SEARCH_FILTER_ENTRY = ".SearchTabs-customFilterEntry"
"""点开筛选面板的那个入口，在 `.SearchTabs-actions` 里。

⚠️ **它是个 `<div>`，不是 `<button>`。** `drive.click_text()` 原来只找
`button:has-text(...)`，所以 probe 采快照时报「点了最新=False」——
不是页面没有，是点错了元素类型。
"""

FILTER_PANEL = ".SearchTabs-customFilter"
FILTER_GROUP = ".SearchTabs-customFilter--group"
"""筛选面板里的一组选项。实测 3 组，顺序：**类型 / 排序 / 时间**。"""
FILTER_TAG = ".SearchTabs-customFilter--tag"
FILTER_TAG_ACTIVE = ".SearchTabs-customFilter--tag.tag-selected"
"""**选中标记。** 实测面板打开时恰好 3 个 `.tag-selected`（每组一个）。

这是能力一唯一可靠的"筛选真的生效了"判据。**必须回读它，不能只看点击有没有抛异常**——
点击静默失败的话，「不限时间」和「一天内」的结果集差着几个月的内容，
而采集照样"成功"完成。这正是文档 4.1 列为不可接受的那类失败。
"""

# 选项文案。**实测值，不是猜的**——猜的三处全错，记录在下面。
FILTER_GROUP_TYPE = 0
FILTER_GROUP_SORT = 1
FILTER_GROUP_TIME = 2
"""三个组在 `.SearchTabs-customFilter--group` 里的下标（按文档顺序）。"""

FILTER_TYPE_UNLIMITED = "不限类型"
"""组0（类型）的默认项。**采集口径里没提它，但必须管**——

它是**账号级的残留状态**：上一次跑如果点过「只看回答」，这个状态会一直留着，
于是后面的采集永远只有回答，文章和想法一条都进不来，而日志上一切正常。
所以每轮开头都把它归位到「不限类型」。

⚠️ 它还是**唯一能看到新问题**的一档：实测「最新发布 + 一天内」下，
   有 45 条结果是光秃秃的 `/question/<id>`（19 位雪花号，刚发布、还没有回答），
   而「只看回答」「只看文章」这两档里一条都没有。更新采集靠的就是这一档。"""

FILTER_TYPE_ANSWER = "只看回答"
FILTER_TYPE_ARTICLE = "只看文章"
FILTER_TYPE_VIDEO = "只看视频"
"""组0 的另外三项。**实测原文**——从 `search_filter_open.html` 的组0 抠出来的，
不是猜的（这个项目因为猜筛选文案错过三次，见下面 TIME 那段的记录）。

⚠️ 「只看视频」**不在采集范围内**：`shared.models` 的 content_type 枚举是
   question / answer / article / thought / comment，**没有 video**。
   列在 `FILTER_TYPE_OPTIONS` 里只是为了让校验表的四个选项完整，
   好让写错的档在**开浏览器之前**就被拦下来。"""

FILTER_TYPE_OPTIONS = (
    FILTER_TYPE_UNLIMITED,
    FILTER_TYPE_ANSWER,
    FILTER_TYPE_ARTICLE,
    FILTER_TYPE_VIDEO,
)
"""组0 的全部选项，实测原文。给 `SearchSpec` 校验入参用。"""

FILTER_SORT_DEFAULT = "综合排序"
"""组1（排序）的**页面默认项**——"不点排序"时知乎给的就是这一档。

⚠️ 它不是单纯的"按热度"：2026-09-26 用户的原话是「默认排序」，
   与「最多赞同」是两个不同的选项，所以这是自己的一个值，别合并。

⚠️ 这一档**不是按时间单调**的。「滚动循环提前收工」（`scrolling.StopReason.
   EARLY_STOP`）那条路只对单调有序的列表成立，用在这一档上就是静默漏采——
   见 `SearchSpec.sort_filter` 与 `scrolling.py` 里 EARLY_STOP 的说明。
"""

FILTER_SORT_NEWEST = "最新发布"
"""⚠️ **不叫「最新」**（那是猜的）。实测是「最新发布」。
"""

FILTER_SORT_OPTIONS = (FILTER_SORT_DEFAULT, "最多赞同", FILTER_SORT_NEWEST)
"""组1 的全部选项，实测原文。

给 `SearchSpec` 校验入参用：排序写错了要在**开浏览器之前**就报出来，
而不是等人等了十分钟、`_apply_filters` 才发现点不着。
"""

FILTER_TIME_UNLIMITED = "不限时间"
FILTER_TIME_DAY = "一天内"
"""⚠️ **不叫「不限」「一天」**（那是猜的）。实测带后缀。

⚠️ 更要命的是：按文案模糊匹配会**跨组串台**——`:has-text('不限')` 会同时
命中组1的「不限类型」。所以定位选项必须**先按组下标取 group，再在组内按
文案匹配**，不能全面板按文案找。
"""

FILTER_TIME_WEEK = "一周内"
FILTER_TIME_MONTH = "一月内"
FILTER_TIME_QUARTER = "三月内"
"""⚠️ **是「三月内」，不是「三个月内」。** 2026-09-26 用户口述全量梯子时说的是
「三个月内」——那是人的说法，页面上的原文没有那个「个」字。

    这一条值得单独记：`SearchSpec` 的入参校验就是为了接住这种错，
    但**只有当写进常量的是实测原文时，校验才拦得住**。
    所以下面 `FILTER_TIME_OPTIONS` 是照着快照逐字抄的，不是照人口述抄的。"""

FILTER_TIME_HALF_YEAR = "半年内"
FILTER_TIME_YEAR = "一年内"

FILTER_TIME_OPTIONS = (
    FILTER_TIME_UNLIMITED,
    FILTER_TIME_DAY,
    FILTER_TIME_WEEK,
    FILTER_TIME_MONTH,
    FILTER_TIME_QUARTER,
    FILTER_TIME_HALF_YEAR,
    FILTER_TIME_YEAR,
)
"""组2 的全部选项，实测原文，**顺序就是页面上的顺序**。

⚠️ **页面的顺序不是单调的**：「不限时间」在最前，之后是**从窄到宽**
   （一天内 → 一周内 → 一月内 → 三月内 → 半年内 → 一年内）。
   所以别指望"照页面顺序走一遍就是一条梯子"——
   `search.TIME_LADDER` 是**从宽到窄**单独排的，和这里的顺序不同。

   这处更正不是咬文嚼字：早先这条注释写的是"页面顺序恰好就是从宽到窄"，
   是错的。谁照着它去推梯子的顺序，会得到一条把「一年内」排在最前的梯子，
   而它跑起来**不会报错**——只是前几档把宽窗口先采了，后面窄窗口新增的
   内容变少，看着像"这个时间段没内容"。"""

# ── 能力二：内容页（回答 / 文章 / 想法 / 问题）──────────────────────

QUESTION_TITLE = "[itemprop='headline'], .Post-Title, .QuestionHeader-title"
"""标题。**三个候选的分工是实测出来的**：

    文章页   meta[itemprop='headline']  1 个，精确
    文章页   .Post-Title                1 个
    回答页   两个都是 0 → 落到 .QuestionHeader-title
    问题页   同上

⚠️ **`itemprop` 在这几页上全是 `<meta content="…">`，文本是空的**。
    所以 `[itemprop='headline']` 曾经"命中但取不到值"，标题一直是 None
    ——必须配合 `parse._text()` 读 `content` 属性（已修）。

⚠️ **不要用 `[itemprop='name']`**：实测文章页上它是**作者昵称**
    （`content='looooooudly'`），回答页上更是命中 7 个（作者、话题都算）。
    把它当标题会得到某个人的网名。

⚠️ `.QuestionHeader-title` 实测命中 **2 个**（吸顶栏 + 正文栏各一个），
    文本相同，取第一个即可。
"""

META_PUBLISHED = "meta[itemprop='datePublished'], meta[itemprop='dateCreated']"
"""发布时间的**精确值**，ISO 8601 带时区（实测 `2025-01-20T14:14:44.000Z`）。

比页面上那行「编辑于 2026-03-24 22:30」可靠得多——那一行是**最后编辑时间**，
而且要靠正则去猜格式。实测两者在文章页上差了**一年多**。

两边都留：`published_at` 用这个精确值，`published_text` 保留页面原文，
人工核对时能看出"页面当时写的是编辑时间"。

## ⚠️ 必须两个候选：文章用 `datePublished`，回答用 `dateCreated`

2026-09-26 实测（每页都是逐条数的，不是抽样）：

    作用域                      datePublished   dateCreated
    article.html（文章）            1/1            0/1
    answer.html（3 个回答卡片）      0/3            **3/3**
    question_newest.html（5 个）     0/5            **5/5**
    large_question.html（89 个）     0/89          **89/89**

只写 `datePublished` 的话，**回答页和问题页的时间会全部退回文本解析**——
不是采不到，是采到两个不同口径的值混在一列里：文章是精确 ISO，
回答是靠正则从「发布于2026-09-26 15:39」里抠的。

（顺带验了 `dateCreated` 的口径：`2023-12-29T02:07:30Z` 对页面的
「发布于2023-12-29 10:07」，正好 +8 小时，就是 UTC ↔ 北京时间，没别的手脚。
而同一张卡片上 `编辑于2026-05-12 16:45` 对应 `dateCreated=08:40Z`，
说明**页面上那行字确实是编辑时间、不是发布时间**——用微数据是对的。）

⚠️ 还有 `dateModified`（89/89 也有），**刻意不加进来**：那是最后编辑时间，
用它等于把"他是什么时候说的"换成"他最后改到什么时候"。
"""

QUESTION_DESCRIPTION = ".QuestionRichText, .QuestionHeader-detail"

AUTHOR_NAME = ".AuthorInfo-name"
AUTHOR_LINK = "[itemprop='author'] .UserLink-link"
"""作者链接。实测回答页 6 个、文章页 2 个。

⚠️ 裸的 `.UserLink-link` 命中的是**页面里所有人的链接**（实测 8 个），
不只是作者。必须挂在 `[itemprop='author']` 下面。
"""

UPVOTE_COUNT_META = "meta[itemprop='upvoteCount']"
UPVOTE_BUTTON = UPVOTE_COUNT_META + ", button[aria-label*='赞同']"
"""赞同数。**优先微数据**，退回按钮。

实测回答页两个都有：`meta[itemprop='upvoteCount']` 3 个（`content="4886"`，
正好是目标回答的赞数），`button[aria-label*='赞同']` 3 个（`"赞同 4886 "`）。
数字一样，但微数据不用解文案、不用按 aria-label 猜格式，所以排前面。

⚠️ 实测 `.VoteButton--up` 是 0，旧版知乎的类名，已删。

⚠️ 两个来源都是**每个回答一份**，所以在回答页上必须配合
    `parse._content_scope()` 限定到目标那条，否则会取到推荐回答的赞数。
"""

COMMENT_COUNT_META = "meta[itemprop='commentCount']"
COMMENT_COUNT_BUTTON = COMMENT_COUNT_META + ", .ContentItem-action"
"""评论数。同样优先微数据（实测 `content="1623"`），退回按钮文本。

⚠️ 退回时 `.ContentItem-action` 实测命中 **18 个**——收藏、分享等所有操作
    按钮都在里面。所以取数时必须**在文本里筛"评论"两个字**
    （`parse._comment_count` 就是这么做的），不能拿第一个了事。
"""

PUBLISHED_TIME = ".ContentItem-time"
"""发布时间。实测回答页 3 个、文章页 1 个、问题页每个回答 1 个。

⚠️ 实测**知乎根本不用 `<time>` 标签**（`time[datetime]` 和 `time` 都是 0），
所以别指望 `datetime` 属性，只能读文本。

实测文本形态：`编辑于2026-09-04 17:55 ・北…`、`发布于2013-12-09 17:38`。
**都带绝对日期**，`parse.parse_datetime` 能解出来。
但评论里的时间是 `04-22` 这种**不带年份**的，会返回 None——
那是刻意的，见 `parse_datetime` 的注释。
"""

THOUGHT_ITEM = "[itemprop='zhihu:pin'], .ContentItem.PinItem"
"""想法（pin）在列表里的卡片。

⚠️ 实测两个候选**数量不一致**：`[itemprop='zhihu:pin']` 是 113 个，
`.ContentItem.PinItem` 是 170 个。也就是说**不是每条想法都带那对
`<meta itemprop="url|name">`**。取列表时用哪个得想清楚：
要"尽量全"就往后落，要"只要结构确定的"就停在第一个。
"""

# ── 能力三：评论 ────────────────────────────────────────────────────

COMMENT_ENTRY_BUTTON = ".BottomActions-CommentBtn, .ContentItem-action"
"""⭐ 评论入口 = **页面最底下那一栏（赞同 / 评论 / 收藏）里的评论按钮**。

实测（`answer.html` / `article.html` / `articalnew.html`），结构是：

    <button class="Button ContentItem-action BottomActions-CommentBtn …">
      <span…><svg…/></span><span…>1623 条评论</span>
    </button>

⚠️ `.BottomActions-CommentBtn` 是 `.ContentItem-action` 的**子集**——同一个
   `<button>` 上两个类都有，所以这一个选择器同时覆盖回答页和文章页。

⚠️ **不要退回"页面下方评论区里的「查看全部评论」"**。实测它不可靠：
   `articalnew.html` 里那种文字**一个都没有**（评论少的时候评论区直接摊开，
   根本没有这个按钮），于是老写法是"没找到入口"，静默走完流程。

⭐ **文案里那个数就是 `declared`，弹窗还没开总数就已经拿到了。** 弹窗开得了就
   以弹窗标题为准（那才是权威），开不了时它是唯一能对账的基准。

⚠️ 判据是**这个按钮的文案**（`comments._entry_kind`），不是"第几个按钮"：
   回答卡片上 `.ContentItem-action` 有六七个（赞同、收藏、分享…），
   按位置取必然取错。
"""

COMMENT_EMPTY_TEXT = "添加评论"
"""零评论时入口按钮的文案。**看到它就别点**——用户实测：点开是个空输入框，
不是弹窗，后面整套"找面板、滚列表"的流程全部落空。

⚠️ 既不是「N 条评论」也不是「添加评论」时**要报错，不能当成 0 条**。
   "这条内容没人评论"是要写进证据说明的结论，和"入口没校准"长得一模一样，
   而后者会让每一次采集都安静地少一条内容。
"""

COMMENT_MODAL = ".Modal-content"
"""评论弹窗容器。

⚠️ 实测 `[role='dialog']` 和 `.Modal` **都是 0**，只有 `.Modal-content`。

⚠️⚠️ **它同时是滚动容器**，但**没法写死选择器**：
弹窗内部那层是 `div.css-tpyajk` 这种**内容哈希类名**，知乎每次发版都会变。
所以 `drive` 层要在运行时探测——在弹窗里找 `scrollHeight > clientHeight`
的那个元素。这个判据与类名无关，比写死稳。

（`window.scrollBy` 对弹窗无效，必须滚元素本身。这是这类爬虫最常见的坑。）
"""

COMMENT_ITEM = "[data-id]"
"""一条评论。

⚠️⚠️ **这是本次校准最重要的一个发现**：评论的结构里**没有任何语义化的
class**（`.CommentItem` / `[itemprop='comment']` 实测都是 0），全是
`css-14nvvry` 这种哈希名。唯一稳定的钩子是 **`data-id` 属性，值就是评论 ID**。

⚠️ **回复是嵌套在父评论的 `[data-id]` 里面的另一层 `[data-id]`**。
实测 `comments_modal.html`：

    data-id=10214848119  depth=4   ← 一级评论
      └ data-id=10215236372  depth=5   ← 它下面的回复

⚠️ 回复**已经不采了**（2026-09-27，理由见 `comments.py` 模块开头），但嵌套这件事
仍然要紧：直接 `select("[data-id]")` 会把两级拍平，所以收 HTML 时必须开
`drive.Harvester(..., roots_only=True)` 把回复滤掉。不滤的话它们会被当成
一条条独立的一级评论存进库，**而且不报错**。
"""

COMMENT_AUTHOR_LINK = "a[href*='/people/'], a[href*='/org/']"
"""⭐ **评论的作者链接和正文页的不是一套，这是必须单列一组的原因。**

实测 `comments_modal.html` 的评论节点里：

    [itemprop='author'] .UserLink-link   0 个
    .UserLink-link                       0 个
    .AuthorInfo-name                     0 个
    a[href*='/people/']                  **2 个**

评论用的是纯 `<a href="/people/…">`，class 是 `css-10u695f` 这种哈希名，
**没有任何语义标记**。原来直接套用正文页的 `AUTHOR_LINK`，结果是
`parse_comment_list()` 在 9 条评论上返回 **0 条**——
而且不报错，只是安静地什么都没采到。

⚠️ 每个评论里有 **2 个**这样的 `<a>`：**第一个是头像（无文字）、
第二个才是昵称**。href 相同，所以取 URL 随便哪个都行；
**取昵称必须挑有文字的那个**（见 `parse._comment_author_name`）。

⚠️⚠️ **`/org/` 这个候选是补漏补出来的，务必别删。** 只写 `/people/` 时，
8 条顶层评论里会**静默丢掉 2 条**（25%），因为那两个作者是**机构号**：

    江绵学堂官方  →  https://www.zhihu.com/org/1ea8544bae009ebd60c4c47aa3be9831

它们既不是匿名也不是已注销，只是不挂在 `/people/` 下。丢掉的原因是
`_comment_from_node` 拿不到作者就整条丢弃（无法归属的评论没有取证价值）——
**规则本身是对的，是选择器选窄了**。

这类漏采最难发现：不报错、不告警，只是每条评论都"成功"地少采了四分之一。
"""

COMMENT_ID_ATTR = "data-id"
"""`[data-id]` 里装的那个属性名 = **评论 ID**。

`fact_content.zhihu_id` 是 not null，而评论原来填的是 None——
**那条记录根本存不进库**。现在从这儿取，既是真 ID 又满足约束。
"""

COMMENT_CONTENT = ".CommentContent"
"""评论正文。实测是稳定的类名（不是哈希名），9 个 / 66 个都对得上。"""

MODAL_PANELS = ".Modal-content > div"
"""弹窗里的**面板**（直接子元素）。

⚠️ 实测 `comments_second_level.html`：整页只有 **1 个** `.Modal-content`，
但它底下可能有**两块并列的子元素**：

    .Modal-content
    ├── div.css-feetku   ' 394 条评论 默认 最新 …'   ← 评论列表
    └── div.css-tpyajk   ' 评论回复 …'               ← 回复面板

两块面板的类名都是哈希名，**不可写死**：同一份快照里出现的三个
`css-xxxxxx`（两块面板 + 评论列表的滚动容器）互不相同，且知乎每次发版都换。
所以定位只能靠**标题文案**——见 `comments._comments_panel`。
"""

REPLY_PANEL_TITLE = "评论回复"
"""回复面板自己的标题。实测是**精确的四个字**（面板里嵌套三层都是它）。

⚠️ **回复已经不采了（2026-09-27，见 `comments.py` 模块开头），这个常量却必须留着。**
它现在的唯一用途是**反向**认出评论列表：`_comments_panel()` 判的是
"弹窗里*不含*这个标题的那一块"。删掉它就只剩"取第一块"，
而"哪一块在前"是渲染顺序、不是语义——知乎换一次发版就会错，且不报错。
"""

# ── 问题页上"就地展开"的评论区（2026-09-27，取自 anwserNew.html）──────
#
# 同样是点「N 条评论」，在**单独的回答页**上直接开弹窗（能力二那条路），
# 在**问题页**上却是在那张回答卡片**内部**摊开一小块评论区。这块有两种形态：
# 评论少就是全部；评论多则多一个「点击查看全部评论」，点它才开出弹窗。

INLINE_COMMENTS = ".Comments-container"
"""就地展开的那块评论区，在回答卡片**内部**。

实测是语义类名（不是 `css-xxxxxx` 哈希名），快照里 2 个实例分属两张不同的卡片。
⚠️ 必须**限定在卡片作用域里**找——整页有多个，取第一个就是取别人的评论。
"""

VIEW_ALL_COMMENTS_TEXTS = ("点击查看全部评论",)
"""内联区里那个"去弹窗"的按钮。

⚠️ **它的有无就是"这块是不是全部"的判据**（`comments._collect_inline`），
所以只认精确文案。元组常量不进 `_REGISTRY`，这是正常的（那里只收 `str`）。
"""

COLLAPSE_COMMENTS_TEXTS = ("收起评论",)
"""展开之后按钮会变成它。采完必须点回去：一个 400 条回答的问题页，
内联区全摊开会把 DOM 撑爆。
"""

COMMENT_MODAL_CLOSE = "button[aria-label='关闭']"
"""弹窗的关闭按钮。用语义属性 `aria-label`，不是哈希类名。

⚠️ 关掉之后要**校验弹窗真的没了**（`MODAL_PANELS` 查不到）再接下一张卡片——
没关掉就会在上面那块面板里接着"采"，采出一批张冠李戴的评论还不报错。
"""

# ── 能力四：问题页的回答列表 ────────────────────────────────────────

ANSWER_ITEM = ".AnswerItem"
"""问题页里的一张回答卡片。实测滚到底 89 个（`large_question.html`）。

## ⚠️ 原来是 `.AnswerItem, .List-item`，**那个逗号是个 bug**（2026-09-26 修）

`.List-item` 不是 `.AnswerItem` 的别名，是它的**外层包装**——实测
89 个 `.List-item`，每一个里面**正好一个** `.AnswerItem`，
两批元素**零重叠**（`a[0] is b[0]` 为 False）。

于是分组 `.AnswerItem, .List-item` 在浏览器里选出 **178 个**元素：
每一条回答被数了两次（外层包装 + 内层卡片）。

它没造成重复入库——包装层里也找得到那张卡片，解析出来的 `zhihu_id` 一样，
会被 `answers.py` 的 `seen` 集合挡掉。但代价是实打实的：

    1. 每轮 DOM 传输和解析都翻倍（`large_question.html` 有 2.2 MB）
    2. `duplicates` 统计被灌进 89 条假数据，**真重复就看不出来了**

而且 `.List-item` 在不同的页面上选的**不是同一批东西**，更说明它不该在这一组里：

    页面                     .AnswerItem   .List-item
    large_question.html            89           89    ← 一一对应（包装）
    question_bottom.html           15           15
    answer.html                     3            2    ← .AnswerItem 才是全的
    search_bottom.html             99          219    ← .List-item 宽得多

`parse._pick_all()` 只取**第一个有命中的候选**，所以解析层一直没暴露这个问题；
是浏览器侧的 `drive.Harvester`（用 `css()`，即整个分组）会真的收到 178 份 HTML。

⚠️ 实测 `[itemprop='answer']` 在**问题页是 0**（它只出现在搜索结果卡片上），
`.AnswerCard` / `.QuestionAnswer` 也都是 0。别用那些。

## 卡片上的 `itemprop` 到底是什么（2026-09-26 实测 89 条逐条数的）

    被采纳的回答      itemprop="acceptedAnswer"    1 条
    其余回答          itemprop="suggestedAnswer"  88 条

所以 `[itemprop='answer']` 选不中任何东西**不是因为它被改叫别的名字了**，
而是知乎按"采纳/建议"分了两个值。想按语义选也可以写成 `[itemprop$='nswer']`，
但没必要——`.AnswerItem` 实测 89/89 全覆盖，且是这两个值共同的类名。

## 卡片里的字段覆盖（89/89，全部满覆盖）

    meta[itemprop='dateCreated']          89/89   精确到秒的发布时间
    meta[itemprop='upvoteCount']          89/89   赞同数
    meta[itemprop='commentCount']         89/89   评论数
    [itemprop='author'] .UserLink-link    89/89   作者链接
    [itemprop='text']                     89/89   正文
    meta[itemprop='url']                  89/89   卡片自己的回答 URL

最后一条尤其有用：它是**机器可读的规范 URL**，比在卡片里翻
`a[href*='/answer/']`（正文里作者常贴自己别的回答）可靠得多。
见 `parse._answer_url_from_meta`。

## 正文没有被截断（这一点专门验过）

`large_question.html` 里最长的一条正文 8710 字、中位数 296 字，
而卡片里**一个「阅读全文」都没有**（实测 0 次），也没有「展开」按钮。
短的那几条是真的短——「+1」「感谢分享。」「良心黑马！」。

所以能力四**不需要点开任何东西**就能拿到全文，也就没有"采到一半正文
却当成全文存下来"的风险。唯一的例外是 1 条正文为空（`name=2986617993`，
`<span itemprop="text">` 里什么都没有），按 `has_body=False` 统计上报即可。
"""

ANSWER_ID_ATTR = "name"
"""回答卡片上直接写着回答 ID 的属性名。

实测：`<div class="ContentItem AnswerItem" itemprop="answer" name="1594809785">`
——**`name` 就是回答 ID**。这是精确识别"这条是不是我要的那条"的唯一可靠办法。

⚠️ 为什么非要用它：回答页（`answer.html`）里除了目标回答，还挂着 2 条
推荐回答，而且侧栏有 **160 多个**指向别处回答的 `<a>`。靠"取第一个"碰巧
能对，靠 `a[href*='/answer/']` 计数会**多采 50 倍**。
见 `parse.parse_content_page()`。
"""

QUESTION_TOTAL = ".List-headerText"
"""声明"共 N 个回答"的那行（实测文本 `252 个回答` / `1,735 个回答`）。

⚠️⚠️ **这是能力四的完整性判据，也是"滚不动了"问题的正解。**

用户报告：问题页一直往下滚**不会出现"没有更多了"**，只会滚不动。
那就没法用文案判断到底了。但这一行给了**声明的总数**：

    采到的条数 == 声明的总数   →  确实采全了
    采到的条数 <  声明的总数   →  **没采全，必须报错，不许当成功**

实测依据 `question_bottom.html`：声明 15，采到 15，对得上。

⚠️ 仍未知：回答数很大时（252 / 1735）知乎会不会**限流**只放一部分出来。
真跑一次大问题才能确定。在确定之前，这个判据的价值恰恰在于——
**它会把"没采全"变成一次可见的报错，而不是一次安静的成功。**
"""

COLLAPSED_ANSWERS_BAR = ".CollapsedAnswers-bar"
"""「N 个回答被折叠」那一行。实测 `question_newest.html` 里 1 个：

    <div class="CollapsedAnswers-bar">
      <button>15 个回答被折叠</button>（<a href="/question/20120168">为什么？</a>）
    </div>

⭐ **它让"不点开折叠回答"这个决定不用付完整性上的代价。**

用户 2026-09-26 决定不点开折叠回答（「被折叠回答也不会被人看，对舆论影响很小」）。
但折叠回答**是算进「N 个回答」那个声明总数里的**——那页声明 1735，
其中 15 个被折叠。数不出这 15 的话，完整性判据会永远差 15 条，
天天误报"没采全"。有了它就能对上：

    采到 1720 + 折叠 15 = 声明 1735  ✓

⚠️ 它是个**浮层/工具栏**，不一定随滚动出现在 DOM 里——所以是"有就用，
    没有就当 0"，不能因为它缺席就判失败（见 `parse.parse_collapsed_count`）。
"""

QUESTION_SORT_BUTTON = "button[role='combobox'], .Select-button"
"""问题页的排序控件。实测是个 combobox 按钮，**当前值就是它的文本**。

⭐ **验证"按时间排序"是否生效，就读这个按钮的文本**——实测点击成功后
它从 `默认排序` 变成 `按时间排序`。这是能力四最关键的断言：

知乎默认按相关热度排序。**如果这一下点击静默失败，更新采集会漏掉排在
后面的新回答而完全不报错。** 用户上一轮采快照时就踩过这个坑——probe 按
文案「最新」找元素，命中的其实是回答正文里的"2026年最新版"，
`点中「最新」=True` 是**假阳性**，两份快照的排序都还停在「默认排序」。

实测回读值（`parse._text(_pick(soup, QUESTION_SORT_BUTTON))`）：

    question_default.html        默认排序
    question_newest.html         按时间排序
    question_sort_open.html      默认排序
    anwser_sort_by_time.html     按时间排序   ← 下拉**开着**的那份
    large_question.html          默认排序
"""

QUESTION_SORT_OPEN_ATTR = "aria-expanded"
"""下拉开没开，看 combobox 上的 `aria-expanded`。

实测 `anwser_sort_by_time.html` 里是 `"true"`（下拉开着），
其余几份没有这个属性或没有该按钮。**比"数一下选项元素在不在"稳**——
选项元素其实一直在 DOM 里（见下），关着的时候也在。
"""

QUESTION_SORT_LIST = ".Select-list"
"""下拉列表容器。实测 1 个（只在 `anwser_sort_by_time.html` 里出现）。

⚠️ **它一直在 DOM 里，跟下拉开没开无关**——`anwser_sort_by_time.html`
里 `aria-expanded="true"` 且有 `.Select-list`；但关着的时候它也可能在。
所以"下拉开着吗"要看 `QUESTION_SORT_OPEN_ATTR`，不是看它在不在。
"""

QUESTION_SORT_OPTION = ".Select-list button[role='option'], button[role='option'], .Select-option"
"""下拉里的选项。**实测 2 个，DOM 顺序就是 `默认排序` → `按时间排序`。**

⭐ 这一项之前一直是空的，因为手上那份 `question_sort_open.html`
**其实是在下拉关着的时候采的**（整份 HTML 里「按时间排序」一次都没出现）。
2026-09-26 用户补采的 `anwser_sort_by_time.html` 才是真的开着：

    <div class="Select-list Answers-select …">
      <button class="Select-option …" role="option">默认排序</button>
      <button class="Select-option …" role="option">按时间排序</button>

⚠️ **`role="option"` 的元素和 combobox 自己的 `<span>` 文案一模一样**
（combobox 选中「按时间排序」时，它的 `<span>` 里也是这四个字）。
所以**不能**按文案在整页找——那会先命中 combobox。
要限定在 `QUESTION_SORT_LIST` 里点，或者用 `drive.click_option()`
（它先开下拉、再在列表里点，见 drive.py）。
"""

QUESTION_SORT_NEWEST_TEXT = "按时间排序"
QUESTION_SORT_DEFAULT_TEXT = "默认排序"
"""下拉里的两个选项，实测原文。

## 两个模式分别点哪个（2026-09-26 用户改的口径）

    全量（backfill）  →  **默认排序**（= 相关热度），为了拿到比较热门的回答
    更新（update）    →  **按时间排序** + 遇到采过的就早停

⚠️ 这**推翻了**先前确认的口径（原名"全量=按最新、更新=按最新+一天内"），
也和最初计划里「两种模式都点按时间排序」相反。以用户最新口径为准。

⚠️ 由此带来一个必须说清楚的性质变化：**「全量」不再是"全部"**。
默认排序是**相关性**排序，不是时间序——它给出的是"最值得看的 N 条"，
不是"所有的 N 条"。所以能力四的"全量"采集，语义是**尽可能多**，
而不是"一条不落"。这一点在 `answers.py` 的完整性判据里写死了。

（顺带确认：问题页**没有时间筛选**，那只有搜索页才有。所以「全量 vs 更新」
在问题页上没法靠筛选区分，只能靠排序 + 采到多少。）
"""

# ── 校准状态 ────────────────────────────────────────────────────────

TODO = "TODO-"
"""旧的"未校准"前缀，历史写法。

现在更直白的写法是**留空字符串**（见 `_PENDING`）——`TODO-xxx` 看着像有个值，
容易让人以为"差不多能用了"，而空串一眼就知道是没值。

两者 `uncalibrated()` 都认。这个常量留着是因为测试的合成 HTML 夹具要用它
把前缀剥掉。
"""

# 模块级字符串常量自动登记（全部大写 = 选择器或文案标记）
_REGISTRY: dict[str, str] = {
    name: value
    for name, value in list(globals().items())
    if name.isupper() and isinstance(value, str) and name != "TODO"
}

# 明确标记为"未校准"的常量：值是空串，或者仍是旧的 TODO- 前缀写法。
# 空串是新的表示法——比 `TODO-xxx` 更直白：**这里根本没有值**。
#
# 2026-09-26：`REPLY_ITEM` / `REPLY_MODAL` 两项补完，**这里是空的了**
#   （`REPLY_ITEM` 后来随二级回复一起删掉，2026-09-27）。
# ⚠️ 空元组是**有意义的**（"没有待校准项"），不要因为"看着多余"删掉它——
#    `uncalibrated()` 要读它，测试也靠它当总闸。
_PENDING: tuple[str, ...] = ()


def uncalibrated() -> list[str]:
    """还没校准的选择器名。空的就说明可以实跑了。"""
    pending = {name for name in _PENDING if not _REGISTRY.get(name)}
    pending |= {
        name for name, value in _REGISTRY.items() if value.startswith("TODO-")
    }
    return sorted(pending)


def summary() -> str:
    """给人看的一行状态，入口处打印用。"""
    pending = uncalibrated()
    total = len(_REGISTRY)
    if not pending:
        return f"选择器已全部校准（{total} 项）"
    return (
        f"⚠️ {len(pending)}/{total} 项选择器未校准，采集会静默漏数据，已拒绝运行。\n"
        f"   待校准：{', '.join(pending)}\n"
        f"   补齐的办法：拿一份真实页面快照（`probe.py`），对着它改这里的常量，"
        f"再把常量名从 `_PENDING` 里删掉。"
    )


_NEVER_MATCHES = ":not(*)"


def css(css_group: str) -> str:
    """把选择器分组变成能直接用的 CSS，顺便剥掉未校准标记。

    ⚠️ 它**不过滤**未校准的候选——猜测值照样会被用上。

    拦截"未校准就实跑"是 `require_calibrated()` 的职责，那才是会出事的环节
    （对着真实账号采集）。这里保留猜测值，是为了让解析层的管道测试在还没有
    真实快照时也能跑通——否则解析函数要等到校准完才能第一次运行，
    那才是真的危险。

    换句话说：**标记用来拦住执行，不用来拦住匹配。**
    """
    usable = [
        part.strip().removeprefix("TODO-") for part in css_group.split(",") if part.strip()
    ]
    return ", ".join(usable) or _NEVER_MATCHES


def candidates(group: str) -> list[str]:
    """把候选拆成列表，**保持书写顺序**（= 优先级，见文件开头）。

    给 `parse._pick()` 用。不要拿 `css()` 的结果去 split——那个是拼给浏览器
    用的，顺序已经被 CSS 分组语义吃掉了（`.A, .B` 命中的是文档里靠前的那个，
    不是字面上靠前的那个）。

    ⚠️ 按逗号裸切，所以**候选里不能出现逗号**（`:has-text('a, b')` 那种，
    何况 `:has-text()` 本来就不能进这个文件）。
    """
    return [part.strip() for part in group.split(",") if part.strip()]


class Uncalibrated(RuntimeError):
    """用到了没校准的选择器。"""


def require_calibrated(*names: str) -> None:
    """在入口处调用：用到的选择器还没校准就直接拒绝运行。

    **为什么是拒绝而不是警告**：没校准的选择器不会报错，它只是匹配不到东西。
    那会变成静默漏采——跑完一圈显示"成功"，实际一条数据都没抓到。
    架构文档 4.1 把这种失败模式明确列为不可接受。

    按能力分开声明（搜索/正文/评论/回答各查各的），所以可以先把搜索校准好
    就跑搜索，不用等全部都改完。
    """
    pending = [name for name in names if name in uncalibrated()]
    if pending:
        raise Uncalibrated(
            f"以下选择器未校准：{', '.join(sorted(pending))}\n"
            f"   这些要么是空值，要么还带着 TODO- 前缀。"
            f"对着 runtime/calib/*.html 补上真实值再跑。"
        )
