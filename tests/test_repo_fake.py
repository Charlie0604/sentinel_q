"""仓储接口的契约测试（架构文档 8.5）。

这些用例跑在 `FakeRepo` 上，不碰网络、不连数据库。它们要钉住的不是
"FakeRepo 好用"，而是**三条并发下的正确性规则**——每条都对应架构文档里
一个已经踩过的坑，而且都容易在实现时写错：

  1. `claim_question` 必须原子抢占（4.1：两个线程不能问同一个问题两遍）
  2. `mark_follow_up_done` 必须条件更新（4.2：否则整个问题的回答会被爬两遍）
  3. `insert_content` 必须靠 url 唯一约束静默挡重复（3.9：不能报错中断任务）
"""

from __future__ import annotations

from core.models import AnalysisResult, ContentRecord
from core.repo import FakeRepo


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
    assert repo.mark_follow_up_done(7) is True
    assert repo.mark_follow_up_done(7) is False


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


def test_queue_claim_hands_each_url_to_one_worker() -> None:
    """8.8：两个采集进程并行抢任务，同一条 URL 不能被抢走两次。"""
    repo = FakeRepo()
    repo.enqueue("https://www.zhihu.com/question/1/answer/2", priority=5)
    repo.enqueue("https://www.zhihu.com/question/1/answer/3")

    first = repo.claim_next_url("worker-a")
    second = repo.claim_next_url("worker-b")
    third = repo.claim_next_url("worker-c")

    assert first is not None and first.url.endswith("/answer/2")
    assert second is not None and second.url.endswith("/answer/3")
    assert third is None  # 队列空了


def test_analysis_records_prompt_version() -> None:
    """8.9：提示词不进 git，所以 prompt_version 是唯一能追溯"当时用的哪版"的地方。"""
    repo = FakeRepo()
    repo.insert_content(_record("https://www.zhihu.com/question/1/answer/2"))

    repo.save_analysis(
        AnalysisResult(
            content_id="c1",
            platform_stance="抹黑",
            risk_level="中风险",
            model_version="claude-sonnet-5",
            prompt_version="a1b2c3d4e5f60718",
        )
    )

    assert repo.analyses["c1"].prompt_version == "a1b2c3d4e5f60718"
