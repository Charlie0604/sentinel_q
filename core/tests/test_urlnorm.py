"""URL 规范化的单元测试（架构文档 3.9 第 1 条）。

这件事必须在采集层解决——不规范化的话，同一内容会以不同 URL 反复入库，
`fact_content.url` 上的唯一约束形同虚设，而唯一约束正是 4.1 那道成本闸门的基础。
"""

from __future__ import annotations

import pytest

from core.urlnorm import normalize

ANSWER = "https://www.zhihu.com/question/123/answer/456"


@pytest.mark.parametrize(
    "raw",
    [
        "/question/123/answer/456",
        "https://www.zhihu.com/question/123/answer/456",
        "http://zhihu.com/question/123/answer/456",
        "https://m.zhihu.com/question/123/answer/456/",
        "https://www.zhihu.com/question/123/answer/456?sort=created&utm_source=wechat",
        "https://www.zhihu.com/question/123/answer/456#comments",
        "//www.zhihu.com/question/123/answer/456",
        "  https://www.zhihu.com/question/123/answer/456  ",
    ],
)
def test_answer_variants_collapse_to_one_url(raw: str) -> None:
    result = normalize(raw)
    assert result is not None
    assert result.url == ANSWER
    assert result.content_type == "answer"
    assert result.zhihu_id == "456"
    assert result.question_id == "123"  # 平铺的所属问题，供 5.5.1 用


def test_question_url() -> None:
    result = normalize("https://www.zhihu.com/question/1997704627181877031")
    assert result is not None
    assert result.url == "https://www.zhihu.com/question/1997704627181877031"
    assert result.content_type == "question"
    assert result.question_id == result.zhihu_id


def test_article_keeps_its_own_host() -> None:
    result = normalize("https://zhuanlan.zhihu.com/p/987654?utm_psn=1")
    assert result is not None
    assert result.url == "https://zhuanlan.zhihu.com/p/987654"
    assert result.content_type == "article"
    assert result.zhihu_id == "987654"
    assert result.question_id is None  # 专栏文章不属于任何问题


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "https://example.com/question/123/answer/456",
        "https://weibo.com/1234567/abcdef",
        "https://www.zhihu.com/people/someone",
        "https://www.zhihu.com/topic/19551275",
        "https://www.zhihu.com/search?q=xxx",
        # ⚠️ 想法（pin）的真实路径规则尚未实测，现在会被挡在这里。
        # 这是有意的：content_type 认不出来就不入库，总好过存一条错类型的行。
        # 实测确认后在这里补上正例，并同步 core/urlnorm.py。
        "https://www.zhihu.com/pin/1234567890",
    ],
)
def test_unrecognized_urls_are_rejected(raw: str | None) -> None:
    """认不出来的一律拒绝。

    `fact_content.content_type` 是 not null 且带 check 约束，
    返回 content_type=None 的行根本存不进库——所以这里必须返回 None。
    """
    assert normalize(raw) is None
