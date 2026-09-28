"""纯解析：HTML 字符串 → 结构。**不 import playwright。**

浏览器负责找元素，这里负责从 HTML 里抠字段。切开是为了能用 `runtime/calib/`
的页面快照当固件测解析——不用拿真实账号去试。

单个字段找不到就留 None，不让整轮采集中断。**例外是"取错了元素"**，
那比取不到危险得多，见 `parse_content_page`。
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from sentinel_q.collector import selectors
from sentinel_q.shared.urlnorm import NormalizedUrl, normalize

log = logging.getLogger(__name__)

# "1.2 万" / "3,456" / "赞同 12" —— 知乎大数会显示成万
_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万|千|k|K)?")
# 绝对日期：2024-01-01 / 2024年1月1日 / 2024-01-01 12:30
_DATE_RE = re.compile(r"(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})日?(?:\s+(\d{1,2}):(\d{2}))?")
# 问题页顶部那行 "N 个回答"（也有 "1,735 个回答" 这种带千分位的）
_TOTAL_RE = re.compile(r"([\d,]+)\s*个回答")
# 「15 个回答被折叠」
_COLLAPSED_RE = re.compile(r"([\d,]+)\s*个回答被折叠")

# 零宽字符（U+200B 等）：知乎的按钮和标题文案前面常挂着一个。
# ⚠️ Python 的 `\s` **不认** U+200B，所以必须显式剥。
_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200d\ufeff]")
# 「394 条评论」——**整串相等**才算，见 `parse_comment_total`。
_COMMENT_TOTAL_RE = re.compile(r"([\d,]+)\s*条评论")
# 在一段**混杂文本里搜**评论数，例如搜索卡片的操作栏 `" 赞同 117   19 条评论 08-31"`。
# ⚠️ 和上面那条**不是一回事**：那条 `fullmatch` 锚定整串，这条 `search` 在段落里找。
_COMMENT_COUNT_IN_TEXT_RE = re.compile(r"[\d,.]+\s*[万千kK]?\s*条评论")

ZHIHU = "https://www.zhihu.com"


@dataclass(frozen=True)
class ParsedItem:
    """从一段 HTML 里抠出来的一条。

    刻意**不是** `ContentRecord`：`extract.py` 负责补快照路径、内容长度、
    分类之后再转过去。
    """

    url: str
    content_type: str | None
    zhihu_id: str | None
    question_id: str | None
    title: str | None = None
    text: str | None = None
    html: str | None = None
    excerpt: str | None = None
    """搜索卡片上的缩略信息。**在页面上被截断了**，不能当正文用——
    所以刻意不复用 `text`（那条路上 `text` 是完整正文）。"""
    author_name: str | None = None
    author_url: str | None = None
    voteup_count: int = 0
    comment_count: int = 0
    published_at: datetime | None = None
    published_text: str | None = None
    """原始发布时间文本。相对时间（"3 小时前"）不换算，见 `parse_datetime`。"""

    parent_id: str | None = None
    """这条东西挂在谁下面。

    ⚠️ 存的是**知乎那边的 ID**（正文的 `zhihu_id`、或父评论的评论 ID），
    **不是库里的 uuid**。所以它没法直接跟 `fact_content.id` join。
    """

    answered_count: int = 0
    """问题页专有：回答总数。**只在 `parse_question_total()` 的返回值上有意义**，
    其他场景恒为 0。放在这里是为了让调用方不必多接一个类型。"""

    @property
    def has_body(self) -> bool:
        return bool(self.text and self.text.strip())


# 搜索条目的两种丢弃原因。**必须分开统计**，两者含义完全不同：
#   NO_LINK     —— 这条结果里根本没链接，多半是选择器没选中（校准信号）
#   NOT_CONTENT —— 有链接但不是内容页（话题、用户、站外），是正常的
# 混成一个数，"被挡掉的数量突然变大"就分不清是选择器错了还是搜索页本来就这样。
SKIP_NO_LINK = "no_link"
SKIP_NOT_CONTENT = "not_content"


@dataclass
class SearchBatch:
    """一轮滚动里新出现的卡片的解析结果。"""

    items: list[ParsedItem] = field(default_factory=list)
    skipped_no_link: int = 0
    skipped_not_content: int = 0

    @property
    def total(self) -> int:
        """这一轮见到的卡片总数（不管采没采到）。"""
        return len(self.items) + self.skipped_no_link + self.skipped_not_content


# ── 能力一：搜索 ────────────────────────────────────────────────────


def parse_search_item(html: str, base_url: str = ZHIHU) -> ParsedItem | None:
    """搜索页里的一条结果 → 它的 URL（只要结果、不要原因时用它）。"""
    return parse_search_item_ex(html, base_url)[0]


def parse_search_item_ex(
    html: str, base_url: str = ZHIHU
) -> tuple[ParsedItem | None, str | None]:
    """同 `parse_search_item`，但把丢弃原因一并返回：`(条目, 原因)`，成功时原因为 None。

    标题和两个计数是卡片上白送的，顺手带走，供阶段二分诊用（那边每条要花
    十秒，见 `ops.UrlEntry`）。

    ⚠️ **不填作者**：搜索卡片上没有 `/people/` 链接，作者不是结构化字段。
    留一个恒为 None 的字段只会让人以为解析挂了。

    ⚠️ 赞同数取自 `aria-label="赞同 1770 "`——按钮**没有文本**，只有一个 SVG，
    所以必须走 `_count_from_button` 里那条 aria-label 兜底路径。
    """
    soup = BeautifulSoup(html, "html.parser")
    link = _pick(soup, selectors.SEARCH_RESULT_LINK)
    if link is None or not link.get("href"):
        return None, SKIP_NO_LINK

    url = urljoin(base_url, str(link.get("href")))
    normalized = normalize(url)
    if normalized is None:
        # 搜索页会混进专栏、话题、用户等非内容链接。它们没有 content_type，
        # 而 fact_content.content_type 是 not null 的，存不进去。
        return None, SKIP_NOT_CONTENT

    return ParsedItem(
        url=normalized.url,
        content_type=normalized.content_type,
        zhihu_id=normalized.zhihu_id,
        question_id=normalized.question_id,
        title=_text(_pick(soup, selectors.QUESTION_TITLE)) or _text(link),
        excerpt=_text(_pick(soup, selectors.SEARCH_EXCERPT)),
        voteup_count=_count_from_button(soup, selectors.UPVOTE_BUTTON),
        comment_count=_comment_count(soup),
    ), None


def parse_search_batch(cards: list[str], base_url: str = ZHIHU) -> SearchBatch:
    """一整轮滚动里新出现的卡片 HTML → 一批条目 + 丢弃统计。

    ⚠️ 卡片里的 href **两种形态都有**：协议相对（`//zhuanlan.zhihu.com/p/…`）
    和站内相对路径（`/question/22230085/answer/1594…`）。这里 `urljoin` 一次搞定，
    调用方别自己拼字符串。
    """
    batch = SearchBatch()
    for card in cards:
        item, reason = parse_search_item_ex(card, base_url)
        if item is not None:
            batch.items.append(item)
        elif reason == SKIP_NO_LINK:
            batch.skipped_no_link += 1
        else:
            batch.skipped_not_content += 1
    return batch


# ── 能力二：内容页 ──────────────────────────────────────────────────


def parse_content_page(html: str, url: str) -> ParsedItem | None:
    """能力二：一个内容页（回答 / 文章 / 想法 / 问题）的正文 → 结构。

    ⚠️ **返回 None 不只表示"URL 认不出来"**，也表示"页面里找不到地址栏所指的
    那条内容"。两种都是"别拿别的凑数"，调用方按失败处理即可。
    """
    normalized = normalize(url)
    if normalized is None:
        return None

    soup = BeautifulSoup(html, "html.parser")
    scope = _content_scope(soup, normalized)
    if scope is None:
        return None

    body = _pick(scope, selectors.RICH_TEXT)
    return ParsedItem(
        url=normalized.url,
        content_type=normalized.content_type,
        zhihu_id=normalized.zhihu_id,
        question_id=normalized.question_id,
        title=_text(_pick(scope, selectors.QUESTION_TITLE)),
        text=_text(body),
        html=str(body) if body else None,
        author_name=_text(_pick(scope, selectors.AUTHOR_NAME)),
        author_url=_href(_pick(scope, selectors.AUTHOR_LINK)),
        voteup_count=_count_from_button(scope, selectors.UPVOTE_BUTTON),
        comment_count=_comment_count(scope),
        published_text=_text(_pick(scope, selectors.PUBLISHED_TIME)),
        published_at=_published_at(scope),
    )


def _content_scope(soup: Tag, normalized) -> Tag | None:
    """确定"这一页里哪一块才是地址栏指的那条内容"。只有回答页需要这一步。

    ⚠️ 回答页里除了目标回答，还挂着"相关推荐"回答，侧栏还有上百个指向别处的
    链接。取第一个碰巧是对的，但那是运气不是判据——`name` 属性**就是回答 ID**，
    直接对上，零猜测。

    对不上时返回 None 让调用方失败，**绝不退而取第一条**：拿推荐回答冒充目标
    回答是最坏的一种错——数据看着齐全，内容却是别人的。
    """
    if normalized.content_type != "answer":
        return soup

    items = _pick_all(soup, selectors.ANSWER_ITEM)
    if not items:
        # 页面上一个回答卡片都没有：可能是合成 HTML（测试夹具），
        # 也可能是知乎换了渲染方式。交给整页去找正文，下面照常统计字段缺失。
        return soup

    for item in items:
        if _attr(item, selectors.ANSWER_ID_ATTR) == normalized.zhihu_id:
            return item

    log.error(
        "回答页里没有 name=%s 的 %s（页面上有 %d 条，name 依次是 %s）。"
        "结构可能变了，本次不采——拿推荐回答冒充目标回答比采不到更糟。",
        normalized.zhihu_id,
        selectors.ANSWER_ITEM,
        len(items),
        [_attr(i, selectors.ANSWER_ID_ATTR) for i in items],
    )
    return None


# ── 能力四：问题页的回答列表 ────────────────────────────────────────


def parse_answer_item(html: str, question_id: str) -> ParsedItem | None:
    """能力四：问题页里的一个回答卡片 → 结构。

    与 `parse_content_page` 的区别：这里吃的是**列表里的一张卡片**，
    URL 得自己推出来，而不是从地址栏拿。

    ⚠️ **不能拿卡片里第一个 `a[href*='/answer/']` 了事**——回答正文里作者
    经常贴自己别的回答的链接。所以先用卡片上的 `name`（= 回答 ID）**验证**
    链接，验证不了再用 `name` + 已知的 question_id 拼出来。两条路都要过
    `normalize`，URL 的规范形式只由 `shared.urlnorm` 说了算。
    """
    soup = BeautifulSoup(html, "html.parser")
    answer_id = _attr(soup, selectors.ANSWER_ID_ATTR)

    normalized = _answer_url_from_meta(soup)
    if normalized is None:
        if link := _answer_link_for(soup, answer_id):
            normalized = normalize(urljoin(ZHIHU, str(link.get("href"))))
    if normalized is None and answer_id and question_id:
        normalized = normalize(f"{ZHIHU}/question/{question_id}/answer/{answer_id}")
    if normalized is None:
        return None

    body = _pick(soup, selectors.RICH_TEXT)
    return ParsedItem(
        url=normalized.url,
        content_type=normalized.content_type,
        zhihu_id=normalized.zhihu_id,
        question_id=question_id,
        title=_text(_pick(soup, selectors.QUESTION_TITLE)),
        text=_text(body),
        html=str(body) if body else None,
        author_name=_text(_pick(soup, selectors.AUTHOR_NAME)),
        author_url=_href(_pick(soup, selectors.AUTHOR_LINK)),
        voteup_count=_count_from_button(soup, selectors.UPVOTE_BUTTON),
        comment_count=_comment_count(soup),
        published_text=_text(_pick(soup, selectors.PUBLISHED_TIME)),
        published_at=_published_at(soup),
    )


def _published_at(scope: Tag) -> datetime | None:
    """发布时间。优先微数据的精确 ISO 值，退回解析页面上的可见文本。

    页面那行字是**"最后编辑时间"不是"发布时间"**——实测文章页上两者差了
    一年多。对取证系统来说，"什么时候说的"必须用前者。后者仍保留在
    `published_text` 里，人工核对时能看出页面当时写的是什么。

    ⚠️ 覆盖不全：**回答页和问题页都没有这个 `<meta>`**，只能退回文本解析，
    所以两条路都得留着。
    """
    if meta := _pick(scope, selectors.META_PUBLISHED):
        if stamp := _parse_iso(_attr(meta, "content")):
            return stamp
    return parse_datetime(_text(_pick(scope, selectors.PUBLISHED_TIME)))


def _parse_iso(text: str | None) -> datetime | None:
    """ISO 8601 → datetime。`2025-01-20T14:14:44.000Z` → UTC 时间。"""
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    # 带时区的换算成 UTC，没带的（不该出现）按 UTC 认——数据库里统一一个口径
    return stamp.astimezone(UTC) if stamp.tzinfo else stamp.replace(tzinfo=UTC)


def _answer_url_from_meta(soup: Tag) -> NormalizedUrl | None:
    """卡片上 `meta[itemprop='url']` 里那条**指向回答自己**的 URL。

    ⭐ 机器可读、无歧义，所以排在翻 `<a>` 前面。

    ⚠️ **一张卡片里有两条 `itemprop='url'`**：先是**作者的**主页链接，然后才是
    回答自己的。取第一条会拿到作者主页，而 `normalize()` 会把它拒掉——
    **拒掉是好事**（不会静默存错），但如果只写 `select_one` 就永远走不到这里。
    所以要按 `/answer/` 筛。
    """
    for meta in soup.select("meta[itemprop='url']"):
        content = str(meta.get("content") or "")
        if "/answer/" in content:
            normalized = normalize(content)
            if normalized is not None:
                return normalized
    return None


def _answer_link_for(soup: Tag, answer_id: str | None) -> Tag | None:
    """卡片里指向**这张卡片自己的回答**的链接；没有 `answer_id` 时退回第一个。

    退回是有代价的（见 `parse_answer_item`），但那种情况下本来也没法验证。
    """
    links = soup.select("a[href*='/answer/']")
    if not links:
        return None
    if not answer_id:
        return links[0]
    for link in links:
        if normalize(urljoin(ZHIHU, str(link.get("href")))) is not None and str(
            link.get("href")
        ).rstrip("/").endswith(f"/answer/{answer_id}"):
            return link
    return None


def parse_question_total(html: str) -> int | None:
    """问题页顶部的"N 个回答"。

    ⭐ **这是能力四的完整性判据。** 问题页一直往下滚**不会出现"没有更多了"**，
    只会滚不动，所以没法用文案判断到底了。但这一行给了**声明的总数**：

        采到的条数 == 声明的总数   →  确实采全了
        采到的条数 <  声明的总数   →  **没采全，报错，不许当成功**

    ⚠️ 仍未知：回答数很大时知乎会不会限流只放一部分出来。在确定之前，
    这个判据的价值恰恰在于**它会把"没采全"变成一次可见的报错，
    而不是一次安静的成功。**

    返回 None 表示页面上没有这一行（不是问题页，或者结构变了）。
    """
    soup = BeautifulSoup(html, "html.parser")
    text = _text(_pick(soup, selectors.QUESTION_TOTAL))
    if not text:
        return None
    match = _TOTAL_RE.search(text)
    return int(match.group(1).replace(",", "")) if match else None


def parse_collapsed_count(html: str) -> int:
    """问题页上「N 个回答被折叠」的 N。**没有这一行就返回 0，不返回 None。**

    折叠回答不点开（它们也不进采集），但它们**算进声明总数里**，
    所以必须数出来，否则 `parse_question_total` 那条完整性判据会永远差那几条。

    ⚠️ **返回 0 而不是 None 是刻意的**：这一行是浮层，不一定随滚动出现在 DOM 里，
    "没看见"不等于"没有折叠的"。调用方拿到的语义是"已知的折叠数下限"，
    完整性判据写成 `采到 + 折叠 >= 声明` 就自然容忍了这一点。
    反过来说：**不能拿它当"问题页有折叠"的判据**（0 也可能是没渲染出来）。
    """
    soup = BeautifulSoup(html, "html.parser")
    bar = _pick(soup, selectors.COLLAPSED_ANSWERS_BAR)
    if bar is None:
        return 0
    return parse_collapsed_text(bar.get_text(" ", strip=True))


def parse_collapsed_text(text: str | None) -> int:
    """同 `parse_collapsed_count`，但吃的是**那一行元素的文本**。

    存在的理由是那一行是浮层，会随滚动出现和消失。所以 `answers.py` 每轮都
    从浏览器里重新读一次它的文本——不必为了读一行字把整页 HTML 重新 parse 一遍。

    ⚠️ 实测文本是 `'6 个回答被折叠 （ 为什么？ ）'`——**别用 `$` 锚定**。
    """
    if not text:
        return 0
    match = _COLLAPSED_RE.search(text)
    return int(match.group(1).replace(",", "")) if match else 0


# ── 能力三：评论 ────────────────────────────────────────────────────


def parse_comment_item(html: str, parent_id: str | None = None) -> ParsedItem | None:
    """能力三：一条评论 → 结构。

    `parent_id` 由调用方给：一级评论传被评论内容的 ID。
    **它同时也是去重之外唯一能说明"这条评论挂在哪条内容下"的字段**。
    """
    soup = BeautifulSoup(html, "html.parser")
    node = _pick(soup, selectors.COMMENT_ITEM) or soup
    return _comment_from_node(node, parent_id)


def parse_comment_list(html: str, content_id: str | None = None) -> list[ParsedItem]:
    """能力三：一段评论 DOM → **一级评论**，拍平成一个列表。

    ⚠️ **只取一级，不往下走。** 回复是嵌套在父评论的 `[data-id]` 里的另一层
    `[data-id]`（见 `selectors.COMMENT_ITEM`），所以这里先挑出"没有 `[data-id]`
    祖先"的节点。挑漏了不会报错——回复会被当成一条条独立的一级评论存进库，
    `parent_id` 全指向正文，看起来完全正常。

    ⚠️ **回复不采是刻意的**（2026-09-27）：点开回复面板之后关不掉，
    回不到原来那条评论的位置，而且每点一次都要重开一次页面等很久。
    取舍的完整理由见 `comments.py` 模块开头。**别把它当 bug 修回来。**
    """
    soup = BeautifulSoup(html, "html.parser")
    return [
        item
        for node in _root_comments(soup)
        if (item := _comment_from_node(node, content_id)) is not None
    ]


def _root_comments(soup: Tag) -> list[Tag]:
    """所有**没有 `[data-id]` 祖先**的评论节点，也就是一级评论。

    ⚠️ 不能直接 `select("[data-id]")` 了事——那会把两级拍平，
    回复看起来和一级评论一模一样，层级就没了。
    """
    return [
        node
        for node in _pick_all(soup, selectors.COMMENT_ITEM)
        if node.find_parent(attrs={selectors.COMMENT_ID_ATTR: True}) is None
    ]


def parse_comment_total(html_or_text: str | None) -> int | None:
    """评论弹窗标题里声明的评论数（「394 条评论」→ 394）。没有就返回 None。

    ⚠️ **必须整串相等**，不能 `search`：正文页上「N 条评论」是**按钮**
    （点了才开弹窗），谈的是"点开看"，只有弹窗标题那处才是"一共有多少条"。

    ⚠️⚠️ **它含不含二级回复，没有实测。** 所以调用方只把它当参考报出来，
    **不当硬判据**——拿一个语义不明朗的数当失败阈值会变成天天误报，
    而天天误报的判据等于没有判据。
    """
    if not html_or_text:
        return None
    if "<" in html_or_text:
        texts = BeautifulSoup(html_or_text, "html.parser").find_all(
            string=_is_comment_total_text
        )
        text = str(texts[0]) if texts else None
    else:
        text = html_or_text
    match = _COMMENT_TOTAL_RE.fullmatch(_norm_comment_text(text))
    return int(match.group(1).replace(",", "")) if match else None


def _is_comment_total_text(node: object) -> bool:
    """BeautifulSoup 的文本节点过滤器：这段文字是不是「N 条评论」。"""
    return bool(_COMMENT_TOTAL_RE.fullmatch(_norm_comment_text(str(node))))


def _norm_comment_text(text: str | None) -> str:
    """剥零宽字符、塌缩空白。评论区的文案比对一律先过这一道。"""
    return " ".join(_ZERO_WIDTH_RE.sub("", text or "").split())


def _comment_from_node(node: Tag, parent_id: str | None) -> ParsedItem | None:
    """一条评论节点 → `ParsedItem`。**拿不到作者就整条丢掉**，正文空不丢。

    ⚠️ **纯图片评论（表情包、截图）要保留**：它们的 `.CommentContent` 是在的，
    里面只有一个 `<img>`，`get_text()` 出来是空串。按"没有正文就丢"处理的话，
    "声明 18 条 / 实收 15 条"会安静地发生——而那正是本来为这种情况准备的
    对账判据，判据跟漏采一起失效就没人能发现了。所以保留 `text=None`，
    图片地址留在 `html` 里，由调用方统计"有几条没有文字"。

    还丢的只剩两种情况，都有明确理由：

      1. **没有作者链接** —— 无法归属的评论没有取证价值；
      2. **既没有评论 ID、正文又是空的** —— 连内容哈希兜底都算不出唯一值
         （空正文的哈希全都一样，一堆评论会被并成一条）。
    """
    # ⚠️ 评论用的是 COMMENT_AUTHOR_LINK（裸 `a[href*='/people/']`），
    # **不是正文页的 AUTHOR_LINK**。实测评论节点里后者命中 0 个，
    # 套错的结果是整棵树一条都解析不出来，而且不报错。
    author_link = _href(node.select_one(selectors.COMMENT_AUTHOR_LINK))
    if not author_link:
        return None

    # ⚠️ 必须在 node 内部找正文，不能退回整页：`[data-id]` 是嵌套的，
    # 退回去会取到**别人的**评论正文。
    body = _pick(node, selectors.COMMENT_CONTENT)
    text = _text(body)

    comment_id = _attr(node, selectors.COMMENT_ID_ATTR)
    if not comment_id and not text:
        return None  # 见上面第 2 条：没有 ID 又没有正文，连唯一标识都造不出来
    zhihu_id, url = _comment_identity(comment_id, author_link, text)

    return ParsedItem(
        url=url,
        content_type="comment",
        zhihu_id=zhihu_id,
        question_id=None,
        parent_id=parent_id,
        text=text,
        html=str(body) if body else None,
        author_name=_comment_author_name(node),
        author_url=author_link,
        voteup_count=_count_from_button(node, selectors.UPVOTE_BUTTON),
        published_text=_text(_pick(node, selectors.PUBLISHED_TIME)),
        published_at=parse_datetime(_text(_pick(node, selectors.PUBLISHED_TIME))),
    )


def _comment_author_name(node: Tag) -> str | None:
    """评论作者的昵称。

    ⚠️ 每个评论节点里有**两个** `a[href*='/people/']`：第一个是**头像**
    （`<a>` 里没有文字），第二个才是昵称。直接用第一个会一直拿到 None，
    而且不报错。
    """
    for link in node.select(selectors.COMMENT_AUTHOR_LINK):
        if text := _text(link):
            return text
    return None


def _comment_identity(
    comment_id: str | None, author_url: str, text: str
) -> tuple[str, str]:
    """评论的自然键 `(zhihu_id, url)`。

    ⚠️ **两者在 `fact_content` 里都是 not null**，所以哪怕拿不到 ID 也得有兜底。

    优先用 `[data-id]` 的真实评论 ID——那是知乎自己的主键，不会撞。兜底才用
    内容哈希，它有个已知代价：**同一个作者发的两条内容完全相同的评论会被判成
    同一条**（刷屏时会发生）。有了真实 ID 之后这条代价基本不出现了。
    """
    if comment_id:
        return comment_id, f"{ZHIHU}/comment/{comment_id}"

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"c-{digest}", f"{author_url}#comment-{digest}"


# ── 字段级工具（都能单独测）─────────────────────────────────────────


def parse_count(text: str | None) -> int:
    """把"1.2 万"、"3,456 赞同"、"赞同 12"这类文本变成整数。

    ⚠️ 知乎大数会显示成"万"，直接 `int()` 会得到 1 而不是 12000——
    赞同数在风险评估里是个有用的信号，不能估错一个量级。
    """
    if not text:
        return 0
    cleaned = text.replace(",", "").replace(" ", "")
    match = _NUM_RE.search(cleaned)
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2)
    if unit == "万":
        value *= 10_000
    elif unit in ("千", "k", "K"):
        value *= 1_000
    return int(value)


def parse_datetime(text: str | None) -> datetime | None:
    """从发布时间文本里解析出**绝对**时间；相对时间一律返回 None。

    ⚠️ 这是刻意的：「3 小时前」换算成时间戳需要假设"抓取那一刻就是发布后 3 小时"，
    而页面可能是缓存的上个月的快照。**猜出来的时间戳用在法律证据上是负资产**——
    它看起来精确，实际可能是错的。所以相对时间只保留原始文本（`published_text`）。

    ⚠️ 评论里的时间是 `04-22` 这种**不带年份**的，也会返回 None——同上，
    猜年份等于伪造证据。
    """
    if not text:
        return None
    match = _DATE_RE.search(text)
    if not match:
        return None
    year, month, day = int(match.group(1)), int(match.group(2)), int(match.group(3))
    hour = int(match.group(4) or 0)
    minute = int(match.group(5) or 0)
    try:
        return datetime(year, month, day, hour, minute, tzinfo=UTC)
    except ValueError:
        return None  # 比如 2024-13-45，页面上不该有，但别让它炸掉整轮


def _pick(soup: Tag, group: str) -> Tag | None:
    """按**候选书写顺序**逐个试，返回第一个命中的。

    这就是架构文档 3.11 那三档里的第 2、3 档：语义化属性（aria-label、
    itemprop）写在前面，CSS 类名写在后面（见 selectors.py 开头的规矩）。

    ⚠️ 不能图省事直接把整组交给 BeautifulSoup——CSS 分组的 `.A, .B` 命中的是
    **文档里靠前**的那个，不是我们字面上靠前的那个。那等于放弃了优先级。
    """
    for candidate in selectors.candidates(group):
        found = soup.select_one(selectors.css(candidate))
        if found is not None:
            return found
    return None


def _pick_all(soup: Tag, group: str) -> list[Tag]:
    """`_pick` 的复数版：用第一个**有命中**的候选，只返回它的结果。

    刻意不是"所有候选的结果取并集"——并集会重复计数（一个元素同时匹配
    `.CommentItem` 和 `[itemprop='comment']` 时），而条数正是校准的判据。
    """
    for candidate in selectors.candidates(group):
        found = soup.select(selectors.css(candidate))
        if found:
            return found
    return []


def _text(el: Tag | None) -> str | None:
    """元素的可见文本；`<meta>` 例外——它的值在 `content` 属性里。

    ⚠️ 这个分支是必须的，不是顺手加的：知乎把一堆微数据放在
    `<meta itemprop="headline" content="真正的标题">` 上，**标签之间的文本是空的**。
    于是 `_pick` 正常命中，`_text` 却返回 None——标题永远是 None，而且不报错。
    """
    if el is None:
        return None
    if el.name == "meta":
        content = el.get("content")
        return str(content) if content else None
    text = el.get_text(" ", strip=True)
    return text or None


def _href(el: Tag | None) -> str | None:
    if el is None:
        return None
    href = el.get("href")
    if not href:
        return None
    return urljoin(ZHIHU, str(href))


def _attr(el: Tag | None, name: str) -> str | None:
    if el is None or not name:
        return None
    value = el.get(name)
    return str(value) if value is not None else None


def _count_from_button(soup: Tag, selector: str) -> int:
    """取赞同数。三个来源按可靠度试：`<meta content>` → 按钮文本 → `aria-label`。

    实测三种形态都存在：`<meta itemprop="upvoteCount" content="4886">`、
    `aria-label="赞同 4886 "`、按钮文本里也可能带数字。
    """
    el = _pick(soup, selector)
    if el is None:
        return 0
    if el.name == "meta":
        return parse_count(_attr(el, "content"))
    return parse_count(el.get_text(" ", strip=True)) or parse_count(_attr(el, "aria-label"))


def _comment_count(soup: Tag) -> int:
    """评论数。优先 `<meta itemprop="commentCount">`，退回"N 条评论"的按钮文本。

    ⚠️ 退回这一路**必须把数字和「条评论」绑在一起取**，不能对整段文本
    `parse_count` 拿第一个数。搜索卡片匹配到的第一个元素是**外层容器**：

        <div class="ContentItem-actions"> → " 赞同 117   19 条评论 08-31"

    第一个数字是**赞同数**。写 `parse_count(text)` 就会静默返回 117，
    而调用方以为那是评论数——两个数还长得一模一样，存进库里肉眼核对都看不出来。

    正文页碰不到这个坑：那些页面有 `meta[itemprop='commentCount']`，走上面那条
    精确路径就返回了。**搜索页没有这个 meta**，所以搜索卡片一接上就会踩中。
    """
    meta = _pick(soup, selectors.COMMENT_COUNT_META)
    if meta is not None:
        return parse_count(_attr(meta, "content"))

    for el in _pick_all(soup, selectors.COMMENT_COUNT_BUTTON):
        if m := _COMMENT_COUNT_IN_TEXT_RE.search(el.get_text(" ", strip=True)):
            return parse_count(m.group(0))
    return 0
