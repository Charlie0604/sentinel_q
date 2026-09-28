"""URL 规范化与类型解析（架构文档 3.9 第 1 条）。

知乎同一个内容有多个 URL 变体：

    /question/123/answer/456
    /question/123/answer/456?sort=created&utm_source=...
    www.zhihu.com/...   vs   zhihu.com/...

不规范化的话，同一内容会以不同 URL 反复入库，`fact_content.url` 上的唯一约束形同虚设。
**这件事必须在采集层解决，不能指望数据库兜底。**
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_CONTENT_HOSTS = {
    "zhihu.com",
    "www.zhihu.com",
    "m.zhihu.com",
    "zhuanlan.zhihu.com",
}

_ANSWER_RE = re.compile(r"^/question/(?P<qid>\d+)/answer/(?P<aid>\d+)$")
_QUESTION_RE = re.compile(r"^/question/(?P<qid>\d+)$")
_ARTICLE_RE = re.compile(r"^/p/(?P<pid>\d+)$")
_PROFILE_RE = re.compile(r"^/(?:people|org)/(?P<uid>[^/]+)$")
"""个人主页。**只认恰好一段路径**，理由见 `profile_user_id()`。"""

_PIN_RE = re.compile(r"^/pin/(?P<tid>\d+)$")
"""想法（pin）。**2026-09-26 对着真实快照实测确认**，原来标着"待实测"。

实测来源：想法搜索页（`search?type=pin&q=…`）里 173 个不同 pin 链接，
形态一律是 `//www.zhihu.com/pin/<纯数字>`，没有子路径、没有 query。

（顺带确认了想法卡片在列表里的语义标记是 `[itemprop='zhihu:pin']`，
内容在 `<meta itemprop="url">` / `<meta itemprop="name">` 里。）
"""


@dataclass(frozen=True)
class NormalizedUrl:
    """规范化结果。

    `question_id` 是"所属问题"，用于平铺到 `fact_content.question_id`（见 5.5.1）：
    有了它，"某个问题下的全部内容"是一次 GROUP BY，而不是递归 CTE。
    """

    url: str
    content_type: str | None  # question / answer / article / thought（想法路径待实测）
    zhihu_id: str | None
    question_id: str | None


def _absolute(raw: str | None) -> str | None:
    """补全成绝对 URL。三种写法都要认：
        //www.zhihu.com/x   协议相对
        /question/123/...   站内相对路径（最常见——从页面里抓到的就是这种）
        www.zhihu.com/x     漏了 scheme
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("//"):
        return "https:" + raw
    if raw.startswith("/"):
        return "https://www.zhihu.com" + raw
    if not raw.startswith(("http://", "https://")):
        return "https://" + raw
    return raw


def normalize(raw: str | None) -> NormalizedUrl | None:
    """把任意知乎 URL 变体规范化为唯一形式。

    返回 `None` 表示这不是一条知乎内容 URL（外链、站内其他页面等），调用方应跳过。
    """
    if not (raw := _absolute(raw)):
        return None

    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if host not in _CONTENT_HOSTS:
        return None

    # 剥掉 query 与 fragment（urlsplit 已经把两者拆开了，这里只取 path）
    path = parts.path.rstrip("/") or "/"

    # 统一 host：**只有专栏文章在 zhuanlan**，其余一律归到 www。
    #
    # 判据用路径而不是来源 host：专栏文章的正规地址是 zhuanlan.zhihu.com/p/<id>，
    # 但从 www.zhihu.com/p/<id> 进来也指同一篇。原来按 host 判定，会把后者
    # 归一成 www 形式，于是同一篇文章有两个规范 URL——唯一约束就挡不住了。
    canonical_host = "zhuanlan.zhihu.com" if _ARTICLE_RE.match(path) else "www.zhihu.com"
    url = f"https://{canonical_host}{path}"

    if match := _ANSWER_RE.match(path):
        return NormalizedUrl(url, "answer", match["aid"], match["qid"])
    if match := _QUESTION_RE.match(path):
        return NormalizedUrl(url, "question", match["qid"], match["qid"])
    if match := _ARTICLE_RE.match(path):
        return NormalizedUrl(url, "article", match["pid"], None)
    if match := _PIN_RE.match(path):
        # 想法不属于任何问题，question_id 为 None（和专栏文章同理）
        return NormalizedUrl(url, "thought", match["tid"], None)

    # 认不出来的路径一律拒绝，而不是返回 content_type=None 的行——
    # fact_content.content_type 是 not null 且带 check 约束，未分类的行根本存不进去。
    #
    # 调用方应当**统计被挡掉的数量**并记进采集 WAL / runs.jsonl，别让它变成静默丢数据。
    return None


def profile_user_id(raw: str | None) -> str | None:
    """主页链接 → 知乎用户 ID，即 `dim_author.zhihu_user_id` 的自然键。

        /people/gqd2kn   → gqd2kn
        /org/19551942    → 19551942

    取**路径最后一段**。⚠️ 只认恰好一段的路径，多一段就返回 None：

        /people/gqd2kn/followers   ← 这是"关注者"页签，不是主页

    照取最后一段的话，`followers` / `answers` / `following` 会被当成一个个
    **新用户**建进 `dim_author`，而每个用户的主页里都有这些页签——脏维度行
    会安静地入库、安静地累积，还和真实用户混在同一列里分不开。
    宁可返回 None 让作者维度空着：空着会被 `unresolved_authors` 数出来并报警，
    而脏行不会。

    返回 None 的其他情况：匿名回答（链接为 None）、站外链接、内容页链接。
    """
    if not (raw := _absolute(raw)):
        return None
    parts = urlsplit(raw)
    if (parts.hostname or "").lower() not in _CONTENT_HOSTS:
        return None
    match = _PROFILE_RE.match(parts.path.rstrip("/"))
    return match["uid"] if match else None
