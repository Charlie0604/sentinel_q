"""议题判定落库：把「一条内容 × 一个议题」的判断写进 `fact_content_event`。

这是 `repo.py` 开头那份名单里的 `events.py`——需要调多个原语、还要在 Python
侧对一批行做判断，所以是**组合函数**，收一个 `repo` 参数，与
`insert_contents(records, *, repo)` 同形。

## 它**只写 `is_relevant is True` 的那些**

`fact_content_event` 上**没有 `is_relevant` 这一列**（5.4 的建表语句）。写一条
"其实不相关"进去，就等于把它记成"相关且中立"——那是凭空造出来的判断，
而且事后看不出来。所以判为不相关的**只计数、不落库**（见 `EventJudgment`
的 docstring：那是它与 4.5.2 的一处有意分歧）。

## 版本号取库里的，不取判定里带的

4.5.4 的 `event_version` 是"这条判断基于议题摘要的哪一版"。判定结构里带了一个，
但**权威值是 `dim_event.version`**：议题摘要是 system prompt 的一部分，
判定的那一刻摘要是什么版本，只有库答得出来。

⚠️ 两处不一致时**拒写**，不"顺手改成库里的值"。改成库里的值是把一条基于旧摘要
的判断伪装成基于新摘要——而 4.5.4 的整套"该议题下有 N 条判断基于旧版本"就是靠
这一列工作的，伪装之后那个数字永远是 0，等于没有。拒写会留下一笔
`version_mismatch`，那才是可查的。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from sentinel_q.shared.models import EventJudgment

log = logging.getLogger(__name__)

STANCES: frozenset[str] = frozenset({"正向", "反向", "中立"})
"""`fact_content_event.stance` 的三个合法值（库上有 check 约束）。

⚠️ 与 `dim_author.stance` 的取值**不是同一套**：那个是对平台的长期立场，
这个是针对**某一个议题**的（5.2 说得很明确，两者是两个独立维度）。
别把这里扩成四个值去迁就那边。
"""


@dataclass
class EventStanceReport:
    """一批议题判定的落库结果。

    这几个计数器和 `IngestReport` 是同一个立场：**每一项都对应一种
    "跑完了但其实缺东西"的状态**——命令正常退出、日志一片干净，
    而库里少了一批关联。
    """

    saved: int = 0

    skipped_irrelevant: int = 0
    """AI 判为"不属于该议题"。**正常，不是错误**——同一家企业、另一桩事
    就是靠这一档挡掉的（4.5.2 的预筛区分不了它，AI 是唯一那道闸门）。"""

    orphaned: int = 0
    """判为相关，但它挂的那条内容**被内容闸门丢了**，没有 `content_id` 可挂。

    ⚠️ 这是**正确**的丢弃（内容都不在库里，关联无从谈起），但必须报出来：
    它意味着 `saved + orphaned + skipped_irrelevant` 才是判定的总数，
    少报的话账上会悄悄缺一块，而缺的那块**永远查不出来**。"""

    unknown_event: int = 0
    """`event_id` 在 `dim_event` 里找不到——外键会炸，所以挡在这里。"""

    version_mismatch: int = 0
    """判定基于的议题版本与库里的当前版本对不上。见模块开头。"""

    bad_stance: int = 0
    """`stance` 不是 `正向 / 反向 / 中立`。库上的 check 约束会拒，挡在这里报人话。"""

    def describe(self) -> str:
        text = f"议题判定入库 {self.saved} 条"
        if self.skipped_irrelevant:
            text += f"，判为不相关 {self.skipped_irrelevant} 条（不落库）"
        if self.orphaned:
            text += f"；⚠️ {self.orphaned} 条判定相关、但它挂的内容没入库"
        if self.version_mismatch:
            text += f"；⚠️ {self.version_mismatch} 条基于旧版摘要，已拒写（该重判）"
        if self.unknown_event:
            text += f"；⚠️ {self.unknown_event} 条的议题不在库里"
        if self.bad_stance:
            text += f"；⚠️ {self.bad_stance} 条的立场取值不合法"
        return text

    @property
    def total(self) -> int:
        """这批一共看了多少条判定。**每一档都要有归宿**，见 `orphaned`。"""
        return (
            self.saved
            + self.skipped_irrelevant
            + self.orphaned
            + self.unknown_event
            + self.version_mismatch
            + self.bad_stance
        )


def save_judged_events(
    judgments: Iterable[tuple[str, EventJudgment]],
    *,
    repo,
    content_ids: Mapping[str, str],
) -> EventStanceReport:
    """把「内容 × 议题」的判定写进 `fact_content_event`。

    `judgments` 里每项是 `(url, 判定)`——用 `url` 而不是 `zhihu_id` 回指内容，
    理由和 `insert_judged` 一样：`fact_content.zhihu_id` 上没有唯一约束。

    `content_ids` 是 `JudgedReport.content_ids`，也就是**这次真插进去的**
    `url → content_id`。用它而不是查库有两个好处：不用给 `Repo` 加一个按 url
    反查的接口（那是 `FakeRepo` 的测试专用方法，决策 52 的单点收口），
    而且"没插进去的那些"会**自然落进 `orphaned`**——那正是要报的那个数。
    """
    report = EventStanceReport()

    for url, judgment in judgments:
        if not judgment.is_relevant:
            report.skipped_irrelevant += 1
            continue

        content_id = content_ids.get(url)
        if content_id is None:
            report.orphaned += 1
            log.warning("这条判定相关，但它挂的内容没入库，关联无处可挂：%s", url)
            continue

        try:
            event_id = int(judgment.event_id)
        except (TypeError, ValueError):
            report.unknown_event += 1
            log.warning("议题标识不是整数（%r），跳过：%s", judgment.event_id, url)
            continue

        event = repo.event_by_id(event_id)
        if event is None:
            report.unknown_event += 1
            log.warning("库里没有议题 %s，跳过：%s", event_id, url)
            continue

        # ⚠️ 比的是**判定当时**那一版与库里当前版。不一致就拒写，
        #    不改成 event.version（那会把"基于旧版"伪装成"基于新版"）。
        if judgment.event_version != event.version:
            report.version_mismatch += 1
            log.warning(
                "议题 %s 已经是第 %s 版，这条判定基于第 %s 版，拒写（该重判）：%s",
                event_id,
                event.version,
                judgment.event_version,
                url,
            )
            continue

        if judgment.stance not in STANCES:
            report.bad_stance += 1
            log.warning("立场取值 %r 不合法，跳过：%s", judgment.stance, url)
            continue

        repo.save_content_event(
            content_id,
            event_id,
            stance=judgment.stance,
            # AI 写的就标 ai。人工复核走 overwrite_event_stance，
            # 那边由实现层强制写 human（和 apply_human_analysis 同一条规矩）。
            analyzed_by="ai",
            event_version=event.version,
            confidence=judgment.confidence,
            prompt_version=judgment.prompt_version,
        )
        report.saved += 1

    return report
