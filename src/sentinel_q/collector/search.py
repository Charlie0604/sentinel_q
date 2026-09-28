"""能力一：搜索并提取搜索页的全部 URL。

## ★ 搜索页给的是**样本**，不是全集——所以全量靠"多档组合"换覆盖面

2026-09-26 实跑量出来的一件事，本模块的整个设计都建立在它上面：

- 一个关键词、**不限时间**，滚到底也只有 153 条（页面自己说「没有更多了」），
  这对一个热门话题来说少得离谱；
- 「不限时间」和「一天内」两档的 URL 集**几乎不相交**（共有 4 条，
  各自独有的有 149 / 176 条）——而「一天内」按定义该是「不限时间」的**子集**。

两条合起来只有一个解释：**搜索页是推荐接口，不是检索接口。**
它按当前筛选条件吐一个**有上限的候选样本**（约 150~200 条），
换一档筛选，**成员**就重洗一遍，不只是顺序变。

所以全量的做法不是"把一档滚得更深"（滚不动，它自己就到底了），
而是**尽量多造几档组合，再把结果并起来**。这就是 `BACKFILL_COMBOS`。

⚠️ **但这仍然不是全集。** 综合排序没推给你的老内容，这里也拿不到。
真正兜底的那一层是**问题页**（架构文档 3.5 阶段三：判定相关的问题，
把它下面的回答**全量**采一遍）。搜索页负责"发现有哪些问题"，
问题页负责"这个问题下有什么"。别把这里的条数当成覆盖度。

## 采什么由"任务类型"决定，不是由参数决定

搜索页的筛选口径（**已确认，与能力四不同**）：

    全量（backfill） = 排序「综合排序」+ 类型 × 时间 共十档（见下）
    更新（update）   = 排序「最新发布」+ 时间「一天内」+ 类型「不限类型」

**全量固定「综合排序」，是刻意的**：用户 2026-09-26 的决定——综合排序背后是
知乎的推荐算法，本身就反映了大众对哪篇东西的关注度，那是一份有价值的信息，
不该被"最新发布"冲掉。

**更新必须点「最新发布」**——今天发的内容今天搜不到，明天再说，监测就断了。
全量口径下这一档不参与（十档里排序是固定的），所以更新路径是它唯一的入口。

⚠️ **由此有一个已知的覆盖缺口，写在这里免得将来被当成 bug 查：**
只在「最新发布」下才浮出来的**老内容**（发表超过一天、综合排序又不推它），
全量的十档和更新的单档都碰不到。这是"固定综合排序"这个决定换来的代价，
代价本身小于"全量口径失去关注度信息"的收益——但它是**已知**的，不是没想到。

⚠️ **能力四（问题回答）的口径和这里相反**，见 `answers.py`：
那边全量用**默认排序**、更新用**按时间排序**。两者不一样是刻意的，
**不要"统一"**——搜索页找的是"搜得到的内容"，问题页找的是"这个问题下的回答"。

## 三个组都要管，而且只能**按组**定位选项

实测筛选面板是**三组**，顺序固定：

    组0 类型  不限类型 / 只看回答 / 只看文章 / 只看视频
    组1 排序  综合排序 / 最多赞同 / 最新发布
    组2 时间  不限时间 / 一天内 / 一周内 / 一月内 / 三月内 / 半年内 / 一年内

组0 **必须管，而且要一档一档地摆**：
它是账号级的残留状态（上次跑剩下的「只看回答」会让文章一条都进不来，
而日志上一切正常），同时它也是全量十档里那个"类型"维度的来源。

⚠️ **按文案在整页找会串台**：组0 有「不限**类型**」、组2 有「不限**时间**」。
所以定位一个选项永远是"**先按组取下标，再在那一组里按文案找**"。

⚠️ 「只看视频」**不采**：`shared.models` 的 content_type 枚举是
question / answer / article / thought / comment，**没有 video**。
采回来也存不进去。

## 筛选点完必须回读验证

这是本模块唯一一处"多点一次也要做"的冗余。原因：
**筛选点击静默失败不会报错**，它只是返回一份口径错误的结果集。
采集如实完成、条数正常、入库正常，只是内容不对——属于架构文档 4.1
明确列为"不可接受"的静默失败。所以点完要回读那一组当前激活的标签，
不对就重试，再不对就抛异常停下。

## 「搜不到东西」的页面**不是空的**

搜一串乱码时知乎不会说"没有找到"，而是甩一个 AI 直答 + 一个叫
「**内容发现**」的推荐板块，里面照样有十几条 URL 完全合法的知乎内容。
不知道这件事的话，采集会把这些推荐内容当成"搜索命中的结果"入库并显示成功。
所以这里检测那几个标志，**照常采、但响亮地报出来**（见 `SearchReport.empty_result`）。

## 产出

`UrlEntry` 流，落进 `runtime/ops/<run>/urls.jsonl`（阶段一的产品）。
**这里不做跨批次的持久去重**——按批查重是入库前的成本闸门（3.9 第 3 条），
由 `fact_content.url` 的唯一约束说了算。这里的 set 只防同一页同一条被 emit 两次。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import quote

from sentinel_q.collector import drive, ops, parse, scrolling, selectors
from sentinel_q.collector.session import BrowserSession

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.zhihu.com/search?type=content&q={query}"

_PANEL_TIMEOUT = 3.0
"""等筛选面板进 DOM 的秒数。见 `_wait_for_panel`。

纯前端的一次 React 状态切换，正常是几十毫秒的事。给 3 秒是留着给
低配机器/页面卡顿，不是"预期它要 3 秒"。"""

_CLICK_ATTEMPTS = 3
"""一组筛选最多点几次。

**3 而不是 2**：现在的循环是"点完立刻回读"，每次尝试都有意义；
早先只读循环开头，第 2 次点击的结果永远不会被看到，多给几次也是白给。"""

FILTER_ENTRY_TEXT = "筛选"
"""筛选面板入口的文案。⚠️ 它是个 `<div>`，不是 `<button>`——见
`selectors.SEARCH_FILTER_ENTRY`。`drive.click_text` 的第二条路（按文案找元素）
就是为它写的；只有第一条路的话这里永远点不着。"""

PACE_FACTOR = scrolling.PACE_FACTOR
"""滚动循环里每轮之间的停顿系数。**定义已经搬到 `scrolling.py`**（2026-09-27）。

搬家的理由：评论区（能力三）现在和搜索共用同一套滚动骨架，
"每轮停多久"属于骨架、不属于某一个能力。这里留个名字是为了让
`search.PACE_FACTOR` 这个已有写法继续可用——改动点少一处是一处。
理由和调参的注意事项见 `scrolling.PACE_FACTOR`。
"""


class FilterNotApplied(RuntimeError):
    """筛选点了但没生效。**宁可停下，也不要拿错口径的数据继续跑。**

    和 `answers.SortNotApplied` 是同一类错误的两个现场，两边都抛异常而不是
    只记日志，理由一样：点击失败是**静默**的，采集会照常"成功"完成。
    """


@dataclass(frozen=True)
class SearchSpec:
    """一次搜索任务的口径。

    `sort` / `time` / `type` 是**显式覆盖**，用来绕开 `mode` 直接指定某一档筛选。
    存的是**页面上的文案原文**（如「综合排序」），不是枚举。

    全量采集的十档组合就是靠这三个覆盖逐档拼出来的，见 `BACKFILL_COMBOS`。

    ⚠️ 覆盖值在 `__post_init__` 里就校验，**不等开浏览器**。写错一个字的代价
    本该是立刻报错，而不是等十分钟后 `_apply_filters` 报 FilterNotApplied。
    这条不是假想：用户口述全量梯子时说的是「三个月内」，而页面上实测是
    「**三月内**」——少了这个字，校验会在开浏览器之前就拦下来。

    ⚠️ 字段名 `type` 遮蔽了内建函数 `type`。**刻意保留**：它和 `sort` / `time`
    是同一层次的三个维度，换成 `kind` 之类的名字反而要读者每次多绕一道。
    遮蔽只发生在类体作用域内，这个类里没有用到 `type()`。
    """

    keyword: str
    mode: ops.TaskMode = "backfill"
    sort: str | None = None
    time: str | None = None
    type: str | None = None

    def __post_init__(self) -> None:
        # 三个维度收一张表：加一维就加一行，不会漏掉校验
        for label, value, options in (
            ("排序", self.sort, selectors.FILTER_SORT_OPTIONS),
            ("时间", self.time, selectors.FILTER_TIME_OPTIONS),
            ("类型", self.type, selectors.FILTER_TYPE_OPTIONS),
        ):
            if value is not None and value not in options:
                raise ValueError(f"{label}「{value}」不是页面上的选项。实测只有：{list(options)}")

    @property
    def type_filter(self) -> str:
        """组0（类型）的**页面原文**。默认「不限类型」。

        ⚠️ **默认值不能改成别的。** 它是唯一能看到**新问题**的一档
        （只有 `/question/<id>`、还没有回答的链接），更新采集靠的就是它。
        全量那十档靠显式覆盖逐档指定「只看文章」/「只看回答」——
        那是组合表的事，不该改这里的默认。
        """
        if self.type is not None:
            return self.type
        return selectors.FILTER_TYPE_UNLIMITED

    @property
    def time_filter(self) -> str:
        """时间范围的**页面原文**。全量=不限时间，更新=一天内。

        ⚠️ 文案是实测值，带后缀。早先猜的「不限」「一天」在页面上根本不存在，
        而"点不着"在这里的表现是 `click_text` 返回 None（有人处理）——
        比默默点错好，但白跑一轮。
        """
        if self.time is not None:
            return self.time
        if self.mode == "backfill":
            return selectors.FILTER_TIME_UNLIMITED
        return selectors.FILTER_TIME_DAY

    @property
    def sort_filter(self) -> str:
        """排序。**全量「综合排序」、更新「最新发布」**（见模块开头）。

        ⚠️ 2026-09-26 **改过一次，方向是反的，别照着旧注释想**：
           早先两种模式都点「最新发布」，理由写的是"不点的话今天发的内容
           要等它热起来才进得来"。用户随后定了全量口径——**综合排序本身
           就是一份信号**（推荐算法背后是大众关注度），不该被冲掉。
           于是全量改回页面默认档。

        ⚠️ 全量这一档意味着**组1 通常一下都不用点**（页面默认就是它），
           `_apply_filters` 会直接跳过这一组。这是对的，不是漏了：
           它仍然会**回读**确认，所以上次跑剩下的「最新发布」照样会被掰回来。
        """
        if self.sort is not None:
            return self.sort
        if self.mode == "backfill":
            return selectors.FILTER_SORT_DEFAULT
        return selectors.FILTER_SORT_NEWEST

    @property
    def url(self) -> str:
        return SEARCH_URL.format(query=quote(self.keyword))


#: 全量要跑的组0 两档。**顺序就是用户口述的顺序**（先文章、再回答）。
#:
#: ⚠️ 「不限类型」不在里面，这是刻意的：全量跑它等于把文章和回答混在一起
#: 再采一遍，而那一档的产出**是前两档的子集**（同样的内容，少了类型约束而已），
#: 白花一轮。它留给更新路径——那里它是唯一能看到**新问题**
#: （只有 `/question/<id>`、还没有回答的链接）的一档。
BACKFILL_TYPES: tuple[str, ...] = (
    selectors.FILTER_TYPE_ARTICLE,
    selectors.FILTER_TYPE_ANSWER,
)

#: 全量的时间梯子，**从宽到窄**。
#:
#: 用户 2026-09-26 口述的是「不限时间，一年内，半年内，三月内，一周内」。
#: 这里多一档「**一月内**」，两个理由：
#:
#: 1. 它是页面上**实测存在**的选项（`selectors.FILTER_TIME_OPTIONS`），
#:    不加白不加；
#: 2. 用户的口述里「一周内」和「三月内」之间空了一大段，而那段正是
#:    "已经有点热、但还没老"的内容——综合排序最容易漏掉的就是这一段。
#:
#: 代价是多 2 档（两种类型各一轮），约等于成本 +20%。用户的原则是
#: 「尽可能创造更多的组合」，所以这个加法是顺着原则走的。
TIME_LADDER: tuple[str, ...] = (
    selectors.FILTER_TIME_UNLIMITED,
    selectors.FILTER_TIME_YEAR,
    selectors.FILTER_TIME_HALF_YEAR,
    selectors.FILTER_TIME_QUARTER,
    selectors.FILTER_TIME_MONTH,
    selectors.FILTER_TIME_WEEK,
)


def backfill_specs(keyword: str) -> list[SearchSpec]:
    """全量的组合清单：**类型 × 时间 = 2 × 6 = 12 档**，排序固定「综合排序」。

    ⚠️ **别把它当成"12 次同样的搜索"。** 每一档拿到的是同一批内容里
    **不同的一个样本**（见模块开头那段实测）——并起来才是全量的产出。
    反过来说，如果哪一档的结果和别档高度重合，那就不是"这批内容很热门"，
    而是**筛选没生效**，见 `BackfillReport.overlap_ratio`。

    ⚠️ 排序显式写成「综合排序」，即使它正是 `mode="backfill"` 推出来的默认值。
    这一处冗余是故意的：这张表是**全量口径的定义**，将来谁动了
    `sort_filter` 的默认档，这张表也不该跟着悄悄漂走。
    """
    return [
        SearchSpec(
            keyword,
            "backfill",
            sort=selectors.FILTER_SORT_DEFAULT,
            type=kind,
            time=span,
        )
        for kind in BACKFILL_TYPES
        for span in TIME_LADDER
    ]


def update_spec(keyword: str) -> SearchSpec:
    """更新的组合：**只有一档**——「最新发布」+「一天内」+「不限类型」。

    ⚠️ `mode="update"` 已经把这三档全推出来了，这个函数存在的意义是
    **把口径写成一个显式的东西**：更新路径只该有一种组合，
    而"只有一种"这件事值得有个名字，不该藏在 `SearchSpec` 的属性默认值里。

    ⚠️ 类型那一档必须是「不限类型」：它是唯一能看到**新问题**的一档。
    换成「只看回答」的话，还没有回答的新问题会整批消失，而日志上一切正常。
    """
    return SearchSpec(keyword, "update")


@dataclass
class SearchReport:
    """一次搜索的结果统计。

    三个 skip 计数**必须报出来**：它们正常时是 0 或很小的数，
    突然变大意味着选择器选宽了或选窄了——这是校准期最重要的信号。
    """

    keyword: str
    urls: list[ops.UrlEntry] = field(default_factory=list)
    duplicates: int = 0
    """同一轮里重复出现的条数（滚动时 DOM 重渲染会重复吐同一张卡片）。"""

    skipped_no_link: int = 0
    """卡片里没找到链接——多半是选择器没选中，是校准信号。"""

    skipped_not_content: int = 0
    """有链接但不是内容页（话题/用户/站外），正常。"""

    filters_readback: tuple[str | None, ...] = ()
    """点完筛选后三个组各自激活的标签。**这是"筛选真的生效了"的唯一证据**，
    留在报告里便于事后核对这一批数据是什么口径采的。"""

    empty_result: bool = False
    """⭐ 命中了「内容发现」这类标志——**这个关键词没有搜索结果**，
    采到的是知乎塞给你的推荐内容（见模块开头）。"""

    scroll: scrolling.ScrollOutcome | None = None

    @property
    def found(self) -> int:
        return len(self.urls)

    def describe(self) -> str:
        parts = [f"「{self.keyword}」抓到 {self.found} 条"]
        if self.filters_readback:
            parts.append("筛选 " + "/".join(t or "?" for t in self.filters_readback))
        if self.duplicates:
            parts.append(f"页内重复 {self.duplicates}")
        if self.skipped_no_link:
            parts.append(f"⚠️ 无链接 {self.skipped_no_link}")
        if self.skipped_not_content:
            parts.append(f"非内容链接 {self.skipped_not_content}")
        if self.scroll is not None:
            parts.append(self.scroll.describe("搜索结果"))
        return "，".join(parts)

    def warn_if_empty(self) -> None:
        """搜不到东西时**响亮地报出来**。

        不报的话，这些推荐内容会被当成"命中关键词的搜索结果"入库：
        条数正常、URL 合法、一切看起来都对，只有内容是无关的。
        这正是架构文档 4.1 点名的那类失败。

        刻意**不抛异常**：数据本身（推荐内容）仍然是要采的（用户要的是
        提到关键词的公开内容），错的只是"它是什么"这个标签。
        """
        if not self.empty_result:
            return
        log.warning(
            "⚠️ 「%s」**没有搜索结果**：页面上出现了 %s——"
            "这 %d 条是知乎的推荐内容，不是命中关键词的结果。"
            "要么换关键词，要么在下游按内容再筛一遍。",
            self.keyword,
            "/".join(selectors.SEARCH_EMPTY_TEXTS),
            self.found,
        )


def search(
    session: BrowserSession,
    spec: SearchSpec,
    *,
    on_batch: Callable[[list[ops.UrlEntry]], None] | None = None,
    max_rounds: int = 500,
    flick_steps: int = drive.FLICK_STEPS,
    pace_factor: float = PACE_FACTOR,
) -> SearchReport:
    """搜一个关键词，滚到底，返回全部内容 URL。

    `on_batch` 每轮被调一次，用来把 URL 立刻追加进 `urls.jsonl`——
    **不要等全部搜完再写**：一轮搜索可能跑十几分钟，中途崩了就全没了。

    Args:
        flick_steps: 每轮拨几下滚轮（见 `drive.scroll_flick`）。调大 = 每轮
            跨得更远、轮数更少、更快，但跨过头的风险也更大。
            ⚠️ 判断调大了有没有副作用的指标是**卡住的轮次数**，不是总耗时。
        pace_factor: 传给 `session.pace` 的系数。见 `PACE_FACTOR`。
    """
    selectors.require_calibrated(
        "SEARCH_RESULT_ITEM",
        "SEARCH_RESULT_LINK",
        "SEARCH_FILTER_ENTRY",
        "FILTER_GROUP",
        "FILTER_TAG_ACTIVE",
    )

    report = SearchReport(keyword=spec.keyword)

    session.open(spec.url)
    report.filters_readback = _apply_filters(session, spec)

    # 「搜不到东西」的检测放在滚动之前：那个板块在第一屏就有，
    # 而且越早报出来，越早能让人换关键词。
    page_html = drive.page_html(session.page)
    report.empty_result = any(text in page_html for text in selectors.SEARCH_EMPTY_TEXTS)
    report.warn_if_empty()

    # ⚠️ Harvester 必须在筛选**之后**建：点了筛选，结果列表整个重渲染，
    #    之前抓到的元素句柄全部失效。
    harvester = drive.Harvester(session.page, selectors.SEARCH_RESULT_ITEM)

    def collect() -> int:
        # 只交本轮新增的那一段给解析器。整体重解析会让 duplicates 变成 O(n²)：
        # 第 10 轮会把前 9 轮已解析过的东西再数一遍"重复"。
        before = len(harvester.items)
        fresh = harvester()
        if fresh:
            batch = _parse_batch(harvester.items[before:], spec, report)
            report.urls.extend(batch)
            if on_batch and batch:
                on_batch(batch)
        return fresh

    report.scroll = scrolling.scroll_until_exhausted(
        # 拨滚轮：连着快滚几下再停。**不要一步跳到底**——
        # 用户实测「快速的滚动到底部它会不加载」。
        scroll=lambda: drive.scroll_flick(session.page, steps=flick_steps),
        at_end=lambda: drive.has_text(session.page, selectors.END_OF_LIST_TEXTS),
        collect=collect,
        nudge=lambda: drive.nudge(session.page),
        pace=lambda: session.pace(pace_factor),
        max_rounds=max_rounds,
    )

    if report.scroll.truncated:
        log.error(
            "%s\n   ⚠️ 这个关键词的搜索结果**没采完**，别当成采全了。",
            report.describe(),
        )
    elif report.scroll.suspicious:
        log.warning(report.describe())
    else:
        log.info(report.describe())
    report.warn_if_empty()  # 采完了再说一次，带最终条数
    return report


OVERLAP_ALARM = 0.5
"""跨组合重合率超过这个值就报警。

**这是个粗尺，不是测量。** 它要区分的是两种差着数量级的情况：

    正常（2026-09-26 实测）  两档之间共有 4 条 / 共 333 条 → 约 1%
    筛选没生效                每档都返回同一批 150 条  → 约 90%

中间没有需要分辨的细节，所以 0.5 这个数怎么定都不影响结论——
它只负责**在两种截然不同的世界之间选边**。别拿它当覆盖率指标用。
"""


@dataclass
class ComboResult:
    """全量里的一档跑完的结果。"""

    spec: SearchSpec
    report: SearchReport

    @property
    def label(self) -> str:
        return f"{self.spec.type_filter} × {self.spec.time_filter}"

    @property
    def raw(self) -> int:
        """这一档**自己**抓到多少条（未与别档合并）。"""
        return self.report.found

    def describe(self) -> str:
        # `describe("本档")` 而不是空串：滚动没采完那几种情况的文案是
        # "❌ {what}卡住了…"，主语留空的话读起来像在说整个关键词。
        scroll = self.report.scroll.describe("本档") if self.report.scroll else ""
        return f"{self.label}：{self.raw} 条  {scroll}".rstrip()


@dataclass
class BackfillReport:
    """一次全量（一个关键词、全部组合）的结果。

    ⚠️ **`found` 是"这个关键词采到多少"，不是"这个关键词有多少内容"。**
    搜索页给的是样本，见模块开头。这个数只能用来跟**历史**比
    （今天 800、昨天 300、前天 750 → 昨天那次有问题），不能当绝对值看。
    """

    keyword: str
    combos: list[ComboResult] = field(default_factory=list)
    urls: list[ops.UrlEntry] = field(default_factory=list)
    """**跨组合去重之后**的 URL。这才是阶段一的产出。"""

    duplicates: int = 0
    """被更早的组合采过的条数。⚠️ 正常时应当**接近 0**（实测两档约 1%）——
    它同时也是"筛选到底生效没有"的探针，见 `overlap_ratio`。"""

    @property
    def found(self) -> int:
        return len(self.urls)

    @property
    def raw_total(self) -> int:
        """各档条数之和，**没去重**。和 `found` 的差就是重合量。"""
        return sum(combo.raw for combo in self.combos)

    @property
    def overlap_ratio(self) -> float:
        """跨组合重合率。0 = 各档完全不重叠，1 = 各档返回的是同一批。

        ⚠️ **这个数异常升高就是"筛选没生效"的证据。** `_apply_filters`
        已经会回读每一组并抛异常，但它验的是**面板上高亮的是哪个标签**；
        万一知乎把筛选做成了"标签变了、结果没变"，回读是过得了的，
        只有这里能发现——所有档拿回同一批 URL。

        分档时算不出这个数（那时候还没有"多档"这回事），所以它是全量路径
        特有的探针。见 `OVERLAP_ALARM`。
        """
        if self.raw_total == 0:
            return 0.0
        return self.duplicates / self.raw_total

    def describe(self) -> str:
        return (
            f"「{self.keyword}」全量：{len(self.combos)} 档，"
            f"各档合计 {self.raw_total} 条，去重后 **{self.found} 条**"
            f"（重合 {self.duplicates} 条 / {self.overlap_ratio:.0%}）"
        )


def backfill(
    session: BrowserSession,
    keyword: str,
    *,
    on_batch: Callable[[list[ops.UrlEntry]], None] | None = None,
    max_rounds: int = 500,
    flick_steps: int = drive.FLICK_STEPS,
    pace_factor: float = PACE_FACTOR,
    combos: Sequence[SearchSpec] | None = None,
) -> BackfillReport:
    """一个关键词的**全量**采集：把 `backfill_specs` 的每一档都跑一遍，并起来。

    ⚠️ **一档都是完整的一次搜索**（导航 + 点三组筛选 + 滚到底），
    所以耗时是单档的 12 倍。别在调试时拿它当 `search` 用。

    ⚠️ 跨组合去重在这个函数里做，`on_batch` 收到的**只有新增的那些**——
    所以 `urls.jsonl` 里同一个 URL 不会出现两次，但"它是哪一档采到的"
    也就查不出来了。这是刻意的（用户 2026-09-26 明确不要每行带口径字段）；
    要知道某一档采了什么，看 `combos` 里每档的条数，别看文件。

    Args:
        combos: 覆盖组合清单，默认 `backfill_specs(keyword)`。
            实跑脚本用它在**不跑满 12 档**的情况下验收单档或指定几档。
            传空序列会立刻返回一个空的报告——不报错，因为它可能只是
            "今天这一档没内容"，但**会记一条 warning**。
    """
    selectors.require_calibrated(
        "SEARCH_RESULT_ITEM",
        "SEARCH_RESULT_LINK",
        "SEARCH_FILTER_ENTRY",
        "FILTER_GROUP",
        "FILTER_TAG_ACTIVE",
    )

    specs = list(combos) if combos is not None else backfill_specs(keyword)
    report = BackfillReport(keyword=keyword)
    if not specs:
        log.warning("「%s」的全量组合清单是空的，一档都没跑。", keyword)
        return report

    #: 已经采过的 URL。只在这一个关键词的内存里活着，**不落盘**
    #: （架构文档 7.8 禁止的是持久去重台账，不是这个）。
    seen: set[str] = set()

    def make_handler(tally: list[int]) -> Callable[[list[ops.UrlEntry]], None]:
        """造一个"把一批 URL 并进总表"的回调，顺带记下这一档的收获。

        ⚠️ **工厂定义在循环外面、计数盒当参数传进来**——不是风格问题。
        直接把 `handle` 定义在循环体里的话，闭包捕获的是 `tally` 这个**名字**，
        而它下一轮会被重新绑定到新的盒子。这里因为 `search()` 是同步调用、
        回调不会活过本轮，所以**碰巧**没出事；但那是巧合，不是设计。
        ruff 的 B023 指的就是这个，它是对的。
        """
        tally[0] = tally[1] = 0  # [本档新增, 别档已有]

        def handle(batch: list[ops.UrlEntry]) -> None:
            fresh = []
            for entry in batch:
                if entry.url in seen:
                    report.duplicates += 1
                    tally[1] += 1
                    continue
                seen.add(entry.url)
                tally[0] += 1
                fresh.append(entry)
            report.urls.extend(fresh)
            if on_batch and fresh:
                on_batch(fresh)

        return handle

    for index, spec in enumerate(specs, start=1):
        label = f"{spec.type_filter} × {spec.time_filter}"
        log.info("全量 %d/%d 档：%s", index, len(specs), label)
        tally = [0, 0]

        combo_report = search(
            session,
            spec,
            on_batch=make_handler(tally),
            max_rounds=max_rounds,
            flick_steps=flick_steps,
            pace_factor=pace_factor,
        )
        report.combos.append(ComboResult(spec=spec, report=combo_report))
        log.info(
            "全量 %d/%d 档 %s 跑完：新增 %d 条，别档已有 %d 条",
            index,
            len(specs),
            label,
            tally[0],
            tally[1],
        )

        # 档与档之间留一次完整停顿。一轮全量是十几个"完整搜索"串起来，
        # 中间不留停顿的话，节奏上就是连续刷搜索页——那正是最容易触发限流的用法。
        session.pace()

    log.info(report.describe())
    if report.overlap_ratio > OVERLAP_ALARM:
        log.error(
            "⚠️ 「%s」各档的 URL 重合率 %.0f%%，远超正常水平（实测约 1%%）。\n"
            "   这不像「内容都热门」，更像**筛选没生效**：每一档都拿回了同一批结果。\n"
            "   先查 combos 里每档的 filters_readback 是不是真的换过档。",
            keyword,
            report.overlap_ratio * 100,
        )
    return report


# ── 内部 ────────────────────────────────────────────────────────────


def _wait_for_panel(session: BrowserSession, timeout: float = _PANEL_TIMEOUT) -> bool:
    """等筛选面板**真的进到 DOM 里**。等到了返回 True，超时返回 False。

    ⚠️ **`session.settle()` 顶替不了这个。** settle 等的是一个**固定时长**，
    它保证不了任何具体元素已经在了；而打开面板是纯前端的一次 React 状态切换，
    回读完全可能发生在面板进 DOM 之前，于是
    `_groups()` 返回空、`_read_group` 返回 `None`。
    2026-09-26 实跑就是崩在这里：日志里读出一个 `None`，
    而错误信息却写着"现在还是「最新发布」"（因为再读一次就有值了）。

    判据用 `_groups()`：实测 `search_filter_open.html` 里
    `.SearchTabs-customFilter--group` 有 3 个，而
    `search_filtered.html`（面板收起）里是 **0 个**——所以"有没有组"
    就是"面板开没开"，这是实测出来的，不是推断。
    """
    deadline = time.monotonic() + timeout
    while True:
        if _groups(session):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def ensure_panel_open(session: BrowserSession) -> None:
    """确保筛选面板是开着的。已经开着就什么都不做。

    ⚠️ **靠实际看，不靠记状态。** 早先这里是一个 `panel_open` 布尔量，
    置 True 之后就不再检查。问题是点完一个选项面板会**自己收起来**
    （实测：`search_filtered.html` 里面板整个不在 DOM 里），
    那个变量却还以为它开着——于是"确保打开"这一步变成了
    **把已经开着的面板点关**，紧接着的回读就得到一个 `None`。

    Raises:
        FilterNotApplied: 入口点不着，或点了面板也没出来。
    """
    if _groups(session):
        return

    if drive.click_text(session.page, (FILTER_ENTRY_TEXT,)) is None:
        raise FilterNotApplied(
            f"找不到搜索页的「{FILTER_ENTRY_TEXT}」入口（{selectors.SEARCH_FILTER_ENTRY}）。\n"
            "   ⚠️ 它是**一个 `<div>`,不是 `<button>`**——如果这里报找不到，\n"
            "   多半是 drive.click_text 的按文案兜底被改坏了，或者选择器过期了。"
        )
    if not _wait_for_panel(session):
        raise FilterNotApplied(
            f"点了「{FILTER_ENTRY_TEXT}」，但面板 {_PANEL_TIMEOUT:.0f} 秒内没出现"
            f"（`{selectors.FILTER_GROUP}` 一个都没匹配到）。\n"
            "   面板收起时这个选择器**本来就该是 0 个**，所以这里的意思是"
            "「点了也没开起来」，不是「选择器挂了」。"
        )


def _apply_filters(session: BrowserSession, spec: SearchSpec) -> tuple[str | None, ...]:
    """把三组筛选都摆到该在的位置，返回每组的回读值。

    组0 类型**必须管**，两个理由：

    1. 它是**账号级的残留状态**——上次跑剩下的「只看回答」会让文章和想法
       一条都进不来，而日志上一切正常。
    2. 全量采集的十档组合就是靠**逐档指定**组0 拼出来的（见 `BACKFILL_COMBOS`）：
       「只看文章」跑完五档时间，「只看回答」再跑一遍。

    ⚠️ **每一组都是"点完立刻回读"，点几次就读几次。** 早先的写法是在每一轮
    **开头**读——两次读取都在两次点击**之前**，最后一次点击从来没有被验证过。
    后果不是"漏报"而是"错报"：筛选明明点成功了也照样抛 `FilterNotApplied`。
    2026-09-26 实跑就撞上了，日志里那句自相矛盾的话是铁证——
    「点了两次都没切到「最新发布」（**现在还是「最新发布」**）」。
    """
    ensure_panel_open(session)

    wanted = (
        (selectors.FILTER_GROUP_TYPE, spec.type_filter),
        (selectors.FILTER_GROUP_SORT, spec.sort_filter),
        (selectors.FILTER_GROUP_TIME, spec.time_filter),
    )

    for group_index, want in wanted:
        current = _read_group(session, group_index)
        if current == want:
            log.info("筛选组 %d 已经是「%s」", group_index, want)
            continue

        for attempt in range(1, _CLICK_ATTEMPTS + 1):
            log.info(
                "筛选组 %d 当前「%s」→ 点「%s」（第 %d/%d 次）",
                group_index,
                current,
                want,
                attempt,
                _CLICK_ATTEMPTS,
            )
            if _click_in_group(session, group_index, want) is None:
                log.warning("筛选组 %d 里没找到可点的「%s」", group_index, want)
            session.guard()
            session.settle()  # 结果列表要重新请求
            ensure_panel_open(session)  # 面板会收起来，重开并**等它真的出现**
            current = _read_group(session, group_index)  # ⭐ 点完立刻回读
            if current == want:
                break
        else:
            raise FilterNotApplied(_filter_error(session, group_index, want))

    readback = tuple(_read_group(session, i) for i in range(len(wanted)))
    log.info("筛选已生效：%s", " / ".join(t or "?" for t in readback))
    return readback


def _filter_error(session: BrowserSession, group_index: int, want: str) -> str:
    current = _read_group(session, group_index)
    available = _group_options(session, group_index)
    return (
        f"筛选组 {group_index} 点了两次都没切到「{want}」（现在还是「{current}」）。\n"
        f"   这一组实际可选项：{available}\n"
        "   ⚠️ 不要忽略这个错误继续跑：筛选没生效时采集照样会「成功」完成，\n"
        "   但拿到的是一份口径错误的数据，而且不会有任何报错。\n"
        "   多半是 selectors.FILTER_* 的文案或组下标需要重新校准。"
    )


def _groups(session: BrowserSession) -> list:
    """筛选面板里的三组。**面板没开时是空的**，调用方要自己确保它是开的。"""
    return drive.all_of(session.page, selectors.FILTER_GROUP)


def _read_group(session: BrowserSession, group_index: int) -> str | None:
    """读某一组**当前激活**的那个标签。读不到返回 None。

    ⚠️ 必须**先按组取，再在组内找激活项**，不能直接取全页的 `.tag-selected`
    然后按下标数：某一组没有选中项时下标会整体错位，
    于是"组1 的回读值"其实来自组2——而回读正是这里唯一的判据，
    它错了整个验证就是假的。
    """
    groups = _groups(session)
    if group_index >= len(groups):
        return None
    el = drive.pick(groups[group_index], selectors.FILTER_TAG_ACTIVE)
    return drive.element_text(el) if el is not None else None


def _group_options(session: BrowserSession, group_index: int) -> list[str]:
    """某一组的全部可选项文案。只为报错信息服务。"""
    groups = _groups(session)
    if group_index >= len(groups):
        return []
    return [
        text
        for el in drive.all_of(groups[group_index], selectors.FILTER_TAG)
        if (text := drive.element_text(el))
    ]


def _click_in_group(
    session: BrowserSession, group_index: int, text: str
) -> str | None:
    """在**指定的那一组里**点一个选项。

    按组点的理由见模块开头：组0 的「不限类型」和组2 的「不限时间」在整页
    按文案找会串台。这里的 `drive.click_text(group, …)` 把查找限制在组的子树里
    （`drive.find_by_text` 传 ElementHandle 时只搜那个元素，见那边的说明）。
    """
    groups = _groups(session)
    if group_index >= len(groups):
        return None
    return drive.click_text(groups[group_index], (text,))


def _parse_batch(
    html_items: list[str], spec: SearchSpec, report: SearchReport
) -> list[ops.UrlEntry]:
    """把一批 HTML 解析成 UrlEntry，顺便累计各种 skip 计数。

    `html_items` 是本轮**新增**的那一段。同一个 URL 又出现一次算 `duplicates`——
    Harvester 只按完全相同的 HTML 去重，卡片重渲染后 HTML 微变（相对时间变了
    之类）就会再吐一次，这里按 URL 兜住。
    """
    seen = {entry.url for entry in report.urls}
    fresh: list[ops.UrlEntry] = []

    for html in html_items:
        item, reason = parse.parse_search_item_ex(html)
        if item is None:
            if reason == parse.SKIP_NO_LINK:
                report.skipped_no_link += 1
            else:
                report.skipped_not_content += 1
            continue
        if item.url in seen:
            report.duplicates += 1
            continue
        seen.add(item.url)
        fresh.append(
            ops.UrlEntry(
                url=item.url,
                content_type=item.content_type,
                question_id=item.question_id,
                keyword=spec.keyword,
                # 搜索页白送的，一路带到阶段二做分诊（见 ops.UrlEntry）
                title=item.title,
                excerpt=item.excerpt,
                voteup_count=item.voteup_count,
                comment_count=item.comment_count,
            )
        )
    return fresh
