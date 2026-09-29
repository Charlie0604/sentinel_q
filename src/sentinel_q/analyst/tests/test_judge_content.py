"""任务 A：正文判定（架构文档 3.5 / 4.4）。

一条回答 / 文章 / 想法 → 是否相关 + 立场 + 风险等级 + 摘要。落 `fact_analysis`。

⚠️ 三件容易被写松的事，这里各钉一条：

  1. **空正文当场抛**，不拿空串去问模型（模型一定会编一个立场出来，还带着
     `prompt_version` 落进库，事后看不出是编的）
  2. **system 是 bundle 拼的七模块，正文进 user**——被审的内容是数据，不是指令
  3. `ContentJudgment` 上用 `zhihu_id` 回指内容，**不是** `content_id`
     （那是 `fact_content` 的主键，AI 模块拿不到，见决策 52）
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from sentinel_q.analyst import judge_content
from sentinel_q.analyst.client import LLMReplyError, LLMSchemaError
from sentinel_q.analyst.fake import FakeLLMClient, full_bundle
from sentinel_q.analyst.judge_content import PendingContent, judge_many, judge_one
from sentinel_q.shared.models import ContentJudgment, ContentRecord


def _record(**overrides: Any) -> ContentRecord:
    fields: dict[str, Any] = {
        "content_type": "answer",
        "zhihu_id": "answer-123",
        "url": "https://www.zhihu.com/answer/123",
        "title": None,
    }
    fields.update(overrides)
    return ContentRecord(**fields)


def _reply(**overrides: Any) -> str:
    data: dict[str, Any] = {
        "is_relevant": True,
        "ai_summary": "作者认为这件事被夸大了",
        "platform_stance": "中立",
        "stance_confidence": 0.8,
        "risk_level": "低风险",
        "risk_reasoning": "没有煽动性表述",
    }
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def _judge(reply: str, **record_overrides: Any) -> ContentJudgment:
    pending = PendingContent(record=_record(**record_overrides), text="正文内容")
    return judge_one(pending, bundle=full_bundle(), client=FakeLLMClient(reply))


# ── 落地 ────────────────────────────────────────────────────────────


def test_every_field_lands_on_the_judgment() -> None:
    judgment = _judge(_reply())

    assert judgment.zhihu_id == "answer-123"
    assert judgment.is_relevant is True
    assert judgment.ai_summary == "作者认为这件事被夸大了"
    assert judgment.platform_stance == "中立"
    assert judgment.stance_confidence == 0.8
    assert judgment.risk_level == "低风险"
    assert judgment.risk_reasoning == "没有煽动性表述"


def test_model_and_prompt_version_are_stamped_on_every_judgment() -> None:
    """决策 47：没有这两个，事后分不清哪条判断是哪版提示词问出来的。"""
    bundle = full_bundle(content_hash="hash-abc")
    pending = PendingContent(record=_record(), text="正文内容")

    judgment = judge_one(pending, bundle=bundle, client=FakeLLMClient(_reply(), model="m-1"))

    assert judgment.model_version == "m-1"
    assert judgment.prompt_version == "hash-abc"


def test_stance_can_be_irrelevant_without_being_a_missing_value() -> None:
    """`不相关` 是个正经的立场取值（决策 28 的情况 4），不是"没判"。"""
    judgment = _judge(_reply(is_relevant=False, platform_stance="不相关"))

    assert (judgment.is_relevant, judgment.platform_stance) == (False, "不相关")


def test_optional_fields_may_be_absent() -> None:
    reply = json.dumps({"is_relevant": True, "platform_stance": "中立", "risk_level": "中风险"})

    judgment = _judge(reply)

    assert (judgment.ai_summary, judgment.stance_confidence, judgment.risk_reasoning) == (
        None,
        None,
        None,
    )


# ── 拼装：system 是提示词，正文进 user ──────────────────────────────


def test_the_content_goes_into_the_user_message_not_the_system_prompt() -> None:
    """⚠️ 分界也是防注入的第一道：正文里的"忽略以上规则"落在 user 里，和 G 模块隔开。"""
    bundle = full_bundle()
    pending = PendingContent(
        record=_record(title="这个标题", content_type="article"), text="正文内容"
    )
    client = FakeLLMClient(_reply())

    judge_one(pending, bundle=bundle, client=client)

    system, user = client.calls[0]
    assert system == bundle.assemble("content")
    assert "正文内容" not in system
    assert "【标题】这个标题" in user
    assert "【类型】article" in user
    assert "【正文】\n正文内容" in user


def test_a_blank_title_is_simply_left_out() -> None:
    """想法和评论没有标题，输出一个空的【标题】只会让模型以为上面漏了一段。"""
    client = FakeLLMClient(_reply())
    pending = PendingContent(record=_record(title="   "), text="正文内容")

    judge_one(pending, bundle=full_bundle(), client=client)

    assert "【标题】" not in client.calls[0][1]


# ── 空正文：当场抛，不烧一次配额 ────────────────────────────────────


@pytest.mark.parametrize("text", ["", "   ", "\n\n"])
def test_blank_text_is_refused_before_calling_the_model(text: str) -> None:
    """⭐ 对着空白问"立场是什么"，模型一定会给答案——而那个答案是凭空编的。"""
    client = FakeLLMClient(_reply())
    pending = PendingContent(record=_record(), text=text)

    with pytest.raises(ValueError) as excinfo:
        judge_one(pending, bundle=full_bundle(), client=client)

    assert "answer-123" in str(excinfo.value)
    assert client.calls == [], "空正文不该真的发一次请求"


# ── 不合约：抛，绝不兜一个默认判断出来 ──────────────────────────────


def test_a_missing_required_field_raises_instead_of_defaulting() -> None:
    """⭐ 缺 `risk_level` 时如果兜成"中风险"，库里就多了一条谁都认不出的假判断。"""
    reply = json.dumps({"is_relevant": True, "platform_stance": "中立"})
    client = FakeLLMClient(reply)

    with pytest.raises(LLMSchemaError) as excinfo:
        judge_one(PendingContent(record=_record(), text="正文"), bundle=full_bundle(), client=client)

    assert "risk_level" in str(excinfo.value)
    assert len(client.calls) == 1, "该问还是得问，只是答复不合约"


def test_an_empty_object_raises_six_ways_to_sunday() -> None:
    with pytest.raises(LLMSchemaError):
        _judge("{}")


def test_a_stance_outside_the_enum_raises() -> None:
    """模型爱把"支持"当成"有利"，但那是两个词，替它翻译等于替它判断。"""
    with pytest.raises(LLMSchemaError):
        _judge(_reply(platform_stance="支持"))


def test_a_risk_level_outside_the_enum_raises() -> None:
    with pytest.raises(LLMSchemaError):
        _judge(_reply(risk_level="极高风险"))


def test_a_non_boolean_relevance_raises() -> None:
    with pytest.raises(LLMSchemaError):
        _judge(_reply(is_relevant="true"))


def test_an_out_of_range_confidence_raises() -> None:
    with pytest.raises(LLMSchemaError):
        _judge(_reply(stance_confidence=1.5))


def test_a_reply_that_is_not_json_raises_a_reply_error() -> None:
    """格式脏和内容不合约是两件事，异常类型也分开——调用方处理方式不一样。"""
    with pytest.raises(LLMReplyError):
        _judge("我觉得这条内容是中立的。")


def test_a_fenced_reply_still_works_end_to_end() -> None:
    judgment = _judge(f"```json\n{_reply()}\n```")

    assert judgment.risk_level == "低风险"


# ── 回指与翻译 ──────────────────────────────────────────────────────


def test_to_analysis_result_fills_in_the_database_key() -> None:
    """AI 模块不知道 `fact_content` 的主键，所以它是**落库那一刻**才补上的。"""
    judgment = _judge(_reply())

    result = judgment.to_analysis_result("0f9c1e2a-uuid")

    assert result.content_id == "0f9c1e2a-uuid"
    assert result.analyzed_by == "ai"
    assert result.ai_summary == judgment.ai_summary
    assert result.platform_stance == judgment.platform_stance
    assert result.risk_level == judgment.risk_level


def test_to_analysis_result_keeps_the_traceability_fields() -> None:
    """决策 47：翻译这一步要是把版本号丢了，追溯就断在这儿了。"""
    bundle = full_bundle(content_hash="hash-abc")
    pending = PendingContent(record=_record(), text="正文内容")
    judgment = judge_one(pending, bundle=bundle, client=FakeLLMClient(_reply(), model="m-1"))

    result = judgment.to_analysis_result("uuid")

    assert (result.model_version, result.prompt_version) == ("m-1", "hash-abc")


# ── 批量 ────────────────────────────────────────────────────────────


def test_a_batch_reports_the_failing_zhihu_id() -> None:
    """标识用的是知乎侧的 ID——AI 模块不知道库里的 uuid，只能拿这个指认。

    ⚠️ 用一个"看正文决定答什么"的客户端，而不是 `FakeLLMClient` 的预设列表：
    列表是**按到达顺序**发的，并发下哪条拿到哪个答复根本说不准，
    那样写出来的测试会偶尔红一次——最难查的那种。
    """

    class _PickyClient:
        model = "fake-model"

        def complete(self, *, system: str, user: str) -> str:
            return "这段不是 JSON" if "会失败的那条" in user else _reply()

    pendings = [
        PendingContent(record=_record(zhihu_id="a0"), text="正常的一条"),
        PendingContent(record=_record(zhihu_id="a1"), text="会失败的那条"),
        PendingContent(record=_record(zhihu_id="a2"), text="正常的另一条"),
    ]

    report = judge_many(pendings, bundle=full_bundle(), client=_PickyClient(), concurrency=3)

    assert report.done == 2
    assert [label for label, _ in report.failed] == ["a1"]
    assert "LLMReplyError" in report.failed[0][1]


def test_a_batch_delivers_every_judgment_through_the_callback() -> None:
    """决策 39：回调里写盘，所以每条都必须经过它一次。"""
    pendings = [PendingContent(record=_record(zhihu_id=f"a{i}"), text="正文") for i in range(5)]
    landed: dict[str, Any] = {}

    report = judge_many(
        pendings,
        bundle=full_bundle(),
        client=FakeLLMClient(_reply()),
        concurrency=4,
        on_result=lambda pending, judgment: landed.__setitem__(judgment.zhihu_id, judgment),
    )

    assert report.ok
    assert sorted(landed) == ["a0", "a1", "a2", "a3", "a4"]
    assert all(judgment.risk_level == "低风险" for judgment in landed.values())


def test_the_task_name_is_the_one_the_bundle_knows() -> None:
    assert judge_content.TASK == "content"
