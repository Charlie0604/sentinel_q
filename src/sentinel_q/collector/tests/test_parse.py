"""解析层测试。

分三部分，**越往后越值钱**：

**第一部分：字段级工具**（`parse_count` / `parse_datetime` / 评论标识）。
这些跟知乎的 DOM 结构无关，现在就能测死。它们也是最容易出**量级错误**的地方——
"1.2 万"解析成 1，赞同数就错了四个数量级，而错的数据比没有数据更危险。

**第二部分：抽取函数的管道**，用**合成 HTML**——只验证
`soup.select → urljoin → normalize → ParsedItem` 这条管道通不通，
**不验证选择器对不对**（用我猜的 HTML 去测我猜的选择器是循环论证）。

**第三部分：真实快照固件**（`TestAgainstRealSnapshots`）。**这才是校准的判据。**
断言的是**条数**，不只是"能解析出东西"——少一条就是静默漏采。

⚠️ 第三部分依赖 `runtime/calib/*.html`，那是**用户自己采集的真实页面**，
目录在 .gitignore 里。所以**在别人的机器上、在 CI 上，这些用例会 skip**。
这是刻意的：那份快照里带着用户的昵称、头像和主页链接，不能进仓库。

⚠️ 快照会随知乎改版而过期。它们**失败时先怀疑校准过期**，
而不是急着改断言——把 `selectors.py` 对着新快照重新校一遍才是正事。
"""

from __future__ import annotations

import ast
import collections
import functools
import pathlib
import re
from datetime import UTC, datetime

import pytest
from bs4 import BeautifulSoup

from sentinel_q.collector import parse, selectors

CALIB_DIR = pathlib.Path("runtime/calib")


# ── 一、字段级工具 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1234", 1234),
        ("1,234", 1234),
        ("1.2 万", 12_000),
        ("1.2万", 12_000),
        ("3.5 千", 3_500),
        ("赞同 42", 42),
        ("42 条评论", 42),
        ("", 0),
        (None, 0),
        ("暂无", 0),
        ("1.2 万 赞同", 12_000),
    ],
)
def test_parse_count(text: str | None, expected: int) -> None:
    """「万」必须正确处理——否则赞同数会差四个数量级。"""
    assert parse.parse_count(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("赞同 117  19 条评论 08-31", 19),
        ("19 条评论", 19),
        ("赞同 1.2 万  3 万条评论 08-31", 30_000),
        ("赞同 117  08-31", 0),  # 只有赞同数，没有评论数 → 0，不能拿赞同数顶替
    ],
)
def test_comment_count_never_mistakes_voteup_for_comments(text: str, expected: int) -> None:
    """⭐ 评论数必须取**紧挨着「条评论」**的那个数。

    搜索卡片的操作栏是一段**混杂文本**：

        <div class="ContentItem-actions"> 赞同 117   19 条评论 08-31 </div>

    而 `COMMENT_COUNT_BUTTON` 匹配到的**第一个**元素就是这个外层容器。
    早先这里写的是 `parse_count(text)`——它取第一个数字，于是返回 **117**，
    也就是**赞同数**，而调用方以为那是评论数。

    两个数在卡片上长得一模一样，存进库里肉眼核对都看不出来。
    正文页碰不到：那些页面有 `meta[itemprop='commentCount']`，走的是上面那条路。
    **搜索页没有这个 meta**，所以搜索一接上就会踩中。
    """
    soup = BeautifulSoup(f'<div class="ContentItem-action">{text}</div>', "html.parser")

    assert parse._comment_count(soup) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2024-01-01", datetime(2024, 1, 1, tzinfo=UTC)),
        ("编辑于 2024-01-01", datetime(2024, 1, 1, tzinfo=UTC)),
        ("2024年1月1日", datetime(2024, 1, 1, tzinfo=UTC)),
        ("2024-01-01 12:30", datetime(2024, 1, 1, 12, 30, tzinfo=UTC)),
        ("发布于 2024/3/5", datetime(2024, 3, 5, tzinfo=UTC)),
    ],
)
def test_parse_datetime_absolute(text: str, expected: datetime) -> None:
    assert parse.parse_datetime(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "3 小时前",  # 相对时间
        "昨天",
        "04-22",  # 评论的格式：不带年份，猜年份等于伪造证据
        "刚刚",
        "",
        None,
    ],
)
def test_parse_datetime_refuses_relative(text: str | None) -> None:
    """相对时间、缺年份的时间**刻意不换算**。

    「3 小时前」要变成时间戳，得假设"抓取那一刻就是发布后 3 小时"，
    但页面可能是缓存的上个月的快照。猜出来的时间戳用在法律证据上是负资产——
    它看起来精确，实际可能是错的。
    """
    assert parse.parse_datetime(text) is None


def test_parse_datetime_survives_impossible_dates() -> None:
    """页面不该有 2024-13-45，但真出现时不能炸掉整轮采集。"""
    assert parse.parse_datetime("2024-13-45") is None


def test_parse_iso_reads_microdata_stamps() -> None:
    """微数据里的 `datePublished` 是精确 ISO 值，比页面文案可靠。"""
    assert parse._parse_iso("2025-01-20T14:14:44.000Z") == datetime(
        2025, 1, 20, 14, 14, 44, tzinfo=UTC
    )
    assert parse._parse_iso("不是时间") is None
    assert parse._parse_iso(None) is None


def test_comment_identity_is_stable_without_an_id() -> None:
    """没有评论 ID 时退回内容哈希，**重跑必须得到同一个值**。

    它进了 `fact_content.url` 的唯一约束，一旦带时间戳或随机数，查重当场失效——
    每次重跑都会把同一批评论再插一遍，AI 也会被重复问。
    """
    args = ("https://www.zhihu.com/people/someone", "这条评论内容")

    assert parse._comment_identity(None, *args) == parse._comment_identity(None, *args)


def test_comment_identity_prefers_the_real_id() -> None:
    """有 `[data-id]` 就用它——那是知乎自己的主键，不会撞。

    退回哈希有个已知代价：**同一作者发的两条内容完全相同的评论会被判成一条**。
    有了真实 ID 这条代价基本不出现（刷屏也算两条）。
    """
    zhihu_id, url = parse._comment_identity("123", "https://www.zhihu.com/people/someone", "内容")

    assert zhihu_id == "123"
    assert url == "https://www.zhihu.com/comment/123"


def test_comment_identity_distinguishes_content_without_an_id() -> None:
    """不同内容必须得到不同的键，否则查重会把两条真评论当成一条。"""
    author = "https://www.zhihu.com/people/someone"

    assert parse._comment_identity(None, author, "甲") != parse._comment_identity(None, author, "乙")


# ── 二、抽取管道（合成 HTML，只验证管道，不验证选择器）──────────────
#
# ⚠️ 下面的 HTML 是**照着 selectors.py 里的选择器造出来的**，所以这些用例过了
#    只能说明管道通（urljoin 没写错、normalize 没拒绝、字段没接反），
#    **不能说明选择器对**。真正的验证在第三部分。


def _markup(css_group: str) -> str:
    """从 CSS 分组里取第一个**能写成 HTML 属性**的候选，用来造合成 HTML。

    支持三种写法，正好对应架构文档 3.11 的第 2、3 档：

        [itemprop='text']  →  itemprop="text"     语义化属性
        [data-id]          →  data-id="x"         只判存在、不判值的属性
        .RichText          →  class="RichText"    CSS 类名

    "第一个"是按 selectors.py 里的书写顺序（= 稳定性优先级）。像
    `[itemprop='author'] a` 这种"后代选择器"没法表达成单个元素的属性，
    跳到下一个候选。
    """
    for candidate in selectors.candidates(css_group):
        candidate = candidate.removeprefix(selectors.TODO).strip()
        if candidate.startswith(".") and " " not in candidate:
            return f'class="{candidate[1:]}"'
        if match := re.fullmatch(r"\[([\w-]+)=['\"]([^'\"]+)['\"]\]", candidate):
            return f'{match.group(1)}="{match.group(2)}"'
        if match := re.fullmatch(r"\[([\w-]+)\]", candidate):
            return f'{match.group(1)}="x"'
    raise AssertionError(f"{css_group} 里没有能写成 HTML 属性的候选，夹具造不出来")


def _search_html(href: str) -> str:
    """一张搜索结果卡片的最小结构。

    `.ContentItem-title` 这一层不能省——`SEARCH_RESULT_LINK` 是
    **后代选择器**（`.ContentItem-title a[href]`），少了它链接就找不着，
    而"找不着链接"在这条管道里表现为 `skipped_no_link`，不是报错。
    """
    return (
        f'<div {_markup(selectors.SEARCH_RESULT_ITEM)}>'
        f'<div class="ContentItem-title"><a href="{href}">一个标题</a></div>'
        f"</div>"
    )


def test_search_item_extracts_and_normalizes_url() -> None:
    item = parse.parse_search_item(_search_html("/question/123/answer/456"))

    assert item is not None
    assert item.url == "https://www.zhihu.com/question/123/answer/456"
    assert item.content_type == "answer"
    assert item.zhihu_id == "456"
    assert item.question_id == "123"


@pytest.mark.parametrize(
    "href",
    [
        "/question/abc/answer/456",  # 看着像内容链接，但 qid 不是数字
        "https://www.example.com/question/123",  # 站外域名
        "https://www.zhihu.com/topic/1955",  # 站内但不是内容
    ],
)
def test_search_item_rejects_links_the_url_layer_rejects(href: str) -> None:
    """被 `shared.urlnorm` 拒绝的链接必须返回 None，而不是造一条 content_type=None 的记录。

    `fact_content.content_type` 是 not null 且带 check 约束的，未分类的行根本存不进去。
    **调用方要统计被挡掉的数量**——这个数突然变大通常意味着选择器选宽了。
    """
    assert parse.parse_search_item(_search_html(href)) is None


def test_search_item_returns_none_without_link() -> None:
    assert parse.parse_search_item("<div>没有链接</div>") is None


def test_search_batch_counts_the_two_drop_reasons_separately() -> None:
    """两个丢弃原因**必须分开计**，否则"被挡掉的数量突然变大"没法归因：

        no_link      —— 卡片里根本没链接，多半是选择器没选中（**校准信号**）
        not_content  —— 有链接但不是内容页（话题、用户），是正常的
    """
    batch = parse.parse_search_batch(
        [
            _search_html("/question/123/answer/456"),  # 好
            "<div>这张卡片里没有链接</div>",  # no_link
            _search_html("https://www.zhihu.com/people/someone"),  # not_content
        ]
    )

    assert len(batch.items) == 1
    assert batch.skipped_no_link == 1
    assert batch.skipped_not_content == 1
    assert batch.total == 3


def test_comment_without_author_is_dropped() -> None:
    """拿不到作者的评论整条丢掉——无法归属的评论在取证上没有价值。"""
    html = f'<div {_markup(selectors.COMMENT_ITEM)}>一条评论</div>'

    assert parse.parse_comment_item(html) is None


def test_comment_parent_id_is_carried_through() -> None:
    """评论的 `parent_id` 由调用方给——一级评论挂的是**被评论的那条内容**。"""
    html = (
        f'<div {_markup(selectors.COMMENT_ITEM)}>'
        f'<a href="/people/someone">某人</a>'
        f'<div {_markup(selectors.COMMENT_CONTENT)}>评论内容</div>'
        f"</div>"
    )

    item = parse.parse_comment_item(html, parent_id="parent-1")

    assert item is not None
    assert item.parent_id == "parent-1"
    assert item.content_type == "comment"
    assert item.author_url == "https://www.zhihu.com/people/someone"


def test_comment_list_takes_only_first_level() -> None:
    """⭐ **嵌套的回复一条都不能吐出来**——这是"只采一级"的全部实现。

    回复是嵌套在父评论的 `[data-id]` 里的另一层 `[data-id]`，所以判据是
    "没有 `[data-id]` 祖先"。挑漏了**不会报错**：回复会被当成一条条独立的
    一级评论，`parent_id` 全指向正文，看起来完全正常。

    ⚠️ 反过来，`parse_comment_item`（单条）依旧认得嵌套——它按调用方给的
    `parent_id` 走，跟层级无关。两者用途不同，不要合并。
    """
    html = (
        '<div data-id="100"><a href="/people/a">甲</a>'
        '<div class="CommentContent">一级评论</div>'
        '<div data-id="200"><a href="/people/b">乙</a>'
        '<div class="CommentContent">二级回复</div>'
        '<div data-id="300"><a href="/people/c">丙</a>'
        '<div class="CommentContent">三级回复</div>'
        "</div></div></div>"
    )

    items = parse.parse_comment_list(html, content_id="999")

    assert [i.zhihu_id for i in items] == ["100"], "只留没有 [data-id] 祖先的那一条"
    assert items[0].parent_id == "999"


def test_the_second_level_reply_path_stays_deleted() -> None:
    """⭐ 二级回复那条路的符号**一个都不许回来**（防止删一半、或者被好心加回去）。

    回复不采是用户 2026-09-27 定的取舍（点开面板就关不掉、回不到原来那条评论、
    每点一次都要重开页面等很久），理由写在 `comments.py` 模块开头。
    但**半截的删除比不删更糟**：留下的 `_next_expand` / `parse_reply_panel`
    再被谁接上一根线，就会重新采一批挂错父级的评论，而且**不报错**。

    ⚠️ 查的是**定义**（AST），不是文本——注释和文档里提这些名字是允许的，
    它们正是在解释"为什么没有这条路"。
    """
    forbidden = {
        "parse.py": {
            "parse_comment_thread",
            "parse_reply_panel",
            "parse_reply_count",
            "parse_reply_expand_count",
            "ReplyPanel",
            "_walk_comments",
            "_child_comments",
        },
        "comments.py": {
            "MAX_EXPANDS",
            "_next_expand",
            "_expand_replies",
            "_absorb_reply_panel",
            "_close_reply_panel",
            "_reply_panel_gone",
            "_absorb_comment_subtree",
            "_comment_node_selector",
            "_PanelStuck",
            "_GrowthCounter",
        },
        "drive.py": {"find_panel", "closest_attr", "element_html"},
        "selectors.py": {
            "REPLY_ITEM",
            "REPLY_EXPAND_BUTTON",
            "REPLY_EXPAND_TEXTS",
            "REPLY_EXPAND_ANCHOR",
            "REPLY_BACK_ICON",
            "COMMENT_PANEL_TITLE_ANCHOR",
        },
    }

    for filename, names in forbidden.items():
        found = names & _defined_names(filename)
        assert not found, f"{filename} 里又出现了二级回复的符号：{sorted(found)}"

    # `extract_comments` 不再接受 `max_expands`：回复不采了，没有"展开几轮"这回事
    extract = _function_def("comments.py", "extract_comments")
    params = {a.arg for a in extract.args.args + extract.args.kwonlyargs}
    assert "max_expands" not in params

    # 报告上那三个只跟回复有关的计数也一并没了
    report = _class_def("comments.py", "CommentsReport")
    fields = {
        node.target.id
        for node in report.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert not ({"replies", "expand_attempts", "expand_clicks", "expand_failed"} & fields)


def _module_ast(filename: str) -> ast.Module:
    path = pathlib.Path(__file__).resolve().parents[1] / filename
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _defined_names(filename: str) -> set[str]:
    """模块里**定义**了哪些名字：顶层函数名、类名、模块级变量名。"""
    names: set[str] = set()
    for node in _module_ast(filename).body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _function_def(filename: str, name: str) -> ast.FunctionDef:
    for node in _module_ast(filename).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{filename} 里没有 {name}()")


def _class_def(filename: str, name: str) -> ast.ClassDef:
    for node in _module_ast(filename).body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise AssertionError(f"{filename} 里没有 {name}")


def test_content_page_uses_url_to_decide_type() -> None:
    """页面类型由 URL 决定，不靠猜 DOM——`shared.urlnorm` 是唯一权威。"""
    html = f'<div {_markup(selectors.RICH_TEXT)}>正文</div>'

    item = parse.parse_content_page(html, "https://zhuanlan.zhihu.com/p/789")

    assert item is not None
    assert item.content_type == "article"
    assert item.has_body


def test_content_page_rejects_unclassifiable_url() -> None:
    """认不出类型的 URL 直接返回 None，不造 `content_type=None` 的记录。

    用的是用户主页——它是站内的、合法的、但确实不是内容页。
    """
    assert parse.parse_content_page("<div></div>", "https://www.zhihu.com/people/someone") is None


def test_content_page_refuses_an_answer_it_cannot_identify() -> None:
    """回答页里对不上号时**返回 None，不许退而取第一条**。

    这是整个解析层最重要的一个判断。真实回答页里有 3 个 `.AnswerItem`——
    目标回答加两条"相关推荐"。取第一条碰巧是对的（实测目标恰好排最前），
    但那是运气。**拿推荐回答冒充目标回答是最坏的一种错**：
    数据看着齐全，内容却是别人的，而且没有任何报错。
    """
    html = (
        '<div class="AnswerItem" name="111"><div itemprop="text">推荐回答的正文</div></div>'
        '<div class="AnswerItem" name="222"><div itemprop="text">另一条推荐的正文</div></div>'
    )

    # URL 要的是 999，页面上一片都没有
    assert parse.parse_content_page(html, "https://www.zhihu.com/question/1/answer/999") is None


# ── 三、真实快照固件 ────────────────────────────────────────────────
#
# ⚠️ 快照含用户本人的昵称与主页链接，**永远不许拷到别处**（见 .gitignore）。
#    这里只读不写。


@functools.cache
def _snapshot(name: str) -> str:
    path = CALIB_DIR / name
    if not path.exists():
        pytest.skip(f"缺快照 {path}（不在仓库里，得由项目所有者本机采集）")
    return path.read_text(errors="ignore")


def _soup(name: str) -> BeautifulSoup:
    return BeautifulSoup(_snapshot(name), "html.parser")


def _cards(name: str, group: str) -> list[str]:
    """按选择器**第一个候选**把卡片抠出来，正是驱动层会喂给解析层的东西。"""
    selector = selectors.candidates(group)[0]
    return [str(node) for node in _soup(name).select(selector)]


def test_snapshot_search_initial_yields_every_card() -> None:
    """断言的是 **total（每张卡片都算到了）**，不是某个写死的采到数。

    写死"采到 18 条"只能证明这一次对上了；`total` 对得上再加
    `skipped_no_link == 0`，才能证明**没有任何一张卡片被无声吞掉**——
    那正是选择器选窄了的样子。
    """
    batch = parse.parse_search_batch(_cards("search_initial.html", selectors.SEARCH_RESULT_ITEM))

    assert batch.total == 18
    assert batch.skipped_no_link == 0, "有卡片没链接 = 选择器没选中，是校准信号"


def test_snapshot_search_bottom_yields_every_card() -> None:
    """滚到底之后有 198 张卡片。这个数是"滚动真的加载了新内容"的判据。

    ⚠️ 采到的是 196——另外 2 张是 `not_content`（链接指向话题/用户之类的
    非内容页）。那是**正常的**，不是漏采；真正要盯的是 `no_link`。
    """
    batch = parse.parse_search_batch(_cards("search_bottom.html", selectors.SEARCH_RESULT_ITEM))

    assert batch.total == 198
    assert batch.skipped_no_link == 0
    assert len(batch.items) + batch.skipped_not_content == batch.total


def _card_and_item_pairs(name: str) -> list[tuple[str, object]]:
    """把每张卡和**它自己**解析出来的条目配成对。

    ⚠️ 不能拿 `_cards()` 和 `parse_search_batch().items` 直接 `zip`：
    有卡片会被判成 `not_content` 丢掉，两个列表长度不同，**一错位就全错**，
    而且报出来的是"170 条对不上"这种吓人的假警报。
    """
    pairs = [(c, parse.parse_search_item_ex(c)[0]) for c in _cards(name, selectors.SEARCH_RESULT_ITEM)]
    return [(c, i) for c, i in pairs if i is not None]


def test_snapshot_every_search_card_carries_the_free_metadata() -> None:
    """⭐ 每张卡片都要给出缩略信息——**文章和回答都得有**。

    ⚠️ 缩略信息**不能用 `[itemprop='articleBody']` 选**。实测这个存档 198 张卡
    （97 文章 + 99 回答），那个选择器只命中文章那 97 张，**99 张回答一张都选不中**。
    漏法还特别隐蔽：文章那半有值、回答那半全是空，看着像"知乎没给回答配摘要"，
    而不是像选择器错了。改用 `.RichText` 后 198/198 全中。
    """
    pairs = _card_and_item_pairs("search_bottom.html")
    kinds = collections.Counter(item.content_type for _card, item in pairs)

    assert kinds["article"] and kinds["answer"], f"这个存档应当两种卡片都有，实为 {dict(kinds)}"
    empty = [i.url for c, i in pairs if not (i.excerpt or "").strip()]
    assert not empty, f"有卡片没解析出缩略信息：{empty[:3]}"


def test_snapshot_printed_counts_match_what_we_parsed() -> None:
    """⭐ **独立核对**：解析出的赞同数/评论数要等于**卡片上印的那个数**。

    防的是"赞同数和评论数取混了"——那个 bug 的产物是两个字段**逐一相等**，
    只盯着解析结果完全看不出来。所以这里绕开解析器，直接从卡片的原始 HTML
    把数字读一遍做对照。
    """
    pairs = _card_and_item_pairs("search_bottom.html")
    bad: list[tuple[str, str, int, int]] = []

    def printed(card: str, pattern: str) -> int | None:
        if m := re.search(pattern, card):
            return parse.parse_count(m.group(0))
        return None

    checked_vote = checked_comment = 0
    for card, item in pairs:
        want_v = printed(card, r"赞同\s*[\d,.]+\s*[万千kK]?")
        if want_v is not None:
            checked_vote += 1
            if item.voteup_count != want_v:
                bad.append((str(item.title)[:24], "赞同", want_v, item.voteup_count))
        want_c = printed(card, r"[\d,.]+\s*[万千kK]?\s*条评论")
        if want_c is not None:
            checked_comment += 1
            if item.comment_count != want_c:
                bad.append((str(item.title)[:24], "评论", want_c, item.comment_count))
        elif item.comment_count:
            bad.append((str(item.title)[:24], "评论(卡片未印)", 0, item.comment_count))

    assert not bad, f"与卡片原文对不上：{bad[:3]}"
    assert checked_vote > 150 and checked_comment > 100, (
        f"能核对的样本太少（赞同 {checked_vote}／评论 {checked_comment}），这条用例就没意义了"
    )


def test_snapshot_no_result_page_is_not_empty() -> None:
    """⭐ **"搜不到"的页面照样有内容——这是本轮最危险的发现。**

    用户搜了一串乱码，知乎甩出 AI 直答 + 一个「内容发现」推荐板块，
    里面是 17 条**完全合法、能解析、能入库**的知乎内容。

    不知道这件事的话，采集会把这些推荐当成"搜索命中的结果"，
    跑完显示成功。所以：

        必须能认出这个状态（SEARCH_EMPTY_TEXTS），并且响亮地报出来。

    这条用例把"它真的不是空的"钉死——以后有人想当然地写
    `if not results: return []` 就会被这里拦住。
    """
    html = _snapshot("search_noresult.html")
    batch = parse.parse_search_batch(_cards("search_noresult.html", selectors.SEARCH_RESULT_ITEM))

    assert any(marker in html for marker in selectors.SEARCH_EMPTY_TEXTS), "认不出「搜不到」状态"
    assert len(batch.items) > 0, "页面其实是有内容的，别当空页处理"


def test_snapshot_search_filter_panel_defaults() -> None:
    """筛选面板：3 组、14 个标签、每组各选中一个。

    ⭐ **默认选中「综合排序」——所以不显式点「最新发布」就一定漏。**
    这就是能力四/能力一必须"点完再回读"的原因。
    """
    soup = _soup("search_filter_open.html")

    assert len(soup.select(selectors.FILTER_GROUP)) == 3
    assert len(soup.select(selectors.FILTER_TAG)) == 14
    assert [
        el.get_text(strip=True) for el in soup.select(selectors.FILTER_TAG_ACTIVE)
    ] == ["不限类型", "综合排序", "不限时间"]


def test_snapshot_filter_texts_match_the_group_they_belong_to() -> None:
    """选项文案必须**按组**核对，而且实测值和猜的不一样。

    猜的是「最新」「不限」「一天」；实测是「最新发布」「不限时间」「一天内」。

    ⚠️ 更要命的是跨组串台：`:has-text('不限')` 会同时命中组1的「不限类型」。
    所以定位选项必须**先按组下标取 group，再在组内按文案匹配**。
    """
    groups = _soup("search_filter_open.html").select(selectors.FILTER_GROUP)
    texts = [[el.get_text(strip=True) for el in g.select(selectors.FILTER_TAG)] for g in groups]

    assert selectors.FILTER_SORT_NEWEST in texts[selectors.FILTER_GROUP_SORT]
    assert selectors.FILTER_TIME_UNLIMITED in texts[selectors.FILTER_GROUP_TIME]
    assert selectors.FILTER_TIME_DAY in texts[selectors.FILTER_GROUP_TIME]
    # 跨组串台的证据：「不限类型」在排序组/时间组里都不该出现
    assert "不限类型" not in texts[selectors.FILTER_GROUP_SORT]
    assert "不限类型" not in texts[selectors.FILTER_GROUP_TIME]


def test_snapshot_answer_page_picks_the_answer_in_the_url() -> None:
    """⭐ 回答页里有 3 个 `.AnswerItem`（目标 + 2 条推荐），**必须取对那一条**。

    判据是卡片上的 `name` 属性 = 回答 ID。靠"取第一个"碰巧也对，但侧栏还有
    160 多个指向别处回答的 `<a>`——按链接数就去重错了。
    """
    url = "https://www.zhihu.com/question/22230085/answer/1594809785"
    item = parse.parse_content_page(_snapshot("answer.html"), url)

    assert item is not None
    assert item.content_type == "answer"
    assert item.zhihu_id == "1594809785"
    assert item.question_id == "22230085"
    assert len(item.text or "") > 18_000
    assert item.author_name and "黑马测试" in item.author_name
    assert item.voteup_count == 4886
    assert item.comment_count == 1623


def test_snapshot_answer_published_at_is_the_creation_time() -> None:
    """⭐⭐ **回答页也有微数据，而且页面上那行字是"编辑时间"，差了将近六年。**

    这条用例原来断言的是 `2026-09-04 17:55`——那是从页面文本
    「编辑于2026-09-04 17:55 ・北京」里正则抠出来的。**它是错的**，
    而且错得很贵：对一个取证系统来说，"他是什么时候说的"是核心字段，
    拿最后一次编辑时间顶上去，等于把 2020 年的发言记成 2026 年的。

    加上 `dateCreated` 这个候选之后拿到的是真值。同一张卡片上三个值：

        meta[itemprop='dateCreated']    2020-11-25T10:17:52Z   ← 发布
        meta[itemprop='dateModified']   2026-09-04T09:55:58Z   ← 最后编辑
        页面上那行字                     「编辑于2026-09-04 17:55」

    后两者是同一件事（09:55Z + 8h = 17:55 北京），互为佐证；
    只有第一个是"什么时候说的"。

    ⚠️ 微软据的**覆盖范围是分页面的**：文章页只有 `datePublished`，
    回答/问题页只有 `dateCreated`。所以 `META_PUBLISHED` 两个候选都要留着，
    缺一个就会有一半页面的时间悄悄退回文本解析。
    """
    item = parse.parse_content_page(
        _snapshot("answer.html"), "https://www.zhihu.com/question/22230085/answer/1594809785"
    )

    assert item is not None
    assert item.published_at == datetime(2020, 11, 25, 10, 17, 52, tzinfo=UTC)
    # 页面原文照旧保留：人工核对时能看出"页面当时写的是编辑时间"
    assert item.published_text is not None
    assert "2026-09-04" in item.published_text
    assert item.published_at < parse.parse_datetime(item.published_text)  # type: ignore[operator]


def test_snapshot_article_title_comes_from_the_meta_tag() -> None:
    """⭐ 文章标题在 `<meta itemprop="headline" content="…">` 里，**标签之间是空的**。

    `_pick` 会正常命中它，但 `_text` 读文本只能拿到空串——标题永远是 None
    而且不报错。这条用例钉住 `_text` 读 `<meta content>` 这条分支。

    ⚠️ 顺带钉住：`[itemprop='name']` 在这页上是**作者昵称**，不是标题。
    """
    item = parse.parse_content_page(_snapshot("article.html"), "https://zhuanlan.zhihu.com/p/19432450422")

    assert item is not None
    assert item.content_type == "article"
    assert item.title == "【补档】至今为止做过的各种测试+网站链接（随时更新）"
    assert item.author_name == "looooooudly"
    assert len(item.text or "") > 15_000


def test_snapshot_article_published_at_uses_microdata_not_the_edited_date() -> None:
    """⭐ 微数据的 `datePublished` 和页面那行字**差了一年多**，必须用前者。

        微数据          2025-01-20T14:14:44Z   ← 发布，用于 fact_content.published_at
        页面上那行字     「编辑于 2026-03-24」  ← 最后编辑，留在 published_text

    "什么时候说的"是取证的关键，用编辑时间会是错的。两边都留，人工能核对。
    """
    item = parse.parse_content_page(_snapshot("article.html"), "https://zhuanlan.zhihu.com/p/19432450422")

    assert item is not None
    assert item.published_at == datetime(2025, 1, 20, 14, 14, 44, tzinfo=UTC)
    assert item.published_text and "2026-03-24" in item.published_text


def test_snapshot_question_declares_and_delivers_the_same_count() -> None:
    """⭐ **问题页"滚不动了"的正解**：拿声明的总数当完整性判据。

    问题页滚到底**不会出现「没有更多了」**（实测 `question_bottom.html` 里
    出现 0 次），只会滚不动。所以没法用文案判断到底了。

    但顶部那行「N 个回答」给了声明值——采到的条数必须对上：

        相等   →  确实采全了
        少了   →  **报错，不许当成功**

    这份快照声明 15、卡片 15。⚠️ 只证明了 n=15；回答数很大时知乎会不会
    限流还没验证过（`question_newest.html` 声明 1735）。
    """
    declared = parse.parse_question_total(_snapshot("question_bottom.html"))
    cards = _cards("question_bottom.html", selectors.ANSWER_ITEM)
    items = [i for i in (parse.parse_answer_item(c, "1") for c in cards) if i]

    assert declared == 15
    assert len(items) == declared, "采到的条数必须等于声明的总数，少一条就是静默漏采"
    assert all(i.url for i in items)


def test_snapshot_question_sort_control_reports_its_current_value() -> None:
    """⭐ **验证"按时间排序"是否生效，就是回读这个 combobox 的文本。**

    知乎默认按相关热度排。如果那一下点击静默失败，采集会漏掉排在后面的
    新回答而完全不报错——文档 4.1 明确列为不可接受的那一类。

    用户上一轮采快照时就踩过：probe 按文案「最新」找元素，命中的其实是
    回答正文里的"2026年最新版"，`点中「最新」=True` 是**假阳性**。
    所以**点击不算数，回读才算数**。

    这两份快照正是"点之前"和"点之后"，逐字对上。
    """
    before = _soup("question_default.html").select_one("button[role='combobox']")
    after = _soup("question_newest.html").select_one("button[role='combobox']")

    assert before is not None and after is not None
    assert before.get_text(strip=True) == selectors.QUESTION_SORT_DEFAULT_TEXT
    assert after.get_text(strip=True) == selectors.QUESTION_SORT_NEWEST_TEXT


def test_snapshot_comment_list_collects_every_first_level_comment() -> None:
    """⭐ **每条一级 `[data-id]` 都必须产出一条评论。**

    这是能力三的完整性判据，因为它把两类静默漏采同时钉住了：

    1. 套错作者选择器 → 一条都解析不出来（曾经就是这样，9 个节点返回 0 条）
    2. 作者选择器写窄了 → 静默丢掉一部分。实测只写 `/people/` 时，
       8 条顶层里丢掉 2 条（**机构号挂在 `/org/` 下**，占 25%）

    所以断言"条数 == 一级 `[data-id]` 的个数"，而不是写死一个数字——
    写死数字只能证明"这次对上了"，对不上这两类变化。
    """
    for name, content_id in [
        ("comments_modal.html", "1594809785"),
        ("article_comments.html", "9"),
    ]:
        soup = _soup(name)
        roots = [
            node
            for node in soup.select(f"[{selectors.COMMENT_ID_ATTR}]")
            if node.find_parent(attrs={selectors.COMMENT_ID_ATTR: True}) is None
        ]
        assert roots, f"{name}: 快照里应该有一级评论节点"

        items = parse.parse_comment_list(_snapshot(name), content_id=content_id)

        assert len(items) == len(roots), f"{name}: 有一级评论没被采出来"
        assert all(i.zhihu_id for i in items), "评论 ID 不能为空——fact_content.zhihu_id 是 not null"


def test_snapshot_nested_replies_are_not_collected() -> None:
    """⭐ 嵌套的回复**一条都不采**，而且它们不能顶掉一级评论的位置。

    实测 `comments_modal.html`：一级 `10214848119` 下面挂着二级 `10215236372`。
    `parse_comment_list` 只吐一级——回复既不在结果里，也不该把父评论挤掉。

    ⚠️ 这是**刻意的取舍**，不是 bug：点开回复面板之后关不掉、回不到原来那条
    评论的位置，而且每点一次都要重开一次页面等很久。完整理由见 `comments.py`
    模块开头。**别把它当 bug 修回来。**
    """
    items = parse.parse_comment_list(_snapshot("comments_modal.html"), content_id="1594809785")
    ids = [i.zhihu_id for i in items]

    assert "10215236372" not in ids, "二级回复不该出现（我们不采它）"
    assert "10214848119" in ids, "它的父评论照采"
    assert all(i.parent_id == "1594809785" for i in items), (
        "采下来的每一条都是**一级评论**，全挂在被评论的内容上"
    )


def test_snapshot_thought_cards_are_marked_two_ways_with_different_counts() -> None:
    """想法卡片有两种标记，**数量对不上**，取列表时得想清楚要哪个。

        [itemprop='zhihu:pin']   113   ← 只有部分想法带这对 <meta>
        .ContentItem.PinItem     170

    要"尽量全"就往后者落，要"结构确定"就停在第一个。这里把差异钉住，
    免得以后有人以为它们等价随手换掉。
    """
    soup = _soup("thought.html")
    semantic = len(soup.select(selectors.candidates(selectors.THOUGHT_ITEM)[0]))
    css = len(soup.select(selectors.candidates(selectors.THOUGHT_ITEM)[1]))

    assert semantic == 113
    assert css == 170
    assert semantic != css


def test_snapshot_real_pages_have_no_uncalibrated_selector() -> None:
    """校准状态的总闸：跑真实采集前，`uncalibrated()` 必须是空的。

    ## 这条测试 2026-09-26 从 `xfail(strict=True)` 转正的

    原来它是 xfail，理由写在当时的 reason 里：*"二级回复的「弹窗」形态还没有
    真实快照，REPLY_ITEM / REPLY_MODAL 是空的"*。用 `strict=True` 就是为了
    在**校准补完的那一刻**报 XPASS，逼人回来把这个标记删掉——
    它按设计工作了：补完 `REPLY_*` 之后它立刻红了，提示写得很清楚。

    现在它是条普通测试。**以后再有没校准的选择器，请照样用
    `xfail(strict=True)` 挂上去**，而不是把 `_PENDING` 里加一项就完事：
    那样没人会记得回来看。也**不许**改成 `xfail(strict=False)`——
    那样它永远不会提醒任何事。
    """
    pending = selectors.uncalibrated()

    assert pending == [], f"还有没校准的选择器：{pending}——采集会静默漏数据"


def test_snapshot_collapsed_answers_are_counted_not_collected() -> None:
    """⭐ 折叠回答**不点开，但必须数出来**。

    用户 2026-09-26 决定不点开折叠回答（「被折叠回答也不会被人看，对舆论影响
    很小」）。这个决定没问题——但折叠回答**是算进「N 个回答」里的**，
    所以不数的话，完整性判据会永远差那几条，天天误报"没采全"。

    实测 `question_newest.html`：声明 **1735**，其中 **15 个被折叠**。
    于是判据是 `采到 + 折叠 >= 声明`。
    """
    html = _snapshot("question_newest.html")

    assert parse.parse_question_total(html) == 1735
    assert parse.parse_collapsed_count(html) == 15


def test_snapshot_collapsed_bar_may_be_absent() -> None:
    """⚠️ **那个"折叠"提示条不一定在 DOM 里**——它是个浮层。

    同一个问题的 `question_sort_open.html` 声明 1735，却数出 **0** 个折叠。
    所以 `parse_collapsed_count` 返回 0 的语义是"**已知的**折叠数下限"，
    不是"没有折叠"。完整性判据得写成 `>=`，不能写 `==`。
    """
    html = _snapshot("question_sort_open.html")

    assert parse.parse_question_total(html) == 1735
    assert parse.parse_collapsed_count(html) == 0, "没渲染出来 ≠ 没有折叠的"


# ── 四、2026-09-26 补采的四份快照 ──────────────────────────────────
#
# 这四份是用户第二轮补采的，每一份都关掉了一个悬着的未知：
#   anwser_sort_by_time.html —— 排序下拉**真的开着**的形态（第一份是误采的）
#   large_question.html      —— 一个滚到底的大问题页（89 条回答，2.2 MB）
#   comments_second_level.html —— 二级回复
#   comments_collapsed.html  —— 评论弹窗还没打开时的页面


def test_snapshot_large_question_is_fully_accounted_for() -> None:
    """⭐⭐ **大问题页的条数核算：`>=` 不是 `==`，这一条是实测出来的。**

    用户要的判据是「根据回答的数量以及我们采集到的数量进行对比来判断是否
    采集到足够的内容」。拿真实快照一算，`==` 会**永远失败**：

        声明   91
        采到   89  （`.AnswerItem`，name 全部唯一）
        折叠    6  （不点开，但它们是算进声明里的）
        ─────────
        89 + 6 = 95  >  91

    也就是说知乎自己那个「N 个回答」跟页面上真实渲染出来的条数**对不齐**
    （它是个缓存计数）。所以"多出来"是正常的，不该报错；
    真正要抓的是"采到的明显少于声明"。

    顺带钉住：89 张卡片的 `name` 全部唯一，且**部分回答没被声明的总数算进去**
    这件事不是采集的锅——`.AnswerItem` 是 89/89 全覆盖的。
    """
    html = _snapshot("large_question.html")

    assert parse.parse_question_total(html) == 91
    assert parse.parse_collapsed_count(html) == 6

    cards = _cards("large_question.html", selectors.ANSWER_ITEM)
    assert len(cards) == 89

    # ⚠️ `name` 在**卡片那个 div** 上，不在 `BeautifulSoup` 文档对象上——
    #    对 `BeautifulSoup(card_html)` 直接 `.get("name")` 恒为 None
    #    （根对象的 name 是 `[document]`）。这正是解析层用 `_pick` 先定位卡片的原因。
    names = [
        parse._attr(BeautifulSoup(c, "html.parser").select_one(selectors.ANSWER_ITEM), "name")
        for c in cards
    ]
    assert all(names), "每张卡片都该有 name（= 回答 ID）"
    assert len(set(names)) == 89, "name 是回答 ID，不该有重复"

    assert len(cards) + parse.parse_collapsed_count(html) >= parse.parse_question_total(html)


def test_snapshot_large_question_items_carry_every_field() -> None:
    """89 张卡片，字段**满覆盖**——这是"能力四不需要点任何东西"的依据。

    实测每一张都有 `dateCreated`（精确到秒）、赞同数、评论数、作者、正文。
    所以问题页的回答可以直接入库，不用逐条打开回答详情页。

    ⚠️ 注意用的是 `dateCreated` 不是 `datePublished`：问题页上后者是 **0/89**。
    """
    url_cards = _cards("large_question.html", selectors.ANSWER_ITEM)
    items = [
        parse.parse_answer_item(c, "34450022") for c in url_cards
    ]

    assert all(i is not None for i in items)
    parsed = [i for i in items if i is not None]

    assert all(i.url.startswith("https://www.zhihu.com/question/34450022/answer/") for i in parsed)
    assert all(i.question_id == "34450022" for i in parsed)
    assert all(i.published_at is not None for i in parsed), "dateCreated 是 89/89"
    assert all(i.author_name for i in parsed)
    assert all(i.published_text for i in parsed)
    assert max(i.voteup_count for i in parsed) > 6000, "热门回答的赞同数要读对"
    assert all(i.has_body for i in parsed if i.zhihu_id != "2986617993")
    assert sum(1 for i in parsed if not i.has_body) == 1, "实测只有 1 条正文为空"

    # ⚠️ **作者不全是个人号**：89 条里 10 条是**机构号**（`/org/…`）。
    # 断言写成 `/people/` 会挂——这正是评论那边踩过的同一个坑
    # （见 selectors.COMMENT_AUTHOR_LINK），只不过这次是回答。
    #
    # ⚠️ **数卡片，不要数链接。** 每张卡片的 `[itemprop='author'] .UserLink-link`
    #    命中 **2 个**（同一作者在头像区和页脚各渲染一次，HTML 里是 178 个），
    #    所以按链接数会数出 20 个机构号、按卡片数只有 10 个——**差一倍**。
    #    这里断言的是卡片数，也就是 `author_url` 的语义。
    orgs = [i for i in parsed if i.author_url and "/org/" in i.author_url]
    people = [i for i in parsed if i.author_url and "/people/" in i.author_url]
    assert len(orgs) == 10, "机构号作者，实测 10 条回答（178 个链接里 20 个）"
    assert len(people) == 79, "个人号作者，实测 79 条"
    assert len(orgs) + len(people) == 89, "两类之外不该有第三种作者链接"


def test_snapshot_large_question_has_no_read_more_button() -> None:
    """⭐ 问题页的回答**没有被截断**，所以能力四不用点「阅读全文」。

    实测：最长正文 8710 字、中位数 296 字，而整页「阅读全文」出现 **0 次**。
    短的那几条是真的短（「+1」「感谢分享。」）。

    这一条要是反过来（有截断但没点开），采到的就是**半个正文**——
    对取证来说比采不到更糟：它看着是完整的，实际是节选。
    """
    html = _snapshot("large_question.html")

    for text in selectors.READ_MORE_TEXTS:
        assert text not in html, f"{text!r} 出现在问题页上，说明正文可能被截断了"

    lengths = sorted(len(i.text or "") for i in _parsed_large_question())
    assert lengths[-1] > 8000, "最长的回答应该是全文，不是节选"
    assert lengths[-1] < 20_000, "8千多字的量级才对；上万说明可能把整页都算进去了"


def _parsed_large_question() -> list[parse.ParsedItem]:
    cards = _cards("large_question.html", selectors.ANSWER_ITEM)
    return [
        i for i in (parse.parse_answer_item(c, "34450022") for c in cards) if i is not None
    ]


def test_snapshot_sort_dropdown_options_are_calibrated() -> None:
    """⭐ 排序下拉的**选项元素**到手了——这一项悬了很久。

    起因是上一轮那份 `question_sort_open.html` 名字叫"下拉开着"，
    实际是在下拉**关着**的时候采的（整页「按时间排序」一次都没出现），
    所以 `QUESTION_SORT_OPTION` 一直是空的。

    用户补采的 `anwser_sort_by_time.html` 才是真的开着
    （combobox 上 `aria-expanded="true"`），选项是：

        <div class="Select-list Answers-select …">
          <button class="Select-option …" role="option">默认排序</button>
          <button class="Select-option …" role="option">按时间排序</button>

    ⚠️ 第二份快照里 combobox 自己的 `<span>` 也是「按时间排序」这四个字，
    所以**按文案在整页找会先命中 combobox**——点它只是把下拉收起来，
    看起来"点过了"而排序纹丝不动。必须限定在选项列表里点。
    """
    html = _snapshot("anwser_sort_by_time.html")
    soup = _soup("anwser_sort_by_time.html")

    options = soup.select(selectors.QUESTION_SORT_OPTION)
    assert len(options) == 2
    assert [o.get_text(" ", strip=True) for o in options] == [
        selectors.QUESTION_SORT_DEFAULT_TEXT,
        selectors.QUESTION_SORT_NEWEST_TEXT,
    ]

    # 下拉开着的时候选项在，而且 combobox 回读的是切换后的值
    assert len(soup.select(selectors.QUESTION_SORT_LIST)) == 1
    assert parse._text(parse._pick(soup, selectors.QUESTION_SORT_BUTTON)) == "按时间排序"

    # 那份误采的快照里根本没有选项，正好反证"快照本身不说明下拉开着"
    assert BeautifulSoup(_snapshot("question_sort_open.html"), "html.parser").select(
        selectors.QUESTION_SORT_OPTION
    ) == []
    assert "按时间排序" not in _snapshot("question_sort_open.html")


def test_snapshot_answer_cards_use_two_different_itemprops() -> None:
    """卡片上的 `itemprop` 是 `acceptedAnswer` / `suggestedAnswer`，**不是 `answer`**。

    实测 89 条：1 条被采纳、88 条建议。这就是 `[itemprop='answer']`
    在问题页上数出 0 个的原因——不是名字变了，是分成了两个值。
    """
    soup = _soup("large_question.html")
    props = collections.Counter(
        c.get("itemprop") for c in soup.select(selectors.ANSWER_ITEM)
    )

    assert props == {"suggestedAnswer": 88, "acceptedAnswer": 1}


def test_parse_collapsed_text_reads_the_floating_bar() -> None:
    """折叠数那行是**浮层**，所以要从"当前文本"解析，而不是从整页 HTML。

    ⚠️ 实测文本是 `'6 个回答被折叠 （ 为什么？ ）'`——后面还挂着一个链接，
    所以正则**不能锚定结尾**。
    """
    assert parse.parse_collapsed_text("6 个回答被折叠 （ 为什么？ ）") == 6
    assert parse.parse_collapsed_text("1,735 个回答被折叠") == 1735
    assert parse.parse_collapsed_text("") == 0
    assert parse.parse_collapsed_text(None) == 0
    assert parse.parse_collapsed_text("没有这一行") == 0


def test_snapshot_comment_buttons_carry_a_zero_width_space() -> None:
    """⭐ 评论按钮的文案前面挂着一个 **U+200B 零宽空格**。

    实测 `comments_collapsed.html`（评论弹窗还没打开的状态）里四个按钮：

        '\\u200b 9 条评论'   '\\u200b 1623 条评论'
        '\\u200b 101 条评论' '\\u200b 33 条评论'

    精确比对如果不剥掉它就会全部落空，而**落空的表现是"没找到按钮"**，
    不是报错——能力三会安静地一条评论都采不到。
    `drive._FIND_BY_TEXT` 里那句 `.replace(/[\u200b-‍﻿]/g, '')`
    就是为它写的。

    ⚠️ 顺带钉住：这一页有 **4 个**「N 条评论」按钮（推荐位里还有三个），
    所以点评论必须靠 `scope` 限定到正文那条，不能按文案在整页找第一个。
    """
    soup = _soup("comments_collapsed.html")

    buttons = [b.get_text() for b in soup.select("button") if "条评论" in b.get_text()]
    assert len(buttons) == 4
    assert all("\u200b" in b for b in buttons)
    assert all(b.replace("\u200b", "").strip().endswith("条评论") for b in buttons)


def test_snapshot_zero_upvotes_is_a_real_value_not_a_parse_failure() -> None:
    """⭐ 89 条里 **44 条赞同数是 0**，而且那是真的 0，不是没读到。

    这条用例存在的理由：`voteup_count == 0` 在代码里同时表示
    "没人赞"和"没读到"两种情况，只看数字分不出来。所以这里用
    **两个互相独立的来源交叉验证**——微数据和 aria-label，
    两个都写着 0，那就是 0。

    （为什么在意：赞同数等于 0 的答案和赞同数读失败的答案，
    在风险评估里含义完全不同。前者是"没人理"，后者是"我们不知道"。）
    """
    soup = _soup("large_question.html")
    agree = 0
    zeros = 0

    for card in soup.select(selectors.candidates(selectors.ANSWER_ITEM)[0]):
        meta = card.select_one(selectors.UPVOTE_COUNT_META)
        button = card.select_one("button[aria-label*='赞同']")
        assert meta is not None and button is not None

        by_meta = parse.parse_count(meta.get("content"))
        by_aria = parse.parse_count(button.get("aria-label"))
        assert by_meta == by_aria, "两个来源必须一致，不一致说明其中一个读歪了"

        agree += 1
        if by_meta == 0:
            zeros += 1

    assert agree == 89
    assert zeros == 44, "实测近一半回答是 0 赞，这是正常的长尾，不是解析失败"


def test_snapshot_filter_panel_has_three_ordered_groups() -> None:
    """搜索页的筛选面板：**三组，顺序固定 类型 / 排序 / 时间**。

    `selectors.FILTER_GROUP_SORT` 这些下标就是照着这里定的，
    顺序错了会把「最新发布」点到类型组去（点不着，白跑一轮）。
    """
    soup = _soup("search_filter_open.html")
    groups = soup.select(selectors.FILTER_GROUP)

    assert len(groups) == 3
    assert [t.get_text(strip=True) for t in groups[selectors.FILTER_GROUP_TYPE].select(
        selectors.FILTER_TAG
    )] == ["不限类型", "只看回答", "只看文章", "只看视频"]
    assert selectors.FILTER_SORT_NEWEST in [
        t.get_text(strip=True) for t in groups[selectors.FILTER_GROUP_SORT].select(
            selectors.FILTER_TAG
        )
    ]
    assert [t.get_text(strip=True) for t in groups[selectors.FILTER_GROUP_TIME].select(
        selectors.FILTER_TAG
    )] == list(selectors.FILTER_TIME_OPTIONS)


def test_snapshot_filter_panel_has_exactly_one_active_tag_per_group() -> None:
    """⭐ 回读判据：**每组恰好一个 `.tag-selected`**，全页恰好 3 个。

    这是"筛选真的生效了"的唯一证据（见 `selectors.FILTER_TAG_ACTIVE`）。
    面板打开时是 3 个；面板关着时是 **0 个**（实测 `search_filtered.html`），
    所以 `search._read_group` 读之前必须先把面板点开——
    否则读回来的 None 会被当成"没选中"，然后白点一轮。
    """
    open_soup = _soup("search_filter_open.html")
    closed_soup = _soup("search_filtered.html")

    assert len(open_soup.select(selectors.FILTER_TAG_ACTIVE)) == 3
    assert len(closed_soup.select(selectors.FILTER_GROUP)) == 0, "面板关着就没有组"
    assert closed_soup.select(selectors.FILTER_TAG_ACTIVE) == []


def test_snapshot_filter_options_are_ambiguous_across_groups() -> None:
    """⭐ **为什么必须按组定位选项**——组间文案会串台。

    组0 有「不限**类型**」、组2 有「不限**时间**」，还有一个更隐蔽的：
    按 `:has-text('不限')` 这种**包含**匹配去找，组0 那项会先被命中。

    这条用例把这个歧义钉住：只要有人把 `_click_in_group` 改成整页按文案找，
    这里的两组选项就会开始互相顶替，而**点击不会报错**——
    只是筛选项点到了别的组上，采到的数据口径全错。
    """
    soup = _soup("search_filter_open.html")
    groups = soup.select(selectors.FILTER_GROUP)

    type_texts = {t.get_text(strip=True) for t in groups[selectors.FILTER_GROUP_TYPE].select(
        selectors.FILTER_TAG
    )}
    time_texts = {t.get_text(strip=True) for t in groups[selectors.FILTER_GROUP_TIME].select(
        selectors.FILTER_TAG
    )}

    assert type_texts & time_texts == set(), "两组之间没有完全重名的选项"
    assert "不限类型" in type_texts and "不限时间" in time_texts
    # 但**包含**匹配会串台：按「不限」找，两组各有一项会命中
    assert sum(1 for t in type_texts | time_texts if "不限" in t) == 2

    # 而「最新发布」只在排序组里，不会串——所以排序那一组的回读最干净
    all_texts = [
        t.get_text(strip=True)
        for g in groups
        for t in g.select(selectors.FILTER_TAG)
    ]
    assert all_texts.count(selectors.FILTER_SORT_NEWEST) == 1


def test_snapshot_filter_type_group_is_a_sticky_account_state() -> None:
    """组0（类型）的默认是「不限类型」——**采集口径里没提它，但必须管**。

    它是账号级的残留状态：上一轮点过「只看回答」，这个状态会留着，
    于是后面采到的永远只有回答，文章和想法一条都进不来，
    而日志上一切正常。这条用例把"默认值是什么"记下来，
    好让 `search._apply_filters` 的归位有据可依。
    """
    soup = _soup("search_filter_open.html")
    groups = soup.select(selectors.FILTER_GROUP)
    active = groups[selectors.FILTER_GROUP_TYPE].select(selectors.FILTER_TAG_ACTIVE)

    assert len(active) == 1
    assert active[0].get_text(strip=True) == selectors.FILTER_TYPE_UNLIMITED


def test_snapshot_empty_search_still_yields_real_urls() -> None:
    """⭐「搜不到东西」的页面**不是空的**——这是本轮最值钱的发现之一。

    用户搜了一串乱码，知乎不说"没有找到"，而是甩一批推荐内容
    （「内容发现」板块）。实测那份快照里照样有 17 条能解析成内容、
    URL 完全合法的知乎条目。

    所以 `search.search()` 的 `empty_result` 只是**报警**，不是**丢弃**：
    数据照采（用户要的是提到关键词的公开内容），但必须让人知道
    "这些不是命中关键词的结果"。
    """
    assert any(t in _snapshot("search_noresult.html") for t in selectors.SEARCH_EMPTY_TEXTS)
    assert "没有找到" not in _snapshot("search_noresult.html"), "知乎不会说这句话"

    batch = parse.parse_search_batch(
        _cards("search_noresult.html", selectors.SEARCH_RESULT_ITEM)
    )
    assert len(batch.items) > 0, "推荐内容照样能解析出 URL"

    # 另外四份快照里一个标志都没有——判别力是干净的
    for name in ("search_initial.html", "search_bottom.html", "search_filtered.html"):
        assert not any(t in _snapshot(name) for t in selectors.SEARCH_EMPTY_TEXTS), name
