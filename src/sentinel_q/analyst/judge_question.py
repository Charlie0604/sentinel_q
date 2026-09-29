"""任务 B：问题判定（架构文档 3.5）。

一个问题 → 是否与监测对象相关。**只有这一项判断**，落 `dim_question.is_relevant`。

## ⚠️ 不判立场

3.5 的对照表里任务 B 的输出栏只有"是否相关"——不问立场、不问风险、不出摘要。
所以 `QuestionJudgment` 上根本没有那几个字段：模型就算多返回了也落不下来，
不会顺着某条看不见的路渗进库里。这不是"随手少写几个字段"，
是决策 31 那条原则的落点："提示词是怎么问，表结构是存什么"。

## ⚠️ 喂的是**问题本身**，不是它底下的某篇回答

输入只有标题 + 描述。这是 3.5 写明的已知取舍：知乎问题标题常常很泛
（"如何看待最近的 XX 事件？"），单看标题可能判不出相关性 → 会漏检。
当前规模下靠人工补录兜底，所以 `question_rubric` 里那句"拿不准倾向判相关"
不是随手写的——判错的代价是多重爬一轮回答，判漏的代价是整条线断掉。

## 它为什么重要

`dim_question.is_relevant` 是个**流程开关**，不只是分析结果（5.5.4）：
判为相关的问题会进【更新问题列表】，第 3 步据此展开它下面的**全部**回答
（不区分相关性，决策 38 的硬前提）。判错一个，下游是一整棵子树。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sentinel_q.analyst import batch
from sentinel_q.analyst.client import LLMClient, parse_reply, require_bool
from sentinel_q.shared.models import PromptBundle, QuestionJudgment

log = logging.getLogger(__name__)

TASK = "question"


@dataclass(frozen=True)
class PendingQuestion:
    """一个待判的问题：知乎问题 ID + 标题 + 描述。

    ⚠️ 键是 `zhihu_qid` 而不是标题或描述的哈希（4.1）：**标题和描述被编辑后
    ID 也不变**。拿标题做键的话，原作者改一次描述，同一个问题就会被当成新问题
    重新问一遍 AI——而这一行的意义恰恰是当"问过就别再问"的台账。
    """

    zhihu_qid: str
    title: str
    description: str | None = None


def build_messages(pending: PendingQuestion, bundle: PromptBundle) -> tuple[str, str]:
    return bundle.assemble(TASK), _user_message(pending)


def _user_message(pending: PendingQuestion) -> str:
    parts = [f"【问题标题】{pending.title.strip()}"]
    description = (pending.description or "").strip()
    if description:
        parts.append(f"【问题描述】{description}")
    else:
        # 说清楚"没有描述"和"描述是空的"是一回事，免得模型以为下面漏了一段
        parts.append("【问题描述】（这个问题的描述是空的，只能按标题判断）")
    return "\n".join(parts)


def judge_one(
    pending: PendingQuestion, *, bundle: PromptBundle, client: LLMClient
) -> QuestionJudgment:
    if not pending.title or not pending.title.strip():
        raise ValueError(
            f"{pending.zhihu_qid} 的标题是空的，判不了相关性——"
            "标题是这个任务唯一的输入，空标题下模型给的任何答案都是编的。"
        )

    system, user = build_messages(pending, bundle)
    data = parse_reply(client.complete(system=system, user=user))

    # ⚠️ 这里**只读 is_relevant 一个键**。模型多返回了立场/风险，就让它
    #    飘在那里——`QuestionJudgment` 上没有那些字段，这是刻意的（见模块开头）。
    return QuestionJudgment(
        zhihu_qid=pending.zhihu_qid,
        is_relevant=require_bool(data, "is_relevant"),
        model_version=client.model,
        prompt_version=bundle.content_hash,
    )


def judge_many(
    pendings: Iterable[PendingQuestion],
    *,
    bundle: PromptBundle,
    client: LLMClient,
    concurrency: int,
    on_result: Callable[[PendingQuestion, QuestionJudgment], None] | None = None,
) -> batch.BatchReport:
    return batch.judge_many(
        pendings,
        lambda pending: judge_one(pending, bundle=bundle, client=client),
        label=lambda pending: pending.zhihu_qid,
        concurrency=concurrency,
        on_result=on_result,
    )
