"""任务 B：问题判定（架构文档 3.5）。

一个问题 → 是否与监测对象相关。**只有这一项判断**，落 `dim_question.is_relevant`。

⚠️ 这个任务最容易写松的地方是"顺手也把立场判了"。3.5 的对照表里任务 B 的输出栏
只有"是否相关"，所以 `QuestionJudgment` 上根本没有那几个字段——模型就算多返回了
也落不下来。这不是随手少写几个字段，是决策 31 那条原则的落点：
**提示词是怎么问，表结构是存什么。**

⚠️ 键用 `zhihu_qid` 而不是标题（4.1）：标题和描述被编辑后 ID 也不变。
"""

from __future__ import annotations

import json

import pytest

from sentinel_q.analyst import judge_question
from sentinel_q.analyst.client import LLMSchemaError
from sentinel_q.analyst.fake import FakeLLMClient, full_bundle
from sentinel_q.analyst.judge_question import PendingQuestion
from sentinel_q.shared.models import QuestionJudgment


def _pending(**overrides: object) -> PendingQuestion:
    fields: dict[str, object] = {
        "zhihu_qid": "100000001",
        "title": "如何看待某某事件？",
        "description": "最近这件事讨论得很多。",
    }
    fields.update(overrides)
    return PendingQuestion(**fields)  # type: ignore[arg-type]


def _judge(reply: str, **overrides: object) -> QuestionJudgment:
    return judge_question.judge_one(
        _pending(**overrides), bundle=full_bundle(), client=FakeLLMClient(reply)
    )


# ── 只有一项判断 ────────────────────────────────────────────────────


def test_only_relevance_comes_back() -> None:
    judgment = _judge('{"is_relevant": true}')

    assert judgment.zhihu_qid == "100000001"
    assert judgment.is_relevant is True


def test_extra_fields_the_model_volunteers_are_dropped() -> None:
    """⭐ 提示词没问的，落不下来。

    模型很爱顺手多给几个字段（立场、风险、摘要）。这个任务只问相关性，
    多出来的就在那里飘着——`QuestionJudgment` 上没有它们的容身之处，
    所以它们也就没法顺着某条看不见的路渗进 `dim_question` 里。
    """
    reply = json.dumps(
        {
            "is_relevant": True,
            "platform_stance": "抹黑",
            "risk_level": "高风险",
            "ai_summary": "多给的一段话",
            "stance_confidence": 0.99,
        },
        ensure_ascii=False,
    )

    judgment = _judge(reply)

    for leaked in ("platform_stance", "risk_level", "ai_summary", "stance_confidence"):
        assert not hasattr(judgment, leaked), f"{leaked} 不该出现在问题判定上"


def test_a_missing_relevance_raises() -> None:
    with pytest.raises(LLMSchemaError):
        _judge("{}")


def test_a_non_boolean_relevance_raises() -> None:
    with pytest.raises(LLMSchemaError):
        _judge('{"is_relevant": "是"}')


# ── 拼装 ────────────────────────────────────────────────────────────


def test_the_question_prompt_is_the_question_task_s_prompt() -> None:
    bundle = full_bundle()
    client = FakeLLMClient('{"is_relevant": true}')

    judge_question.judge_one(_pending(), bundle=bundle, client=client)

    system, user = client.calls[0]
    assert system == bundle.assemble("question")
    assert "<question_tasks>" in system
    # ⚠️ 一个 bundle 服务三个任务能不能成立，就看这几行
    assert "<c_tasks>" not in system
    assert "<event_tasks>" not in system
    assert "【问题标题】如何看待某某事件？" in user
    assert "【问题描述】最近这件事讨论得很多。" in user


def test_a_question_without_a_description_says_so() -> None:
    """空描述和"描述是空的"是一回事，说清楚，免得模型以为下面漏了一段。"""
    client = FakeLLMClient('{"is_relevant": true}')

    judge_question.judge_one(_pending(description=None), bundle=full_bundle(), client=client)

    assert "只能按标题判断" in client.calls[0][1]


def test_a_whitespace_description_is_treated_as_absent() -> None:
    client = FakeLLMClient('{"is_relevant": true}')

    judge_question.judge_one(_pending(description="   \n "), bundle=full_bundle(), client=client)

    assert "只能按标题判断" in client.calls[0][1]


def test_a_blank_title_is_refused_before_calling_the_model() -> None:
    """标题是这个任务唯一的输入，空标题下模型给的任何答案都是编的。"""
    client = FakeLLMClient('{"is_relevant": true}')

    with pytest.raises(ValueError) as excinfo:
        judge_question.judge_one(_pending(title="   "), bundle=full_bundle(), client=client)

    assert "100000001" in str(excinfo.value)
    assert client.calls == []


# ── 版本戳 ──────────────────────────────────────────────────────────


def test_the_judgment_carries_the_versions() -> None:
    bundle = full_bundle(content_hash="hash-q")
    client = FakeLLMClient('{"is_relevant": false}', model="m-q")

    judgment = judge_question.judge_one(_pending(), bundle=bundle, client=client)

    assert (judgment.model_version, judgment.prompt_version) == ("m-q", "hash-q")


# ── 批量 ────────────────────────────────────────────────────────────


def test_a_batch_is_labelled_by_the_question_id() -> None:
    pendings = [_pending(zhihu_qid=f"q{i}", title=f"第 {i} 个问题") for i in range(4)]

    report = judge_question.judge_many(
        pendings,
        bundle=full_bundle(),
        client=FakeLLMClient('{"is_relevant": true}'),
        concurrency=2,
    )

    assert report.ok
    assert report.done == 4


def test_a_batch_reports_the_failing_question_id() -> None:
    class _PickyClient:
        model = "fake-model"

        def complete(self, *, system: str, user: str) -> str:
            return "{}" if "坏问题" in user else '{"is_relevant": true}'

    pendings = [
        _pending(zhihu_qid="q0", title="好问题"),
        _pending(zhihu_qid="q1", title="坏问题"),
    ]

    report = judge_question.judge_many(
        pendings, bundle=full_bundle(), client=_PickyClient(), concurrency=2
    )

    assert report.done == 1
    assert [label for label, _ in report.failed] == ["q1"]


def test_the_task_name_is_the_one_the_bundle_knows() -> None:
    assert judge_question.TASK == "question"
