"""能力三：打开一条内容的评论，把**一级评论**取下来。

## 为什么不做二级回复（2026-09-27 定的）

用户原话：

    采集二级评论会出问题（无法返回一级评论而且每次打开新的页面都要等很久，
    放弃了，不追求十全十美）

两个代价都实测过：

    关不掉 → 点开回复面板之后不一定关得掉；关不掉就**回不到原来那条评论**的位置，
             后面每一次展开都落空（这一条正是"无法返回一级评论"）
    等太久 → 每点开一次都是一次整页 `settle()`。一条热帖几百条评论，
             光等就等掉几十分钟

所以**只采一级评论**，`parse.parse_comment_list()` 也只吐一级。

⚠️ 这不是"还没写完"，是**已经定了的取舍**，别当 bug 修回去。代价是明确的：
库里的评论区是平的，回复一条都不在。将来真要补，得先解决"怎么可靠地关掉
那块面板"，否则补出来的还是同一个坑。

## 入口是页面最底下那一栏的评论按钮

实测（`comments_collapsed.html` / `comments_modal.html`）：评论不是一开始就在
DOM 里的，要点底部操作栏那个「N 条评论」才弹出评论窗口，再滚动加载。
判据和踩过的坑见 `_open_modal`。

## 终止条件

评论弹窗滚到底会不会出现「没有更多了」**没有实测**（两份弹窗快照都没滚到底）。
所以这里只认 `selectors.END_OF_LIST_TEXTS` 那几条通用的；认不出来就退化成
"连续 N 轮无新增 + 条数核算"。**不要为了让它更快停下来就凭空加一条文案**——
加错了它永远不命中，改动看起来像优化，实际什么都没变。

## 完整性：条数核算

评论弹窗的标题写着总数（实测「394 条评论」）。和问题页一样，这是唯一能回答
"采全了没有"的判据——滚动循环只会告诉你它为什么停，不会告诉你停得对不对。

⚠️ 但「N 条评论」**含不含回复没有实测**，所以它只进 `describe()`，
**不当硬失败阈值**：拿一个语义不明的数当阈值，会变成天天误报，
而天天误报的判据等于没有判据。

真正会触发失败的是这三件**确凿**的事：

  * 弹窗没打开（一条都采不到）；
  * 弹窗里认不出评论列表面板（知乎改版了，不许闷头往下采）；
  * 滚动容器找不到、或滚动被截断（只采到首屏 / 撞上轮次上限）。

## 已确认的取舍

* **不点开折叠的评论**：用户 2026-09-26 的原话是
  「被折叠回答也不会被人看，对舆论影响很小」，那是回答；评论这边
  知乎没有折叠说法，但同理——**不额外做"更多"菜单里的展开**。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from sentinel_q.collector import content, drive, parse, scrolling, selectors
from sentinel_q.collector.session import BrowserSession
from sentinel_q.shared.urlnorm import NormalizedUrl, normalize

log = logging.getLogger(__name__)


@dataclass
class CommentsReport:
    """一条内容的评论采集结果。

    `ok` 只反映**确凿的失败**（弹窗没开 / 开了但一条都没解析出来、
    滚动容器找不到、滚动被截断）。条数对不上只报出来、不算失败——
    理由见模块开头。
    """

    url: str
    content_id: str | None = None

    opened: bool = False
    """评论弹窗有没有打开。False 的话后面全是空的，一条都没采到。"""

    inline: bool = False
    """这批是**就地展开**采的（问题页上的回答卡片），没走弹窗。

    ⚠️ `opened` 说的只是"**弹窗**开了"，而这一支根本没有弹窗，
    所以 `opened` 永远是 False——判成功/失败的地方必须带上这个字段
    （见 `ok` 与 `describe()`），否则每一条内联回答都会被当成采集故障。
    """

    no_comments: bool = False
    """入口按钮写着「添加评论」= 这条内容**一条评论都没有**。

    ⚠️ 和 `opened=False` 必须分得开：前者是正常结果，后者是采集故障，
    而两者的条数都是 0。`ok` 与 `describe()` 都按这个区分走。
    """

    entry_text: str | None = None
    """入口按钮上的原文（「1623 条评论」/「添加评论」）。**报出来是为了能人工核对**——
    判据说到底是从这段文字读出来的，它写进日志才追得回"当时按钮上到底写的什么"。"""

    declared: int | None = None
    """知乎声明的评论总数。**两处来源**：入口按钮上的「N 条评论」，
    以及打开后弹窗标题里的那个数（后者权威，读到就覆盖）。None = 两处都没读到。"""

    scroll: scrolling.ScrollOutcome | None = None
    scroll_container_found: bool = True
    """没找到滚动容器 = 弹窗滚不动，**只能采到首屏**。"""

    scroll_target: str = ""
    """我们打算滚的那个元素是谁（`drive.describe_scroller` 的输出）。

    ⚠️ **要留在报告里，不能只打一行日志**：「找到了容器」和「容器真的滚了」
    是两件事，而两者的失败日志长得一模一样。留下身份，下次实跑才不用再猜。
    """

    scroll_stuck: bool = False
    """滚了若干轮，那个元素的**位置一步都没动** = 滚的不是真正会滚的东西。

    ⚠️ 和 `scroll_container_found=False` 是**两个不同的洞**：那个是"没找到"，
    这个是"找到了但推不动"。后者以前是**完全静默**的——写不进 `scrollTop`
    不报错，循环空转到 STABLE，日志读起来像"评论本来就这么点"。
    用户 2026-09-27 看到的现象正是它：「评论本身不滚动，背后的页面在滚动」。
    """

    items: list[parse.ParsedItem] = field(default_factory=list)
    empty_bodies: int = 0
    """没有文字的评论条数——**实测真的存在**，都是纯图片评论（表情包/截图）。
    它们**不会被丢掉**（见 `parse._comment_from_node`），但要说出来。"""

    duplicates: int = 0
    """滚动重渲染导致的重复条数（按评论 ID 去重）。**校准信号。**

    ⚠️ 它现在**只**反映"页面在重复渲染"这一件事：以前二级回复的重读也会计进来，
    那个来源随二级回复一起去掉了（见模块开头）。"""

    seen: set[str] = field(default_factory=set, repr=False)

    @property
    def collected(self) -> int:
        return len(self.items)

    @property
    def missing(self) -> int:
        """相对弹窗标题声明还差多少条。声明没有就返回 0。"""
        if self.declared is None:
            return 0
        return max(0, self.declared - self.collected)

    @property
    def ok(self) -> bool:
        """这一轮算不算成功。**只看确凿的失败**，见类文档。"""
        if self.no_comments:
            return True  # 0 条评论是正常结果，不是"一条都没采到"
        # `inline` 也要算"看到了评论区"：就地展开那一支没有弹窗，`opened` 恒为 False
        if not (self.opened or self.inline) or not self.items:
            return False
        if not self.scroll_container_found or self.scroll_stuck:
            return False
        return not (self.scroll is not None and self.scroll.truncated)

    def describe(self) -> str:
        # ⚠️ 这两条必须**排在采到多少条的前面**。只说"采到 0 条"的话，读日志的人
        # 分不清是"这条内容真没评论"还是"弹窗压根没打开"——前者是正常结果，
        # 后者是采集故障，而两者的数字长得一模一样。带上入口原文才核得动。
        if self.no_comments:
            return f"{self.url}：0 条评论（入口按钮是「{self.entry_text}」）"
        # `inline` 也算采到了：就地展开没有弹窗，`opened` 恒为 False
        if not (self.opened or self.inline):
            # 措辞刻意不提"弹窗"：这条路既可能是弹窗没开出来，也可能是内联区
            # 没展开（问答页那种）。对读日志的人来说，"评论区没打开"才是
            # 两种情况下都对的那句话。
            return (
                f"❌ {self.url}：评论区没能打开，一条都没采"
                f"（入口按钮是「{self.entry_text or '没找到'}」，原因见上面的 error 日志）"
            )

        parts = [f"{self.url}：采到 {self.collected} 条一级评论"]
        if self.declared is not None:
            # 内联那支没有弹窗标题可读，这个数只来自卡片上的入口按钮
            parts.append(
                f"，{'入口按钮' if self.inline else '弹窗'}声明 {self.declared} 条"
            )
            if self.missing:
                parts.append(f"（⚠️ 差 {self.missing} 条）")
        else:
            parts.append("，⚠️ 没读到「N 条评论」，**无法核算是否采全**")

        text = "".join(parts)
        if self.duplicates:
            text += f"，重复 {self.duplicates}"
        if self.empty_bodies:
            text += f"，纯图片评论 {self.empty_bodies} 条（无文字）"
        if self.inline:
            # 不滚：这块是"评论少所以全部已展示"，`scroll` 只是为免报警填的占位。
            # 让它照常输出"已到末尾：0 轮"是**假的**，我们从没滚过。
            text += "；就地展开采的（卡片里没有「点击查看全部评论」），未滚动"
            return text
        if not self.scroll_container_found:
            text += "；❌ 弹窗里找不到滚动容器，**只采到了首屏**"
        if self.scroll_stuck:
            # ⚠️ 这一句必须**顶掉**下面的 `self.scroll.describe()`：那句会说
            #    "连续多轮无新增后停止…可能是真到底"，而这里已经有确凿证据
            #    说明根本不是到底，是**我们压根没滚动**。两句一起出现，
            #    读日志的人只会信前一句。
            rounds = self.scroll.rounds if self.scroll is not None else 0
            text += (
                f"；❌ 滚了 {rounds} 轮，但滚动目标（{self.scroll_target}）"
                "的位置**一步都没动**——我们滚的不是那个真正会滚的元素，"
                "只采到了首屏"
            )
            return text
        if self.scroll is not None:
            text += "；" + self.scroll.describe("评论")
        return text


# ── 主流程 ──────────────────────────────────────────────────────────


def extract_comments(
    session: BrowserSession,
    url: str,
    *,
    on_item: Callable[[parse.ParsedItem], None] | None = None,
    max_rounds: int = 500,
    flick_steps: int = drive.FLICK_STEPS,
    reuse_open_page: bool = False,
) -> CommentsReport:
    """打开一条内容，把它的**一级评论**采下来。**不抛异常。**

    Args:
        url: 内容页地址（回答 / 文章 / 想法 / 问题）。评论的 `parent_id`
            要挂到这条内容上，所以 URL 必须能被 `shared.urlnorm` 认出来。
        on_item: 每采到一条就回调一次。**不要等全部采完再落库**——
            一条热帖的评论要滚十几分钟，中途崩了就全没了。
        flick_steps: 一次滚动连滚几"下"。和 `search.search()` 同名同义，
            见 `_collect_first_level` 顶部关于什么时候该调小它的说明。
        reuse_open_page: 调用方**刚刚才打开过这个 URL**、页面还停在那儿时传 True，
            省掉一次导航（能力二采完正文接着采评论就是这么用的）。
            **页面不在这个 URL 上时照样会导航**，见 `_on_that_page`。
    """
    # 列的是**这个模块真正会读的**选择器（不是"相关的一堆"）：
    # 门槛定得过宽会在没必要的改动上拦住采集，定得过窄就白设了。
    # 注意 `COMMENT_MODAL` 不在这里——它只是 `MODAL_PANELS` 的来源说明，
    # 代码定位面板用的是后者（靠标题文案挑，不是靠 `.Modal-content`）。
    selectors.require_calibrated(
        "COMMENT_ITEM",
        "COMMENT_ID_ATTR",
        "COMMENT_AUTHOR_LINK",
        "COMMENT_CONTENT",
        "MODAL_PANELS",
    )

    report = CommentsReport(url=url)

    normalized = normalize(url)
    if normalized is None:
        log.error(
            "%s 不是能认出来的知乎内容页，评论没法归属（`parent_id` 要挂到内容上）。"
            "**不采**——挂错父级的评论比采不到更糟。",
            url,
        )
        return report
    report.content_id = normalized.zhihu_id

    # 页面已经在目标 URL 上就不重新导航——省一次整页加载，那正是"合并"的意义。
    session.open(
        url, navigate=not (reuse_open_page and _on_that_page(session, normalized))
    )
    session.guard()

    if not _open_modal(session, report):
        # 0 条评论也走这条分支，但**不是失败**——性质由 `_open_modal` 判，
        # 这里只管报出来（`_report_finish` 按 `no_comments` 决定喊多大声）。
        _report_finish(report)
        return report
    report.opened = True

    panel = _comments_panel(session)
    if panel is None:
        log.error(
            "%s：弹窗开了，但里面认不出评论列表面板。\n"
            "   ⚠️ 有两块面板时，靠标题文案「%s」把回复面板挑出去、剩下的就是评论列表。"
            "两块都认不出来 = 知乎改版了，**不要闷头按'第一块是评论列表'往下采**。",
            url,
            selectors.REPLY_PANEL_TITLE,
        )
        return report

    # 声明总数的位置：面板顶部那行「394 条评论」。
    # ⚠️ 必须**锚定整串** + **排除评论节点**，两重都要：
    #   实测同一条评论的正文里完全可以出现「25 条评论」这种字样，
    #   而 `find_text` 是"文档顺序里第一个命中的文本节点"——
    #   不锚定的话，声明的总数会被某条评论里的数字顶掉，
    #   然后完整性核算拿一个错的基准去对账（或者干脆对不上还说不清为什么）。
    header = drive.find_text(panel, _COMMENT_TOTAL_PATTERN, exclude=f"[{selectors.COMMENT_ID_ATTR}]")
    # ⚠️ 读不到标题就**保留入口按钮上的数字**，不要覆盖成 None：那个数同样是
    #    知乎给的，比"没读到"有用得多（完整性核算全靠它）。
    if (declared := parse.parse_comment_total(drive.element_text(header))) is not None:
        report.declared = declared

    sink = _Sink(report, report.content_id, on_item)
    _collect_first_level(session, panel, report, sink, max_rounds, flick_steps)

    _report_finish(report)
    return report


def extract_answer_comments(
    session: BrowserSession,
    url: str,
    *,
    on_item: Callable[[parse.ParsedItem], None] | None = None,
    max_rounds: int = 500,
    flick_steps: int = drive.FLICK_STEPS,
) -> CommentsReport:
    """采**问题页上某一条回答**的评论。**不导航**，页面得已经开着。**不抛异常。**

    ## 为什么和 `extract_comments` 分开

    同样是点「N 条评论」，在**单独的回答页**上直接开弹窗；在**问题页**上不是——
    它在**那张回答卡片内部**就地摊开一小块评论区（用户原话：「点击后不会打开评论页面，
    而是打开文章底部（展示一部分评论）」）。所以"点完等着找弹窗"在问题页上必然落空。

    那块内联区有两种形态，判据是**里面有没有「点击查看全部评论」**：

      * **有** → 点它才开出能力二那种弹窗，剩下照搬 `_collect_first_level`；
      * **没有** → 这块里就是全部（评论少），就地解析，**不滚动**。

    ⚠️ 第二支的完整性是**一个假设**，来自用户的口径「评论数量少就会全部展示」。
    快照（`anwserNew.html`）里没按钮的那块声明 8 条、DOM 里 7 条一级，与之吻合，
    但**没有反证**。所以那一支必须靠 `describe()` 的条数核算和人工抽查兜底。

    Args:
        url: **那条回答自己的地址**（`/answer/<ID>`）。作用域从它推出来，
            不要另传一个 ID——`content.scope_selector` 和解析用的是同一条判据，
            再传一个就有"两处各判各的"的机会。
        on_item: 每采到一条回调一次，语义同 `extract_comments`。
    """
    selectors.require_calibrated(
        "COMMENT_ITEM",
        "COMMENT_ID_ATTR",
        "COMMENT_AUTHOR_LINK",
        "COMMENT_CONTENT",
        "MODAL_PANELS",
        "INLINE_COMMENTS",
        "COMMENT_MODAL_CLOSE",
    )

    report = CommentsReport(url=url)

    normalized = normalize(url)
    if normalized is None or normalized.content_type != "answer":
        log.error(
            "%s 不是回答地址，采不了「问题页里某条回答的评论」——作用域要靠回答 ID 限定，"
            "限定不了就会点到别的卡片上，**而那不报错**。",
            url,
        )
        return report
    report.content_id = normalized.zhihu_id

    # ⚠️ 复用 `content.scope_selector`，和 `parse._content_scope` 是同一条判据
    scope = content.scope_selector(url)
    if scope is None:
        return report
    target = drive.first(session.page, scope)
    if target is None:
        log.error(
            "%s：页面上找不到这张回答卡片（%s）。多半是页面还没滚到它、"
            "或者这条回答不在本次采到的清单里。",
            url,
            scope,
        )
        return report

    found = _entry_buttons(target)
    if len(found) != 1:
        # 问题页上这个作用域**正好 1 个**候选（实测 13 张卡全对得上），
        # 所以 0 个是有具体原因的，别只说"没找到"。
        log.error(
            "%s：这张卡的评论入口有 %d 个候选（选择器 %s）。\n"
            "   0 个的常见原因是**这块评论区已经展开着**——那时按钮文案是「%s」而不是"
            "「N 条评论」，于是这里认不出来。\n"
            "   ⚠️ 不自动点「收起评论」再来一遍：那是猜，猜错会把别条内容的评论挂到这条上。",
            report.url,
            len(found),
            selectors.COMMENT_ENTRY_BUTTON,
            selectors.COLLAPSE_COMMENTS_TEXTS[0],
        )
        return report

    button, text, kind = found[0]
    report.entry_text = text

    if kind == "empty":
        report.no_comments = True
        report.declared = 0
        _report_finish(report)
        return report

    declared = parse.parse_comment_total(text)
    assert declared is not None  # `_entry_kind` 已经判过
    report.declared = declared

    if not drive.click_element(button):
        log.error("%s：评论入口点不动（文案 %r）", report.url, text)
        return report
    session.guard()

    # ⚠️⚠️ **必须轮询等它展开，不能点完看一眼。** 不给 `ready` 的 `settle()` 等的
    #    是一个**固定时长**，它自己的 docstring 就写着"保证不了任何具体元素已经在了"。
    #    2026-09-27 实跑正是死在这儿：一跑 5 条回答，**前两条**（紧跟在一趟很重的
    #    回答滚动之后、页面还在忙）在展开到位之前就被判成"卡片里找不到内联评论区"，
    #    采 0 条直接往下走——这就是"第一条的评论老是打不开"那个现象。
    #    展开是**一次 XHR**（不是纯前端状态切换），所以确实需要等。
    session.settle(ready=lambda: _comment_area(session, scope) != "")

    # ⚠️ **重新拿一次卡片。** 展开会把卡片整块重渲染，点之前那个元素句柄可能已经
    #    脱离 DOM 了——那时后面的查询会静默返回 None/False，报出来的错变成
    #    "找不到内联评论区"，和真正的原因（句柄失效）对不上。
    refreshed = drive.first(session.page, scope)
    if refreshed is not None:
        target = refreshed

    sink = _Sink(report, report.content_id, on_item)
    area = _comment_area(session, scope)

    if area == "modal":
        # 这个入口**直接开出了弹窗**，中间没有「点击查看全部评论」那一步。
        # ⚠️ 这种情况是**推出来的**，没有快照直接证明：日志里那两条大评论量
        #    （72 / 52 条）的回答点完入口后，卡片里既没有内联区也没有那个按钮。
        #    但不管成不成立，这么写都比"再去点一个不存在的按钮"对。
        try:
            _collect_open_modal(session, report, sink, max_rounds, flick_steps)
        finally:
            _close_modal(session, report)
    elif drive.has_text(target, selectors.VIEW_ALL_COMMENTS_TEXTS):
        _collect_via_modal(session, target, report, sink, max_rounds, flick_steps)
    else:
        _collect_inline(target, report, sink)

    # ⚠️ **顺序不能反。** 弹窗开着的时候它那层遮罩盖在卡片上，去点「收起评论」
    #    会被遮罩吃掉——收起不了，还要多刷一条假 warning。先关弹窗再收卡片。
    #    正常路径上 `_collect_via_modal` 的 finally 已经关过了，这里是兜底。
    _close_modal(session, report)
    _collapse_card(session, scope, report)
    _report_finish(report)
    return report


def extract_all(
    session: BrowserSession,
    urls: Iterable[str],
    *,
    on_item: Callable[[parse.ParsedItem], None] | None = None,
) -> list[CommentsReport]:
    """按 URL 清单逐个采评论。

    ⚠️ **串行 + 每次之间留停顿**（`session.pace`）。评论是最重的采集动作
    （每条内容都要开弹窗、滚几百条、点几百次），单账号并发刷是最容易被限流的
    用法，而本系统明确不搭代理池对抗限流（架构文档边界条款）。
    """
    reports: list[CommentsReport] = []
    for url in urls:
        reports.append(extract_comments(session, url, on_item=on_item))
        session.pace()
    return reports


def summarize(reports: list[CommentsReport]) -> str:
    """一批采集的汇总。**失败必须显式报出来，不能只看"跑完了"。**"""
    failed = [r for r in reports if not r.ok]
    total = sum(r.collected for r in reports)
    text = f"共 {len(reports)} 条内容、{total} 条评论；失败 {len(failed)} 条"
    if failed:
        text += "（失败的 URL 见上方的 error 日志）"
    return text


# ── 导航 ────────────────────────────────────────────────────────────


def _on_that_page(session: BrowserSession, normalized: NormalizedUrl) -> bool:
    """当前页面是不是就停在 `normalized` 这条内容上。

    ⚠️ 比的是**规范化之后**的 URL，不是字符串相等：地址栏上可能带查询参数、
      末尾斜杠、锚点，字符串比会把"就在这儿"误判成"不在这儿"，于是每次都白导航一趟。

    ⚠️ 判不了就返回 False（= 去导航）。**误判成"在"才是危险的**——那会拿别条内容的
       评论挂到这条上，采集照常成功、日志一片干净。多导航一次只花几秒。
    """
    try:
        current = normalize(session.page.url)
    except Exception:  # noqa: BLE001 - 拿不到地址栏就当"不在"，退回导航
        return False
    return current is not None and current.url == normalized.url


# ── 弹窗 ────────────────────────────────────────────────────────────

_COMMENT_TOTAL_PATTERN = r"^[\d,]+\s*条评论$"
"""弹窗标题里「394 条评论」那块的形态。**锚定整串**，理由见调用处。

`_FIND_TEXT` 匹配的是**归一化之后**的文本节点（已剥零宽字符、
空白塌缩、首尾去空），而实测那处文本节点的整串就是 `'394 条评论'`，
所以锚定不会误伤。判据和 `parse.parse_comment_total` 的 `fullmatch`
保持一致——**两处对"什么算声明总数"必须用同一条规则**，
否则会出现"驱动找到了、解析说不是"这种自相矛盾的日志。
"""


def _entry_kind(text: str) -> str | None:
    """入口文案是哪种：`"count"`（有评论）/ `"empty"`（0 条）/ None（认不出来）。"""
    if selectors.COMMENT_EMPTY_TEXT in text:
        return "empty"
    return "count" if parse.parse_comment_total(text) is not None else None


def _entry_buttons(target) -> list[tuple[Any, str, str]]:
    """作用域里所有像评论入口的按钮：`(元素, 文案, 性质)`。

    ⚠️ **判据是"这个按钮的文案"，不是"第几个按钮"。** 回答卡片上的
    `.ContentItem-action` 有六七个（赞同、收藏、分享…），按位置取必然取错。
    """
    found = []
    for el in drive.all_of(target, selectors.COMMENT_ENTRY_BUTTON):
        text = drive.element_text(el)
        kind = _entry_kind(text) if text else None
        if kind is not None:
            found.append((el, text, kind))
    return found


def _open_modal(session: BrowserSession, report: CommentsReport) -> bool:
    """点开评论弹窗，返回它开没开。**0 条评论的情况见 `report.no_comments`。**

    ## 入口是页面最底下那一栏的评论按钮（2026-09-27 改）

    原来找的是页面下方评论区里的「查看全部评论」，用户实测那不可靠：
    **评论少的时候评论区直接摊开，压根没有这个按钮**，于是每一次都"没找到入口"。
    改成点底部操作栏那个按钮，顺带拿到一个额外好处——**数字就印在按钮上**，
    所以 `declared` 在弹窗打开之前就有了。

    零评论时按钮文案是「添加评论」，**那种情况不点**：点开是个空输入框，
    不是弹窗，后面"找面板、滚列表"的流程会全部落空，而日志上看着像采到了 0 条。

    ## 为什么点击必须限定作用域

    回答页上除了目标回答，还挂着"相关推荐"的卡片，**它们各自带一个
    「N 条评论」按钮**。整页按文案点会点到别人的按钮上，于是：

      * 弹窗照常打开、评论照常采集、日志一切正常；
      * 只是采到的这一批评论**挂在错误的内容下面**。

    这是"取错了元素比取不到更危险"的又一例，所以能用
    `content.scope_selector()` 限定就一定限定。
    """
    if drive.all_of(session.page, selectors.MODAL_PANELS):
        return True  # 已经开着（比如调用方自己点过）

    scope = content.scope_selector(session.page.url)
    target = _scope_el(session, scope)

    found = _entry_buttons(target)
    if len(found) != 1:
        # ⚠️ **两个候选就报错，不挑一个。** 挑错的后果不是"采不到"，
        #    而是"拿别人的评论挂到这条内容上"——采集照常成功、日志一片干净。
        #    已知会走到这里的情形：问题页（作用域是整页，问题头一个按钮 +
        #    每个回答各一个，**还没有快照校准过该点哪个**）。
        log.error(
            "%s：评论入口有 %d 个候选（选择器 %s；作用域：%s）。\n"
            "   要么一个都没有（选择器该重新校准），要么不止一个（**分不清哪个是这条内容的**）。"
            "两个都不猜——挑错的那个会把别条内容的评论挂到这条上，而且不报错。",
            report.url,
            len(found),
            selectors.COMMENT_ENTRY_BUTTON,
            scope or "整页",
        )
        return False

    button, text, kind = found[0]
    report.entry_text = text

    if kind == "empty":
        # 0 条评论：不点。**这不是失败**，是这条内容本来就没有。
        report.no_comments = True
        report.declared = 0
        return False

    declared = parse.parse_comment_total(text)
    assert declared is not None  # `_entry_kind` 已经判过
    report.declared = declared

    if not drive.click_element(button):
        log.error("%s：评论入口点不动（文案 %r）", report.url, text)
        return False
    session.guard()
    session.settle()
    if drive.all_of(session.page, selectors.MODAL_PANELS):
        log.info("评论弹窗已打开（入口文案「%s」）", text)
        return True
    log.error(
        "%s：点了「%s」但弹窗没出来。多半是没点着，或者知乎改了弹窗的结构。",
        report.url,
        text,
    )
    return False


def _scope_el(session: BrowserSession, scope: str | None):
    """内容卡片元素；限定不了就退回整页。"""
    if not scope:
        return session.page
    el = drive.first(session.page, scope)
    if el is None:
        # 卡片没找到（页面没渲染完 / 结构变了）。退回整页比什么都不点强，
        # 但**必须报出来**：退回整页就意味着可能点到相关推荐上去。
        log.warning(
            "找不到内容卡片 %s（回答页的评论入口必须限定到目标回答上）。\n"
            "   ⚠️ 退回整页点击：**可能点到「相关推荐」的评论去**——"
            "那样采到的评论会挂在错误的内容下面，而且不报错。",
            scope,
        )
        return session.page
    return el


def _comment_area(session: BrowserSession, scope: str) -> str:
    """这张回答卡的评论区**此刻是什么形态**：`"modal"` / `"inline"` / `""`（还没出来）。

    ⚠️ **两种形态要一起等**，不能只等内联那一支：弹窗是挂在 `body` 上的 portal，
    **不在卡片里**，只在卡片里找永远找不到它——而"评论多"的那几张卡正是这种。
    等到之后拿它当分岔判据，判据才和页面上真实发生的事对得上。
    """
    if drive.all_of(session.page, selectors.MODAL_PANELS):
        return "modal"
    target = drive.first(session.page, scope)
    if target is not None and drive.first(target, selectors.INLINE_COMMENTS) is not None:
        return "inline"
    return ""


def _comments_panel(session: BrowserSession):
    """弹窗里的**评论列表面板** = 弹窗里"不含回复面板标题"的那一块。

    这样写而不是"取第一块"：回复面板是**后插进来**的，但"哪一块在前"
    是渲染顺序，不是语义。靠语义（标题）挑才稳。
    """
    pattern = "^" + selectors.REPLY_PANEL_TITLE + "$"
    panels = drive.all_of(session.page, selectors.MODAL_PANELS)
    if not panels:
        return None
    for panel in panels:
        if drive.find_text(panel, pattern, exclude="") is None:
            return panel
    return None


# ── 一级评论：滚动加载 ──────────────────────────────────────────────


def _declared_reached(report: CommentsReport) -> bool:
    """一级评论是不是已经够数了（`stop_early` 的判据，单拎出来是为了能测）。

    ⚠️ **它只在"这条内容没有任何回复"时才会成立。** 知乎那个「N 条评论」数是
    **一级 + 二级回复**的总数——校准快照里声明 9 条，实际是 8 条一级 + 1 条回复；
    而我们只采一级。所以有回复的帖子（热门帖基本都是）`collected` 永远够不到
    声明的数，照旧滚满 `stable_rounds` 轮，这一下省不掉。

    好在它**不会因此变得危险**，反而是稳的：既然「声明 >= 一级数」，
    那么「collected >= 声明」就只可能在一级**已经全采完**时才成立。
    宁可少触发，不能早触发——早触发就是静默少采，正是本项目最怕的失败。
    """
    return report.declared is not None and report.collected >= report.declared


def _collect_first_level(
    session: BrowserSession,
    panel,
    report: CommentsReport,
    sink: _Sink,
    max_rounds: int,
    flick_steps: int,
) -> None:
    """在弹窗里滚到底，把一级评论收下来。

    ⚠️ **必须滚弹窗元素，不是窗口**——`window.scrollBy` 对弹窗无效，
    这正是 `drive.scroll_flick` 写成"双形态"的原因（见那边的说明）。

    ## 滚动节奏和能力一（搜索栏）对齐

    原来是 `drive.scroll_step`（滚 400px + 冻一拍）配 `session.pace(0.5)`，
    实跑太慢。现在和能力一同款：一次 `scroll_flick` 连滚 `flick_steps` 下，
    停顿系数用共用的 `scrolling.PACE_FACTOR`——**"滚一下停一拍"重复几十次
    本来就不像人**，那是更显眼的机器节奏（理由整段写在 `PACE_FACTOR` 那儿）。

    ⚠️ **弹窗的视口比整页矮得多，一轮跨 2000px 有跨过头的风险。**
    判断标准是**卡住的轮次数**（日志里「连续 N 轮无新增」），不是总耗时：
    变多了就把 `flick_steps` 调到 3。跨过头不会丢数据（下一轮照样收得到），
    但会空转掉几轮——而这里**不抢救**（下面刻意不给 `nudge`），空转满
    `stable_rounds` 轮就直接收工，所以它现在是**安静地少采**，
    只能靠 `describe()` 里的条数核算发现。
    """
    scroller = drive.find_scroll_container(panel)
    report.scroll_container_found = scroller is not None
    if scroller is None:
        # 把现场一起打出来：光说"没找到"分不开"容器在弹窗外面"和
        # "找到了但 overflow: visible 写不进去"，那是两种改法。
        census = "\n".join(drive.describe_scroll_candidates(panel))
        log.error(
            "%s：弹窗里找不到**滚得动**的元素。\n"
            "%s\n"
            "   ⚠️ 这一轮**只能采到首屏**，别当成采全了。多半是弹窗还没渲染完，"
            "或者知乎改了弹窗的布局方式。",
            report.url,
            census,
        )
        scroller = panel  # 还是滚一下：万一它其实能滚，只是没测出来

    # ⚠️ **把"我们在滚谁"写下来。** 排"评论没滚动"这种问题时，没有这一行就只能
    #    猜：日志里"连续 N 轮无新增"在"滚错元素"和"真到底了"两种情况下一模一样。
    before = drive.probe_scroller(scroller)
    report.scroll_target = drive.describe_scroller(before)
    log.info("滚动目标：%s", report.scroll_target)

    # ⚠️ roots_only=True 是**必须的**：回复是嵌套在父评论的 `[data-id]` 里的，
    #    全部收下来的话到 Python 那边已经分不出层级了（游离片段没有祖先），
    #    于是每一条回复都会被当成一条独立的一级评论入库——**而且不报错**。
    harvester = drive.Harvester(panel, selectors.COMMENT_ITEM, roots_only=True)

    def collect() -> int:
        fresh = harvester()
        if not fresh:
            return 0
        added = 0
        for html in harvester.items[-fresh:]:
            added += sink.absorb(parse.parse_comment_list(html, sink.content_id))
        return added

    def stop_early() -> bool:
        """采够了就收工，**不再为了确认"到底"多滚三轮**。"""
        if not _declared_reached(report):
            return False
        log.info(
            "已采到 %d 条，弹窗声明的 %d 条够了，不再往下滚",
            report.collected,
            report.declared,
        )
        return True

    report.scroll = scrolling.scroll_until_exhausted(
        scroll=lambda: drive.scroll_flick(scroller, steps=flick_steps),
        at_end=lambda: drive.has_text(scroller, selectors.END_OF_LIST_TEXTS),
        collect=collect,
        stop_early=stop_early,
        # ⚠️ **刻意不给 `nudge`**（2026-09-27 项目所有者定）：评论连续 3 轮无新增就
        #    直接当到底（STABLE），不再上滚下滚抢救。评论优先级本就不高、弹窗的
        #    补加载也快，而抢救一次要多空转 3 轮，50 篇累积起来很可观。
        #    代价是**可能少采**——靠 `describe()` 的"声明 N 条 / 差 K 条"兜底核对。
        pace=lambda: session.pace(scrolling.PACE_FACTOR),
        max_rounds=max_rounds,
    )

    # ⚠️⚠️ **滚动要验，不能只看"循环跑完了"。** 目标写不进 `scrollTop` 时，
    #    整个循环**不报错**：3 轮空转 → STABLE → 日志说"连续多轮无新增后停止，
    #    可能是真到底"——而真相是我们一步都没滚、只采到了首屏。
    #    判据要求**声明数还没够**（`missing > 0`）：真到底了却刚好没动是可能的
    #    （内容就一屏），那种情况不该报错。
    after = drive.probe_scroller(scroller)
    if (
        before is not None
        and after is not None
        and before.get("offset") == after.get("offset")
        and report.missing > 0
        and report.scroll.stop_reason
        in (
            scrolling.StopReason.STABLE,
            scrolling.StopReason.GAVE_UP,
            scrolling.StopReason.MAX_ROUNDS,
        )
    ):
        report.scroll_stuck = True
        census = "\n".join(drive.describe_scroll_candidates(panel))
        log.error(
            "%s：滚了 %d 轮，但滚动目标（%s）的位置**一步都没动**（%s → %s）。\n"
            "%s\n"
            "   ⚠️ 说明我们滚的**不是那个真正会滚的元素**——评论本身不滚动，"
            "就是这里。这一轮只采到了首屏，别当成采全了。",
            report.url,
            report.scroll.rounds,
            report.scroll_target,
            before.get("offset"),
            after.get("offset"),
            census,
        )


def _collect_via_modal(
    session: BrowserSession,
    target,
    report: CommentsReport,
    sink: _Sink,
    max_rounds: int,
    flick_steps: int,
) -> None:
    """评论多的那块：点「点击查看全部评论」开出弹窗，之后照搬能力二那一套。"""
    # ⚠️ `click_text` 返回 None 就是**没点着**，必须当场处理——它的 docstring
    #    专门写了这条（旧版在搜索页点错过元素，还不报错）。
    if drive.click_text(target, selectors.VIEW_ALL_COMMENTS_TEXTS) is None:
        log.error(
            "%s：认得出「%s」但点不动它。**这条回答的评论一条都没采到**——"
            "别把 report 里的 0 当成「这条没有评论」。",
            report.url,
            selectors.VIEW_ALL_COMMENTS_TEXTS[0],
        )
        return
    session.guard()
    session.settle()

    try:
        _collect_open_modal(session, report, sink, max_rounds, flick_steps)
    finally:
        # ⚠️ **必须在 finally 里关。** 上面任何一条 `return`（认不出面板、
        #    面板里没有评论…）都会把弹窗**留在页面上**，而下一条回答的采集
        #    紧接着就在这个弹窗上做——采到的还是上一条的评论，日志一片干净。
        #    这正是"能打开、但不知道在哪里关"的那个洞。
        _close_modal(session, report)


def _collect_open_modal(
    session: BrowserSession,
    report: CommentsReport,
    sink: _Sink,
    max_rounds: int,
    flick_steps: int,
) -> None:
    """弹窗**已经开着**之后的那一段：认面板、读声明数、滚到底。"""
    panel = _comments_panel(session)
    if panel is None:
        log.error(
            "%s：点了「%s」但认不出评论列表面板（%s）。\n"
            "   ⚠️ 页面上的弹窗**不会**因此关掉，是 `finally` 兜的底；"
            "但这条回答的评论一条都没采到。",
            report.url,
            selectors.VIEW_ALL_COMMENTS_TEXTS[0],
            selectors.MODAL_PANELS,
        )
        return
    report.opened = True

    # 弹窗标题那个数比入口按钮印的更权威，读到就覆盖（同 `extract_comments`）
    header = drive.find_text(
        panel, _COMMENT_TOTAL_PATTERN, exclude=f"[{selectors.COMMENT_ID_ATTR}]"
    )
    if (declared := parse.parse_comment_total(drive.element_text(header))) is not None:
        report.declared = declared

    _collect_first_level(session, panel, report, sink, max_rounds, flick_steps)


def _collect_inline(target, report: CommentsReport, sink: _Sink) -> None:
    """评论少的那块：卡片里没有「点击查看全部评论」，就地解析，**不滚动**。

    ⚠️ 内联区在页面流里，**不是独立滚动容器**（没有自己的滚动条），
    所以这里不走滚动骨架——`scroll_until_exhausted` 那一套对它没有意义。

    ⚠️ 完整性靠的是**「没有那个按钮」这个判据**，是个假设（见
    `extract_answer_comments`）。所以这里把结论写进日志，不安静地过去。
    """
    container = drive.first(target, selectors.INLINE_COMMENTS)
    if container is None:
        # ⚠️ **这里不设 `inline`。** 设了的话 `describe()` 会照着内联那一支
        #    输出"按「这块就是全部」处理"——而实际上我们什么都没看到。
        #    保持它是 False，收尾时才会报成"评论区没能打开"。
        log.error(
            "%s：点了评论入口，但卡片里找不到内联评论区（%s）。"
            "**这条回答的评论一条都没采到。**",
            report.url,
            selectors.INLINE_COMMENTS,
        )
        return
    report.inline = True

    # roots_only 的理由同 `_collect_first_level`：回复嵌在父评论的 [data-id] 里，
    # 全收下来到 Python 那边已经分不出层级，每条回复都会被当成独立的一级评论。
    harvester = drive.Harvester(container, selectors.COMMENT_ITEM, roots_only=True)
    fresh = harvester()
    if fresh:
        for html in harvester.items[-fresh:]:
            sink.absorb(parse.parse_comment_list(html, sink.content_id))

    # `scroll` 是**为了不让 `_report_finish` 每次都报 warning 而填的占位**，
    # 不代表真滚过——`describe()` 认 `inline`，会另说一句，不会输出"已到末尾"。
    report.scroll = scrolling.ScrollOutcome(
        0, report.collected, scrolling.StopReason.END_MARKER
    )
    log.info(
        "就地展开：卡片里没有「%s」，按「这块就是全部」处理，采到 %d 条一级评论",
        selectors.VIEW_ALL_COMMENTS_TEXTS[0],
        report.collected,
    )


def _close_modal(session: BrowserSession, report: CommentsReport) -> None:
    """关掉弹窗，并**校验它真的没了**。

    ⚠️ 没关掉的后果不是"这条采坏了"，是**后面每一条都可能采坏**：下一张卡片的
    弹窗会在这块面板上接着用，采到的还是上一条的评论，而日志一片干净。
    """
    if not drive.all_of(session.page, selectors.MODAL_PANELS):
        return  # 本来就没有弹窗（正常收尾 / 调用方拿它当兜底），无事可做
    close = drive.first(session.page, selectors.COMMENT_MODAL_CLOSE)
    if close is None or not drive.click_element(close):
        log.error(
            "%s：找不到或点不动弹窗关闭按钮（%s）。\n"
            "   ⚠️ **弹窗留着会污染后面每一条回答的采集**（会采到上一条的评论），"
            "请核对这次的整个评论批次。",
            report.url,
            selectors.COMMENT_MODAL_CLOSE,
        )
        return
    session.settle()
    if drive.all_of(session.page, selectors.MODAL_PANELS):
        log.error(
            "%s：点了关闭按钮，弹窗还在。**下一张卡片的评论很可能采错。**",
            report.url,
        )


def _collapse_card(session: BrowserSession, scope: str, report: CommentsReport) -> None:
    """把展开的评论区折回去。

    ⚠️ 必须做：一个 400 条回答的问题页，内联区全摊开会把 DOM 撑爆。
    点不着只记 warning——**它不影响已经采到的数据**，不值得当失败。
    """
    target = drive.first(session.page, scope)
    if target is None:
        return
    if drive.click_text(target, selectors.COLLAPSE_COMMENTS_TEXTS) is None:
        log.warning(
            "%s：采完了但没找到「%s」，这块评论区会一直摊着（页面会越来越重）。",
            report.url,
            selectors.COLLAPSE_COMMENTS_TEXTS[0],
        )


# ── 收集 ────────────────────────────────────────────────────────────


class _Sink:
    """把解析出来的条目去重、计数、回调出去。

    去重按 **`zhihu_id`**（= 知乎自己的评论 ID），不是按内容哈希：
    同一个人把同一句话发两遍，在知乎就是两条不同的评论，不该并成一条。
    """

    def __init__(
        self,
        report: CommentsReport,
        content_id: str | None,
        on_item: Callable[[parse.ParsedItem], None] | None,
    ) -> None:
        self.report = report
        self.content_id = content_id
        self.on_item = on_item

    def absorb(self, items: Iterable[parse.ParsedItem]) -> int:
        """收下一批条目，返回**新增**条数。

        ⚠️ 撞上 `seen` 就计入 `duplicates`。以前二级回复的重读还要靠一个
        `recount_duplicates` 开关排除在统计之外，那个来源随二级回复一起去掉了
        （2026-09-27）——**现在进来的重复全都是"页面在重复渲染"**，
        那个计数器也就回到了干净的含义。
        """
        added = 0
        for item in items:
            if item is None:
                continue
            if item.zhihu_id and item.zhihu_id in self.report.seen:
                self.report.duplicates += 1
                continue
            if item.zhihu_id:
                self.report.seen.add(item.zhihu_id)
            self.report.items.append(item)
            if not item.has_body:
                self.report.empty_bodies += 1
            added += 1
            if self.on_item:
                self.on_item(item)
        return added


def _report_finish(report: CommentsReport) -> None:
    """按结论决定喊多大声。"""
    text = report.describe()
    if not report.ok:
        log.error("%s\n   ⚠️ 这批评论不完整，别当成采全了。", text)
        return
    if report.no_comments:
        # 0 条评论是正常结果，但它是个**结论**（要写进证据说明的），所以照常出声，
        # 只是不当作问题——`scroll is None` 那条分支会让它变成 warning。
        log.info(text)
        return
    if report.missing or report.scroll is None:
        log.warning(text)
        return
    log.info(text)
