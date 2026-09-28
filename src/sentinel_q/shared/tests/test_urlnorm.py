"""URL 规范化的单元测试（架构文档 3.9 第 1 条）。

这件事必须在采集层解决——不规范化的话，同一内容会以不同 URL 反复入库，
`fact_content.url` 上的唯一约束形同虚设，而唯一约束正是 4.1 那道成本闸门的基础。
"""

from __future__ import annotations

import pytest

from sentinel_q.shared.urlnorm import normalize, profile_user_id

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
        "https://zhuanlan.zhihu.com/p/987654",
        "https://www.zhihu.com/p/987654",  # 同一个 /p/<id>，只是从别的域名进来的
        "//www.zhihu.com/p/987654",
    ],
)
def test_article_host_does_not_split_the_identity(raw: str) -> None:
    """**同一个 /p/<id>，无论从哪个域名进来，都必须归一成同一个 URL。**

    归一化按来源 host 判定的话，`www.zhihu.com/p/987654` 会得到 www 形式的
    规范 URL，`zhuanlan.zhihu.com/p/987654` 得到 zhuanlan 形式的——同一篇文章
    两个规范 URL，`fact_content.url` 上的唯一约束就形同虚设了。
    所以判据必须是**路径**，不是 host。
    """
    result = normalize(raw)
    assert result is not None
    assert result.url == "https://zhuanlan.zhihu.com/p/987654"


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.zhihu.com/pin/1234567890",
        "//www.zhihu.com/pin/1234567890",
        "https://www.zhihu.com/pin/1234567890?page=video_pin&scene=share",
    ],
)
def test_pin_url(raw: str) -> None:
    """想法（pin）的路径规则 2026-09-26 对着真实快照实测确认。

    实测来源：想法搜索页里 173 个 pin 链接，形态一律 `/pin/<纯数字>`。
    """
    result = normalize(raw)
    assert result is not None
    assert result.url == "https://www.zhihu.com/pin/1234567890"
    assert result.content_type == "thought"
    assert result.zhihu_id == "1234567890"
    assert result.question_id is None  # 想法不属于任何问题


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
        "https://www.zhihu.com/pin/not-a-number",  # 想法路径对但 id 不是数字
        "https://www.zhihu.com/pin/123/456",  # 想法没有子路径
    ],
)
def test_unrecognized_urls_are_rejected(raw: str | None) -> None:
    """认不出来的一律拒绝。

    `fact_content.content_type` 是 not null 且带 check 约束，
    返回 content_type=None 的行根本存不进库——所以这里必须返回 None。
    """
    assert normalize(raw) is None


# ── 作者：`dim_author` 的自然键 ─────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.zhihu.com/people/gqd2kn", "gqd2kn"),
        ("//www.zhihu.com/people/gqd2kn", "gqd2kn"),
        ("/people/gqd2kn", "gqd2kn"),
        ("https://www.zhihu.com/people/gqd2kn/", "gqd2kn"),
        # 链接上带查询参数是页面里抓到的常态，不该影响取 ID
        ("https://www.zhihu.com/people/gqd2kn?utm_source=profile", "gqd2kn"),
        ("https://www.zhihu.com/org/19551942", "19551942"),
    ],
)
def test_profile_user_id_is_the_last_segment(raw: str, expected: str) -> None:
    assert profile_user_id(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        # ⭐ 主页里的**子页签**。最后一段是 followers/answers，不是用户 ID。
        #    照取的话每个用户的关注者页都会变成一个新用户——脏维度行。
        "https://www.zhihu.com/people/gqd2kn/followers",
        "https://www.zhihu.com/people/gqd2kn/answers",
        "/people/gqd2kn/following",
        # 不是主页
        "https://www.zhihu.com/question/123/answer/456",
        "https://www.zhihu.com/",
        "https://example.com/people/gqd2kn",
        None,
        "",
        "   ",
    ],
)
def test_non_profile_links_yield_no_user_id(raw: str | None) -> None:
    assert profile_user_id(raw) is None
