"""事件任务：一条内容 × 一个议题（架构文档 4.5）。

判"这条内容说的是不是**这一桩事**"，以及"相对**这个议题**是什么立场"。
落 `fact_content_event`。

⚠️ 议题是人工确认过的（"跟监测对象有关"这一层已经定了），所以这里判的是
**二级**相关性：说的是不是**这个议题所描述的那一桩事**。最容易判错的是
"同一个企业、另一桩事"——预筛按关键词捞，而同一企业的几桩事共用同一批词，
所以这个任务挡不住的话，证据表里就会混进一批答非所问的行。

⚠️ 这里有三处和别的任务不一样，各钉一条：

  1. **议题摘要进 system**（4.5：`dim_event.summary` 当 system prompt 的背景用），
     关键词进 user 当线索。混在一起的话，内容里的指令性文字看起来就像是议题描述。
  2. **`event_version` 原样回带**（4.5.4）——它记的是"这条判断当时基于哪一版"，
     不是"现在最新是哪一版"，所以这里绝不能顺手取成最新的。
  3. **`is_relevant` 为 false 时也要求给 stance**：模块 E 让它每次都输出，
     缺了就是不合约。这条判断不会被采纳，但字段不能少——宽松处理等于给
     "模型偷懒少输出一个字段"开了个口子。
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from sentinel_q.analyst import judge_event
from sentinel_q.analyst.client import LLMSchemaError
from sentinel_q.analyst.fake import FakeLLMClient, full_bundle
from sentinel_q.analyst.judge_event import EventBrief, PendingEvent
from sentinel_q.shared.models import ContentRecord, EventJudgment

SUMMARY = "某地一家公司被指在宣传物料中使用了未经授权的素材，双方各执一词。"
KEYWORDS = ("某某公司", "素材授权", "宣传物料")


def _record(**overrides: Any) -> ContentRecord:
    fields: dict[str, Any] = {
        "content_type": "answer",
        "zhihu_id": "answer-123",
        "url": "https://www.zhihu.com/answer/123",
        "title": "说两句",
    }
    fields.update(overrides)
    return ContentRecord(**fields)


def _event(**overrides: Any) -> EventBrief:
    fields: dict[str, Any] = {
        "event_id": "evt-7",
        "version": 3,
        "summary": SUMMARY,
        "keywords": KEYWORDS,
    }
    fields.update(overrides)
    return EventBrief(**fields)


def _pending(**overrides: Any) -> PendingEvent:
    fields: dict[str, Any] = {"record": _record(), "text": "正文内容", "event": _event()}
    fields.update(overrides)
    return PendingEvent(**fields)


def _reply(**overrides: Any) -> str:
    data: dict[str, Any] = {"is_relevant": True, "stance": "正向", "confidence": 0.7}
    data.update(overrides)
    return json.dumps(data, ensure_ascii=False)


def _judge(reply: str, **overrides: Any) -> EventJudgment:
    return judge_event.judge_one(_pending(**overrides), bundle=full_bundle(), client=FakeLLMClient(reply))


# ── 落地 ────────────────────────────────────────────────────────────


def test_the_judgment_is_keyed_by_content_and_event() -> None:
    judgment = _judge(_reply())

    assert (judgment.zhihu_id, judgment.event_id) == ("answer-123", "evt-7")
    assert (judgment.is_relevant, judgment.stance, judgment.confidence) == (True, "正向", 0.7)


def test_every_stance_in_the_event_enum_is_accepted() -> None:
    """⚠️ 这套枚举和任务 A 的立场**不是一套**：那边是"对监测对象有利/抹黑"，
    这边是"对该议题正向/反向"。同一份提示词里出现两套词，别把它们混用。"""
    for stance in judge_event.STANCES:
        assert _judge(_reply(stance=stance)).stance == stance


def test_a_stance_from_the_other_task_is_refused() -> None:
    """'有利' 是任务 A 的词，拿到这里就是枚举越界——**不替它翻译**。"""
    with pytest.raises(LLMSchemaError):
        _judge(_reply(stance="有利"))


def test_the_event_version_is_carried_back_verbatim() -> None:
    """4.5.4：它记的是"这条判断当时基于哪一版"，不是"现在最新是哪一版"。"""
    judgment = _judge(_reply(), event=_event(version=1))

    assert judgment.event_version == 1


def test_stance_is_required_even_when_the_content_is_irrelevant() -> None:
    """模块 E 让它每次都输出。宽松处理等于给"模型偷懒少输出一个字段"开了个口子。"""
    with pytest.raises(LLMSchemaError) as excinfo:
        _judge(json.dumps({"is_relevant": False}, ensure_ascii=False))

    assert "stance" in str(excinfo.value)


def test_an_irrelevant_content_still_needs_a_stance_it_just_will_not_be_kept() -> None:
    """判 false 的那条将来**不写 `fact_content_event`**：那张表没有 `is_relevant` 列，
    写了就等于把"其实不相关"记成"相关且中立"，而这一行是要当证据用的。"""
    judgment = _judge(_reply(is_relevant=False, stance="中立"))

    assert judgment.is_relevant is False


# ── 拼装：摘要在 system，关键词在 user ──────────────────────────────


def test_the_event_summary_goes_into_the_system_prompt() -> None:
    bundle = full_bundle()
    client = FakeLLMClient(_reply())

    judge_event.judge_one(_pending(), bundle=bundle, client=client)

    system, user = client.calls[0]
    assert system.startswith(bundle.assemble("event"))
    assert "<event_tasks>" in system
    assert "<c_tasks>" not in system  # 一个 bundle 服务三个任务的关键断言
    assert SUMMARY in system
    assert "【本次要判定的议题】" in system
    assert SUMMARY not in user


def test_the_event_name_reaches_the_model() -> None:
    """`dim_event.name` 是人工命名时被要求带时间的那一栏（"XX事件-2026年3月"），
    比散文摘要更便宜也更强的一个锚点。"""
    client = FakeLLMClient(_reply())

    judge_event.judge_one(
        _pending(event=_event(name="某公司素材风波-2026年3月")), bundle=full_bundle(), client=client
    )

    system, user = client.calls[0]
    assert "某公司素材风波-2026年3月" in system
    assert "某公司素材风波-2026年3月" not in user


def test_a_missing_name_leaves_no_empty_heading() -> None:
    """和关键词那条同一个道理：空标题看起来像漏了一段。"""
    client = FakeLLMClient(_reply())

    judge_event.judge_one(_pending(event=_event(name="  ")), bundle=full_bundle(), client=client)

    assert "【本次要判定的议题】\n\n" not in client.calls[0][0]
    assert client.calls[0][0].count("【本次要判定的议题】") == 1


# ── 时间范围 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("start", "end", "rendered"),
    [
        (date(2026, 9, 20), date(2026, 10, 5), "2026-09-20 ~ 2026-10-05"),
        (date(2026, 9, 20), None, "2026-09-20 起（仍在持续）"),
        (None, date(2026, 10, 5), "截至 2026-10-05"),
    ],
)
def test_the_window_is_rendered_for_every_shape_of_range(
    start: date | None, end: date | None, rendered: str
) -> None:
    client = FakeLLMClient(_reply())

    judge_event.judge_one(
        _pending(event=_event(start_date=start, end_date=end)), bundle=full_bundle(), client=client
    )

    system = client.calls[0][0]
    assert "【议题的时间范围】" in system
    assert rendered in system
    assert rendered not in client.calls[0][1]


def test_no_dates_means_no_window_section_at_all() -> None:
    client = FakeLLMClient(_reply())

    judge_event.judge_one(_pending(), bundle=full_bundle(), client=client)

    assert "【议题的时间范围】" not in client.calls[0][0]


def test_the_window_is_hedged_so_early_content_is_not_thrown_away() -> None:
    """⭐ 4.5.3 特意为"事件被正式认定之前的零星讨论／预兆性内容"留了 7 天缓冲，
    那一批是**相关**内容。只甩一个日期区间不加这句，模型会把它们当"时间对不上"
    挡掉——正好挡掉设计明说要收的那一批。
    """
    client = FakeLLMClient(_reply())

    judge_event.judge_one(
        _pending(event=_event(start_date=date(2026, 9, 20))), bundle=full_bundle(), client=client
    )

    assert "不要只按日期卡" in client.calls[0][0]


def test_every_keyword_reaches_the_model() -> None:
    client = FakeLLMClient(_reply())

    judge_event.judge_one(_pending(), bundle=full_bundle(), client=client)

    system, user = client.calls[0]
    for keyword in KEYWORDS:
        assert keyword in system
    # ⚠️ user 里**只有**被审的那条内容，议题元数据一个字都不进去
    assert "【正文】\n正文内容" in user
    for keyword in KEYWORDS:
        assert keyword not in user


def test_the_keywords_are_flagged_as_leads_only() -> None:
    """⚠️ 关键词是 `pg_trgm` 粗筛用的，命中不等于相关。

    不写清楚的话，模型会把它当成判据——而 4.5.2 的预筛阈值本来就要标得松，
    松阈值下"命中了但其实是说别的事"的内容会很多。
    """
    client = FakeLLMClient(_reply())

    judge_event.judge_one(_pending(), bundle=full_bundle(), client=client)

    assert "线索" in client.calls[0][0]


def test_an_event_without_keywords_omits_that_block_entirely() -> None:
    """没有关键词时别留一个空的【关键词】标题，那看起来像漏了一段。"""
    client = FakeLLMClient(_reply())

    judge_event.judge_one(_pending(event=_event(keywords=())), bundle=full_bundle(), client=client)

    assert "【这个议题的关键词】" not in client.calls[0][0]


def test_a_blank_summary_is_refused_before_calling_the_model() -> None:
    """⭐ 摘要就是这个任务的全部背景。没有它，模型只会按关键词字面猜。"""
    client = FakeLLMClient(_reply())

    with pytest.raises(ValueError) as excinfo:
        judge_event.judge_one(
            _pending(event=_event(summary="   ")), bundle=full_bundle(), client=client
        )

    assert "evt-7" in str(excinfo.value)
    assert client.calls == []


def test_a_blank_body_is_refused_before_calling_the_model() -> None:
    client = FakeLLMClient(_reply())

    with pytest.raises(ValueError):
        judge_event.judge_one(_pending(text="  "), bundle=full_bundle(), client=client)

    assert client.calls == []


# ── 版本戳 ──────────────────────────────────────────────────────────


def test_the_judgment_carries_the_versions() -> None:
    bundle = full_bundle(content_hash="hash-e")

    judgment = judge_event.judge_one(
        _pending(), bundle=bundle, client=FakeLLMClient(_reply(), model="m-e")
    )

    assert (judgment.model_version, judgment.prompt_version) == ("m-e", "hash-e")


# ── 批量 ────────────────────────────────────────────────────────────


def test_a_batch_is_labelled_by_content_times_event() -> None:
    """一个议题下的失败日志如果只写内容 ID，看不出是哪次比对出的问题。"""
    pendings = [
        _pending(record=_record(zhihu_id=f"a{i}"), event=_event(event_id=f"evt-{i}"))
        for i in range(3)
    ]

    report = judge_event.judge_many(
        pendings, bundle=full_bundle(), client=FakeLLMClient(_reply()), concurrency=3
    )

    assert report.ok
    assert report.done == 3


def test_a_batch_names_the_content_and_the_event_that_failed() -> None:
    class _PickyClient:
        model = "fake-model"

        def complete(self, *, system: str, user: str) -> str:
            return "不是 JSON" if "会失败的那条" in user else _reply()

    pendings = [
        _pending(record=_record(zhihu_id="a0"), text="正常的一条"),
        _pending(record=_record(zhihu_id="a1"), text="会失败的那条"),
    ]

    report = judge_event.judge_many(
        pendings, bundle=full_bundle(), client=_PickyClient(), concurrency=2
    )

    assert report.done == 1
    assert report.failed[0][0] == "a1×evt-7"


def test_the_task_name_is_the_one_the_bundle_knows() -> None:
    assert judge_event.TASK == "event"
