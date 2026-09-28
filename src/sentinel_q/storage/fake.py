"""纯内存仓储实现，供单元测试使用。

无网络、无数据库、毫秒级。它要**忠实复现**几条容易写错的语义，
否则测试通过不代表生产正确：

  - `claim_question` 的原子抢占：第二次调用必须返回 None
  - `mark_follow_up_done` 的条件更新：第二次必须返回 False
  - `insert_content` 的 url 唯一约束：重复写入返回 None，而不是抛异常

这几条都是并发下的正确性所在（分别见 4.1 / 4.2 / 3.9）。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sentinel_q.shared.models import (
    AnalysisResult,
    ContentRecord,
    PromptBundle,
)


class FakeRepo:
    """内存实现。所有状态都在实例上，测试之间互不干扰。"""

    def __init__(self) -> None:
        self.contents: dict[str, ContentRecord] = {}  # url -> record（对应 url 唯一约束）
        self.analyses: dict[str, AnalysisResult] = {}  # content_id -> result
        self._content_ids: dict[str, str] = {}  # content_id -> url（插入顺序即 created_at 顺序）
        self.resolved: dict[str, dict[str, object]] = {}  # content_id -> 落库时翻译出的主键
        self._question_ids: dict[str, int] = {}  # zhihu_qid -> question_id
        self._relevance: dict[int, bool] = {}
        self._follow_up_done: set[int] = set()
        self._next_question_id = 1
        self.authors: dict[str, tuple[str | None, str | None]] = {}  # 用户ID -> (昵称, 主页)
        self._author_ids: dict[str, int] = {}  # zhihu_user_id -> author_id
        self._next_author_id = 1
        self._active_prompt: PromptBundle | None = None

    # ── 查重 ────────────────────────────────────────────────────────

    def existing_urls(self, urls: Sequence[str]) -> set[str]:
        return {url for url in urls if url in self.contents}

    def existing_question_ids(self, zhihu_qids: Sequence[str]) -> set[str]:
        return {qid for qid in zhihu_qids if qid in self._question_ids}

    # ── 问题：先插后问 ──────────────────────────────────────────────

    def claim_question(self, zhihu_qid: str, url: str | None, title: str | None) -> int | None:
        if zhihu_qid in self._question_ids:
            return None  # 别人已经问过了（或正在问）——本进程不是第一个到达者
        question_id = self._next_question_id
        self._next_question_id += 1
        self._question_ids[zhihu_qid] = question_id
        # is_relevant 留空 = "已抢占，待判断"
        return question_id

    def set_question_relevance(self, question_id: int, is_relevant: bool) -> None:
        self._relevance[question_id] = is_relevant

    def mark_follow_up_done(self, question_id: int) -> bool:
        if question_id in self._follow_up_done:
            return False  # 条件更新未命中：别人已经触发过全量采集了
        self._follow_up_done.add(question_id)
        return True

    def question_relevance(self, question_id: int) -> bool | None:
        """仅供测试断言用。"""
        return self._relevance.get(question_id)

    # ── 作者维度 ────────────────────────────────────────────────────

    def ensure_author(
        self, zhihu_user_id: str, nickname: str | None, profile_url: str | None
    ) -> int:
        author_id = self._author_ids.get(zhihu_user_id)
        if author_id is None:
            author_id = self._next_author_id
            self._next_author_id += 1
            self._author_ids[zhihu_user_id] = author_id
        # 重复调用返回**同一个** ID，但昵称/链接取最新值（对应 upsert）
        self.authors[zhihu_user_id] = (nickname, profile_url)
        return author_id

    def question_id_for(self, zhihu_qid: str) -> int | None:
        return self._question_ids.get(zhihu_qid)

    # ── 内容入库 ────────────────────────────────────────────────────

    def insert_content(
        self, record: ContentRecord, *, parent_id: str | None = None
    ) -> str | None:
        """维度表同步在这里收口（对应 `SupabaseRepo` 的同名方法）。"""
        if record.url in self.contents:
            return None  # url 唯一约束静默挡掉重复
        content_id = str(uuid.uuid4())
        self.contents[record.url] = record
        self._content_ids[content_id] = record.url
        # 落库那一刻翻译出来的三个主键，供测试断言
        self.resolved[content_id] = {
            "author_id": (
                self.ensure_author(
                    record.author_zhihu_id, record.author_name, record.author_url
                )
                if record.author_zhihu_id
                else None
            ),
            "question_id": (
                self.question_id_for(record.question_zhihu_id)
                if record.question_zhihu_id
                else None
            ),
            "parent_id": parent_id,
        }
        return content_id

    def content_id_for(self, url: str) -> str | None:
        """仅供测试断言用。"""
        for content_id, known_url in self._content_ids.items():
            if known_url == url:
                return content_id
        return None

    def save_analysis(self, result: AnalysisResult) -> None:
        self.analyses[result.content_id] = result

    # ── 提示词 ──────────────────────────────────────────────────────

    def active_prompt_bundle(self) -> PromptBundle | None:
        return self._active_prompt

    def push_prompt_bundle(self, bundle: PromptBundle) -> None:
        self._active_prompt = bundle
