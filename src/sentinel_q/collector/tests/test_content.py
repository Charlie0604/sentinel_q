"""能力二（打开 URL 取正文）的测试。

`content.extract_content()` 本身要浏览器，跑不了。但这里面有几块能测死的，
而且**每一块错了都会静默存下错数据**：

  1. `scope_selector()` —— 回答页要限定在哪张卡片上。限定失败的话，
     点「阅读全文」会点到"相关推荐"上去，而**取值照样成功**。
  2. 「详情页到底截不截断」这个判断 —— 判断错了，残缺正文会被当全文入库。
  3. `_body_ready()` —— 等正文渲染完的判据。判早了就是残缺正文。
  4. `ContentReport.ok` —— 空正文算不算成功。不算失败的话，库里会躺着
     一批"采到了"却什么都没有的行。

第 2 条不是逻辑，是**实测事实**。所以它的用例读真实快照，
把那张对照表钉住；快照不在（别人的机器 / CI）就 skip。其余几条只用假对象。
"""

from __future__ import annotations

import functools
import pathlib

import pytest
from bs4 import BeautifulSoup

from sentinel_q.collector import content, parse, selectors

CALIB_DIR = pathlib.Path("runtime/calib")


@functools.cache
def _snapshot(name: str) -> str:
    path = CALIB_DIR / name
    if not path.exists():
        pytest.skip(f"缺快照 {path}（不在仓库里，得由项目所有者本机采集）")
    return path.read_text(errors="ignore")


# ── 一、作用域：回答页必须限定到目标那张卡片 ────────────────────────


def test_回答页的作用域限定到目标回答() -> None:
    """⭐ 实测 `answer.html` 里有 **3 个** `.AnswerItem`：

        [0] 目标回答（name=1594809785）
        [1] [2] 「相关推荐」里的另外两条回答

    不限定的话，"在回答页上点阅读全文"会点到推荐卡片上——
    而**采集照样成功**，只是展开的是别人的回答、拿的还是目标的正文，
    看起来一切正常。所以这里必须按 `name`（= 回答 ID）限定。
    """
    scope = content.scope_selector("https://www.zhihu.com/question/22230085/answer/1594809785")

    assert scope == ".AnswerItem[name='1594809785']"


def test_只有回答页需要限定作用域() -> None:
    """文章 / 想法 / 问题页整页就是那条内容，不需要限定。

    这不是图省事：给它们硬凑一个作用域选择器，凑错了会变成
    "找不到元素 → 不点开 → 残缺正文入库"，比不限定更糟。
    """
    assert content.scope_selector("https://zhuanlan.zhihu.com/p/123456") is None
    assert content.scope_selector("https://www.zhihu.com/pin/123456789") is None
    assert content.scope_selector("https://www.zhihu.com/question/22230085") is None


def test_认不出来的网址不编作用域() -> None:
    """URL 不合法就返回 None，让 `parse` 那边去报"这条采不了"。

    不要在这里猜——猜出来的作用域可能命中页面上别的东西。
    """
    for bad in ("", "不是 url", "https://www.example.com/question/1/answer/2"):
        assert content.scope_selector(bad) is None, bad


def test_作用域取的是第一个候选而不是整组() -> None:
    """⚠️ `A, B[name='x']` 在 CSS 里是"A 全部 **或** B 且 name=x"。

    把整个分组拼进去，限定作用当场失效——又会退回"整页第一个"，
    而这正是这条作用域存在的理由。所以必须取 `candidates()[0]`。
    """
    scope = content.scope_selector("https://www.zhihu.com/question/1/answer/2")

    assert scope is not None
    assert ", " not in scope
    assert scope.count("[name=") == 1


# ── 二、实测事实：详情页不截断，列表页每张卡片都截断 ────────────────
#
# 这一组是**校准结论**，不是逻辑。它的价值在于：哪天知乎改了详情页
# （开始截断正文了），这里会先红——而不是等到一批残缺正文静默入库之后
# 才有人发现。


@pytest.mark.parametrize(
    ("name", "expected_more"),
    [
        ("answer.html", 0),  # 回答详情页
        ("article.html", 0),  # 文章详情页
        ("large_question.html", 0),  # 问题页 89 张回答卡片
        ("question_bottom.html", 0),  # 问题页 15 张回答卡片
        ("search_bottom.html", 198),  # 搜索列表
        ("thought.html", 182),  # 想法搜索列表
    ],
)
def test_折叠标记只出现在列表页(name: str, expected_more: int) -> None:
    """⭐ 六份快照的对照表：**详情页 0 个，列表页每张卡片一个**。

    这条结论支撑两件事：

      * 能力二不必点「阅读全文」——详情页给的就是全文；
      * 能力一**只能产 URL**，因为它拿到的正文是折叠的摘要。

    顺带否掉一个看着更省事的方案："让搜索页直接给正文，省掉二次打开"。
    那个方案会把 198 条残缺正文当全文存进证据库。
    """
    soup = BeautifulSoup(_snapshot(name), "html.parser")
    more = len(soup.select(selectors.CONTENT_MORE_BUTTON))
    collapsed = len(soup.select(selectors.CONTENT_COLLAPSED))

    assert more == expected_more, f"{name} 的「阅读全文」按钮数变了"
    assert collapsed == more, "两个标记是成对的（实测数字完全一致），不一致说明结构变了"


def test_搜索列表的正文确实是残缺的() -> None:
    """光有按钮还不够——要证明"列表里的正文比详情页短"。

    `search_bottom.html` 里被折叠的卡片正文都是百来字，
    而 `article.html` 的正文一万六千多字。差着两个数量级，
    这就是"绝不能让列表页供正文"的实据。
    """
    article = BeautifulSoup(_snapshot("article.html"), "html.parser")
    article_body = article.select_one(selectors.candidates(selectors.RICH_TEXT)[1])
    assert article_body is not None

    search = BeautifulSoup(_snapshot("search_bottom.html"), "html.parser")
    collapsed_cards = search.select(selectors.CONTENT_COLLAPSED)
    assert collapsed_cards, "这份快照里应该有折叠的卡片"

    lengths = sorted(
        len(card.get_text(" ", strip=True)) for card in collapsed_cards
    )
    assert len(article_body.get_text(" ", strip=True)) > 10_000
    assert lengths[len(lengths) // 2] < 500, "折叠卡片的正文只有百来字"


# ── 三、等正文渲染完：`_body_ready` ─────────────────────────────────
#
# 这是能力二提速的全部内容：把 `settle()` 里那个固定 1.2 秒换成"正文真的
# 出来了就往下走"。**快慢不是重点，重点是判据错了会静默存下残缺正文**，
# 所以下面钉的是判据本身。

ANSWER_URL = "https://www.zhihu.com/question/22230085/answer/1594809785"
ARTICLE_URL = "https://zhuanlan.zhihu.com/p/123456"


class FakePage:
    """假页面：按剧本依次吐出每次 `evaluate` 的返回值（= 读到的字数）。"""

    def __init__(self, lengths: list[int]) -> None:
        self.lengths = list(lengths)
        self.calls: list[tuple[str, object]] = []

    def evaluate(self, script: str, arg: object = None) -> int:
        self.calls.append((script, arg))
        return self.lengths.pop(0)


class FakeSession:
    def __init__(self, page: FakePage) -> None:
        self.page = page


def _ready(url: str, lengths: list[int]) -> tuple[FakePage, list[bool]]:
    """按剧本来一遍 `session.open(ready=...)` 会做的事：反复问，直到说成立。"""
    page = FakePage(lengths)
    check = content._body_ready(FakeSession(page), url)  # type: ignore[arg-type]
    return page, [check() for _ in lengths]


def test_正文静默了一整段才算渲染完() -> None:
    """⭐ 有文字 + **字数连着几次没变** = 渲染完了。第一次读到什么都不算数。

    React 是先挂上空容器再往里填的，所以"容器在"既不是充分条件，
    第一次读到的字数也不是终值。
    """
    _, verdicts = _ready(ARTICLE_URL, [120, 120, 120])

    assert verdicts == [False, False, True]


def test_字数还在涨就继续等() -> None:
    """残缺正文就是这样来的：容器已经在了，字还没填完。"""
    _, verdicts = _ready(ARTICLE_URL, [120, 340, 340, 340])

    assert verdicts == [False, False, False, True]


def test_只静默一拍不算数() -> None:
    """⭐ 这是 `_BODY_STABLE_READS = 3` 而不是 2 的理由。

    两段渲染之间停一拍（下面 `[120, 340, 340]` 里第二个 340）是**间隙**，
    不是填完了。只看"两次一样"就会在这里收工，采下只有第一段的残缺正文。
    """
    _, verdicts = _ready(ARTICLE_URL, [120, 340, 340])

    assert verdicts == [False, False, False], "还差一次确认，不能算就绪"


def test_卡片还没出现不算就绪() -> None:
    """`-1`：`scope` 给了但那张回答卡片还没渲染出来。"""
    _, verdicts = _ready(ANSWER_URL, [-1, -1, 300, 300, 300])

    assert verdicts == [False, False, False, False, True]


def test_容器在但没有文字不算就绪() -> None:
    """`0`：卡片在，正文容器还没挂上。**空正文绝不能算"好了"。**"""
    _, verdicts = _ready(ARTICLE_URL, [0, 0, 0])

    assert verdicts == [False, False, False]


def test_回答页的判据限定在目标卡片里() -> None:
    """⭐ 作用域和解析层用同一条判据。

    整页找的话，回答页的「相关推荐」里也有正文容器——会拿**别人**的正文
    当"渲染好了"的信号，然后在一个还空着的目标回答上收工。
    """
    page, _ = _ready(ANSWER_URL, [300])

    assert page.calls[0][1][1] == ".AnswerItem[name='1594809785']"


def test_文章页不限作用域() -> None:
    """文章 / 想法 / 问题页整页就是那条内容，和 `scope_selector` 保持一致。"""
    page, _ = _ready(ARTICLE_URL, [300])

    assert page.calls[0][1][1] is None


# ── 四、空正文算失败 ────────────────────────────────────────────────


def _item(url: str, text: str | None) -> parse.ParsedItem:
    return parse.ParsedItem(url=url, content_type="article", zhihu_id="1",
                            question_id=None, text=text)


def test_空正文不算成功() -> None:
    """⭐ 正文容器找不到时 `parse` **不报错**，只给回空字符串——
    就这么入库的话，"失败 0 条"底下躺着一批什么都没有的行。
    """
    assert not content.ContentReport(url=ARTICLE_URL, item=_item(ARTICLE_URL, "")).ok
    assert not content.ContentReport(url=ARTICLE_URL, item=_item(ARTICLE_URL, None)).ok
    assert not content.ContentReport(url=ARTICLE_URL, item=_item(ARTICLE_URL, "  ")).ok


def test_正常正文算成功() -> None:
    assert content.ContentReport(url=ARTICLE_URL, item=_item(ARTICLE_URL, "正文")).ok


def test_折叠的正文不算成功() -> None:
    """点过「阅读全文」也没展开的，存下去的是残缺正文，不是证据。"""
    report = content.ContentReport(url=ARTICLE_URL, item=_item(ARTICLE_URL, "正文"))
    report.truncated = True

    assert not report.ok
