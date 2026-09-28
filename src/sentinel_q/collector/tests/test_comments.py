"""能力三（评论）的离线测试。

两类测试，分工明确：

  * **真快照测试**：把 `runtime/calib/*.html` 读进来，在真页面结构上钉住
    「哪块面板是评论列表」「声明的总数是多少」。跑这些不需要网络也不需要浏览器。
  * **假对象测试**：`extract_comments()` 本体跑不了（要真浏览器），
    但里面**判据**的部分能在假对象上测干净——尤其是评论入口按钮那几条
    （点错了弹窗照开、评论照采、日志一片干净，只是采到的是别人的）。

⚠️ **这里不再有二级回复的测试**（2026-09-27）：回复整条路都去掉了，
取舍见 `comments.py` 模块开头。快照里的嵌套回复节点还有用——它是
`parse._root_comments()` 存在的理由，只取一级的判据在 `test_parse.py` 里。

⚠️ 所有断言里的数字都是**对着快照数出来的**，不是估的。
改快照就等于改事实，那时候要连这些数字一起改。
"""

from __future__ import annotations

import inspect
import re

import pytest
import soupsieve
from bs4 import BeautifulSoup

from sentinel_q.collector import comments, drive, parse, selectors
from sentinel_q.shared.config import REPO_ROOT

CALIB = REPO_ROOT / "runtime" / "calib"

MODAL = "comments_modal.html"
"""就地展开形态：8 条一级 + 1 条嵌在里面的二级回复。

⚠️ 那条嵌套的回复现在**不采**了，但这个快照仍然有用：它是
「回复节点嵌套在一级评论里」这一事实的证据（`parse._root_comments` 靠它定位）。
"""

SECOND = "comments_second_level.html"
"""面板形态：2 块面板，第 2 块是二级回复（1 父 + 18 回复）。

⚠️ 回复面板本身已经不采了，留着它是因为**弹窗里有两块面板**这个形态还在：
`comments._comments_panel()` 得在这种弹窗里挑出评论列表那一块。
"""

COLLAPSED = "comments_collapsed.html"
"""弹窗还没打开的状态——用来验"没开的时候别硬采"。"""

ARTICLE = "article_comments.html"
"""文章页的评论弹窗，单面板、66 条。"""

pytestmark = pytest.mark.skipif(
    not (CALIB / MODAL).exists(),
    reason=f"缺少快照 {CALIB}（该目录不入库，换机器要重新采集）",
)


def snapshot(name: str) -> str:
    return (CALIB / name).read_text(errors="replace")


def panels(name: str) -> list:
    return BeautifulSoup(snapshot(name), "html.parser").select(selectors.MODAL_PANELS)


# ── 面板识别 ────────────────────────────────────────────────────────


def test_the_comments_panel_is_the_one_without_the_reply_title():
    """有两块面板时，评论列表是**不带「评论回复」标题**的那一块。

    ⚠️ 判据是**反向**的：不是"认出评论列表"，是"排除掉回复面板"。
    回复不采之后这个标题看着像废字符串，**其实删不得**——删了就只能
    "取第一块"，而"哪一块在前"是渲染顺序、不是语义，知乎换一次发版就错，
    **而且不报错**（照常采，只是采到的是回复）。

    ⚠️ 这个测试真正钉住的是**不能用类名**：同一份快照里评论列表是
    `css-feetku`、回复面板是 `css-tpyajk`；换一份快照
    （`comments_modal.html`）评论列表自己就变成了 `css-tpyajk`。
    哈希类名是**每次构建都换**的，跨快照都不成立，更别说跨发版。
    """
    ps = panels(SECOND)
    assert len(ps) == 2, "面板形态应该正好 2 块（评论列表 + 回复面板）"

    titled = [i for i, p in enumerate(ps) if _has_exact_text(p, selectors.REPLY_PANEL_TITLE)]
    assert titled == [1], f"带「{selectors.REPLY_PANEL_TITLE}」标题的面板应该是第 2 块"

    # 反过来：评论列表面板必须**不**含这个标题，否则 `_comments_panel` 会挑不出人来
    assert not _has_exact_text(ps[0], selectors.REPLY_PANEL_TITLE)


@pytest.mark.parametrize("name", [MODAL, SECOND, ARTICLE])
def test_exactly_one_panel_is_the_comments_list(name):
    """三份快照里都恰好有一块"不是回复面板"的面板 = 评论列表。

    一块都挑不出来时 `comments._comments_panel()` 返回 None 并报错——
    那种情况下**不能退化成"取第一块"**，因为改版后第一块可能是别的东西。
    """
    ps = panels(name)
    candidates = [
        p for p in ps if not _has_exact_text(p, selectors.REPLY_PANEL_TITLE)
    ]
    assert len(candidates) == 1, f"{name}：评论列表面板应该恰好 1 块，实际 {len(candidates)}"


def test_collapsed_page_has_no_modal_yet():
    """没点开评论入口时整页**一个弹窗都没有**。

    这是 `drive.all_of(page, MODAL_PANELS)` 当"弹窗开没开"的判据的依据：
    空列表 = 还没开，非空 = 已经开了。
    """
    assert panels(COLLAPSED) == []


# ── 声明总数：为什么必须限定在面板里 ────────────────────────────────


def test_whole_page_comment_total_is_wrong_and_the_decoys_are_real():
    """⭐ **这条是"必须限定作用域"的实测证据，不是理论担忧。**

    整页 `comments_second_level.html` 里，整串等于「N 条评论」的文本节点有
    **5 个**：弹窗里 1 个（真的，394），弹窗外的推荐内容卡片上 4 个
    （25 / 394 / 408 / 279）。

    所以不限定作用域时 `parse.parse_comment_total()` 返回 **25**——
    某张推荐卡片自己的评论数，而真值是 394。**它不会报错。**

    ⚠️ 顺带说明"按数值找"也救不了：外面那个诱饵里**也有一个 394**，
    和真值一模一样。唯一可靠的判据是"文本节点在不在这块面板里"。
    """
    text = snapshot(SECOND)
    assert parse.parse_comment_total(text) == 25, "整页取到的应该是推荐卡片那个 25"

    hits = BeautifulSoup(text, "html.parser").find_all(string=parse._is_comment_total_text)
    inside = [str(h) for h in hits if h.parent.find_parent(class_="Modal-content")]
    assert inside == ["394 条评论"], f"弹窗里应该只有一个声明总数，实际 {inside}"


@pytest.mark.parametrize(
    ("name", "expected"),
    [(MODAL, 9), (SECOND, 394), (ARTICLE, 65)],
)
def test_comment_total_scoped_to_the_panel(name, expected):
    """限定在面板里之后，声明的总数就是对的。

    ⚠️ 不用 `_comments_panel()`——那要真浏览器。这里直接按"评论列表永远是第 1 块"
    取，**只在这三份快照上成立**（`test_the_comments_panel_is_the_one_without_the_reply_title`
    才是在验"怎么认出哪块是评论列表"）。
    """
    node = _find_text_like_js(
        panels(name)[0], comments._COMMENT_TOTAL_PATTERN, exclude="[data-id]"
    )
    assert node is not None, f"{name} 的评论面板里没找到声明总数"
    assert parse.parse_comment_total(node.get_text()) == expected


def test_comment_total_pattern_is_anchored():
    """形态判据必须锚定整串，否则会被评论正文里的数字顶掉。

    `drive._FIND_TEXT` 是"文档顺序里第一个命中的文本节点"。不锚定的话，
    一条写着「才 25 条评论」的评论如果排在标题前面，声明的总数就成了 25。
    """
    pattern = comments._COMMENT_TOTAL_PATTERN
    assert re.search(pattern, "394 条评论")
    assert not re.search(pattern, "才 25 条评论就破防了"), "不该被正文里的片段命中"
    assert not re.search(pattern, "回复 394 条评论"), "不该被前后缀命中"


def test_comment_total_is_never_inside_a_comment_node():
    """声明的总数不会藏在某条评论里——所以排除 `[data-id]` 是安全的。

    排除它是纯保险：万一某条评论的正文整串就是「394 条评论」，
    没有这道闸就会被当成标题。
    """
    for name in (MODAL, SECOND, ARTICLE):
        for panel in panels(name):
            for node in panel.find_all(string=parse._is_comment_total_text):
                assert node.parent.find_parent(attrs={selectors.COMMENT_ID_ATTR: True}) is None


# ── 收集：去重 ──────────────────────────────────────────────────────


def test_sink_dedupes_by_comment_id_not_by_content():
    """去重按**知乎的评论 ID**，不按内容。

    同一个人把同一句话发两遍，在知乎就是两条不同的评论，不该并成一条。
    （正文页那边按 URL 去重是另一回事；评论的"身份"就是它的 ID。）
    """
    report = comments.CommentsReport(url="u", content_id="content-1")
    sink = comments._Sink(report, "content-1", None)

    same_text = _item("a", parent="content-1", text="好")
    assert sink.absorb([same_text]) == 1
    assert sink.absorb([_item("b", parent="content-1", text="好")]) == 1, "ID 不同就是两条"
    assert report.collected == 2

    assert sink.absorb([_item("a", parent="content-1")]) == 0
    assert report.duplicates == 1, "同 ID 再见到就是重复渲染"


def test_sink_counts_repeated_renders_but_keeps_one_copy():
    """同一条评论再见到：**只留一份**，但要把 `duplicates` 记上。

    这个计数器的用途是发现"滚动时页面在重复渲染"（校准信号）。
    以前二级回复的重读也会计进来，那个来源随回复一起去掉了（见模块开头）——
    **现在进来的重复全都是"页面在重复渲染"**，含义是干净的。
    """
    report = comments.CommentsReport(url="u", content_id="content-1")
    sink = comments._Sink(report, "content-1", None)
    parent = _item("a", parent="content-1")

    assert sink.absorb([parent]) == 1
    assert sink.absorb([parent]) == 0, "同 ID 再见到不算新增"
    assert report.duplicates == 1
    assert report.collected == 1, "重复渲染不该在库里留下两份"


def test_sink_reports_empty_bodies_and_calls_back():
    report = comments.CommentsReport(url="u", content_id="content-1")
    seen: list[parse.ParsedItem] = []
    sink = comments._Sink(report, "content-1", seen.append)

    sink.absorb([_item("a", parent="content-1", text=""), _item("b", parent="content-1")])
    assert report.empty_bodies == 1
    assert [i.zhihu_id for i in seen] == ["a", "b"], (
        "每采到一条就回调一次——不能等全部采完再落库"
    )
    assert report.collected == 2


# ── 报告：失败必须显式 ──────────────────────────────────────────────


def test_ok_is_false_when_the_comment_area_never_opened():
    """评论区没打开时必须**说出来**，不能只报"采到 0 条"。

    「这条内容真的没评论」和「评论区压根没打开」在数字上完全一样（都是 0），
    但一个是正常结果、一个是采集故障。日志里分不清这两者的话，
    整轮采集会安静地少掉一批评论。

    ⚠️ 措辞是"评论区"不是"弹窗"：问答页那一路既可能开弹窗、也可能就地展开，
    两种都没出来时，说"弹窗没打开"会把读日志的人往错的方向带。
    """
    report = comments.CommentsReport(url="u", opened=False)
    assert not report.ok
    assert "评论区没能打开" in report.describe()


def test_ok_is_false_when_the_modal_opened_but_nothing_came_back():
    """**弹窗开了、但一条都没解析出来** = 失败。

    这和"0 条评论"是不同的：那条路上 `no_comments=True`（入口就写着「添加评论」），
    压根不会走到这里。走到这里说明弹窗里有东西，只是评论选择器没匹配上——
    静默返回 0 的话，整轮采集会少掉一批评论而没人发现。

    ⚠️ 反过来，`missing`（条数与声明的差额）**不进 `ok`**：
    「N 条评论」的语义没实测全，拿一个语义不明的数当阈值，
    会变成天天误报，而天天误报的判据等于没有判据。
    """
    report = comments.CommentsReport(url="u", opened=True)
    assert not report.ok
    assert "采到 0 条一级评论" in report.describe()

    report = comments.CommentsReport(
        url="u", opened=True, items=[_item("a", parent="content-1")]
    )
    assert report.ok, "采到东西了就该是好的"


def test_missing_is_reported_but_does_not_fail_the_run():
    report = comments.CommentsReport(
        url="u", opened=True, declared=394, items=[_item("a", parent="content-1")]
    )
    assert report.missing == 393
    assert report.ok, "条数对不上只报出来，不当失败——判据语义未实测"
    assert "差 393 条" in report.describe()


def test_no_scroll_container_is_a_failure_not_a_warning():
    """找不到滚动容器 = 只采到首屏。**这必须失败。**

    它和"条数差几百"是两回事：那个可能只是口径问题，这个是确定的漏采。
    """
    report = comments.CommentsReport(
        url="u", opened=True, items=[_item("a", parent="content-1")]
    )
    assert report.ok

    report.scroll_container_found = False
    assert not report.ok
    assert "首屏" in report.describe()


def test_truncated_scroll_fails_the_run():
    from sentinel_q.collector.scrolling import ScrollOutcome, StopReason

    report = comments.CommentsReport(
        url="u", opened=True, items=[_item("a", parent="content-1")]
    )
    report.scroll = ScrollOutcome(9, 3, StopReason.GAVE_UP, nudges=3)
    assert not report.ok
    assert "卡住" in report.describe()


def test_declared_reached_needs_a_declared_count():
    """读不到「N 条评论」就没有判据可言，**不能当成"已经够了"**。

    当成够了就是一条都不滚，静默只采首屏——比不早停危险得多。
    """
    report = comments.CommentsReport(url="u", declared=None)

    assert comments._declared_reached(report) is False


def test_declared_reached():
    """采到声明的条数就算够。

    ⚠️ 用的是 `>=` 不是 `==`：页面重复渲染时可能多出来，卡在等号上就永远不成立。
    """
    report = comments.CommentsReport(
        url="u",
        declared=3,
        items=[_item("a", parent="c"), _item("b", parent="c")],
    )
    assert comments._declared_reached(report) is False

    report.items.append(_item("c", parent="c"))
    assert comments._declared_reached(report) is True

    report.items.append(_item("d", parent="c"))
    assert comments._declared_reached(report) is True, "多采到了也算够"


def test_the_declared_count_includes_replies_so_this_rarely_fires():
    """⚠️ 这个判据**只在没有回复的帖子上**成立——用校准快照钉住这个事实。

    `comments_modal.html` 里声明 9 条，实际是 8 条一级 + 1 条嵌着的回复。
    所以我们最多采到 8，**永远够不到 9**：有回复的帖子省不掉那三轮。
    哪天知乎改成"只数一级"，这条测试会红——那时候早停的覆盖面会大得多，
    是该有人知道的事。
    """
    panel = panels(MODAL)[0]
    declared = parse.parse_comment_total(
        _find_text_like_js(panel, comments._COMMENT_TOTAL_PATTERN, exclude="[data-id]").get_text()
    )
    items = parse.parse_comment_list(str(panel), None)

    assert declared == 9
    assert len(items) == 8, "声明 9 而一级只有 8——差的那条是二级回复"
    report = comments.CommentsReport(url="u", declared=declared, items=items)
    assert comments._declared_reached(report) is False, "够不到的，别指望它早停"


def test_the_comment_scroll_never_nudges():
    """评论区**不做**上滚下滚抢救（2026-09-27 项目所有者定）。

    连续 `stable_rounds` 轮无新增就直接当到底（STABLE）。这条挡的是"顺手加回来"：
    抢救一次要多空转好几轮，而且是**静默变慢**——没有任何测试会因此变红，
    只有实跑时那几十篇内容一起拖长才看得出来。
    """
    source = inspect.getsource(comments._collect_first_level)

    assert "nudge=lambda" not in source


# ── 复用已打开的页面 ────────────────────────────────────────────────

ANSWER = "https://www.zhihu.com/question/123/answer/456"


def _open_on_page(monkeypatch, page_url: str | None, **kwargs) -> FakeSession:
    """跑一遍 `extract_comments` 的**开头**，返回记下了导航开关的那个假会话。

    ⚠️ 只跑到开弹窗为止：`require_calibrated` 和 `_open_modal` 都换成假的，
    所以它既不查选择器校准状态，也不碰浏览器。弹窗之后的部分和导航无关，
    由本文件其它测试负责。
    """
    monkeypatch.setattr(comments.selectors, "require_calibrated", lambda *names: None)
    monkeypatch.setattr(comments, "_open_modal", lambda session, report: False)

    session = FakeSession(page_url)
    comments.extract_comments(session, ANSWER, **kwargs)
    return session


@pytest.mark.parametrize(
    ("page_url", "expected_navigate"),
    [
        (ANSWER, False),
        # 地址栏上带查询参数和锚点是常态——规范化之后就是同一条
        ("https://www.zhihu.com/question/123/answer/456?sort=created#comments", False),
        ("https://www.zhihu.com/question/123/answer/999", True),
        ("https://www.zhihu.com/", True),
        (None, True),
    ],
)
def test_reuse_open_page_navigates_unless_the_page_is_already_there(
    monkeypatch, page_url, expected_navigate
):
    """页面就在目标 URL 上时**不导航**（省一次整页加载），否则一律导航。

    ⚠️ 后三行是**安全方向**：页面是别条内容、认不出来、地址栏拿不到，
    全都退回导航。误判成"就在这儿"会拿别条内容的评论挂到这条上，
    而且采集照常成功、日志一片干净——多导航一次只花几秒。
    """
    session = _open_on_page(monkeypatch, page_url, reuse_open_page=True)
    assert session.navigations == [expected_navigate]


def test_page_is_always_navigated_without_the_reuse_flag(monkeypatch):
    """不给 `reuse_open_page` 时行为不变：哪怕页面就停在这儿，也照常导航一次。"""
    session = _open_on_page(monkeypatch, ANSWER)
    assert session.navigations == [True]


# ── 评论入口：页面最底下那个按钮 ────────────────────────────────────


class _SoupButton:
    """让 BeautifulSoup 的元素能被 `drive.element_text` 读——它只调 `inner_text()`。"""

    def __init__(self, tag) -> None:
        self.tag = tag

    def inner_text(self) -> str:
        return self.tag.get_text(" ")


def _entry_buttons(monkeypatch: pytest.MonkeyPatch, name: str, scope: str | None = None):
    """在真快照上跑**生产代码** `comments._entry_buttons`。

    只把两个驱动换成 BeautifulSoup 的等价物（`all_of` → CSS 查询、
    `element_text` → `get_text`），**判据本身一个字没改**——
    所以这里数出来的候选数就是线上会数出来的。
    """
    soup = BeautifulSoup(snapshot(name), "html.parser")
    root = soup.select_one(scope) if scope else soup
    monkeypatch.setattr(
        comments.drive,
        "all_of",
        lambda target, sel: [_SoupButton(el) for el in target.select(sel)],
    )
    monkeypatch.setattr(comments.drive, "element_text", lambda el: el.inner_text())
    return comments._entry_buttons(root)


def test_the_article_page_has_exactly_one_entry_button(monkeypatch: pytest.MonkeyPatch):
    """⭐ 文章页上「6 条评论」出现**两次**，但只有一处是按钮。

    实测：一处是底部操作栏里的 `<button class="… BottomActions-CommentBtn">`，
    另一处是页面下方评论区自己的**标题**（`div.css-1k10w8f`，点不动）。
    两个文本一模一样——按文案找的话，挑中哪个纯看谁在文档里靠前。
    所以判据必须是"**这个元素是按钮**"，不是"这段文字在页面上"。
    """
    soup = BeautifulSoup(snapshot("articalnew.html"), "html.parser")
    same_text = [
        n for n in soup.find_all(string=True) if _norm(n) == "6 条评论"
    ]
    assert len(same_text) == 2, "两处同文案——正是这条测试要钉住的前提"

    found = _entry_buttons(monkeypatch, "articalnew.html")
    assert [(_norm(text), kind) for _el, text, kind in found] == [("6 条评论", "count")]


def test_the_answer_page_needs_its_scope_because_recommendations_carry_the_same_button(
    monkeypatch: pytest.MonkeyPatch,
):
    """⭐ 回答页整页有 **3** 个入口候选，限定到目标回答之后只剩 **1** 个。

    另外两个是"相关推荐"卡片各自的评论按钮。它们没被点错不是靠运气——
    `_scope_el` 把查找限制在了目标回答那张卡片里。点错了弹窗照开、
    评论照采、日志一片干净，只是**采到的评论挂在别人下面**。
    """
    whole = _entry_buttons(monkeypatch, "answer.html")
    assert [_norm(text) for _el, text, _kind in whole] == [
        "1623 条评论",
        "101 条评论",
        "33 条评论",
    ], "整页就有歧义——所以 `_open_modal` 的'候选必须恰好一个'会在这里拦住"

    scoped = _entry_buttons(monkeypatch, "answer.html", "[name='1594809785']")
    assert [(_norm(text), kind) for _el, text, kind in scoped] == [("1623 条评论", "count")]


def test_the_question_header_button_is_not_counted_as_an_entry(
    monkeypatch: pytest.MonkeyPatch,
):
    """问题头自己的「9 条评论」**不是** `.ContentItem-action`，不该被算进来。

    它出现在回答页上，而且**排在目标回答前面**。把它算进来的话，
    回答页整页会挑中"问题的评论"——挂错得比挑中相关推荐更隐蔽。
    （`answer.html` 上它是 `button.Button.css-0`，在 `.QuestionHeader-Comment` 里。）
    """
    soup = BeautifulSoup(snapshot("answer.html"), "html.parser")
    header = soup.select_one(".QuestionHeader-Comment button")
    assert header is not None and _norm(header.get_text(" ")) == "9 条评论"
    assert not header.get("class") or "ContentItem-action" not in header["class"]

    assert "9 条评论" not in [
        _norm(text) for _el, text, _kind in _entry_buttons(monkeypatch, "answer.html")
    ]


def test_entry_kind_is_anchored_so_it_cannot_borrow_a_neighbours_text():
    """整串锚定：`赞同 9 条评论` 这种**前后还有字**的按钮不能算命中。

    ⚠️ 用 `search` 而不是 `fullmatch` 的话它会命中——而那个元素多半是
    整个操作栏，点它等于在页面上随便点一下。
    """
    assert comments._entry_kind("9 条评论") == "count"
    assert comments._entry_kind("\u200b 9 条评论") == "count", "零宽字符要能剥掉"
    assert comments._entry_kind("赞同 9 条评论") is None
    assert comments._entry_kind(selectors.COMMENT_EMPTY_TEXT) == "empty"


class _FakeEntrySession:
    """够 `_open_modal` 用：它只读 `page.url`，再调 `guard()` / `settle()`。"""

    def __init__(self) -> None:
        self.page = FakePage(ANSWER)
        self.settles = 0

    def guard(self) -> None:
        pass

    def settle(self) -> None:
        self.settles += 1


class _EntryButton:
    """假按钮：`element_text` 只调 `inner_text()`，`click_element` 只调 `click()`。"""

    def __init__(self, text: str, log: list) -> None:
        self.text = text
        self.log = log

    def inner_text(self) -> str:
        return self.text

    def click(self, **_kwargs) -> None:
        self.log.append(self.text)


class _EntryRun:
    """把 `_open_modal` 单独拎出来跑：驱动层全换成假的，只验**判据**。

    `buttons` 是页面上**所有 `.ContentItem-action` 类按钮的文案**——
    回答卡片上还有赞同、收藏、分享那些，所以要一个一个按文案筛，
    不能按位置取。

    `clicked` 记的是"到底点了没有"：「添加评论」= 0 条评论**不许点**
    这条规则只有它验得出来——真点下去也不报错，只是弹窗永远出不来，
    然后整轮流程走了个空。
    """

    def __init__(self, *buttons: str, opens: bool = True) -> None:
        self.clicked: list[str] = []
        self.buttons = [_EntryButton(text, self.clicked) for text in buttons]
        self.opens = opens
        self.session = _FakeEntrySession()
        self.report = comments.CommentsReport(url=ANSWER)

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(comments.content, "scope_selector", lambda url: None)
        monkeypatch.setattr(comments.drive, "element_text", lambda el: el.inner_text())
        monkeypatch.setattr(comments.drive, "click_element", self._click)
        monkeypatch.setattr(comments.drive, "all_of", self._all_of)

    def _click(self, el, **_kwargs) -> bool:
        el.click()
        return True

    def _all_of(self, _target, selector) -> list:
        # 同一个入口被问两件事：弹窗开没开、有哪些按钮候选。
        if selector == selectors.MODAL_PANELS:
            return [object()] if (self.clicked and self.opens) else []
        return list(self.buttons)

    def run(self, monkeypatch: pytest.MonkeyPatch) -> bool:
        self.install(monkeypatch)
        return comments._open_modal(self.session, self.report)


def test_zero_comments_means_do_not_click(monkeypatch: pytest.MonkeyPatch):
    """⭐ 入口文案是「添加评论」= 0 条评论，**一个点击都不许发**。

    用户实测：那种按钮点开是个空的评论输入框，不是弹窗。点了之后
    "找面板、滚列表"全套流程都会落空——而结果是"采到 0 条"，
    和"这条内容真没人评论"在数字上一模一样。
    """
    run = _EntryRun(selectors.COMMENT_EMPTY_TEXT, "赞同 1.2 万", "收藏")

    assert run.run(monkeypatch) is False, "没开弹窗"
    assert run.clicked == [], "「添加评论」绝不能点"
    assert run.report.no_comments is True
    assert run.report.declared == 0
    assert run.report.ok, "0 条评论是**正常结果**，不是失败"
    assert "0 条评论" in run.report.describe()


def test_a_counted_button_is_clicked_and_its_number_becomes_declared(
    monkeypatch: pytest.MonkeyPatch,
):
    """有评论就点，而且**数字在弹窗打开之前就拿到了**。

    这是这次改入口顺带赚到的：总数原本只能等弹窗标题渲染出来才读得到，
    现在印在按钮上——弹窗万一没开，`declared` 也还在，完整性核算还有基准。
    """
    run = _EntryRun("赞同 1.2 万", "1623 条评论", "收藏")

    assert run.run(monkeypatch) is True
    assert run.clicked == ["1623 条评论"], "只许点那一个，别的按钮一个都不许碰"
    assert run.report.declared == 1623
    assert run.report.opened is False, "`opened` 由调用方在成功之后置位"
    assert run.report.no_comments is False
    assert run.session.settles == 1, "点完要等渲染，不能立刻往下找面板"


def test_a_button_that_does_not_open_the_modal_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """点了却没出弹窗 = 确凿的失败，`no_comments` 必须是 False。"""
    run = _EntryRun("1623 条评论", opens=False)

    assert run.run(monkeypatch) is False
    assert run.clicked == ["1623 条评论"]
    assert run.report.no_comments is False, "点不开不能算成'这条没人评论'"
    assert not run.report.ok


def test_an_unrecognised_entry_text_is_refused_not_treated_as_zero(
    monkeypatch: pytest.MonkeyPatch,
):
    """⭐ 文案既不是「N 条评论」也不是「添加评论」时，**报错，不当成 0 条**。

    当成 0 条的话，选择器一失效，每一次采集都会安静地少一条内容，
    而且报告上写着"0 条评论"——一个看起来完全正常的结论。
    """
    run = _EntryRun("评论 1623")

    assert run.run(monkeypatch) is False
    assert run.clicked == []
    assert run.report.no_comments is False
    assert run.report.declared is None
    assert not run.report.ok


def test_no_entry_button_at_all_is_a_failure(monkeypatch: pytest.MonkeyPatch):
    """一个入口都找不到 = 选择器要重新校准。**同样不当成 0 条。**"""
    run = _EntryRun()

    assert run.run(monkeypatch) is False
    assert run.clicked == []
    assert run.report.no_comments is False
    assert not run.report.ok


def test_two_candidates_are_refused_rather_than_guessed(
    monkeypatch: pytest.MonkeyPatch,
):
    """⭐ 两个入口候选 = **报错，不挑一个**。

    问题页的整页作用域就会是这样：问题头一个按钮 + 每个回答各一个。
    挑错的后果是"拿别人的评论挂到这条内容上"——采集照常成功、日志一片干净。
    """
    run = _EntryRun("9 条评论", "1623 条评论")

    assert run.run(monkeypatch) is False
    assert run.clicked == [], "有歧义时**一个都不许点**"
    assert run.report.no_comments is False, "有歧义不等于'这条没人评论'"
    assert run.report.declared is None, "同样不该从任何一个候选上抄数字"
    assert not run.report.ok


def test_the_modal_is_never_opened_twice(monkeypatch: pytest.MonkeyPatch):
    """弹窗已经开着（调用方自己点过）就直接算成功，一个点击都不发。"""
    run = _EntryRun("1623 条评论")

    def already_open(_target, _selector) -> list:
        return [object()]

    monkeypatch.setattr(comments.content, "scope_selector", lambda url: None)
    monkeypatch.setattr(comments.drive, "all_of", already_open)

    assert comments._open_modal(run.session, run.report) is True
    assert run.clicked == []


def test_extract_comments_reports_zero_comments_without_failing(
    monkeypatch: pytest.MonkeyPatch,
):
    """一路走下来：0 条评论该是**通过**，不是失败，而且要说清楚原因。"""
    monkeypatch.setattr(comments.selectors, "require_calibrated", lambda *names: None)

    def zero(session, report) -> bool:
        report.entry_text = selectors.COMMENT_EMPTY_TEXT
        report.no_comments = True
        report.declared = 0
        return False

    monkeypatch.setattr(comments, "_open_modal", zero)

    report = comments.extract_comments(FakeSession(ANSWER), ANSWER)

    assert report.ok
    assert report.collected == 0
    assert "0 条评论" in report.describe()
    assert "共 1 条内容、0 条评论；失败 0 条" == comments.summarize([report])


# ── 辅助 ────────────────────────────────────────────────────────────


_ZERO_WIDTH = re.compile(r"[\u200b-\u200d\ufeff]")
"""零宽字符：U+200B 零宽空格 / U+200D 零宽连接符 / U+FEFF BOM。

⚠️ **必须写成转义序列。** 把真字符敲进源码的话，文件里就有几个看不见的
字符——读的人看不出这行在干什么，想删都选不中。和 `parse._ZERO_WIDTH_RE`
是同一组字符，改动要同步。
"""


def _norm(text: object) -> str:
    """复刻 `drive._FIND_TEXT` 的归一化：剥零宽 + 空白塌缩 + 去首尾。"""
    return re.sub(r"\s+", " ", _ZERO_WIDTH.sub("", str(text or ""))).strip()


def _closest_matches(element, selector: str) -> bool:
    """`element.closest(selector)`：元素自己或它的任一祖先匹配。"""
    cur = element
    while cur is not None and getattr(cur, "name", None):
        if soupsieve.match(selector, cur):
            return True
        cur = cur.parent
    return False


def _find_text_like_js(scope, pattern: str, *, exclude: str = "", inside: str = ""):
    """在 Python 里复刻 `drive._FIND_TEXT` 的查找语义。

    浏览器侧的 `_FIND_TEXT` 没法离线跑，但它的判据（归一化后的整串匹配、
    文档顺序优先、排除/限定某类祖先）可以在这里照样实现一遍。
    **这样 `_COMMENT_TOTAL_PATTERN` 就是对着真快照验过的，而不是"看起来对"。**

    ⚠️ `exclude` 只支持 `[属性]` 这一种写法——正好是调用方传的形态。
    传别的形式会断言失败，而不是安静地不排除。
    """
    attr = None
    if exclude:
        match = re.fullmatch(r"\[([\w-]+)\]", exclude)
        assert match, f"这个测试助手只支持 [属性] 形式的 exclude，收到 {exclude!r}"
        attr = match.group(1)

    regex = re.compile(pattern)
    for node in scope.find_all(string=True):
        if not regex.search(_norm(node)):
            continue
        element = node.parent
        if element is None:
            continue
        if attr and element.find_parent(attrs={attr: True}) is not None:
            continue
        if inside and not _closest_matches(element, inside):
            continue
        return element
    return None


def _has_exact_text(scope, text: str) -> bool:
    """子树里有没有**整串等于** `text` 的文本节点（归一化之后）。"""
    return any(_norm(node) == text for node in scope.find_all(string=True))


def _item(zhihu_id: str, *, parent: str | None, text: str = "正文") -> parse.ParsedItem:
    return parse.ParsedItem(
        url=f"https://www.zhihu.com/comment/{zhihu_id}",
        content_type="comment",
        zhihu_id=zhihu_id,
        question_id=None,
        parent_id=parent,
        text=text,
        author_name="某人",
        author_url="https://www.zhihu.com/people/someone",
    )


class FakeSession:
    """够 `extract_comments` 开头那段用的假会话——只记 `open()` 收没收到导航开关。"""

    def __init__(self, page_url: str | None) -> None:
        self.page = FakePage(page_url)
        self.navigations: list[bool] = []

    def open(self, url: str, *, navigate: bool = True):
        self.navigations.append(navigate)
        return self.page

    def guard(self) -> None:
        pass


class FakePage:
    """只有地址栏的假页面。

    `page_url=None` 时读 `.url` 会抛——复刻"地址栏拿不到"那条路
    （`drive.page_url` 在页面正在跳转时就会这样）。
    """

    def __init__(self, page_url: str | None) -> None:
        self._page_url = page_url

    @property
    def url(self) -> str:
        if self._page_url is None:
            raise RuntimeError("页面正在跳转，地址栏读不到")
        return self._page_url




# ── 问题页：回答卡片里"就地展开"的评论区（能力四）────────────────────

QUESTION = "anwserNew.html"
"""问题页快照：13 张回答卡，其中**两张的评论区已经展开着**。

展开的这两张正好把两种形态各占一份，是这次改动唯一的一手证据：

  * `2127524338`：16 条 `[data-id]`，**有**「点击查看全部评论」→ 走弹窗那一支；
  * `437149444`：7 条 `[data-id]`，**没有**那个按钮 → 走就地采那一支。

⚠️ 快照里**没有**"点了「点击查看全部评论」之后"的形态，所以弹窗那一支
（`_collect_via_modal`）**还没有真实页面校准过**。上面那个按钮的有无是
判据，但判据成立 ≠ 点下去真的开出 `.Modal-content`。
"""


def _card(answer_id: str):
    """快照里那张回答卡片。"""
    soup = BeautifulSoup(snapshot(QUESTION), "html.parser")
    return soup.select_one(f"{selectors.ANSWER_ITEM}[name='{answer_id}']")


def _inline(answer_id: str):
    """那张卡片里就地展开的评论区。"""
    return _card(answer_id).select_one(selectors.INLINE_COMMENTS)


def _parse_inline(answer_id: str) -> list[parse.ParsedItem]:
    """按 `_collect_inline` 的取法（`roots_only`）解析那块内联区。"""
    box = _inline(answer_id)
    roots = [el for el in box.select(selectors.COMMENT_ITEM) if not el.find_parent(selectors.COMMENT_ITEM)]
    items: list[parse.ParsedItem] = []
    for el in roots:
        items.extend(parse.parse_comment_list(str(el), answer_id))
    return items


def test_the_question_page_card_needs_its_scope(monkeypatch: pytest.MonkeyPatch):
    """⭐ 问题页整页有几十个入口候选，限定到卡片之后正好 **1** 个。

    和回答页（`answer.html`）是同一个道理，但量级大得多：问题页每张回答卡
    都带一个自己的评论按钮。不限定作用域就会点到**别的卡片**上，
    弹窗照开、评论照采、日志一片干净，只是挂错了内容。
    """
    whole = _entry_buttons(monkeypatch, QUESTION)
    assert len(whole) > 1, "整页就有歧义——所以「候选必须恰好一个」会在这里拦住"

    scoped = _entry_buttons(monkeypatch, QUESTION, "[name='2006572065']")
    assert [(_norm(text), kind) for _el, text, kind in scoped] == [("1566 条评论", "count")]


def test_an_already_expanded_card_has_no_entry_button(monkeypatch: pytest.MonkeyPatch):
    """⭐ 评论区展开着的那两张卡，入口候选是 **0** 个——按钮已经变成「收起评论」。

    这是 `extract_answer_comments` 会**报错跳过**的情形。0 个候选绝不能
    当成"这条没有评论"：一个是"认不出入口"，一个是"确实没有"，
    而两者的数字都是 0，`describe()` 里必须分得开（那条路靠 `no_comments`）。
    """
    for answer_id in ("2127524338", "437149444"):
        found = _entry_buttons(monkeypatch, QUESTION, f"[name='{answer_id}']")
        assert found == [], f"{answer_id} 的评论区展开着，认不出「N 条评论」入口"
        assert _has_exact_text(
            _card(answer_id), selectors.COLLAPSE_COMMENTS_TEXTS[0]
        ), "认不出来的原因就是按钮文案已经变成「收起评论」"


def test_the_view_all_button_decides_the_branch():
    """⭐ 有没有「点击查看全部评论」，就是走弹窗还是就地采的分岔判据。

    这正是用户说的那个坑：问题页上点「N 条评论」**不开弹窗**，而是就地摊开；
    只有评论多的时候，那块里才多一个按钮通向真正的弹窗。
    """
    assert _has_exact_text(_inline("2127524338"), selectors.VIEW_ALL_COMMENTS_TEXTS[0]), (
        "评论多的那块应该有「点击查看全部评论」"
    )
    assert not _has_exact_text(_inline("437149444"), selectors.VIEW_ALL_COMMENTS_TEXTS[0]), (
        "评论少的那块没有这个按钮——所以它里面就是全部"
    )


def test_the_inline_container_parses_like_the_modal():
    """内联区和弹窗里的评论**结构相同**，现有解析器直接能用，不用另写一份。

    ⚠️ `parent_id` 必须挂到**那条回答**上（不是问题、不是外层卡片）——
    挂错了这批评论在库里就归属到别人名下，而那不报错。
    """
    assert len(_parse_inline("2127524338")) == 16
    assert len(_parse_inline("437149444")) == 7

    items = _parse_inline("437149444")
    assert {item.parent_id for item in items} == {"437149444"}
    assert all(item.zhihu_id for item in items), "评论 ID 是 not null，取不到就存不进库"
    assert all(item.has_body for item in items), "这条帖子里没有纯图片评论"


def test_an_inline_report_is_ok_even_though_no_modal_ever_opened():
    """⭐ `ok` 的第一道门是 `opened`，而**就地展开根本没有弹窗**。

    不把 `inline` 也算成"看到了评论区"的话，走这一支的**每一条回答**都会被
    `_report_finish` 当成采集故障报 error——数据是对的、报告是错的，
    而报告错了就等于没有判据（这个项目最怕的就是判据失灵）。
    """
    report = comments.CommentsReport(
        url="https://www.zhihu.com/answer/437149444", inline=True
    )
    report.items = [_item("480982883", parent="437149444")]
    report.declared = 8

    assert report.ok, "就地采到的评论是正常结果，不是故障"


def test_an_inline_report_never_claims_it_scrolled_to_the_end():
    """⭐ 就地采不滚，所以 `scroll` 那个占位值**不能**被说成"已到末尾"。

    `scroll` 是填给 `_report_finish` 免得每条都报一次 warning 的，
    不是真滚过。让它照常输出「评论已到末尾：0 轮」就是**编**了一个
    没发生过的动作，读日志的人会以为滚动加载跑过了。
    """
    report = comments.CommentsReport(
        url="https://www.zhihu.com/answer/437149444", inline=True
    )
    report.items = [_item("480982883", parent="437149444")]
    report.declared = 8

    text = report.describe()
    assert "已到末尾" not in text
    assert "弹窗" not in text, "内联这一支没有弹窗，措辞得跟着换"
    assert "就地展开" in text and "未滚动" in text


def test_extract_answer_comments_never_navigates():
    """⭐ 这条路径**不导航**：页面必须停在问题页上。

    导航到那条回答自己的地址就前功尽弃了——**单独回答页上点同一个按钮
    开的是弹窗**，正是问题页这条路要绕开的形态。
    """
    source = inspect.getsource(comments.extract_answer_comments)
    assert "session.open(" not in source


# ── 问题页的弹窗（「点击查看全部评论」开出来的那个）──────────────────

QA_MODAL = "anwser_comment.html"
"""问题页上**点开「点击查看全部评论」之后**的快照。

这份快照回答了一个悬了两天的问题：这个入口开出来的到底是不是能力二那套结构。
**是。** 逐项对过，和 `comments_modal.html` 一模一样：

  * `.Modal-content` 1 个，`.Modal-content > div` 1 个，都是 `css-tpyajk`；
  * 面板的直接子元素都是 `css-1onritu`（头）+ `css-34podr`（体）+ 一个输入框；
  * 关闭按钮都是 `button[aria-label='关闭']`，整页**只有 1 个**。

唯一不同的是**量**：这里声明 72 条、DOM 里只有 26 个根——能力二那份声明 9 条、
DOM 里 9 个，`stop_early` 一上来就成立，**滚动循环从来没真正滚过**。
所以"滚动采不全"这条路上，这份快照是第一份真证据。
"""


def _modal_panel():
    return BeautifulSoup(snapshot(QA_MODAL), "html.parser").select_one(selectors.MODAL_PANELS)


def test_the_question_page_modal_is_the_same_structure_as_the_answer_page_one():
    """⭐ 问题页这个入口开出来的是**同一套** `.Modal-content`，不是新形态。

    这条一旦成立，`_comments_panel` / `_collect_first_level` 就都还能用；
    不成立的话整条弹窗支路都是白写的——所以把它钉住。
    """
    qa = _modal_panel()
    answer_page = panels("comments_modal.html")[0]
    assert qa is not None
    assert [c.get("class") for c in qa.find_all(recursive=False)] == [
        c.get("class") for c in answer_page.find_all(recursive=False)
    ], "问题页弹窗和回答页弹窗的面板结构应当一致"


def test_the_question_page_modal_declares_more_than_it_renders():
    """⭐ 声明 72 条，DOM 里只有 26 个根——**必须滚动才采得全**。

    这是"滚不动"会**安静地少采**的量化证据：不滚就是 26/72，
    而这个差额只有 `describe()` 的条数核算看得出来。
    """
    soup = BeautifulSoup(snapshot(QA_MODAL), "html.parser")
    panel = soup.select_one(selectors.MODAL_PANELS)
    roots = [el for el in panel.select(selectors.COMMENT_ITEM) if not el.find_parent(selectors.COMMENT_ITEM)]
    assert len(roots) == 26
    # 走"整串匹配文本节点"那条路，和 `_collect_first_level` 读声明数的方式一样
    header = _find_text_like_js(
        panel, comments._COMMENT_TOTAL_PATTERN, exclude=f"[{selectors.COMMENT_ID_ATTR}]"
    )
    assert parse.parse_comment_total(header.get_text(" ", strip=True)) == 72


def test_the_close_button_is_outside_the_panel_it_closes():
    """⭐ 关闭叉是 `.Modal-content` 的**兄弟**，不在面板里。

    ⚠️ 所以 `_close_modal` 只能**整页**找（`drive.first(session.page, …)`），
    改成"在面板里找关闭按钮"就会永远找不到——而它的失败形态是
    **弹窗留着、污染后面每一条回答**，不是当下报错。
    """
    soup = BeautifulSoup(snapshot(QA_MODAL), "html.parser")
    panel = soup.select_one(selectors.MODAL_PANELS)
    modal = soup.select_one(selectors.COMMENT_MODAL)
    closer = soup.select(selectors.COMMENT_MODAL_CLOSE)

    assert len(closer) == 1, "整页只该有一个关闭按钮，多了就不能盲点第一个"
    assert panel.select(selectors.COMMENT_MODAL_CLOSE) == [], "面板里没有关闭按钮"
    assert closer[0].find_parent(selectors.COMMENT_MODAL) is None, "它在 .Modal-content 外面"
    assert closer[0].parent is modal.parent.parent, "它挂在 .Modal-content 的祖父节点上"
    assert closer[0].parent in modal.parents, "和 .Modal-content 同属那层浮层"


def test_the_scroller_is_chosen_by_whether_it_can_actually_scroll():
    """⭐ 判据是**计算样式的 overflow-y**，不是"写 scrollTop 试一下"。

    ⚠️ 这条钉的是个反直觉的坑：写 `scrollTop` 再读回来看着更"实测"，但
    `scroll-behavior: smooth` 下赋值后立刻读，动画还没起步，**真能滚的**
    会被读成"没动"。宁可查样式，不要制造这个假阴性。
    """
    js = drive._SCROLL_CONTAINER
    assert "getComputedStyle" in js and "overflowY" in js
    assert "scrollTop =" not in js, "别用写 scrollTop 的探针：smooth 滚动会骗人"


class _FakeModalSession:
    """够 `_collect_via_modal` 收尾那一段用：只记 `guard()` / `settle()`。

    `settle(ready=…)` 的 `ready` 要**收下来**：能力四的"等评论区展开"就是靠它，
    而"到底传没传"正是要断言的东西（见 `test_opening_the_comments_polls_…`）。
    """

    def __init__(self) -> None:
        self.page = object()
        self.settles = 0
        self.paces = 0
        self.settle_readies: list[object] = []

    def guard(self) -> None:
        pass

    def settle(self, ready=None) -> None:
        self.settles += 1
        self.settle_readies.append(ready)

    def pace(self, *_args, **_kwargs) -> None:
        self.paces += 1


def _stub_scroll(
    monkeypatch: pytest.MonkeyPatch, *, found: bool = True, census=(), offsets=(0, 0)
) -> None:
    """把滚动那一整套换成假的：只留"找不找得到滚动容器"这一个变量。

    `offsets` 是 `probe_scroller` **依次**吐出来的位置读数（滚动前一个、滚动后一个）。
    两者相等 = 那个元素一步都没动，也就是"滚的不是真正会滚的东西"。
    """
    monkeypatch.setattr(comments.drive, "find_scroll_container", lambda *a, **k: (object() if found else None))
    monkeypatch.setattr(comments.drive, "describe_scroll_candidates", lambda *a, **k: list(census))
    monkeypatch.setattr(comments.drive, "has_text", lambda *a, **k: False)
    monkeypatch.setattr(comments.drive, "Harvester", lambda *a, **k: (lambda: 0))
    reads = iter(
        {
            "label": "div.css-34podr",
            "viewport": 600,
            "content": 9000,
            "offset": offset,
            "overflowY": "auto",
        }
        for offset in offsets
    )
    monkeypatch.setattr(comments.drive, "probe_scroller", lambda *a, **k: next(reads, None))
    monkeypatch.setattr(
        comments.scrolling,
        "scroll_until_exhausted",
        lambda **_kw: comments.scrolling.ScrollOutcome(3, 0, comments.scrolling.StopReason.STABLE),
    )


def _collect_into(report: comments.CommentsReport) -> None:
    comments._collect_first_level(
        _FakeModalSession(), object(), report, comments._Sink(report, None, None), 10, 4
    )


def _ready_report() -> comments.CommentsReport:
    """一份"除了滚动目标没动之外哪都正常"的报告。

    ⚠️ 这些字段不是摆设：`ok` 有好几道门（开没开、采没采到），不带它们的话
    `assert report.ok is False` 会因为别的门而恒真——那就测不到 `scroll_stuck`。
    """
    report = comments.CommentsReport(url=ANSWER, declared=72, opened=True)
    report.items.append(object())  # 只用来让 `collected` 不为 0
    return report


def test_the_scroller_search_climbs_out_of_the_panel():
    """⭐ 面板里找不到滚动容器时，要**往祖先找**。

    ⚠️ 问答页那个评论弹窗就是这个形状：面板（`.Modal-content > div`）里面
    一个能滚的都没有，真正吃滚动条的是它祖先那一层。只往下找会返回 null，
    调用方退回去滚面板本身——`scrollTop` 写不进去、**不报错也不抛异常**，
    于是滚动循环空转到 STABLE，用户看到的就是「评论本身不滚动」。
    """
    js = drive._SCROLL_CONTAINER
    assert "if (climb)" in js, "只往下找的版本在'滚动条挂在面板外面'时会安静地不滚"
    assert "parentElement" in js.split("if (climb)")[1]


def test_the_scroller_never_climbs_onto_the_page_itself():
    """⭐ 往上找**必须停在 `body` 之前**。

    再往上就是 `body` / `html`，滚它们等于滚整个页面——那正是用户报的现象：
    「评论没滚，背后的回答页面在滚」。宁可返回 null，让调用方去报错。
    """
    climb_block = drive._SCROLL_CONTAINER.split("if (climb)")[1]
    assert "el !== document.body" in climb_block, "往上找没有在 body 之前停住"
    assert "documentElement" not in climb_block, (
        "往上找的那一段里出现了 documentElement：那等于允许滚整个页面"
    )


def test_the_scroll_target_is_named_in_the_report(monkeypatch: pytest.MonkeyPatch, caplog):
    """⭐ "我们在滚谁"要写进报告和日志。

    ⚠️ 没有这一行，"滚错了元素"和"真到底了"在日志里长得**一模一样**
    （都是"连续 N 轮无新增"），只能靠猜。
    """
    _stub_scroll(monkeypatch, offsets=(0, 2000))
    report = _ready_report()

    with caplog.at_level("INFO"):
        _collect_into(report)

    assert "div.css-34podr" in report.scroll_target
    assert "overflowY=auto" in report.scroll_target
    assert "位置=0" in report.scroll_target
    assert "滚动目标" in caplog.text


def test_a_scroll_target_that_never_moves_is_reported(
    monkeypatch: pytest.MonkeyPatch, caplog
):
    """⭐⭐ 滚了若干轮、目标位置一步没动 = **我们滚的不是那个会滚的东西**。

    这是用户 2026-09-27 报的现象的判据：「评论本身不滚动，背后的页面在滚动」。
    以前这种情况是**完全静默**的：循环空转到 STABLE，日志说"可能是真到底"。
    """
    _stub_scroll(
        monkeypatch,
        offsets=(0, 0),
        census=["    div.css-34podr 视口=600 内容=9000 overflowY=visible ← 写不进去，不算滚动容器"],
    )
    report = _ready_report()

    with caplog.at_level("ERROR"):
        _collect_into(report)

    assert report.scroll_stuck is True
    assert report.ok is False, "滚不动还判成功，等于把'只采到首屏'当成了采全"
    assert "一步都没动" in report.describe()
    assert "一步都没动" in caplog.text
    assert "div.css-34podr" in caplog.text, "报错要带上现场清单，不然没法定位"


def test_a_scroll_target_that_actually_moves_is_not_reported(monkeypatch: pytest.MonkeyPatch):
    """对照组：位置动了就不该报"滚不动"——否则这个判据只是恒真的噪音。"""
    _stub_scroll(monkeypatch, offsets=(0, 2000))
    report = _ready_report()

    _collect_into(report)

    assert report.scroll_stuck is False
    assert report.ok is True


def test_a_target_that_stays_put_is_fine_when_the_count_is_already_reached(
    monkeypatch: pytest.MonkeyPatch,
):
    """位置没动但**声明数已经够了** = 内容本来就只有一屏，不是滚不动。

    ⚠️ 少了这道门，每一个"评论刚好一屏"的帖子都会被刷一条假故障。
    """
    _stub_scroll(monkeypatch, offsets=(0, 0))
    report = comments.CommentsReport(url=ANSWER, declared=1, opened=True)
    report.items.append(object())

    _collect_into(report)

    assert report.scroll_stuck is False


def test_probing_a_dead_handle_is_not_an_error():
    """句柄失效读不出来是**正常**的：返回 None，调用方按"没证据"处理。

    ⚠️ 不能把读不出来当成"没动"——那会给每一次句柄失效都刷一条假故障。
    """
    assert drive.probe_scroller(None) is None
    assert drive.probe_scroller(object()) is None  # 没有 evaluate()


def test_describe_scroller_names_the_element():
    line = drive.describe_scroller(
        {
            "label": "div.css-34podr",
            "viewport": 600,
            "content": 9000,
            "offset": 400,
            "overflowY": "auto",
        }
    )
    assert "div.css-34podr" in line
    assert "overflowY=auto" in line
    assert "位置=400" in line
    assert drive.describe_scroller(None) == "（读不出来）"


def test_a_missing_scroller_says_what_it_saw(monkeypatch: pytest.MonkeyPatch, caplog):
    """⭐ 找不到滚动容器时，报错里要带**现场清单**。

    "找不到"三个字分不开两种病：容器在弹窗外面（往下找永远找不到），
    还是找到了但 `overflow: visible` 写不进去。这两种修法不同、日志却一样，
    所以我不能跑浏览器、只能靠这条清单在下一次实跑时读出结论。
    """
    census = [
        "  弹窗里内容超出视口的元素：",
        "    div.css-34podr 视口=600 内容=3000 overflowY=visible ← 写不进去，不算滚动容器",
    ]
    _stub_scroll(monkeypatch, found=False, census=census)
    report = comments.CommentsReport(url=ANSWER)

    with caplog.at_level("ERROR"):
        comments._collect_first_level(
            _FakeModalSession(), object(), report, comments._Sink(report, None, None), 10, 4
        )

    assert report.scroll_container_found is False
    logged = caplog.text
    assert "找不到**滚得动**的元素" in logged
    assert "div.css-34podr" in logged and "写不进去" in logged


def test_the_modal_gets_closed_even_when_the_panel_is_unrecognisable(
    monkeypatch: pytest.MonkeyPatch,
):
    """⭐ 认不出面板就返回时，弹窗**也必须关掉**。

    ⚠️ 这是真踩过的洞：`return` 一走，弹窗就留在页面上，下一条回答的采集
    紧接着在**同一个弹窗**里做——采到的是上一条的评论，而日志一片干净。
    所以关弹窗在 `finally` 里，不在正常路径的末尾。
    """
    closed: list[str] = []
    monkeypatch.setattr(comments.drive, "click_text", lambda *a, **k: "点击查看全部评论")
    monkeypatch.setattr(comments, "_comments_panel", lambda session: None)
    monkeypatch.setattr(comments, "_close_modal", lambda session, report: closed.append("closed"))
    report = comments.CommentsReport(url=ANSWER)

    comments._collect_via_modal(
        _FakeModalSession(), object(), report, comments._Sink(report, None, None), 10, 4
    )

    assert closed == ["closed"], "面板认不出来时弹窗被留在了页面上"
    assert report.opened is False, "没认出面板就不算开着"


def test_the_modal_is_closed_before_the_card_is_collapsed():
    """⭐ 顺序：先关弹窗，再收卡片。

    ⚠️ 反过来的话，点「收起评论」时弹窗那层遮罩还盖在卡片上，点击被吃掉——
    卡片收不起来，还要多刷一条假 warning，把真正的故障埋掉。
    """
    source = inspect.getsource(comments.extract_answer_comments)
    assert source.index("_close_modal(session, report)") < source.index(
        "_collapse_card(session, scope, report)"
    ), "先收卡片再关弹窗，收起那一下会被弹窗遮罩吃掉"


# ── 点开评论区：等它真的展开（2026-09-27 实跑踩的坑）──────────────────

ANSWER_ID = "1970296996372934942"
"""实跑里**第一条**回答的 ID——正是"评论永远打不开"的那一条（入口声明 72 条）。"""


class _Board:
    """`extract_answer_comments` 那一趟的假驱动：只关心**分支怎么选**。"""

    def __init__(self, *, area: str = "", view_all: bool = False) -> None:
        self.area = area
        self.view_all = view_all
        self.calls: list[str] = []
        self.settle_readies: list[object] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        card = object()
        monkeypatch.setattr(comments.selectors, "require_calibrated", lambda *names: None)
        monkeypatch.setattr(comments.content, "scope_selector", lambda url: f"[name='{ANSWER_ID}']")
        monkeypatch.setattr(comments.drive, "first", lambda *a, **k: card)
        # ⚠️ 文案里的零宽空格是**照抄实跑的**（日志里那条入口文案带着它，
        #    写成转义而不是字面量——看不见的字符塞在源码里没人 debug 得动）。
        monkeypatch.setattr(
            comments, "_entry_buttons", lambda target: [(object(), "\u200b72 条评论", "counted")]
        )
        monkeypatch.setattr(comments.drive, "click_element", lambda el, **k: True)
        monkeypatch.setattr(comments.drive, "has_text", lambda *a, **k: self.view_all)
        monkeypatch.setattr(comments, "_comment_area", lambda session, scope: self.area)
        monkeypatch.setattr(comments, "_close_modal", lambda s, r: self.calls.append("close"))
        monkeypatch.setattr(comments, "_collapse_card", lambda s, sc, r: self.calls.append("collapse"))
        monkeypatch.setattr(comments, "_report_finish", lambda report: None)
        monkeypatch.setattr(
            comments, "_collect_open_modal", lambda *a, **k: self.calls.append("open_modal")
        )
        monkeypatch.setattr(
            comments, "_collect_via_modal", lambda *a, **k: self.calls.append("via_modal")
        )
        monkeypatch.setattr(comments, "_collect_inline", lambda *a, **k: self.calls.append("inline"))

    def run(self, monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], _FakeModalSession]:
        self.install(monkeypatch)
        session = _FakeModalSession()
        comments.extract_answer_comments(session, ANSWER)
        return self.calls, session


def test_opening_the_comments_polls_instead_of_looking_once(monkeypatch: pytest.MonkeyPatch):
    """⭐⭐ 点完入口后要**轮询等它展开**，不能看一眼就下结论。

    ⚠️ 这是 2026-09-27 实跑事故的直接原因：`settle()` 不给 `ready` 时等的是
    **固定时长**，保证不了任何元素已经在了（它自己的 docstring 就这么写的）。
    那次跑 5 条回答，**前两条**（紧跟在一趟很重的回答滚动之后）就是这么被误判成
    "卡片里找不到内联评论区"、采 0 条往下走的——用户看到的"第一条评论永远打不开"。

    所以这条钉的是**调用方式**：必须把"评论区出现了"当成 `ready` 条件传进去。
    """
    board = _Board(area="inline")
    _, session = board.run(monkeypatch)

    assert session.settle_readies, "点完入口没有把 ready 条件交给 settle——又变回看一眼了"
    assert all(callable(r) for r in session.settle_readies)


def test_a_modal_that_opens_straight_from_the_entry_is_not_clicked_again(
    monkeypatch: pytest.MonkeyPatch,
):
    """⭐ 入口**直接开出弹窗**时，别再去找「点击查看全部评论」。

    ⚠️ 这条形态是**推出来的**，没有快照直接证明：实跑日志里评论量大的那两条
    （72 / 52 条）点完入口后，卡片里既没有内联区、也没有那个按钮。既然弹窗
    已经在页面上，唯一正确的下一步就是**直接采它**——再去点一个不存在的按钮
    只会得到"认得出但点不动"的假错误。
    """
    board = _Board(area="modal", view_all=True)
    calls, _ = board.run(monkeypatch)

    assert "open_modal" in calls, "弹窗已经开着，应当直接采"
    assert "via_modal" not in calls, "不该再去点「点击查看全部评论」"
    assert calls.index("close") < calls.index("collapse"), "顺序：先关弹窗再收卡片"


def test_an_inline_area_with_the_button_still_goes_through_the_modal(
    monkeypatch: pytest.MonkeyPatch,
):
    """内联区里**有**「点击查看全部评论」= 评论多，走弹窗那一支（原判据不变）。"""
    board = _Board(area="inline", view_all=True)
    calls, _ = board.run(monkeypatch)
    assert calls[:1] == ["via_modal"]


def test_an_inline_area_without_the_button_is_collected_in_place(
    monkeypatch: pytest.MonkeyPatch,
):
    """内联区里**没有**那个按钮 = 评论少，就地采（原判据不变）。"""
    board = _Board(area="inline", view_all=False)
    calls, _ = board.run(monkeypatch)
    assert calls[:1] == ["inline"]


def test_an_inline_area_that_never_shows_up_is_not_called_in_place(
    monkeypatch: pytest.MonkeyPatch,
):
    """评论区**压根没出来**时，收尾照旧（关弹窗、收卡片）。"""
    board = _Board(area="", view_all=False)
    calls, _ = board.run(monkeypatch)
    assert calls[:1] == ["inline"], "仍要进去报一次错（采 0 条），但结论必须由它自己判"


def test_a_missing_inline_container_is_not_reported_as_collected_in_place(
    monkeypatch: pytest.MonkeyPatch,
):
    """⭐ 内联区没找到时**不能**标成"就地展开"。

    ⚠️ 标了的话 `describe()` 会照着内联那一支输出"按「这块就是全部」处理"——
    把"什么都没看到"说成"看过了，就这些"。这正是最该避免的那种谎报。

    ⚠️ 这条**不能用 `_Board`**：那边把 `_collect_inline` 换成了假函数，
    于是 `report.inline` 根本没人碰，断言会**空过**（变异测试抓到过这一次）。
    只换掉 `drive.first`，其余走真代码。
    """
    monkeypatch.setattr(comments.drive, "first", lambda *a, **k: None)
    report = comments.CommentsReport(url=ANSWER)

    comments._collect_inline(object(), report, comments._Sink(report, None, None))

    assert report.inline is False, "没找到内联区就不算就地展开"
    assert report.ok is False
    assert "评论区没能打开" in report.describe()
    assert "这块就是全部" not in report.describe()


def test_the_comment_area_checks_the_modal_before_the_card(monkeypatch: pytest.MonkeyPatch):
    """⭐ `_comment_area` 的顺序：**先看弹窗，再看卡片里有没有内联区**。

    ⚠️ 顺序不能反。弹窗是挂在 `body` 上的 portal，**不在卡片里**——先查卡片
    的话，一个"直接开出弹窗"的入口会被判成 `""`（没展开），于是既不采弹窗、
    又去点一个不存在的「点击查看全部评论」。
    """
    session = _FakeModalSession()
    scope = "[name='1']"
    card = object()

    # 弹窗在 → 一律算 modal，哪怕卡片里同时也有内联区
    monkeypatch.setattr(comments.drive, "all_of", lambda t, s: [object()])
    monkeypatch.setattr(comments.drive, "first", lambda t, s: card if s == scope else object())
    assert comments._comment_area(session, scope) == "modal"

    # 没有弹窗、卡片里也没有内联区 → 还没展开
    monkeypatch.setattr(comments.drive, "all_of", lambda t, s: [])
    monkeypatch.setattr(comments.drive, "first", lambda t, s: None)
    assert comments._comment_area(session, scope) == ""

    # 卡片在、内联区也在 → 就地展开
    monkeypatch.setattr(comments.drive, "first", lambda t, s: card if s == scope else object())
    assert comments._comment_area(session, scope) == "inline"
