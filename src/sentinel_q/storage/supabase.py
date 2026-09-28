"""Supabase 上的真实实现。

⚠️ **用 psycopg 直连 Postgres，不走 supabase-py / PostgREST**。理由：
批量查重的 `= any(%s)`、`claim_question` 的 `on conflict do nothing returning`、
`mark_follow_up_done` 的条件更新，都是原生 SQL 表达起来更直接的东西；
PostgREST 要绕一圈还不一定等价。

⚠️ **这个文件里不该出现爬虫的运维状态**（任务断点、URL 清单、限流采样）——
那些是本地文件（架构文档 3.8 / 7.8），爬虫本地运行，为它建表等于每条 URL
一次网络往返。这里只放业务数据和提示词。

⚠️ 免费版必须用 Supabase 的连接池端口（6543），不要用直连端口（5432）：
直连端口在免费版**只有 IPv6**（IPv4 是付费附加项），普通网络下根本连不上。
另一个后果是事务模式不支持预编译语句，所以 `psycopg.connect` 要带
`prepare_threshold=None`（见 `__init__`）。
每个进程一个长连接即可，不要每查询一次新建。

⚠️ 本文件尚未实测——Supabase 项目建好后，用 tests/ 下带
`@pytest.mark.integration` 的用例跑一遍再算数。
"""

from __future__ import annotations

from collections.abc import Sequence

from sentinel_q.shared.models import (
    AnalysisResult,
    ContentRecord,
    PromptBundle,
)

# 键与 ContentRecord 的字段一一对应，外加上调用方翻译好的三个库里主键
# （author_id / question_id / parent_id）——见 insert_content。
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

_QUESTION_ID = "select question_id from dim_question where zhihu_qid = %s"

# 作者维度 upsert。**必须 returning**：do nothing 在昵称变更时也不返回行，
# 那就拿不到 author_id 了。
_ENSURE_AUTHOR = """
insert into dim_author (zhihu_user_id, nickname, profile_url)
values (%s, %s, %s)
on conflict (zhihu_user_id) do update set
    nickname     = excluded.nickname,
    profile_url  = excluded.profile_url,
    last_seen_at = now()
returning author_id
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
    """实现 `storage.repo.Repo` 协议。"""

    def __init__(self, dsn: str) -> None:
        import psycopg  # 延迟导入：单元测试不需要装驱动
        from psycopg.rows import dict_row

        # prepare_threshold=None 是 6543 端口（连接池事务模式）的硬要求：
        # pgbouncer 在事务模式下不保证同一连接的语句落在同一后端，
        # 预编译语句会失效。表现是跑到第 5 次同一条语句时才炸，很难查。
        self._conn = psycopg.connect(
            dsn, row_factory=dict_row, prepare_threshold=None
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SupabaseRepo:
        return self

    def __exit__(self, *exc: object) -> bool:
        """连着的那个长连接**必须显式关**。

        `psycopg.connect` 不是 `with` 就能自动收的（`self._conn` 是实例属性，
        不是 with 里的那个对象），所以这里手动关。少了它，命令行跑完
        连接会挂到进程退出才断——而连接池端口 6543 的连接数是有限的。
        """
        self.close()
        return False

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

    # ── 作者维度 ────────────────────────────────────────────────────

    def ensure_author(
        self, zhihu_user_id: str, nickname: str | None, profile_url: str | None
    ) -> int:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_ENSURE_AUTHOR, (zhihu_user_id, nickname, profile_url))
            return cur.fetchone()["author_id"]

    def question_id_for(self, zhihu_qid: str) -> int | None:
        with self._conn.cursor() as cur:
            cur.execute(_QUESTION_ID, (zhihu_qid,))
            row = cur.fetchone()
            return row["question_id"] if row else None

    # ── 内容入库 ────────────────────────────────────────────────────

    def insert_content(
        self, record: ContentRecord, *, parent_id: str | None = None
    ) -> str | None:
        """维度表同步在这里收口：记录只带知乎侧标识，主键在这一层翻译。"""
        params = dict(vars(record))
        params["parent_id"] = parent_id

        params["author_id"] = (
            self.ensure_author(record.author_zhihu_id, record.author_name, record.author_url)
            if record.author_zhihu_id
            else None
        )
        params["question_id"] = (
            self.question_id_for(record.question_zhihu_id) if record.question_zhihu_id else None
        )

        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_INSERT_CONTENT, params)
            row = cur.fetchone()
            return str(row["content_id"]) if row else None

    def save_analysis(self, result: AnalysisResult) -> None:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_SAVE_ANALYSIS, vars(result))

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
        from psycopg.types.json import Jsonb

        # 同一个事务里"先下线旧的、再上线新的"——避免出现两个 active
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute("update dim_prompt set is_active = false where is_active")
            cur.execute(
                """
                insert into dim_prompt (version, content_hash, content, note, is_active)
                values (%s, %s, %s, %s, true)
                """,
                # ⚠️ 必须包 Jsonb：psycopg 不会自动把 dict 适配到 jsonb 列，
                #    直接传会抛 cannot adapt type 'dict'
                (
                    bundle.version,
                    bundle.content_hash,
                    Jsonb(bundle.modules),
                    bundle.note,
                ),
            )
