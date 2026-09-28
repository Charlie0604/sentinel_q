"""能力四：提取一个问题下的全部回答。

## 排序口径（2026-09-26 用户改的口径，以这份为准）

    全量（backfill）  →  **默认排序**（= 相关热度）
    更新（update）    →  **按时间排序** + 追上旧内容就**早停**

⚠️ 这**推翻了**两处先前的说法：最初计划里是「两种模式都点按时间排序」，
中途确认过一次「全量=按最新+时间不限，更新=按最新+时间一天」。

⚠️ 随之而来一个必须讲清楚的性质变化：**「全量」不再是"全部"。**
默认排序是**相关性**排序，它给的是"最值得看的那些"，不是"所有的"。
所以本模块的"全量"语义是**尽可能多**，不是"一条不落"。
用户对这个取舍的原话是：

    至于漏了一些我认为无伤大雅，不然要处理的bug太多了

**这个取舍是可以接受的，前提是它不能被悄悄执行。** 所以下面的
`AnswersReport` 把"采到多少 / 页面声明多少 / 差多少"全部算出来并报出来——
漏可以，但必须让人知道漏了。

## 完整性：条数核算，不是滚动循环报的

用户报告：「问题页一直往下滚**不会出现"没有更多了"**，只会滚不动了」。
实测确认：`question_bottom.html` / `large_question.html` 里
「没有更多了」出现 **0 次**，而搜索页有。

所以问题页**没有显式终点**，滚动循环只可能以 `GAVE_UP` 收场，
而 `GAVE_UP` 同时对应"真采完了"和"卡在半路"两种情况
（见 `tests/test_scrolling.py::test_scroll_loop_cannot_tell_complete_from_stuck`）。
完整性**只能**另找判据，也就是 `.List-headerText` 那行声明的总数：

    采到 + 被折叠 >= 声明总数   →  采全了
    否则                        →  **不完整，必须报出来**

用的是 `>=` 而不是 `==`，这不是随手写的，实测三份快照正好说明为什么：

    快照                    声明    采到    折叠    采到+折叠
    question_bottom.html      15      15       0      15  = 15  ✓
    large_question.html       91      89       6      95  > 91  ⚠️
    question_newest.html    1735       5      15      20  < 1735（只采了首屏）

`large_question.html` 那份 **95 > 91**：知乎自己那个「N 个回答」跟页面上
真实渲染出来的条数对不齐（它是个缓存的计数）。所以 `==` 会**永远失败**，
而"多出来"是正常的，不该报错。

## 折叠回答：不点开，但要数出来

用户 2026-09-26 决定不点开折叠回答（「被折叠回答也不会被人看，
对舆论影响很小」）。这个决定本身没问题，但折叠回答**是算进声明总数里的**——
不数出来的话完整性判据永远差那几条，天天误报"没采全"。
所以下面每轮都去读一次 `CollapsedAnswers-bar`（见 `parse.parse_collapsed_text`）。

## 早停只对"单调有序"成立

更新采集按时间排序，遇到一条采过的就意味着**它下面的都更旧、也都采过**。
按相关性排序**没有**这个性质——一条采过的后面完全可能是没采过的新内容。
所以 `stop_early` 只在更新模式接上，全量模式给 `None`。
判据是"**连续** N 条都在库里"而不是"碰见一条就停"：库里可能缺了中间几条，
连续计数能容忍个别空洞。

## 不点「阅读全文」

实测 `large_question.html` 的 89 张卡片里「阅读全文」出现 **0 次**、
「展开」按钮 0 个，最长正文 8710 字、中位数 296 字——**问题页的回答卡片
没有被截断**，正文就是全文。短的那几条是真的短（「+1」「感谢分享。」）。
所以能力四不需要点开任何东西，也就没有"采到一半正文却当成全文存下来"的风险。

（唯一例外：实测有 1 条卡片 `[itemprop='text']` 是空的，`name=2986617993`。
按 `empty_bodies` 统计上报，不特殊处理。）
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from sentinel_q.collector import drive, ops, parse, scrolling, selectors
from sentinel_q.collector.session import BrowserSession

log = logging.getLogger(__name__)

QUESTION_URL = "https://www.zhihu.com/question/{qid}"

EARLY_STOP_AFTER = 5
"""更新采集时，**连续**遇到这么多条已经在库里的回答就收工。

不是 1：库里可能缺了中间几条（上一轮卡住了、或者按热度采的时候跳过了一些），
连续计数能容忍个别空洞，不会因为撞见一条旧的就把后面新的全丢掉。
"""


class SortNotApplied(RuntimeError):
    """排序点了但没生效。**宁可停下，也不要拿错排序的数据继续跑。**

    排序点击失败是**静默**的：采集照样跑完、条数正常、入库正常，
    只是内容是一份按别的顺序排的数据。全量模式下这意味着热门回答没采到，
    更新模式下意味着新回答没采到——都属于架构文档 4.1 列为"不可接受"的
    那类失败（看起来一切正常，采到的却是另一回事）。
    """


@dataclass(frozen=True)
class AnswerSpec:
    """一次问题采集的口径。"""

    question_id: str
    mode: ops.TaskMode = "backfill"

    @property
    def url(self) -> str:
        return QUESTION_URL.format(qid=self.question_id)

    @property
    def sort_text(self) -> str:
        """该点哪个排序。

        ⚠️ 全量点**默认排序**——那本来就是知乎的初始状态，所以
        `_select_sort` 在全量模式下通常一次都不点，只回读确认。
        """
        if self.mode == "backfill":
            return selectors.QUESTION_SORT_DEFAULT_TEXT
        return selectors.QUESTION_SORT_NEWEST_TEXT

    @property
    def early_stop(self) -> bool:
        """能不能用"追上旧内容"来早停。**只有按时间排序的更新模式可以。**"""
        return self.mode == "update"


@dataclass
class KnownRun:
    """连续遇到多少条"库里已经有了"的回答。更新采集的早停判据。

    `is_known` 由调用方给（`storage.repo` 的查重），本模块**不 import 存储层**——
    模块间只通过 `shared.models` 和数据库通信，这条纪律见架构文档 7.3。
    """

    is_known: Callable[[str], bool] | None = None
    threshold: int = EARLY_STOP_AFTER
    run: int = 0
    first_url: str | None = None
    """这一串"已采过"里的第一条。早停时报出来，便于人工核对判断对不对。"""

    def observe(self, url: str) -> bool:
        """看一条回答，返回"该早停了吗"。"""
        if self.is_known is None:
            return False
        if not self.is_known(url):
            self.run = 0
            return False
        if self.run == 0:
            self.first_url = url
        self.run += 1
        return self.run >= self.threshold

    @property
    def tripped(self) -> bool:
        return self.run >= self.threshold


@dataclass
class AnswersReport:
    """一次问题采集的结果统计。

    ⚠️ **`ok` 为 False 不等于"这次白跑了"**——数据还是有价值的，
    只是不完整。用户明确说了漏一点无伤大雅。这里的作用是
    **把"漏了"变成一次可见的报错，而不是一次安静的成功**。
    """

    question_id: str
    url: str
    mode: ops.TaskMode

    declared: int | None = None
    """页面声明的回答总数（`.List-headerText`）。None = 页面上没有这一行，
    **无法核算完整性**——那是"不知道"，不是"没问题"。"""

    collapsed: int = 0
    """被折叠的回答数（每轮刷新，见 `parse.parse_collapsed_text`）。
    它算在声明总数里，所以不数出来完整性判据会对不上。"""

    sort_readback: str | None = None
    """点完排序后回读到的 combobox 文本。**这是"排序真的生效了"的唯一证据。**"""

    scroll: scrolling.ScrollOutcome | None = None
    items: list[parse.ParsedItem] = field(default_factory=list)

    duplicates: int = 0
    """卡片重复出现的条数（同一回答被渲染两次，按 zhihu_id 去重）。"""

    unparsed: int = 0
    """解析不出 URL 的卡片数。**这是校准信号**——正常应该是 0。"""

    empty_bodies: int = 0
    """正文为空的回答数（实测确实存在，见模块开头）。"""

    early_stop: bool = False
    early_stop_at: str | None = None

    @property
    def collected(self) -> int:
        return len(self.items)

    @property
    def accounted(self) -> int:
        """采到的 + 被折叠的 = 能对上账的总数。"""
        return self.collected + self.collapsed

    @property
    def complete(self) -> bool | None:
        """是否采全。**None = 页面没给总数，核算不了。**"""
        if self.declared is None:
            return None
        return self.accounted >= self.declared

    @property
    def missing(self) -> int:
        """还差多少条。核算不了时返回 0（并靠 `complete is None` 表达）。"""
        if self.declared is None:
            return 0
        return max(0, self.declared - self.accounted)

    @property
    def ok(self) -> bool:
        """这一轮算不算成功。

        条数对上就算成功，**不管滚动循环是怎么停的**——`GAVE_UP` 恰好是
        问题页的正常终态（那儿永远没有"没有更多了"），所以不能一看到它就报错。
        `GAVE_UP` 的意义由条数核算来裁决。
        """
        if self.complete is True:
            return True
        if self.early_stop:
            return True  # 更新模式的正常收场：上面这段是新的，下面都是旧的
        return False

    def describe(self) -> str:
        parts = [
            f"问题 {self.question_id}「{self.mode}」采到 {self.collected} 条"
            f"（页面声明 {self.declared if self.declared is not None else '未知'}"
        ]
        if self.collapsed:
            parts.append(f"，其中折叠 {self.collapsed} 条未采")
        parts.append("）")
        text = "".join(parts)

        if self.sort_readback:
            text += f"，排序回读「{self.sort_readback}」"
        if self.duplicates:
            text += f"，页内重复 {self.duplicates}"
        if self.empty_bodies:
            text += f"，正文为空 {self.empty_bodies}"
        if self.unparsed:
            text += f"，⚠️ 解析不出 {self.unparsed}"

        if self.early_stop:
            # 早停时**不能**再报"不完整"——那是设计内的收场。
            # 但也得说清楚是哪条触发的，好让人核对这个判断对不对。
            return f"{text}；⏹ 早停：已追上以前采过的内容（起始于 {self.early_stop_at}）"

        match self.complete:
            case True:
                text += "；✅ 条数与声明一致"
            case None:
                text += "；⚠️ 页面上没有「N 个回答」，**无法核算是否采全**"
            case False:
                text += f"；❌ **不完整**：还差 {self.missing} 条，别当成采完了"

        if self.scroll is not None:
            text += "；" + self.scroll.describe("回答")
        return text


# ── 主流程 ──────────────────────────────────────────────────────────


def extract_answers(
    session: BrowserSession,
    spec: AnswerSpec,
    *,
    is_known: Callable[[str], bool] | None = None,
    on_item: Callable[[parse.ParsedItem], None] | None = None,
    early_stop_after: int = EARLY_STOP_AFTER,
    max_rounds: int = 500,
    flick_steps: int = drive.FLICK_STEPS,
    pace_factor: float = scrolling.PACE_FACTOR,
) -> AnswersReport:
    """打开问题页，按模式选排序，滚到采不动为止，返回全部回答。

    Args:
        is_known: 查重回调——"这条 URL 库里已经有了吗"。**只有传了它，
            更新模式才会早停**；不传就一路采到底（全量模式本来就不该早停）。
        on_item: 每采到一条就回调一次。**不要等全部采完再落库**：
            一个大问题要滚十几分钟，中途崩了就全没了。
        early_stop_after: 连续多少条已采过的算追上旧内容。
        flick_steps: 每轮拨几下滚轮（见 `drive.scroll_flick`）。调大 = 每轮
            跨得远、跑得快，**也更容易跨过没触发懒加载的那一段**；
            判断标准是日志里「连续 N 轮无新增」变没变多，不是总耗时。
        pace_factor: 传给 `session.pace` 的系数。见 `scrolling.PACE_FACTOR`。
    """
    selectors.require_calibrated(
        "ANSWER_ITEM",
        "ANSWER_ID_ATTR",
        "RICH_TEXT",
        "QUESTION_TOTAL",
        "COLLAPSED_ANSWERS_BAR",
        "QUESTION_SORT_BUTTON",
        "QUESTION_SORT_OPTION",
    )

    report = AnswersReport(
        question_id=spec.question_id, url=spec.url, mode=spec.mode
    )

    session.open(spec.url)

    # 声明总数。**在滚动之前读**——它在吸顶栏里，滚下去就找不到了。
    report.declared = parse.parse_question_total(drive.page_html(session.page))
    if report.declared is None:
        log.warning(
            "问题 %s 的页面上没有「N 个回答」那一行，**这一轮无法核算是否采全**。"
            "多半是 %s 需要重新校准。",
            spec.question_id,
            selectors.QUESTION_TOTAL,
        )
    else:
        log.info(
            "问题 %s 声明 %d 个回答，目标排序「%s」",
            spec.question_id,
            report.declared,
            spec.sort_text,
        )

    report.sort_readback = _select_sort(session, spec)

    # ⚠️ Harvester 必须在**排序之后**建：点了排序整个回答列表会重渲染，
    #    之前抓到的元素句柄全部失效。和 search.py 里"筛选之后才能建
    #    Harvester"是同一个坑。
    harvester = drive.Harvester(session.page, selectors.ANSWER_ITEM)

    seen: set[str] = set()
    known = KnownRun(
        is_known=is_known if spec.early_stop else None,
        threshold=early_stop_after,
    )

    def collect() -> int:
        """收一轮。返回**本轮新增的回答条数**。

        返回的是"新增回答数"而不是"新增卡片数"：一轮里如果全是已经见过的
        回答，那对采集没有推进，应当计入停滞——否则页面反复重渲染同一批卡片
        会让循环误以为一直在加载，一路空转到 max_rounds。
        """
        fresh = harvester()
        if not fresh:
            return 0

        added = 0
        for html in harvester.items[-fresh:]:
            item = parse.parse_answer_item(html, spec.question_id)
            if item is None:
                report.unparsed += 1
                continue
            if item.zhihu_id in seen:
                report.duplicates += 1
                continue
            seen.add(item.zhihu_id)
            report.items.append(item)
            added += 1
            if not item.has_body:
                report.empty_bodies += 1
            if on_item:
                on_item(item)
            if known.observe(item.url):
                report.early_stop = True
                report.early_stop_at = known.first_url
        return added

    def stop_early() -> bool:
        """还要不要继续滚。

        两个判据，**每轮都重新算**——尤其是折叠数：那一行是浮层，
        首屏读的时候可能还没渲染出来（实测滚到底才有），
        只在开头读一次的话完整性判据会永远差那几条。
        """
        # 折叠数是浮层，会随滚动出现/消失，每轮刷新
        report.collapsed = parse.parse_collapsed_text(
            drive.text_of(session.page, selectors.COLLAPSED_ANSWERS_BAR)
        )

        if report.early_stop:
            return True

        # 用户要的第一条判据：「根据回答的数量以及我们采集到的数量进行对比
        # 来判断是否采集到足够的内容」。够了就别再滚了。
        if report.complete is True:
            log.info(
                "已采到 %d 条 + 折叠 %d 条 >= 声明的 %d 条，不用再滚了",
                report.collected,
                report.collapsed,
                report.declared,
            )
            return True
        return False

    # ⚠️ 节奏和**能力一（搜索页）完全一致**，2026-09-27 用户要求对齐。
    #    原来这里是 `scroll_step`（滚 400px + 冻 1.5~4.0 秒）——
    #    和搜索页以前那套一样，"滚一下停一拍"重复几十次，**既慢又不像人**
    #    （理由整段写在 `scrolling.PACE_FACTOR` 那儿）。
    #    现在换成"连拨几下滚轮再停"：默认 5×400=2000px、约 0.35 秒。
    #
    # ⚠️ 但**不要 scroll_bottom**（一步跳到底）：用户实测「快速的滚动到底部
    #    它会不加载」——知乎的懒加载挂在滚动事件上，一步到底只产生一次事件。
    report.scroll = scrolling.scroll_until_exhausted(
        scroll=lambda: drive.scroll_flick(session.page, steps=flick_steps),
        at_end=lambda: drive.has_text(session.page, selectors.END_OF_LIST_TEXTS),
        collect=collect,
        nudge=lambda: drive.nudge(session.page),
        stop_early=stop_early,
        pace=lambda: session.pace(pace_factor),
        max_rounds=max_rounds,
    )

    _report_finish(report)
    return report


# ── 内部 ────────────────────────────────────────────────────────────


def _select_sort(session: BrowserSession, spec: AnswerSpec) -> str:
    """选排序并**回读确认**。回读不过就重试一次，再不过就抛。

    回读比点击重要得多：点击失败是静默的，回读是唯一能把静默失败变成
    可见失败的地方。实测 combobox 的文本会如实反映当前排序
    （`anwser_sort_by_time.html` 点完之后读出来就是「按时间排序」）。
    """
    want = spec.sort_text

    current = drive.text_of(session.page, selectors.QUESTION_SORT_BUTTON)
    if current and want in current:
        # 全量模式几乎总是走这一支：默认排序本来就是知乎的初始状态。
        log.info("排序已经是「%s」，不用点（回读：%r）", want, current)
        return current

    for attempt in (1, 2):
        picked = drive.click_option(
            session.page,
            texts=(want,),
            option_selector=selectors.QUESTION_SORT_OPTION,
            open_button=selectors.QUESTION_SORT_BUTTON,
        )
        if picked is None:
            log.warning("第 %d 次没在下拉里找到「%s」这个选项", attempt, want)
        session.guard()
        session.settle()

        current = drive.text_of(session.page, selectors.QUESTION_SORT_BUTTON)
        if current and want in current:
            log.info("排序已切换为「%s」（回读：%r）", want, current)
            return current
        log.warning("第 %d 次点击后回读为 %r，重试", attempt, current)

    raise SortNotApplied(
        f"问题 {spec.question_id} 的排序切不到「{want}」（两次都没成功）。\n"
        f"   最后回读到的 combobox 文本是 {current!r}。\n"
        "   ⚠️ 不要忽略这个错误继续跑：排序没生效时采集照样会「成功」完成，\n"
        "   但拿到的是一份排序错误的数据，而且不会有任何报错。\n"
        f"   多半是 selectors.QUESTION_SORT_OPTION / QUESTION_SORT_BUTTON 需要重新校准。"
    )


def _report_finish(report: AnswersReport) -> None:
    """按结论决定喊多大声。**成功不用喊，不成功必须喊。**"""
    text = report.describe()
    if report.ok:
        log.info(text)
        return
    if report.complete is None:
        log.warning("%s", text)
        return
    log.error(
        "%s\n   ⚠️ 这批数据不完整。漏采本身可以接受（用户已确认），"
        "但**不能不知道它漏了**。\n"
        "   常见原因：滚动被卡住（看上面的 GAVE_UP / max_rounds）、"
        "或 %s 选窄了只匹配到一部分卡片。",
        text,
        selectors.ANSWER_ITEM,
    )
