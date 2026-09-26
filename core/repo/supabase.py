"""Supabase 上的真实实现。

⚠️ **用 psycopg 直连 Postgres，不走 supabase-py / PostgREST**。理由：
`claim_next_url` 的 `for update skip locked` 是原生 SQL 特性，
PostgREST 表达不了；而它正是 2~4 个采集进程能并行抢任务的全部依据（8.8）。
顺带，批量查重的 `= any(%s)` 在原生 SQL 里也更自然。

⚠️ 免费版有连接数限制，应用侧应使用 Supabase 的连接池端口（6543），
不要用直连端口。每个进程一个长连接即可，不要每查询一次新建。

⚠️ 本文件尚未实测——Supabase 项目建好后，用 tests/ 下带
`@pytest.mark.integration` 的用例跑一遍再算数。
"""

from __future__ import annotations

from collections.abc import Sequence

from core.models import AnalysisResult, ContentRecord, CrawlQueueItem, PromptBundle

# 列顺序与 ContentRecord 的字段一一对应
_INSERT_CONTENT = """
insert into fact_content (
    content_type, zhihu_id, url, author_id, question_id, parent_id,
    title, content_text, storage_path, voteup_count, comment_count,
    content_length, raw_content_hash, snapshot_path, html_snapshot_path,
    screenshot_path, published_at
) values (
    %(content_type)s, %(zhihu_id)s, %(url)s, %(author_id)s, %(question_id)s, %(parent_id)s,
    %(title)s, %(content_text)s, %(storage_path)s, %(voteup_count)s, %(comment_count)s,
    %(content_length)s, %(raw_content_hash)s, %(snapshot_path)s, %(html_snapshot_path)s,
    %(screenshot_path)s, %(published_at)s
)
on conflict (url) do nothing
returning content_id
"""

# 「先插后问」的原子抢占（4.1）：有返回行才是第一个到达者
_CLAIM_QUESTION = """
insert into dim_question (zhihu_qid, url, title, is_relevant)
values (%s, %s, %s, null)
on conflict (zhihu_qid) do nothing
returning question_id
"""

# 条件更新（4.2）：有返回行才触发全量回答采集
_MARK_FOLLOW_UP = """
update dim_question set follow_up_done = true
where question_id = %s and follow_up_done = false
returning question_id
"""

# `skip locked` 是关键：别人正在处理的行直接跳过而非排队等待（8.8）
_CLAIM_NEXT_URL = """
update crawl_queue
set status = 'claimed', claimed_by = %s, claimed_at = now(), attempts = attempts + 1
where url = (
    select url from crawl_queue
    where status = 'pending'
    order by priority desc, created_at
    for update skip locked
    limit 1
)
returning url, task_id, question_id, content_type, priority, attempts
"""

_SAVE_ANALYSIS = """
insert into fact_analysis (
    content_id, ai_summary, platform_stance, stance_confidence,
    risk_level, risk_reasoning, analyzed_by, model_version, prompt_version
) values (
    %(content_id)s, %(ai_summary)s, %(platform_stance)s, %(stance_confidence)s,
    %(risk_level)s, %(risk_reasoning)s, %(analyzed_by)s, %(model_version)s, %(prompt_version)s
)
on conflict (content_id) do update set
    ai_summary        = excluded.ai_summary,
    platform_stance   = excluded.platform_stance,
    stance_confidence = excluded.stance_confidence,
    risk_level        = excluded.risk_level,
    risk_reasoning    = excluded.risk_reasoning,
    analyzed_by       = excluded.analyzed_by,
    model_version     = excluded.model_version,
    prompt_version    = excluded.prompt_version,
    analyzed_at       = now()
"""


class SupabaseRepo:
    """实现 `core.repo.base.Repo` 协议。"""

    def __init__(self, dsn: str) -> None:
        import psycopg  # 延迟导入：单元测试不需要装驱动
        from psycopg.rows import dict_row

        self._conn = psycopg.connect(dsn, row_factory=dict_row)

    def close(self) -> None:
        self._conn.close()

    # ── 查重 ────────────────────────────────────────────────────────

    def existing_urls(self, urls: Sequence[str]) -> set[str]:
        with self._conn.cursor() as cur:
            cur.execute("select url from fact_content where url = any(%s)", (list(urls),))
            return {row["url"] for row in cur.fetchall()}

    def existing_question_ids(self, zhihu_qids: Sequence[str]) -> set[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "select zhihu_qid from dim_question where zhihu_qid = any(%s)",
                (list(zhihu_qids),),
            )
            return {row["zhihu_qid"] for row in cur.fetchall()}

    # ── 问题 ────────────────────────────────────────────────────────

    def claim_question(self, zhihu_qid: str, url: str | None, title: str | None) -> int | None:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_CLAIM_QUESTION, (zhihu_qid, url, title))
            row = cur.fetchone()
            return row["question_id"] if row else None

    def set_question_relevance(self, question_id: int, is_relevant: bool) -> None:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                """
                update dim_question
                set is_relevant = %s, relevant_checked_at = now()
                where question_id = %s
                """,
                (is_relevant, question_id),
            )

    def mark_follow_up_done(self, question_id: int) -> bool:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_MARK_FOLLOW_UP, (question_id,))
            return cur.fetchone() is not None

    # ── 内容入库 ────────────────────────────────────────────────────

    def insert_content(self, record: ContentRecord) -> str | None:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_INSERT_CONTENT, vars(record))
            row = cur.fetchone()
            return str(row["content_id"]) if row else None

    def save_analysis(self, result: AnalysisResult) -> None:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_SAVE_ANALYSIS, vars(result))

    # ── 采集队列 ────────────────────────────────────────────────────

    def claim_next_url(self, worker: str) -> CrawlQueueItem | None:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_CLAIM_NEXT_URL, (worker,))
            row = cur.fetchone()
            if not row:
                return None
            return CrawlQueueItem(
                url=row["url"],
                task_id=row["task_id"],
                question_id=row["question_id"],
                content_type=row["content_type"],
                priority=row["priority"],
                attempts=row["attempts"],
            )

    def finish_url(self, url: str, status: str, error: str | None = None) -> None:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                """
                update crawl_queue
                set status = %s, last_error = %s, claimed_by = null
                where url = %s
                """,
                (status, error, url),
            )

    # ── 提示词 ──────────────────────────────────────────────────────

    def active_prompt_bundle(self) -> PromptBundle | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "select version, content_hash, content, note from dim_prompt where is_active limit 1"
            )
            row = cur.fetchone()
            if not row:
                return None
            return PromptBundle(
                version=row["version"],
                content_hash=row["content_hash"],
                modules=row["content"],
                note=row["note"],
            )

    def push_prompt_bundle(self, bundle: PromptBundle) -> None:
        # 同一个事务里"先下线旧的、再上线新的"——避免出现两个 active
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute("update dim_prompt set is_active = false where is_active")
            cur.execute(
                """
                insert into dim_prompt (version, content_hash, content, note, is_active)
                values (%s, %s, %s, %s, true)
                """,
                (bundle.version, bundle.content_hash, bundle.modules, bundle.note),
            )
