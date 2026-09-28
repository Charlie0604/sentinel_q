"""仓储接口。

"能独立测试"的关键不在测试代码，而在**数据库访问是否可以替换**。
这个接口就是那道缝：`SupabaseRepo` 真连库，`FakeRepo` 纯内存。
于是绝大多数测试都能在不碰网络、不连数据库的情况下跑完（架构文档 7.5）。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from sentinel_q.shared.models import AnalysisResult, ContentRecord, PromptBundle


class Repo(Protocol):
    """数据读写接口。每个方法都对应架构文档里一条已确定的规则。"""

    # ── 查重：AI 调用前的成本闸门（3.9 第 3 条 / 4.1）──────────────────

    def existing_urls(self, urls: Sequence[str]) -> set[str]:
        """这一批 URL 里，哪些已经入库了。命中即跳过，不进 AI。

        按批查询，一批一次往返——不要为它做本地持久化台账（见 7.8）。
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

    # ── 作者维度 ────────────────────────────────────────────────────

    def ensure_author(
        self, zhihu_user_id: str, nickname: str | None, profile_url: str | None
    ) -> int:
        """按知乎用户 ID upsert 一条 `dim_author`，返回 `author_id`。

        `insert ... on conflict (zhihu_user_id) do update set nickname = ...,
        profile_url = ..., last_seen_at = now() returning author_id`

        **必须是 upsert，不能是普通 insert**：同一个人在这批里发 50 条，
        每条都要拿到同一个 `author_id`。普通 insert 撞唯一约束会抛异常，
        `do nothing` 则不返回行、等于拿不到 ID。

        昵称和链接**每次覆盖**：`nickname` 会改，`profile_url` 是人工核实的入口，
        都取最新值。`first_seen_at` 不动——那是"第一次见"的存档。
        """
        ...

    def question_id_for(self, zhihu_qid: str) -> int | None:
        """按知乎问题 ID 查 `dim_question.question_id`（库里那个 bigint）。

        ⚠️ **纯查询，不是 `claim_question`。** 后者的语义是"先插后问"的原子抢占，
        有返回值才表示"该去问 AI 相关性了"。采集侧拿它换 ID 等于把抢占吃掉，
        那个问题就永远不会被送去判相关性，而且全程不报错（见 `from_document` 的说明）。
        """
        ...

    # ── 内容入库（3.9）──────────────────────────────────────────────

    def insert_content(
        self, record: ContentRecord, *, parent_id: str | None = None
    ) -> str | None:
        """写入一条内容，返回 `content_id`；URL 已存在则返回 None。

        **这是维度表同步的单一收口**：`record` 只带知乎侧的原始标识，
        `author_id` 由这里先 `ensure_author` 拿到、`question_id` 由这里查
        `dim_question` 翻译——调用方（主程序）不用管（决策 52）。

        `parent_id` 是唯一的例外，得由调用方给：它是 uuid，
        只能靠"同一批里父级先插完"来搭，而那个映射只有批次视角看得见
        （见 `storage/ingest.py` 的 `IdIndex`）。

        并发写入靠 `url` 唯一约束 + `on conflict do nothing` 静默挡掉，
        不能让重复写入报错中断整个任务。
        """
        ...

    def save_analysis(self, result: AnalysisResult) -> None:
        """写入 `fact_analysis`。**与 `insert_content` 同批调用**（决策 51）。

        顺序是 `insert_content` → 拿到 `content_id` → `save_analysis`：
        `fact_analysis.content_id` 有外键指向 `fact_content`，反了会炸。

        ⚠️ `insert_content` 返回 `None`（URL 重复）时**不要往下调它**——
        那一行的分析早就有了，硬写会撞外键。
        """
        ...

    # ── 提示词（7.9）──────────────────────────────────────────────

    def active_prompt_bundle(self) -> PromptBundle | None:
        """取当前生效的那一版提示词。任务开始时调用一次，整个任务内不变。"""
        ...

    def push_prompt_bundle(self, bundle: PromptBundle) -> None:
        """把本地工作区的提示词推上库（人工改完提示词后执行）。

        线上库是提示词的**权威副本**——它不进 git，所以库是唯一能找到它、
        也是唯一能追溯"这条判断当时用的哪版"的地方。
        """
        ...
