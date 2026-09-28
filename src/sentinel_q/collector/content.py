"""能力二：打开一个 URL，把里面的正文取出来。

吃的是能力一（搜索）产出的 URL，产出的是**能直接入库的 `ParsedItem`**：
回答 / 文章 / 想法 / 问题，四种类型走同一条路。

"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sentinel_q.collector import drive, parse, selectors
from sentinel_q.collector.session import BrowserSession
from sentinel_q.shared.urlnorm import normalize

log = logging.getLogger(__name__)


@dataclass
class ContentReport:
    """一次正文采集的结果。

    `ok` 是**采集是否可信**，不是"有没有拿到东西"——拿到了但正文是**空的**、
    **残缺的**，或者页面根本不是那条内容，都算不 ok。空正文尤其要算失败：
    它进库就是一条"采到了"的假象（`parse.ParsedItem.has_body`）。
    """

    url: str
    item: parse.ParsedItem | None = None
    truncated: bool = False
    """正文**仍然是折叠状态**——点过「阅读全文」也没展开，或者点完还在。
    实测两张详情页都不会发生；发生就是遇到了没实测过的页面形态。"""

    expand_clicks: int = 0

    @property
    def ok(self) -> bool:
        return self.item is not None and self.item.has_body and not self.truncated

    @property
    def content_type(self) -> str | None:
        return self.item.content_type if self.item else None

    def describe(self) -> str:
        if self.item is None:
            return f"❌ {self.url}：页面里取不到这条内容（见日志，多半是选择器过期）"
        text = (
            f"{self.content_type} {self.item.zhihu_id}：正文 {len(self.item.text or '')} 字"
            f"，作者 {self.item.author_name or '?'}"
            f"，赞同 {self.item.voteup_count}"
        )
        if self.expand_clicks:
            text += f"，点开折叠 {self.expand_clicks} 次"
        if self.truncated:
            text += "；❌ **正文仍是折叠状态，不是全文**"
        if self.item.published_at is None:
            # 不是错误（相对时间刻意不换算，见 parse），但值得记一笔：
            # 取证时"什么时候发的"经常比正文本身还重要。
            text += "；⚠️ 没有绝对时间戳"
        return text


def extract_content(session: BrowserSession, url: str) -> ContentReport:
    """打开一个内容页，取正文。**不抛异常**——失败记在报告里，由调用方决定怎么办。

    单个 URL 取失败不该中断整批采集：一批几百条，坏一条就全停的话
    那一条会永远卡在那里。调用方按 `report.ok` 统计失败率，
    失败率高到不正常时**那才是**该停下来看的事。
    """
    selectors.require_calibrated(
        "RICH_TEXT",
        "AUTHOR_NAME",
        "AUTHOR_LINK",
        "UPVOTE_BUTTON",
    )

    report = ContentReport(url=url)
    # ⚠️ 这里是**唯一**一处给 open() 传 ready 的地方：正文页等的就是正文。
    #    等不到不抛异常，交给下面的解析一层报失败（那样失败是看得见的）。
    session.open(url, ready=_body_ready(session, url))

    # 折叠才点。实测详情页这里永远是 0，所以正常情况下一次选择器都不打。
    report.expand_clicks = _expand_if_collapsed(session, url)

    report.item = parse.parse_content_page(drive.page_html(session.page), url)
    if report.item is None:
        log.error(
            "%s 取不到正文。多半是页面结构变了，或者地址栏指的那条内容"
            "在页面上找不到（拿推荐内容冒充目标内容比采不到更糟，所以这里不兜底）。",
            url,
        )
        return report

    if not report.item.has_body:
        # 正文容器找不到时 parse **不报错**，只安静地给回一个空字符串，
        # 所以必须在这里拦一道（`answers` / `comments` 也是这么做的）。
        # 多数是等超时了——页面慢，不是页面没有。
        log.error(
            "%s 定位到了这条内容，但**正文是空的**（多半是没渲染完就取了，"
            "也可能是这条内容真的没有文字）。空正文入库等于伪造一条"
            '"采到了"的记录，所以这条算失败，也不入库。',
            url,
        )
        return report

    report.truncated = _still_collapsed(session)
    if report.truncated:
        log.error(
            "%s 的正文**仍是折叠状态**，存下去的会是残缺正文而不是全文。\n"
            "   ⚠️ 实测 answer.html / article.html 都不会这样，说明这是没校准过的"
            "页面形态（多半是想法详情页）。\n"
            "   请人工打开这条 URL 看一眼，再把选择器补上——**不要就这么入库**。",
            url,
        )
    elif report.item.published_at is None:
        log.warning("%s 没有绝对时间戳（页面只给了相对时间）", url)

    log.info(report.describe())
    return report


def extract_all(
    session: BrowserSession,
    urls: Iterable[str],
    *,
    on_item: Callable[[parse.ParsedItem], None] | None = None,
) -> list[ContentReport]:
    """按 URL 清单逐个取正文。

    ⚠️ **串行 + 每次之间留停顿**（`session.pace`）。单账号并发刷是最容易被
    限流的用法，而本系统明确不搭代理池对抗限流（架构文档边界条款）。
    跑慢一点是设计的一部分。

    `on_item` 每取到一条就回调一次：**不要等一批跑完再落库**——
    几百条要跑很久，中途崩了就全没了。
    """
    reports: list[ContentReport] = []
    for url in urls:
        report = extract_content(session, url)
        reports.append(report)
        if report.item is not None and on_item is not None:
            on_item(report.item)
        session.pace()
    return reports


def summarize(reports: list[ContentReport]) -> str:
    """一批采集的汇总。**失败必须显式报出来，不能只看"跑完了"。**"""
    from collections import Counter

    failed = [r for r in reports if not r.ok]
    kinds = Counter(r.content_type or "失败" for r in reports)
    text = (
        f"共 {len(reports)} 条："
        + "，".join(f"{k} {v}" for k, v in kinds.most_common())
        + f"；失败 {len(failed)} 条"
    )
    if failed:
        text += "（失败的 URL 见上方的 error 日志）"
    return text


# ── 内部 ────────────────────────────────────────────────────────────

_BODY_STABLE_READS = 3
"""连着读到几次一样的字数才算正文渲染完。

3 次 = 约 300 毫秒的静默。**不能改成 2**：一次静默可能只是两段渲染之间的
间隙，连着两次静默才说明它真的填完了。
"""


def _body_ready(session: BrowserSession, url: str) -> Callable[[], bool]:
    """等正文**渲染完**的判据，交给 `session.open(ready=...)`。

    返回一个**有状态**的判据（每调一次读一次字数），成立的条件是：
    正文容器在、有文字，而且**连着 `_BODY_STABLE_READS` 次读到的字数一样**。

    ⚠️ **只看"容器在不在"是不够的。** React 是先挂上空容器再往里填内容的，
    容器在而正文没填完时采下来的是**残缺正文**——而且不报错。字数还在涨
    就说明还在填，连着几次一样才算停下来了。多读一次的成本是一次 JS 往返。

    ⚠️ 作用域用 `scope_selector`，和解析层是同一条判据。整页找的话，回答页的
    「相关推荐」里也有正文容器，会拿**别人**的正文当"渲染好了"的信号，
    然后在一个还空着的目标回答上收工。
    """
    scope = scope_selector(url)
    reads: list[int] = []

    def check() -> bool:
        reads.append(drive.text_length(session.page, selectors.RICH_TEXT, scope=scope))
        # `<= 0` 一票否决：`-1`（卡片还没出现）和 `0`（容器还没挂上）都不是就绪。
        if len(reads) < _BODY_STABLE_READS or reads[-1] <= 0:
            return False
        return len(set(reads[-_BODY_STABLE_READS:])) == 1

    return check


def _expand_if_collapsed(session: BrowserSession, url: str) -> int:
    """正文折叠着就点开。返回点了几次。

    作用域按内容类型定，**不是整页乱点**：回答页上除了目标回答，
    还挂着"相关推荐"的卡片，整页点会把那些也展开——白点十几次，
    还会让页面结构变化，给后面取值添乱。

    ⚠️ **实测详情页走到这里通常一次选择器都不打**（`answer.html` /
    `article.html` 的折叠标记都是 0）。留着它是为了没实测过的页面形态，
    尤其是想法详情页。
    """
    scope = scope_selector(url)

    if scope is not None:
        # 回答页：把查找限制在那张卡片里
        target = drive.first(session.page, scope)
        if target is None:
            # 回答卡片都找不到，多半是页面没渲染完或结构变了。
            # 不在这里报错——parse 那边会用更准的判据再判一次。
            return 0
    else:
        # 其它内容页：整页就是作用域。先问一句有没有折叠，
        # 免得为每一条内容都多打一次点击。
        if not drive.has_text(session.page, selectors.READ_MORE_TEXTS):
            return 0
        target = session.page

    clicked = 0
    for text in selectors.READ_MORE_TEXTS:
        if drive.click_text(target, (text,)) is not None:
            clicked += 1
            session.settle()
    return clicked


def _still_collapsed(session: BrowserSession) -> bool:
    """页面上还有没有折叠着的正文。

    ⚠️ 只在**目标内容的作用域里**看才有意义——列表页那种"页面上有 198 个
    折叠卡片"的情况，整页判断会永远为真。这里用内容页的实际形态判断：
    详情页的目标内容只有一个，所以整页有折叠标记就等于目标正文没展开。
    """
    return drive.first(session.page, selectors.CONTENT_MORE_BUTTON) is not None


def scope_selector(url: str) -> str | None:
    """要展开的是页面上的哪一块。**回答页必须限定，其它页整页即可。**

    回答页实测有 3 个 `.AnswerItem`（目标 + 两条相关推荐），`name` 属性
    就是回答 ID，直接对上——和 `parse._content_scope` 用的是同一条判据。

    ⚠️ 取**第一个候选**再拼属性，不能把整个分组拼进去：
    `A, B[name='x']` 在 CSS 里的意思是"A 全部，或 B 且 name=x"，
    等于失去了限定作用，又会点回"整页第一个"。

    ⚠️ **`comments.py` 也在用这个函数**（所以它不是私有的了）。理由一样：
    回答页上除了目标回答，还有"相关推荐"的卡片，它们**各自带着一个
    「N 条评论」按钮**。不限定作用域就点评论入口，会点开**别人的**评论，
    而采集照样"成功"——拿到的是一整批挂在错误内容下的评论。
    """
    normalized = normalize(url)
    if normalized is None or normalized.content_type != "answer":
        return None
    return f"{selectors.candidates(selectors.ANSWER_ITEM)[0]}[name='{normalized.zhihu_id}']"
