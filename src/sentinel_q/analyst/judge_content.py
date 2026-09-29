"""任务 A：正文判定（架构文档 3.5 / 4.4）。

一条回答 / 文章 / 想法 → 是否相关 + 立场 + 风险等级 + 摘要。落 `fact_analysis`。

## ⚠️ 正文由调用方传进来，这个模块不读文件

长内容走 `storage_path`、`content_text` 为空（决策 32 提到的那个真实缺陷：
事件分类只查 `content_text` 会漏掉全部长文章）。把正文从哪儿取出来——
是 `record.content_text` 还是 `storage_path` 指向的那个文件——是主程序的事。
`analyst` 不碰磁盘，所以这里收的是已经取好的 `text`。

## ⚠️ 空正文当场抛，不拿空串去问模型

对着一段空白问"这条内容对监测对象的立场是什么"，模型**一定会给一个答案**——
而那个答案是凭空编的，还会带着 `prompt_version` 一起落进 `fact_analysis`，
事后看不出它是编的。所以 `text` 全空白时抛 `ValueError`：调用方得先决定
"这条到底有没有东西可判"，而不是让模型替它决定。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from sentinel_q.analyst import batch
from sentinel_q.analyst.client import (
    LLMClient,
    optional_float,
    optional_str,
    parse_reply,
    require_bool,
    require_choice,
)
from sentinel_q.shared.models import ContentJudgment, ContentRecord, PromptBundle

log = logging.getLogger(__name__)

TASK = "content"
"""`PROMPT_MODULES` 里的任务名，也是 `PromptBundle.assemble()` 的参数。"""

STANCES = ("有利", "抹黑", "中立", "不相关")
"""4.4 模块 E 的枚举，也是 `fact_analysis.platform_stance` 那个 check 约束的取值。

⚠️ `不相关` 是一个**正经的立场取值**，不是"没判"：判为不相关的内容若其所属问题
相关，仍要入库并照实填 `不相关`（决策 28 的情况 4）。"""

RISKS = ("低风险", "中风险", "高风险")


@dataclass(frozen=True)
class PendingContent:
    """一条待判的内容：记录 + 它的正文。

    正文不塞进 `ContentRecord` 里：那个类是采集侧的产物，只有知乎侧的元数据，
    正文可能躺在磁盘上（`storage_path`）。两件事分开，谁也不用假装知道对方。
    """

    record: ContentRecord
    text: str


def build_messages(pending: PendingContent, bundle: PromptBundle) -> tuple[str, str]:
    """拼出 (system, user)。

    七模块进 system（4.4 明说"拼装成最终 system prompt"），
    **被审的那条内容进 user**——它是数据，不是指令。这个分界也是防提示词注入的
    第一道：内容里的"忽略以上规则"落在 user 里，和 system 里的 G 模块天然隔开。
    """
    return bundle.assemble(TASK), _user_message(pending)


def _user_message(pending: PendingContent) -> str:
    record = pending.record
    parts = []
    if record.title and record.title.strip():
        parts.append(f"【标题】{record.title.strip()}")
    if record.content_type:
        parts.append(f"【类型】{record.content_type}")
    parts.append("【正文】")
    parts.append(pending.text.strip())
    return "\n".join(parts)


def judge_one(
    pending: PendingContent, *, bundle: PromptBundle, client: LLMClient
) -> ContentJudgment:
    """判一条。**不吞异常**——失败要让 `batch.judge_many` 记进账里。"""
    if not pending.text or not pending.text.strip():
        raise ValueError(
            f"{pending.record.zhihu_id} 的正文是空的，不能拿去问模型——"
            "模型会对着一片空白编出一个立场，而那条判断事后看不出是编的。"
            "调用方先决定这条有没有东西可判。"
        )

    system, user = build_messages(pending, bundle)
    data = parse_reply(client.complete(system=system, user=user))

    # ⚠️ `is_relevant` / `platform_stance` / `risk_level` 是**必填**：模块 E 让模型
    #    每次都输出它们，缺了就是答复不合约，不能替它补一个默认值。
    #    摘要和置信度、判断依据是选填——4.4 的 E 里它们没有"必填"的语义，
    #    缺了不影响这条判断能不能用。
    return ContentJudgment(
        zhihu_id=pending.record.zhihu_id,
        is_relevant=require_bool(data, "is_relevant"),
        ai_summary=optional_str(data, "ai_summary"),
        platform_stance=require_choice(data, "platform_stance", STANCES),
        stance_confidence=optional_float(data, "stance_confidence"),
        risk_level=require_choice(data, "risk_level", RISKS),
        risk_reasoning=optional_str(data, "risk_reasoning"),
        model_version=client.model,
        prompt_version=bundle.content_hash,
    )


def judge_many(
    pendings: Iterable[PendingContent],
    *,
    bundle: PromptBundle,
    client: LLMClient,
    concurrency: int,
    on_result: Callable[[PendingContent, ContentJudgment], None] | None = None,
) -> batch.BatchReport:
    """并发判一批（4.2）。一次请求还是一条内容，并发的是调用。"""
    return batch.judge_many(
        pendings,
        lambda pending: judge_one(pending, bundle=bundle, client=client),
        label=lambda pending: pending.record.zhihu_id,
        concurrency=concurrency,
        on_result=on_result,
    )
