"""事件任务：一条内容 × 一个议题（架构文档 4.5）。

判"这条内容说的是不是**这一桩事**"，以及"相对**这个议题**是什么立场"。
落 `fact_content_event`。

## ⚠️ 议题是人工确认过的，所以这是一层**二级**相关性

`dim_event` 里的行是人建的：建它的时候就定过"这事跟监测对象有关"。所以
"这条内容跟监测对象有没有关系"**不是这个任务的题目**——那是任务 A／B 的题目。
这里要判的是更窄的一件事：

    **这条内容说的，是不是这个议题所描述的那一桩事。**

**最容易判错的一类：同一个监测对象、另一桩事。** 监测对象会反复出事，议题表里
可能同时躺着它好几桩事；而 4.5.2 的预筛是**按关键词**做的，同一个企业的几桩事
**共用同一批词**（企业名、产品词、"标注""核查""配料表"）。于是预筛在这一类上
几乎不起作用——**这个函数是唯一那道闸门**。一条"企业对了、事由错了"的内容
判成相关，就会以"关于这件事"的样子进证据表，而且此后没人会再看第二遍。

所以提示词里那条相关性标准是**要正面证据**的：内容的事由和时间对得上摘要才算相关。

## ⚠️ 预筛不在这里

关键词命中是**数据库查询**（4.5.2 的 `pg_trgm`），按 7.3 的硬规则它属于数据库模块。
这个模块拿到的是**已经筛好的候选集**——它不需要知道怎么筛的，更不需要连库。

## ⚠️ 两层筛的关系：预筛是粗筛，AI 是精筛

4.5.2 原本说"是否属于该议题"由预筛决定、AI 只判立场。实际让 AI 也判一次，
于是粗筛命中但 AI 判 `is_relevant = false` 的内容**不写 `fact_content_event`**
——那张表没有 `is_relevant` 列，写了就等于把"其实不相关"记成"相关且中立"，
而这一行将来是要当证据用的。

反过来，AI 判相关但预筛没命中的内容，这个任务**根本看不到**。所以预筛的阈值
（`set_limit()`）标定得松一点比紧一点安全，§8 待办里那条"pg_trgm 阈值标定"
说的就是这个。

## ⚠️ 立场是相对**议题**的，不是相对监测对象的

`fact_content_event.stance` 的注释写着"该内容针对【该议题】的立场"。同一个议题下
支持不同方的两条内容，立场是相反的。监测对象说明只用来理解背景。

## `event_version` 必须原样带回去（4.5.4）

它对应 `dim_event.version`。事件摘要被实质性改写时人工把 version 加一，
系统据此在前端标出"该事件下有 N 条判断基于旧版本"。比对逻辑就是
`fact_content_event.event_version != dim_event.version`——所以这个数字
**不能在这里"顺手"取成最新的**，它记的是"这条判断当时基于哪一版"。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date

from sentinel_q.analyst import batch
from sentinel_q.analyst.client import (
    LLMClient,
    optional_float,
    parse_reply,
    require_bool,
    require_choice,
)
from sentinel_q.shared.models import ContentRecord, EventJudgment, PromptBundle

log = logging.getLogger(__name__)

TASK = "event"

STANCES = ("正向", "反向", "中立")
"""`fact_content_event.stance` 的 check 约束取值。**和任务 A 的立场枚举不是一套**：
那边是"对监测对象有利/抹黑"，这边是"对该议题正向/反向"。"""


@dataclass(frozen=True)
class EventBrief:
    """本次要判的那个议题。

    `event_id` 是个**不透明的字符串**——AI 模块不认识它，只负责原样回带
    （落库时由 storage 换成 `dim_event.event_id`）。之所以不在这里放 bigint，
    是因为"库里那个主键长什么样"是数据库模块的事。
    """

    event_id: str
    version: int  # dim_event.version，回带进 event_version
    summary: str
    """`dim_event.summary`：200~300 字背景，会被当 system prompt 传。

    ⚠️ 它和 `name`／时间范围一起，是**唯一**能把这桩事和同一企业的其他事区分开的
    东西（关键词共用、区分不了）。所以 DDL 里那句"包含关键时间节点和各方核心主张"
    不是修辞：摘要里没写清哪款产品、什么由头、什么时间，模型就只能按关键词字面猜，
    而提示词教它的做法是**判不相关**。写摘要的人等于在决定这个议题收不收得进内容。
    """

    name: str = ""
    """`dim_event.name`：人工命名的议题名，DDL 给的例子是"XX事件-2026年3月"。

    ⚠️ **它比摘要便宜也更强**：摘要是一段人写的散文，可能忘了写时间；名字是
    人工命名时被要求带时间的那一栏。同一家企业有几桩事在跑的时候，这一行
    往往就是模型分辨"是不是这桩"的第一个锚点。

    默认为空只是为了让 `EventBrief` 好构造；主程序**应该**填。
    """

    start_date: date | None = None
    """`dim_event.start_date`。⚠️ 见 `end_date` 那条。"""

    end_date: date | None = None
    """`dim_event.end_date`；留空 = 仍在持续。

    ⚠️ **这两个字段在补录议题上是刚需。** 4.5.3 的时间窗口只对 `event_type='new'`
    生效；`backfill`（补录的历史议题）是**全库扫、没有时间窗口**的——那正是
    "同一企业、另一桩事"最容易混进来的场景，而模型手上如果连个日期锚都没有，
    就只能靠摘要的字面描述猜。

    没传时背景块里不会出现【议题的时间范围】那一节（不留空标题，同 `keywords`）。
    """

    keywords: tuple[str, ...] = ()


@dataclass(frozen=True)
class PendingEvent:
    """一条待判的内容 × 一个议题。正文同样由调用方取好。"""

    record: ContentRecord
    text: str
    event: EventBrief


def build_messages(pending: PendingEvent, bundle: PromptBundle) -> tuple[str, str]:
    """拼出 (system, user)。

    ⚠️ **议题背景（名称 + 摘要 + 时间范围 + 关键词）全进 system**，
    user 里**只有被审的那条内容**。

    4.5 明说 `dim_event.summary` 会被当作 system prompt 传给 AI 做背景上下文。
    名称、时间范围、关键词跟着摘要一起走：它们都是"在跟什么比"，
    而 user 里那条是"被审的东西"。这么切的好处是 user 消息的形状和任务 A 完全一致
    ——里面永远只有知乎上抓来的一段不可信文本。**事件元数据混进 user，
    内容里的一句"忽略以上规则"看起来就像是议题描述的一部分了。**
    """
    system = bundle.assemble(TASK) + "\n\n" + _background(pending.event)
    return system, _user_message(pending)


def _window(event: EventBrief) -> str | None:
    """把起止日期渲染成一句人话。两端都空时返回 None（调用方据此整节省略）。

    ⚠️ 用 ISO 日期（`2026-09-20`）而不是"2026 年 9 月 20 日"：模型两种都认，
    但 ISO 不会被误读成"9 月 20 日之后的 2026 年"，和摘要里可能出现的
    "9 月 26 日"这类写法也不会混成一段。
    """
    start, end = event.start_date, event.end_date
    if start and end:
        return f"{start.isoformat()} ~ {end.isoformat()}"
    if start:
        return f"{start.isoformat()} 起（仍在持续）"
    if end:
        return f"截至 {end.isoformat()}"
    return None


def _background(event: EventBrief) -> str:
    """议题背景块：名称 + 摘要 + 时间范围 + 关键词。整块拼进 system。

    ⚠️ 这里只**陈述事实**，"对不上就是另一桩事"那类**判断指令**不写在这儿——
    它们住在 `event_rubric.txt` 里。两边都写一遍的话，改一处就会和另一处对不上，
    而模型看到的永远是两份措辞略有出入的规则。
    """
    parts = ["【本次要判定的议题】"]
    if event.name.strip():
        parts.append(event.name.strip())
    parts.append(event.summary.strip())

    window = _window(event)
    if window:
        parts.append("【议题的时间范围】")
        parts.append(window)
        # ⚠️ 这句话必须有。范围是"这桩事本身"的时间，而 4.5.3 特意为"事件被正式
        #    认定之前的零星讨论／预兆性内容"留了 7 天缓冲——那是**相关**内容。
        #    只甩一个日期区间不加这句，模型会把它们当"时间对不上"挡掉，
        #    正好挡掉 4.5.3 明说要收的那一批。
        parts.append(
            "⚠️ 这是这桩事本身的时间。紧挨着范围前后（尤其是之前）的内容也可能是"
            "相关讨论，不要只按日期卡。"
        )

    if event.keywords:
        parts.append("【这个议题的关键词】")
        parts.append("、".join(event.keywords))
        # ⚠️ 不写这句，模型会拿关键词当判据。而 4.5.2 的预筛阈值本来就要标得松，
        #    松阈值下"命中了但其实在说别的事"的内容会很多——那正是要 AI 再筛一遍的原因。
        parts.append(
            "⚠️ 关键词只是把候选集缩小用的线索，命中不等于相关、没命中也不代表无关。"
            "以上面那段摘要为准。"
        )
    return "\n".join(parts)


def _user_message(pending: PendingEvent) -> str:
    record = pending.record
    parts = []
    if record.title and record.title.strip():
        parts.append(f"【标题】{record.title.strip()}")
    parts.append("【正文】")
    parts.append(pending.text.strip())
    return "\n".join(parts)


def judge_one(
    pending: PendingEvent, *, bundle: PromptBundle, client: LLMClient
) -> EventJudgment:
    if not pending.text or not pending.text.strip():
        raise ValueError(
            f"{pending.record.zhihu_id} 的正文是空的，判不了议题归属；"
            "空正文下模型给的立场是编的，而这一行是要当证据用的。"
        )
    if not pending.event.summary or not pending.event.summary.strip():
        raise ValueError(
            f"议题 {pending.event.event_id} 的摘要是空的，判不了归属——"
            "摘要就是这个任务的全部背景，没有它模型只会按关键词字面猜。"
        )

    system, user = build_messages(pending, bundle)
    data = parse_reply(client.complete(system=system, user=user))

    return EventJudgment(
        zhihu_id=pending.record.zhihu_id,
        event_id=pending.event.event_id,
        event_version=pending.event.version,
        is_relevant=require_bool(data, "is_relevant"),
        # ⚠️ 即使 is_relevant 为 false 也要求给 stance：模块 E 让它每次都给，
        #    缺了就是答复不合约。这条判断不会被采纳，但字段不能少——
        #    宽松处理等于给"模型偷懒少输出一个字段"开了个口子。
        stance=require_choice(data, "stance", STANCES),
        confidence=optional_float(data, "confidence"),
        model_version=client.model,
        prompt_version=bundle.content_hash,
    )


def judge_many(
    pendings: Iterable[PendingEvent],
    *,
    bundle: PromptBundle,
    client: LLMClient,
    concurrency: int,
    on_result: Callable[[PendingEvent, EventJudgment], None] | None = None,
) -> batch.BatchReport:
    return batch.judge_many(
        pendings,
        lambda pending: judge_one(pending, bundle=bundle, client=client),
        label=lambda pending: f"{pending.record.zhihu_id}×{pending.event.event_id}",
        concurrency=concurrency,
        on_result=on_result,
    )
