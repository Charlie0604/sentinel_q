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


def normalize(raw: str | None) -> NormalizedUrl | None:
    """把任意知乎 URL 变体规范化为唯一形式。

    返回 `None` 表示这不是一条知乎内容 URL（外链、站内其他页面等），调用方应跳过。
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    # 补全成绝对 URL。三种写法都要认：
    #   //www.zhihu.com/x   协议相对
    #   /question/123/...   站内相对路径（最常见——从页面里抓到的就是这种）
    #   www.zhihu.com/x     漏了 scheme
    if raw.startswith("//"):
        raw = "https:" + raw
    elif raw.startswith("/"):
        raw = "https://www.zhihu.com" + raw
    elif not raw.startswith(("http://", "https://")):
        raw = "https://" + raw

    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    if host not in _CONTENT_HOSTS:
        return None

    # 剥掉 query 与 fragment（urlsplit 已经把两者拆开了，这里只取 path）
    path = parts.path.rstrip("/") or "/"

    # 统一 host：专栏内容有自己的域名，其余一律归到 www
    canonical_host = "zhuanlan.zhihu.com" if host == "zhuanlan.zhihu.com" else "www.zhihu.com"
    url = f"https://{canonical_host}{path}"

    if match := _ANSWER_RE.match(path):
        return NormalizedUrl(url, "answer", match["aid"], match["qid"])
    if match := _QUESTION_RE.match(path):
        return NormalizedUrl(url, "question", match["qid"], match["qid"])
    if match := _ARTICLE_RE.match(path):
        return NormalizedUrl(url, "article", match["pid"], None)

    # 认不出来的路径一律拒绝，而不是返回 content_type=None 的行——
    # fact_content.content_type 是 not null 且带 check 约束，未分类的行根本存不进去。
    #
    # ⚠️ 代价：想法（pin）的 URL 规则实测确认之前，它会被挡在这里。
    #    调用方应当**统计被挡掉的数量**并记进 crawl_runs，别让它变成静默丢数据
    #    （见第三章待定事项"实测确认'想法'的 URL 路径规则"）。
    return None
