"""纯内存仓储实现，供单元测试使用。

无网络、无数据库、毫秒级。它要**忠实复现**几条容易写错的语义，
否则测试通过不代表生产正确：

  - `claim_question` 的原子抢占：第二次调用必须返回 None
  - `mark_follow_up_done` 的条件更新：第二次必须返回 False
  - `insert_content` 的 url 唯一约束：重复写入返回 None，而不是抛异常
  - `apply_human_analysis` 保留 model_version / prompt_version（决策 47）

这几条都是并发与审计上的正确性所在（分别见 4.1 / 4.2 / 3.9 / 5.4）。

## 诚实子集：`HONEST`

**本类只实现它能如实模拟的方法，其余的故意不实现**（调用处报 AttributeError）。
分界线不是"难不难写"，是**要不要把数据库的语义重写一遍**：

    单表筛选/排序/limit  +  按外键取回**一行**  =  诚实
    多行 join（扇出）、聚合（group by / count）、全文相似度  =  不诚实

后半类的实现写出来就是**第二个查询引擎**——在 Python 里手写一遍条件 join
和排序，与 SQL 有一百处可以不一致，而测试会一直绿。更糟的是它会让测试
**看起来**覆盖了那些查询，于是没人再去做真库验证。

不实现的方法一律**不写桩**：桩（`raise NotImplementedError`）会让人以为
"补一下就行"，而 AttributeError 在测试里是当场、无歧义的失败。

`storage/tests/test_fake_honesty.py` 用一个双向断言把这件事机械化：

    {Repo 协议的方法} − HONEST  ==  {FakeRepo 里不存在的方法}

也就是说，**给 FakeRepo 补一个 HONEST 之外的实现，或者给协议加了方法忘了
归置，测试都会红**——这正是它存在的意义：挡住"为了让某个用例通过，
顺手给 fake 补一个猜的实现"。

## 还有一条：写操作的失败方式要对齐 Postgres

**update 不命中任何行 = 静默无事发生，不是抛异常。** `update ... where id = $1`
在 id 不存在时受影响 0 行，SQL 不报错，所以
`set_question_relevance` / `mark_answers_collected` / `refresh_content_metrics` /
`mark_content_status` / `set_author_watch` 对不存在的 id 一律**静默返回**。

⚠️ 这条曾经反过：`mark_follow_up_done(7)` 对不存在的问题返回 `True`。
而 `True` 的语义是"**这次由我触发了**该问题下的全量回答采集"（决策 40），
凭空返回 True 会让调用方以为问题存在且没被采过。现在不存在的 id 返回 `False`。

**insert 撞约束 = 抛异常**（`insert_evidence` 对不存在的内容抛 `KeyError`，
对应外键）。"0 行受影响"和"约束拒绝"在 Postgres 里就是两种不同的结果，
fake 分开对待才是忠实的。

要给人话的错误提示（"这条内容不存在"），由组合函数先查一次再写——
那是 `review.py` / `evidence.py` 那一层的活，不是存储层的。

⚠️ **本类手里的时间是 `datetime.now()`（UTC），对应库里那些
`default now()` 的列**（`collected_at` / `first_seen_at` / `analyzed_at`）。
这不是为了"让时间窗口能测"而编的，它是那一列的真值 —— 但不代表
依赖它的查询（`alert_rows` / `count_by_risk`）就诚实了，那两个是聚合。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime

from sentinel_q.shared.models import (
    AnalysisResult,
    ContentRecord,
    PromptBundle,
)
from sentinel_q.storage.repo import (
    AnalysisPatch,
    AuthorRow,
    ContentRow,
    EventRow,
    EventStancePatch,
    EventStanceRow,
    EvidenceRow,
    QuestionRow,
)

# 本类如实实现了的方法。见模块开头的分界线。
# ⚠️ 这个集合与实现必须同步——`test_fake_honesty.py` 两个方向都会断言。
HONEST: frozenset[str] = frozenset(
    {
        # ── 查重 ──
        "existing_urls",
        "existing_question_ids",
        "all_urls",
        # ── 问题 ──
        "all_questions",
        "follow_up_questions",
        "question_by_id",
        "claim_question",
        "set_question_relevance",
        "mark_follow_up_done",
        "mark_answers_collected",
        # ── 作者 ──
        "ensure_author",
        "question_id_for",
        "author_by_id",
        "set_author_watch",
        # ── 内容 ──
        "insert_content",
        "save_analysis",
        "refresh_content_metrics",
        "mark_content_status",
        "content_by_id",
        # ── 复核 ──
        "analysis_for",
        "analysis_reviewed_by",
        "event_stances_for",
        "apply_human_analysis",
        "overwrite_event_stance",
        "save_content_event",
        # ── 议题：只有按 id 的增删改查；列表与统计是聚合，见下面 NOT_HONEST ──
        "create_event",
        "update_event",
        "event_by_id",
        # ── 证据 ──
        "insert_evidence",
        "evidence_for",
        # ── 提示词 ──
        "active_prompt_bundle",
        "push_prompt_bundle",
    }
)

# 协议里**故意不实现**的那些。单独列一份，是为了让"哪些没做、为什么"
# 在代码里看得见，而不是只能靠"翻遍全文件找不到"来发现。
NOT_HONEST: dict[str, str] = {
    "search_contents": "条件 join + 排序 + 分页",
    "count_contents": "与 search_contents 必须同口径，但那口径是拼出来的 SQL",
    "alert_rows": "三表 join（内容/作者/判断）",
    "count_by_risk": "group by 聚合",
    "list_events": "left join + group by + having",
    "count_stale_judgments": "跨表比对版本的 count",
    "prescreen_rows": "pg_trgm word_similarity，没有内存等价物",
}


@dataclass
class _AuthorState:
    """`dim_author` 的一行。非 frozen：`last_seen_at` 每次 `ensure_author` 都变。"""

    author_id: int
    zhihu_user_id: str
    nickname: str | None = None
    profile_url: str | None = None
    stance: str | None = None
    is_watched: bool = False
    watch_note: str | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


@dataclass
class _ContentMeta:
    """`fact_content` 上**不在 `ContentRecord` 里**的那几列。

    `ContentRecord` 是采集侧的产物，只带知乎侧标识（决策 52），所以
    `content_id` / `collected_at` / `status` 这三个库侧的东西不在里面。
    它们不是"重复的真相源"，是内容记录本来就没建模的列。
    """

    collected_at: datetime
    status: str = "active"


def _now() -> datetime:
    """对应库里那些 `default now()` 的列（timestamptz）。"""
    return datetime.now(UTC)


class FakeRepo:
    """内存实现。所有状态都在实例上，测试之间互不干扰。"""

    def __init__(self) -> None:
        self.contents: dict[str, ContentRecord] = {}  # url -> record（对应 url 唯一约束）
        self.analyses: dict[str, AnalysisResult] = {}  # content_id -> result
        self._content_ids: dict[str, str] = {}  # content_id -> url（插入顺序即 created_at 顺序）
        self._content_meta: dict[str, _ContentMeta] = {}  # content_id -> 库侧那几列
        self.resolved: dict[str, dict[str, object]] = {}  # content_id -> 落库时翻译出的主键
        self._reviewed_by: dict[str, str] = {}  # content_id -> 复核人

        self.questions: dict[int, QuestionRow] = {}  # question_id -> 行
        self._question_ids: dict[str, int] = {}  # zhihu_qid -> question_id
        self._next_question_id = 1

        self._author_rows: dict[int, _AuthorState] = {}  # author_id -> 行
        self._author_ids: dict[str, int] = {}  # zhihu_user_id -> author_id
        self._next_author_id = 1

        self._events: dict[int, EventRow] = {}  # event_id -> 行
        self._next_event_id = 1
        self._event_stances: dict[tuple[str, int], EventStanceRow] = {}

        self._evidence: dict[int, EvidenceRow] = {}  # evidence_id -> 行
        self._next_evidence_id = 1

        self._active_prompt: PromptBundle | None = None

    @property
    def authors(self) -> dict[str, tuple[str | None, str | None]]:
        """仅供测试断言用：`zhihu_user_id -> (昵称, 主页)`。

        是**派生视图**不是第二份存储——现存用例读的是这个形状。
        """
        return {
            row.zhihu_user_id: (row.nickname, row.profile_url)
            for row in self._author_rows.values()
        }

    # ── 查重 ────────────────────────────────────────────────────────

    def existing_urls(self, urls: Sequence[str]) -> set[str]:
        return {url for url in urls if url in self.contents}

    def existing_question_ids(self, zhihu_qids: Sequence[str]) -> set[str]:
        return {qid for qid in zhihu_qids if qid in self._question_ids}

    def all_urls(self) -> set[str]:
        return set(self.contents)

    # ── 问题：先插后问 ──────────────────────────────────────────────

    def claim_question(
        self,
        zhihu_qid: str,
        url: str | None,
        title: str | None,
        *,
        description: str | None = None,
        asked_at: datetime | None = None,
        follower_count: int | None = None,
        view_count: int | None = None,
        answer_count: int | None = None,
    ) -> int | None:
        if zhihu_qid in self._question_ids:
            return None  # 别人已经问过了（或正在问）——本进程不是第一个到达者
        question_id = self._next_question_id
        self._next_question_id += 1
        self._question_ids[zhihu_qid] = question_id
        # is_relevant 留空 = "已抢占，待判断"。
        # first_seen_at 在这里落值，对应 `dim_question.first_seen_at default now()`
        # ——它是【更新问题列表】的 `since` 判据，不是为测试方便造的。
        self.questions[question_id] = QuestionRow(
            question_id=question_id,
            zhihu_qid=zhihu_qid,
            url=url,
            title=title,
            description=description,
            asked_at=asked_at,
            follower_count=follower_count,
            view_count=view_count,
            answer_count=answer_count,
            first_seen_at=_now(),
        )
        return question_id

    def set_question_relevance(self, question_id: int, is_relevant: bool) -> None:
        row = self.questions.get(question_id)
        if row is None:
            return  # 0 行受影响——update 不命中任何行时不报错（见模块开头的规则）
        self.questions[question_id] = replace(
            row, is_relevant=is_relevant, relevant_checked_at=_now()
        )

    def mark_follow_up_done(self, question_id: int) -> bool:
        row = self.questions.get(question_id)
        if row is None or row.follow_up_done:
            # 条件更新未命中：要么问题不存在，要么别人已经触发过全量采集了。
            # ⚠️ 不存在的 id 必须返回 False 而不是抛异常——SQL 里那是一条
            #    `update ... where question_id = $1`，0 行受影响而已。
            #    True 的语义是"这次由我触发了采集"，凭空返回 True 会让调用方
            #    以为问题存在且没被采过。
            return False
        self.questions[question_id] = replace(row, follow_up_done=True)
        return True

    def mark_answers_collected(self, question_id: int) -> None:
        row = self.questions.get(question_id)
        if row is None:
            return  # 0 行受影响
        self.questions[question_id] = replace(row, answers_collected_at=_now())

    def all_questions(self) -> list[QuestionRow]:
        return list(self.questions.values())

    def follow_up_questions(self, since: datetime) -> list[QuestionRow]:
        return [
            row
            for row in self.questions.values()
            # ⚠️ `is True` 而不是真值判断：is_relevant 是**三态**，
            #    None = 已抢占待判，不能被当成"已判为相关"。
            if row.is_relevant is True
            and row.first_seen_at is not None
            and row.first_seen_at >= since
        ]

    def question_by_id(self, question_id: int) -> QuestionRow | None:
        return self.questions.get(question_id)

    def question_relevance(self, question_id: int) -> bool | None:
        """仅供测试断言用。"""
        return self.questions[question_id].is_relevant

    # ── 作者维度 ────────────────────────────────────────────────────

    def ensure_author(
        self, zhihu_user_id: str, nickname: str | None, profile_url: str | None
    ) -> int:
        author_id = self._author_ids.get(zhihu_user_id)
        if author_id is None:
            author_id = self._next_author_id
            self._next_author_id += 1
            self._author_ids[zhihu_user_id] = author_id
            self._author_rows[author_id] = _AuthorState(
                author_id=author_id,
                zhihu_user_id=zhihu_user_id,
                first_seen_at=_now(),  # first_seen 只落一次
            )
        row = self._author_rows[author_id]
        # 重复调用返回**同一个** ID，但昵称/链接取最新值（对应 upsert）
        row.nickname = nickname
        row.profile_url = profile_url
        row.last_seen_at = _now()
        return author_id

    def question_id_for(self, zhihu_qid: str) -> int | None:
        return self._question_ids.get(zhihu_qid)

    def author_by_id(self, author_id: int) -> AuthorRow | None:
        row = self._author_rows.get(author_id)
        if row is None:
            return None
        return AuthorRow(
            author_id=row.author_id,
            zhihu_user_id=row.zhihu_user_id,
            nickname=row.nickname,
            profile_url=row.profile_url,
            stance=row.stance,
            is_watched=row.is_watched,
            watch_note=row.watch_note,
            first_seen_at=row.first_seen_at,
            last_seen_at=row.last_seen_at,
        )

    def set_author_watch(
        self,
        zhihu_user_id: str,
        *,
        is_watched: bool | None = None,
        watch_note: str | None = None,
        stance: str | None = None,
    ) -> None:
        author_id = self._author_ids.get(zhihu_user_id)
        if author_id is None:
            # 0 行受影响：update 不命中时不报错。⚠️ 界面因此可能"保存成功"
            # 但什么都没发生——要报人话就得由组合函数先查一次 author，
            # 那是 review/作者维护那一层的活，不是存储层的。
            return
        row = self._author_rows[author_id]
        # 三个 `is not None` = coalesce：没给就不动，**不是**没给就清空。
        # 这三个字段的语义是 null = 未知（5.5.5），"误清掉"事后看不出来。
        if is_watched is not None:
            row.is_watched = is_watched
        if watch_note is not None:
            row.watch_note = watch_note
        if stance is not None:
            row.stance = stance

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
        self._content_meta[content_id] = _ContentMeta(collected_at=_now())
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

    # ── 内容维护 ────────────────────────────────────────────────────

    def refresh_content_metrics(
        self, content_id: str, *, voteup_count: int, comment_count: int
    ) -> None:
        url = self._content_ids.get(content_id)
        if url is None:
            return  # 0 行受影响
        # ⚠️ 原地重建 `contents` 里那条记录，**不另建 metrics 字典**——
        #    那样 fake 内部就有两个真相源，测出来的东西取决于读的是哪一个。
        #    只动这两个字段，正对应 SQL 里"刻意不碰 content_text /
        #    raw_content_hash"（更新正文会破坏决策 51）。
        self.contents[url] = replace(
            self.contents[url], voteup_count=voteup_count, comment_count=comment_count
        )

    def mark_content_status(self, content_id: str, status: str) -> None:
        meta = self._content_meta.get(content_id)
        if meta is None:
            return  # 0 行受影响
        meta.status = status

    def content_by_id(self, content_id: str) -> ContentRow | None:
        url = self._content_ids.get(content_id)
        if url is None:
            return None
        record = self.contents[url]
        meta = self._content_meta[content_id]
        # 字段名与 ContentRow 逐字对应（对应 supabase.py 的 `_row()`）。
        return ContentRow(
            content_id=content_id,
            content_type=record.content_type,
            status=meta.status,
            zhihu_id=record.zhihu_id,
            url=record.url,
            author_id=self.resolved[content_id]["author_id"],
            question_id=self.resolved[content_id]["question_id"],
            parent_id=self.resolved[content_id]["parent_id"],
            title=record.title,
            content_text=record.content_text,
            storage_path=record.storage_path,
            voteup_count=record.voteup_count,
            comment_count=record.comment_count,
            content_length=record.content_length,
            raw_content_hash=record.raw_content_hash,
            snapshot_path=record.snapshot_path,
            html_snapshot_path=record.html_snapshot_path,
            screenshot_path=record.screenshot_path,
            published_at=record.published_at,
            collected_at=meta.collected_at,
        )

    # ── 复核 ────────────────────────────────────────────────────────

    def analysis_for(self, content_id: str) -> AnalysisResult | None:
        return self.analyses.get(content_id)

    def analysis_reviewed_by(self, content_id: str) -> str | None:
        return self._reviewed_by.get(content_id)

    def event_stances_for(self, content_id: str) -> list[EventStanceRow]:
        rows = [
            row
            for (cid, _event_id), row in self._event_stances.items()
            if cid == content_id
        ]
        return sorted(rows, key=lambda row: row.event_id)

    def apply_human_analysis(
        self,
        content_id: str,
        patch: AnalysisPatch,
        *,
        reviewed_by: str | None = None,
    ) -> None:
        """人工改写判断（决策 5 / 47）。

        ⚠️ **`model_version` / `prompt_version` 必须留着**，这是这个方法与
        `save_analysis` 唯一的实质差别。`save_analysis` 是整行覆盖，人工复核
        走它会把"当时用的哪版提示词"抹成 NULL，而 5.4 明说不保留修改历史，
        抹了就永远查不回来。

        ⚠️ 但**这个实现测不出 `supabase.py` 的 SQL 对不对**——这里"保留"是
        天然成立的，那边的 SQL 得靠一个静态断言兜（`_APPLY_HUMAN_ANALYSIS`
        的 set 子句里不许出现 model_version / prompt_version）。
        """
        patch_fields = {
            name: value
            for name, value in vars(patch).items()
            if value is not None  # 没给的字段不动，对应 SQL 里的 coalesce
        }
        old = self.analyses.get(content_id)
        if old is None:
            # upsert 的 insert 分支：库里还没有判断行时，人工那份就是第一份
            new = AnalysisResult(
                content_id=content_id,
                analyzed_by="human",
                **patch_fields,
            )
        else:
            new = replace(old, analyzed_by="human", **patch_fields)
        self.analyses[content_id] = new
        if reviewed_by is not None:
            self._reviewed_by[content_id] = reviewed_by

    def save_content_event(
        self,
        content_id: str,
        event_id: int,
        *,
        stance: str,
        analyzed_by: str,
        event_version: int,
        confidence: float | None = None,
        prompt_version: str | None = None,
        notes: str | None = None,
    ) -> None:
        self._event_stances[(content_id, event_id)] = EventStanceRow(
            event_id=event_id,
            event_name=self._event_name(event_id),
            stance=stance,
            confidence=confidence,
            analyzed_by=analyzed_by,
            event_version=event_version,
            prompt_version=prompt_version,
            notes=notes,
            analyzed_at=_now(),
        )

    def overwrite_event_stance(
        self, content_id: str, event_id: int, patch: EventStancePatch
    ) -> None:
        """人工改写议题立场。upsert 不是 update（见 repo.py 的说明）。"""
        key = (content_id, event_id)
        old = self._event_stances.get(key)
        event = self._events.get(event_id)
        # 新挂的这条要取 dim_event 的当前版本，否则一出生就是"基于旧版本"
        version = event.version if event else None
        if old is None:
            self._event_stances[key] = EventStanceRow(
                event_id=event_id,
                event_name=self._event_name(event_id),
                stance=patch.stance,
                confidence=patch.confidence,
                notes=patch.notes,
                analyzed_by="human",
                event_version=version,
                analyzed_at=_now(),
            )
            return
        self._event_stances[key] = replace(
            old,
            stance=patch.stance if patch.stance is not None else old.stance,
            confidence=patch.confidence if patch.confidence is not None else old.confidence,
            notes=patch.notes if patch.notes is not None else old.notes,
            analyzed_by="human",
            event_version=version,
            analyzed_at=_now(),
        )

    # ── 议题 ────────────────────────────────────────────────────────

    def create_event(
        self,
        *,
        name: str,
        summary: str,
        keywords: Sequence[str],
        event_type: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> int:
        event_id = self._next_event_id
        self._next_event_id += 1
        self._events[event_id] = EventRow(
            event_id=event_id,
            name=name,
            summary=summary,
            keywords=list(keywords),
            event_type=event_type,
            start_date=start_date,
            end_date=end_date,
            created_at=_now(),
        )
        return event_id

    def update_event(
        self,
        event_id: int,
        *,
        name: str | None = None,
        summary: str | None = None,
        keywords: Sequence[str] | None = None,
        event_type: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        bump_version: bool = False,
        mark_backfill_scanned: bool = False,
    ) -> bool:
        event = self._events.get(event_id)
        if event is None:
            return False
        # 局部更新：没给的不动（对应 SQL 里的 coalesce）
        fields: dict[str, object] = {}
        for key, value in (
            ("name", name),
            ("summary", summary),
            ("event_type", event_type),
            ("start_date", start_date),
            ("end_date", end_date),
        ):
            if value is not None:
                fields[key] = value
        if keywords is not None:
            fields["keywords"] = list(keywords)
        if bump_version:
            fields["version"] = event.version + 1
        if mark_backfill_scanned:
            fields["backfill_scanned"] = True
        self._events[event_id] = replace(event, **fields)
        return True

    def event_by_id(self, event_id: int) -> EventRow | None:
        return self._events.get(event_id)

    def _event_name(self, event_id: int) -> str | None:
        """对应 `event_stances_for` 里那个 `left join dim_event` 的外键取回。"""
        event = self._events.get(event_id)
        return event.name if event else None

    # ── 证据 ────────────────────────────────────────────────────────

    def insert_evidence(
        self,
        content_id: str,
        *,
        evidence_type: str | None,
        evidence_number: str | None,
        filed_by: str | None,
        notes: str | None = None,
    ) -> int:
        # 对应 `fact_evidence.content_id` 的外键。库里会拒，这里也拒——
        # 不然"登记前先确认内容在不在"那条守卫就没法测。
        if content_id not in self._content_ids:
            raise KeyError(f"库里没有内容 {content_id!r}（对应外键约束）")
        evidence_id = self._next_evidence_id
        self._next_evidence_id += 1
        self._evidence[evidence_id] = EvidenceRow(
            evidence_id=evidence_id,
            content_id=content_id,
            evidence_type=evidence_type,
            evidence_number=evidence_number,
            filed_by=filed_by,
            filed_at=_now(),
            notes=notes,
        )
        return evidence_id

    def evidence_for(
        self, *, content_id: str | None = None, limit: int = 50
    ) -> list[EvidenceRow]:
        rows = [
            row
            for row in self._evidence.values()
            if content_id is None or row.content_id == content_id
        ]
        # 对应 `order by filed_at desc, evidence_id desc`
        rows.sort(key=lambda row: (row.filed_at, row.evidence_id), reverse=True)
        return rows[:limit]

    # ── 提示词 ──────────────────────────────────────────────────────

    def active_prompt_bundle(self) -> PromptBundle | None:
        return self._active_prompt

    def push_prompt_bundle(self, bundle: PromptBundle) -> None:
        self._active_prompt = bundle
