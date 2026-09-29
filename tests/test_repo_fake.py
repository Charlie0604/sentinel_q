"""仓储接口的契约测试（架构文档 7.5）。

这些用例跑在 `FakeRepo` 上，不碰网络、不连数据库。它们要钉住的不是
"FakeRepo 好用"，而是**几条容易在实现时写错的规则**——每条都对应架构文档里
一个已经踩过的坑：

  1. `claim_question` 必须原子抢占（4.1：两个线程不能问同一个问题两遍）
  2. `mark_follow_up_done` 必须条件更新（4.2：否则整个问题的回答会被爬两遍）
  3. `insert_content` 必须靠 url 唯一约束静默挡重复（3.9：不能报错中断任务）
  4. `ensure_author` 必须是 upsert（作者维度：同一个人只能有一行 `dim_author`）
"""

from __future__ import annotations

from sentinel_q.shared.models import AnalysisResult, ContentRecord
from sentinel_q.storage import FakeRepo


def _record(url: str, zhihu_id: str = "456") -> ContentRecord:
    return ContentRecord(content_type="answer", zhihu_id=zhihu_id, url=url)


def test_claim_question_only_first_arrival_wins() -> None:
    """4.1 的先插后问：第二次调用必须拿到 None，否则会重复问 AI。"""
    repo = FakeRepo()

    first = repo.claim_question("1997704627181877031", "https://www.zhihu.com/question/1", "标题")
    second = repo.claim_question("1997704627181877031", "https://www.zhihu.com/question/1", "标题")

    assert first is not None
    assert second is None  # 别人已经在问了，本进程不该再提交 AI 调用


def test_question_relevance_starts_unknown() -> None:
    """`is_relevant` 三态：null 表示"已抢占，待判断"，不是 false。"""
    repo = FakeRepo()
    question_id = repo.claim_question("123", None, None)
    assert question_id is not None
    assert repo.question_relevance(question_id) is None


def test_follow_up_only_fires_once() -> None:
    """4.2 的条件更新：并发下只应有一个线程触发全量回答采集。"""
    repo = FakeRepo()
    question_id = repo.claim_question("123", None, None)
    assert question_id is not None

    assert repo.mark_follow_up_done(question_id) is True
    assert repo.mark_follow_up_done(question_id) is False


def test_follow_up_on_missing_question_returns_false() -> None:
    """不存在的 question_id：SQL 里是 `update ... where id = $1` 命中 0 行。

    0 行受影响**不报错**，返回 False 而不是抛异常。⚠️ 这里返回 True 是危险的：
    `True` 的语义是"这次由我触发了该问题下的全量回答采集"（决策 40），
    凭空返回 True 会让调用方以为问题存在且没被采过。
    """
    repo = FakeRepo()
    assert repo.mark_follow_up_done(7) is False


def test_updates_on_missing_rows_are_silent() -> None:
    """update 不命中任何行 = 静默无事发生，与 Postgres 一致。

    要给人话的错误提示（"这条内容不存在"）由组合函数先查一次再写，
    那是 review / evidence 那一层的活。
    """
    repo = FakeRepo()
    repo.set_question_relevance(7, True)
    repo.mark_answers_collected(7)
    repo.refresh_content_metrics("no-such-id", voteup_count=1, comment_count=2)
    repo.mark_content_status("no-such-id", "deleted_detected")
    repo.set_author_watch("no-such-author", is_watched=True)
    assert repo.all_questions() == []


def test_duplicate_url_is_swallowed_not_raised() -> None:
    """3.9：2~4 个进程并行采集时，重复写入要被静默挡掉，不能中断整个任务。"""
    repo = FakeRepo()

    first = repo.insert_content(_record("https://www.zhihu.com/question/1/answer/2"))
    second = repo.insert_content(_record("https://www.zhihu.com/question/1/answer/2"))

    assert first is not None
    assert second is None


def test_existing_urls_batch_lookup() -> None:
    """3.9 第 3 条的成本闸门：按批查，命中即跳过，不进 AI。"""
    repo = FakeRepo()
    repo.insert_content(_record("https://www.zhihu.com/question/1/answer/2"))

    known = repo.existing_urls(
        [
            "https://www.zhihu.com/question/1/answer/2",
            "https://www.zhihu.com/question/1/answer/3",
        ]
    )

    assert known == {"https://www.zhihu.com/question/1/answer/2"}


def test_ensure_author_is_an_upsert_not_an_insert() -> None:
    """⭐ 同一个人返回**同一个** `author_id`，昵称取最新值。

    写成普通 insert 的话，同一个人在这批里的第二条内容就会撞唯一约束——
    要么抛异常中断整批，要么 `do nothing` 之后拿不到 ID、`author_id` 又变 null。
    """
    repo = FakeRepo()

    first = repo.ensure_author("gqd2kn", "旧昵称", "https://www.zhihu.com/people/gqd2kn")
    second = repo.ensure_author("gqd2kn", "新昵称", "https://www.zhihu.com/people/gqd2kn")

    assert first == second
    assert len(repo.authors) == 1
    assert repo.authors["gqd2kn"][0] == "新昵称", "昵称会改，每次覆盖"


def test_ensure_author_keeps_different_people_apart() -> None:
    repo = FakeRepo()
    assert repo.ensure_author("a", None, None) != repo.ensure_author("b", None, None)


def test_analysis_records_prompt_version() -> None:
    """8.9：提示词不进 git，所以 prompt_version 是唯一能追溯"当时用的哪版"的地方。"""
    repo = FakeRepo()
    content_id = repo.insert_content(_record("https://www.zhihu.com/question/1/answer/2"))
    assert content_id is not None

    repo.save_analysis(
        AnalysisResult(
            content_id=content_id,
            platform_stance="抹黑",
            risk_level="中风险",
            model_version="claude-sonnet-5",
            prompt_version="a1b2c3d4e5f60718",
        )
    )

    assert repo.analyses[content_id].prompt_version == "a1b2c3d4e5f60718"


def test_claim_question_carries_the_three_heat_metrics() -> None:
    """⭐ 迁移 0005 的三个热度列要**真的落到那行上**，不是收下就扔。

    ⚠️ 这三个参数默认全是 `None`，所以"签名收了但忘了写进 `QuestionRow`"
    在类型上完全合法、调用方一个错都不会报——只是库里那三列永远空着。
    而热度是**非回溯**的：漏了一次，那次的值就永远拿不回来了。

    ⚠️ `0` 和 `None` 要分得开：0 个关注是个合法观测值。这条同时用 0
    和非 0 各钉一次（三列都可空、刻意不给 `default 0`，理由见 0005）。
    """
    repo = FakeRepo()

    question_id = repo.claim_question(
        "123",
        "https://www.zhihu.com/question/123",
        "标题",
        description="描述",
        follower_count=0,
        view_count=84357,
        answer_count=48,
    )

    assert question_id is not None
    row = repo.all_questions()[0]
    assert row.follower_count == 0, "0 个关注是观测值，不该退化成 None"
    assert row.view_count == 84357
    assert row.answer_count == 48


def test_claim_question_leaves_the_heat_metrics_empty_when_not_given() -> None:
    """不给就是 `None`——**不能自作主张填 0**。

    能力五之前的所有调用方都不传这三个参数，它们进库的必须是"没采到"，
    而不是"这道题 0 个关注、0 次浏览"。后者会让统计把整批老数据算成
    真实的 0（迁移 0005 那三列可空、不给 `default 0` 就是为这件事）。
    """
    repo = FakeRepo()

    question_id = repo.claim_question("123", None, None)

    assert question_id is not None
    row = repo.all_questions()[0]
    assert (row.follower_count, row.view_count, row.answer_count) == (None, None, None)
