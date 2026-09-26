"""仓储接口。

"能独立测试"的关键不在测试代码，而在**数据库访问是否可以替换**。
这个接口就是那道缝：`SupabaseRepo` 真连库，`FakeRepo` 纯内存。
于是绝大多数测试都能在不碰网络、不连数据库的情况下跑完（架构文档 8.5）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from core.models import AnalysisResult, ContentRecord, CrawlQueueItem, PromptBundle


class Repo(Protocol):
    """数据读写接口。每个方法都对应架构文档里一条已确定的规则。"""

    # ── 查重：AI 调用前的成本闸门（3.9 第 3 条 / 4.1）──────────────────

    def existing_urls(self, urls: Sequence[str]) -> set[str]:
        """这一批 URL 里，哪些已经入库了。命中即跳过，不进 AI。

        按批查询，一批一次往返——不要为它做本地持久化台账（见 8.8）。
        """
        ...

    def existing_question_ids(self, zhihu_qids: Sequence[str]) -> set[str]:
        """这一批知乎问题 ID 里，哪些已经在 `dim_question` 里了。

        ⚠️ 查问题是按 `zhihu_qid` 而不是标题——问题改了描述 ID 也不变（4.1）。
        """
        ...

    # ── 问题：先插后问的原子抢占（4.1）───────────────────────────────

    def claim_question(self, zhihu_qid: str, url: str | None, title: str | None) -> int | None:
        """`insert ... on conflict do nothing returning`。

        有返回行 = 本进程是第一个到达者，才提交 AI 调用；
        无返回行 = 别人已经问过了（或已经在问），直接跳过。
        `is_relevant` 留 null 表示"已抢占，待判断"。
        """
        ...

    def set_question_relevance(self, question_id: int, is_relevant: bool) -> None:
        """写入问题判定结果，同时填 `relevant_checked_at`。"""
        ...

    def mark_follow_up_done(self, question_id: int) -> bool:
        """条件更新，返回 True 才触发"该问题下全量回答采集"（4.2）。

        `update ... where question_id = $1 and follow_up_done = false returning`
        ——并发下多个线程可能同时发现"这个问题刚变相关"，
        不加条件会把整个问题的回答**重新爬两遍**。
        """
        ...

    # ── 内容入库（3.9）──────────────────────────────────────────────

    def insert_content(self, record: ContentRecord) -> str | None:
        """写入一条内容，返回 `content_id`；URL 已存在则返回 None。

        并发写入靠 `url` 唯一约束 + `on conflict do nothing` 静默挡掉，
        不能让重复写入报错中断整个任务。
        """
        ...

    def save_analysis(self, result: AnalysisResult) -> None:
        """写入 `fact_analysis`。AI 结果一返回就调用（4.2）——不要等整批处理完。"""
        ...

    # ── 采集队列（8.8）────────────────────────────────────────────

    def claim_next_url(self, worker: str) -> CrawlQueueItem | None:
        """抢一条待采集 URL，队列空则返回 None。

        用 `for update skip locked`：别人正在处理的行直接跳过而非排队等待，
        所以 2~4 个采集进程不会互相卡住，也不需要任何外部锁服务。
        """
        ...

    def finish_url(self, url: str, status: str, error: str | None = None) -> None:
        """标记一条 URL 采集完成 / 失败（status: done / failed / skipped）。"""
        ...

    # ── 提示词（8.9）──────────────────────────────────────────────

    def active_prompt_bundle(self) -> PromptBundle | None:
        """取当前生效的那一版提示词。任务开始时调用一次，整个任务内不变。"""
        ...

    def push_prompt_bundle(self, bundle: PromptBundle) -> None:
        """把本地工作区的提示词推上库（人工改完提示词后执行）。

        线上库是提示词的**权威副本**——它不进 git，所以库是唯一能找到它、
        也是唯一能追溯"这条判断当时用的哪版"的地方。
        """
        ...
