"""带判定的入库（决策 51）与议题判定落库（4.5）的测试。

`test_ingest.py` 测的是**只有内容、没有判定**那条路（`insert_contents`）；
这一份测的是它的兄弟 `insert_judged`——AI 的判定和内容**同一批**写进去。

两件事在这里被钉住：

  1. **决策 51**：库里不许出现"有内容、没分析"的行。可执行的断言是
     `test_every_inserted_row_has_an_analysis`——它专门拿"`zhihu_id` 为空的
     记录"和"url 重复的记录"两种情形去撞，因为那两种正是"反查索引"写法
     会静默漏写的场合。
  2. **§5.5.2 的内容闸门**：自身相关 **或** 所属问题相关。四种情况逐条测。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sentinel_q.shared.models import ContentJudgment, ContentRecord, EventJudgment
from sentinel_q.storage.events import save_judged_events
from sentinel_q.storage.fake import FakeRepo
from sentinel_q.storage.ingest import insert_contents, insert_judged

# ── 造数据的小工具 ──────────────────────────────────────────────────


def record(
    *,
    zhihu_id: str = "9001001",
    content_type: str = "answer",
    url: str | None = None,
    question_zhihu_id: str | None = None,
    parent_zhihu_id: str | None = None,
) -> ContentRecord:
    return ContentRecord(
        content_type=content_type,
        zhihu_id=zhihu_id,
        url=url or f"https://www.zhihu.com/{content_type}/{zhihu_id or 'noid'}",
        author_zhihu_id="someone",
        author_name="某人",
        author_url="https://www.zhihu.com/people/someone",
        question_zhihu_id=question_zhihu_id,
        parent_zhihu_id=parent_zhihu_id,
        snapshot_path="snap/x.html.gz",
        published_at=datetime(2026, 9, 20, 10, 0, tzinfo=UTC),
    )


def judged(
    *,
    relevant: bool,
    stance: str | None = None,
    model_version: str = "deepseek-flash",
    prompt_version: str = "979935e9ac7ff951",
) -> ContentJudgment:
    return ContentJudgment(
        zhihu_id="9001001",
        is_relevant=relevant,
        ai_summary="（测试）",
        platform_stance=stance,
        stance_confidence=0.9,
        risk_level="低风险",
        risk_reasoning="（测试）",
        model_version=model_version,
        prompt_version=prompt_version,
    )


def event_judged(
    event_id: int,
    *,
    relevant: bool = True,
    stance: str | None = "中立",
    event_version: int = 1,
) -> EventJudgment:
    return EventJudgment(
        zhihu_id="9001001",
        event_id=str(event_id),
        event_version=event_version,
        is_relevant=relevant,
        stance=stance,
        confidence=0.8,
        model_version="deepseek-flash",
        prompt_version="979935e9ac7ff951",
    )


def repo_with_question(zhihu_qid: str) -> FakeRepo:
    """一个已经问过 `zhihu_qid` 的库。`claim_question` 是唯一的注入口。"""
    repo = FakeRepo()
    repo.claim_question(zhihu_qid, None, None)
    return repo


# ── §5.5.2 的四种情况 ───────────────────────────────────────────────


class TestTheContentGate:
    def test_case4_answer_is_kept_by_its_parent(self) -> None:
        """⭐ 情况四：回答自己判为不相关，但**所属问题判为相关** → 留。

        这是决策 28 那条闸门的后半句，也是 §3.3 第⑥步"只要问题判为相关，
        它下面的回答不区分相关性一律入库"的落点。漏掉它的话，
        一个刚刚被判为相关的问题下面会一条回答都进不来——
        而报告上只会显示"内容闸门挡掉 N 条"，看起来像正常过滤。
        """
        repo = repo_with_question("q1")
        answer = record(zhihu_id="9001005", question_zhihu_id="q1")

        report = insert_judged(
            [answer],
            {answer.url: judged(relevant=False, stance="不相关")},
            repo=repo,
            relevant_questions={"q1"},
        )

        assert report.kept_by_parent == 1
        assert report.ingest.inserted == 1
        assert report.dropped_irrelevant == 0
        assert set(report.content_ids) == {answer.url}

    def test_case4_platform_stance_conflict_is_counted_not_corrected(self) -> None:
        """⭐ 情况四但立场没填 `'不相关'`：**照插，只计数，不改数据**。

        §5.5.2 说这一档"照实填"。AI 给了一个别的立场（说明它对同一条内容
        判出了两个互相矛盾的结论）值得记一笔，但**改数据等于替 AI 改口径**，
        而口径是它判出来的、要留痕的（决策 47）。
        """
        repo = repo_with_question("q1")
        answer = record(zhihu_id="9001005", question_zhihu_id="q1")

        report = insert_judged(
            [answer],
            {answer.url: judged(relevant=False, stance="抹黑")},
            repo=repo,
            relevant_questions={"q1"},
        )

        assert report.stance_conflict == 1
        assert report.ingest.inserted == 1, "照样入库"
        analysis = repo.analysis_for(report.content_ids[answer.url])
        assert analysis is not None
        assert analysis.platform_stance == "抹黑", "不许被改成'不相关'"

    def test_neither_relevant_is_dropped(self) -> None:
        """情况二：自己和所属问题都不相关 → 丢。内容闸门正常工作的样子。"""
        repo = repo_with_question("q1")
        answer = record(zhihu_id="9001005", question_zhihu_id="q1")

        report = insert_judged(
            [answer],
            {answer.url: judged(relevant=False, stance="不相关")},
            repo=repo,
            relevant_questions=set(),  # q1 判过，但不在本轮更新列表里
        )

        assert report.dropped_irrelevant == 1
        assert report.ingest.inserted == 0
        assert repo.all_urls() == set(), "一个字节都不该落库"

    def test_a_content_without_a_question_is_dropped_when_irrelevant(self) -> None:
        """文章/想法没有所属问题，判为不相关时没有第二条路可走。"""
        repo = FakeRepo()
        article = record(zhihu_id="8801003", content_type="thought")

        report = insert_judged(
            [article],
            {article.url: judged(relevant=False, stance="不相关")},
            repo=repo,
            relevant_questions=set(),
        )

        assert report.dropped_irrelevant == 1
        assert report.kept_by_parent == 0
        assert report.dropped_unknown_parent == 0

    def test_a_parent_that_is_not_in_dim_question_is_reported_loudly(self) -> None:
        """⭐ 所属问题压根不在库里：丢，但**记的是另一个计数器**。

        这一档和"问题判过、判为不相关"在库里长得一样（都没进），
        成因却完全是两回事：那是判断结果，这是**第②步漏了登记**、
        或者调用方拿了一份过时的【更新问题列表】。混在一起的话，
        一条真正的流水线缺陷会被当成正常的过滤消化掉。
        """
        repo = FakeRepo()  # 一个问题都没问过
        answer = record(zhihu_id="9001005", question_zhihu_id="从没登记过")

        report = insert_judged(
            [answer],
            {answer.url: judged(relevant=False, stance="不相关")},
            repo=repo,
            relevant_questions=set(),
        )

        assert report.dropped_unknown_parent == 1
        assert report.dropped_irrelevant == 0
        assert report.ingest.inserted == 0


# ── 决策 51：不许有"有内容、没分析"的行 ─────────────────────────────


class TestDecision51:
    def test_every_inserted_row_has_an_analysis(self) -> None:
        """⭐⭐ 决策 51 的可执行断言。

        两种情形一起撞，因为它们是同一个 bug 的两个入口：

          - **`zhihu_id` 为空**的记录。它在库里照样有 `content_id`，
            但 `IdIndex.add` 会跳过它（那个索引按知乎 id 键）。
            任何"插完整批再从索引反查"的写法都会**静默漏掉它的分析**。
          - **url 重复**的记录。它压根没插进去（`on conflict do nothing`），
            给它写分析会撞外键；反查写法则会把它算成"插入成功"。

        两种写法的 `inserted` 都是对的——所以这个错只能靠
        "入库几条 × 分析几条"来发现。
        """
        repo = FakeRepo()
        no_id = record(zhihu_id="", url="https://www.zhihu.com/comment/1")
        normal = record(zhihu_id="2", content_type="comment")
        duplicate = record(zhihu_id="3", url=no_id.url)  # 与 no_id 同一个 url
        records = [no_id, normal, duplicate]

        report = insert_judged(
            records,
            {r.url: judged(relevant=True, stance="中立") for r in records},
            repo=repo,
            relevant_questions=set(),
        )

        assert report.ingest.inserted == 2
        assert report.ingest.duplicates == 1
        assert set(repo.analyses) == set(report.content_ids.values())
        assert len(report.content_ids) == 2
        assert len(repo.all_urls()) == len(repo.analyses), "入库几条就得有几条分析"

    def test_unjudged_row_is_not_inserted(self) -> None:
        """⭐ 没有判定的记录**不插**，而不是插了留个空。

        "插进去、分析待补"正是决策 51 要挡的那个状态：它会让
        `fact_analysis` 上的 join 悄悄少几行，而所有计数都正常。
        """
        repo = FakeRepo()
        has_judgment = record(zhihu_id="1")
        no_judgment = record(zhihu_id="2")

        report = insert_judged(
            [has_judgment, no_judgment],
            {has_judgment.url: judged(relevant=True, stance="中立")},
            repo=repo,
            relevant_questions=set(),
        )

        assert report.unjudged == 1
        assert report.ingest.inserted == 1
        assert repo.all_urls() == {has_judgment.url}

    def test_a_question_record_is_refused_without_an_exception(self) -> None:
        """⭐ 问题不进 `fact_content`（迁移 0004 的 check 约束）。

        挡在 Python 侧是为了**报一句人话**。放进 SQL 的话，一场
        500 条的入库会因为末尾一行 `content_type='question'` 抛一句
        英文约束异常，而前面 499 条的处境没人说得清。
        """
        repo = FakeRepo()
        question = record(zhihu_id="1002003006", content_type="question")

        report = insert_judged(
            [question],
            {question.url: judged(relevant=True)},
            repo=repo,
            relevant_questions=set(),
        )

        assert report.rejected_questions == 1
        assert report.ingest.inserted == 0
        assert repo.all_urls() == set()

    def test_relevant_questions_has_no_default(self) -> None:
        """⭐ 漏传【更新问题列表】必须当场炸，不能默认成空集。

        默认空集是最坏的降级：情况四的每一条都会被丢，
        而报告上 `dropped_irrelevant` 的数字**和"模型判错了"一模一样**。
        与其静默少采一批回答，不如让调用方在签名上就过不去。
        """
        with pytest.raises(TypeError):
            insert_judged([], {}, repo=FakeRepo())  # type: ignore[call-arg]

    def test_analysis_carries_prompt_version(self) -> None:
        """决策 47：`prompt_version` 必须进库——提示词不进 git，库是唯一的追溯处。"""
        repo = FakeRepo()
        answer = record()

        report = insert_judged(
            [answer],
            {answer.url: judged(relevant=True, stance="中立", prompt_version="deadbeef")},
            repo=repo,
            relevant_questions=set(),
        )

        analysis = repo.analysis_for(report.content_ids[answer.url])
        assert analysis is not None
        assert analysis.prompt_version == "deadbeef"
        assert analysis.model_version == "deepseek-flash"
        assert analysis.analyzed_by == "ai"


# ── 两个入口共用一份父级映射 ────────────────────────────────────────


class TestSharedBatchLogic:
    def test_both_entry_points_count_dangling_identically(self) -> None:
        """⭐ `insert_contents` 和 `insert_judged` 必须报出**同一个**断链数。

        这一条守着"父级映射只有一份实现"。抄第二份的时候漏一处，
        表现是"评论都采到了，只是层级全塌成平的"——不报错、数字也好看。
        """
        orphan = record(zhihu_id="200", content_type="comment", parent_zhihu_id="不存在")

        plain = insert_contents([orphan], repo=FakeRepo())
        gated = insert_judged(
            [orphan],
            {orphan.url: judged(relevant=True, stance="中立")},
            repo=FakeRepo(),
            relevant_questions=set(),
        )

        assert plain.dangling_parents == gated.ingest.dangling_parents == 1

    def test_parent_and_child_resolve_within_one_gated_batch(self) -> None:
        """带闸门的那条路也要能把子级挂上父级——判定不影响父级映射。"""
        repo = FakeRepo()
        parent = record(zhihu_id="100", content_type="comment")
        child = record(zhihu_id="200", content_type="comment", parent_zhihu_id="100")

        report = insert_judged(
            [parent, child],
            {r.url: judged(relevant=True, stance="中立") for r in (parent, child)},
            repo=repo,
            relevant_questions=set(),
        )

        assert report.ingest.dangling_parents == 0
        assert repo.resolved[report.content_ids[child.url]]["parent_id"] == (
            report.content_ids[parent.url]
        )

    def test_content_ids_covers_exactly_the_inserted_rows(self) -> None:
        """⭐ 重跑时 `content_ids` 是空的——**这是对的**。

        它只装"这一次真插进去的"。第二次跑全部被 url 唯一约束挡掉，
        于是没有新的 `content_id` 可给下游挂议题判定。
        要是把重复的那些也算进来（或者去查库补齐），事件那条线就会
        对着**上一轮**的内容重写一遍判定。
        """
        repo = FakeRepo()
        answer = record()
        judgments = {answer.url: judged(relevant=True, stance="中立")}

        first = insert_judged([answer], judgments, repo=repo, relevant_questions=set())
        second = insert_judged([answer], judgments, repo=repo, relevant_questions=set())

        assert set(first.content_ids) == {answer.url}
        assert second.ingest.duplicates == 1
        assert second.content_ids == {}

    def test_a_dropped_content_never_reaches_the_database(self) -> None:
        """被闸门丢掉的记录不插——但它的判定也不该被写进 `content_ids`。"""
        repo = FakeRepo()
        relevant = record(zhihu_id="1")
        dropped = record(zhihu_id="2")

        report = insert_judged(
            [relevant, dropped],
            {
                relevant.url: judged(relevant=True, stance="中立"),
                dropped.url: judged(relevant=False, stance="不相关"),
            },
            repo=repo,
            relevant_questions=set(),
        )

        assert report.ingest.inserted == 1
        assert set(report.content_ids) == {relevant.url}
        assert set(repo.analyses) == {report.content_ids[relevant.url]}


# ── 议题判定落库（4.5）───────────────────────────────────────────────


def repo_with_event(name: str = "测试议题") -> tuple[FakeRepo, int]:
    repo = FakeRepo()
    event_id = repo.create_event(
        name=name,
        summary="（测试用摘要）",
        keywords=["青禾乳业"],
        event_type="new",
    )
    return repo, event_id


def inserted_content(repo: FakeRepo, *, zhihu_id: str = "9001001") -> tuple[str, str]:
    """插一条内容，返回 `(url, content_id)`。"""
    answer = record(zhihu_id=zhihu_id)
    report = insert_judged(
        [answer],
        {answer.url: judged(relevant=True, stance="中立")},
        repo=repo,
        relevant_questions=set(),
    )
    return answer.url, report.content_ids[answer.url]


class TestSaveJudgedEvents:
    def test_a_relevant_judgment_reaches_the_table(self) -> None:
        """正向路径：`analyzed_by` 必须是 `'ai'`，版本号取库里的那一版。"""
        repo, event_id = repo_with_event()
        url, content_id = inserted_content(repo)

        report = save_judged_events(
            [(url, event_judged(event_id, stance="反向"))],
            repo=repo,
            content_ids={url: content_id},
        )

        assert report.saved == 1
        rows = repo.event_stances_for(content_id)
        assert len(rows) == 1
        assert rows[0].stance == "反向"
        assert rows[0].analyzed_by == "ai", "AI 写的不能标成 human"
        assert rows[0].event_version == 1

    def test_an_irrelevant_judgment_is_never_written(self) -> None:
        """⭐ 判为"不属于该议题"的**不落库**。

        `fact_content_event` 上没有 `is_relevant` 列，写进去等于把
        "其实不相关"记成"相关且中立"——凭空造出来的判断，事后看不出来。
        """
        repo, event_id = repo_with_event()
        url, content_id = inserted_content(repo)

        report = save_judged_events(
            [(url, event_judged(event_id, relevant=False))],
            repo=repo,
            content_ids={url: content_id},
        )

        assert report.skipped_irrelevant == 1
        assert report.saved == 0
        assert repo.event_stances_for(content_id) == []

    def test_orphaned_judgments_are_counted(self) -> None:
        """⭐ 判定相关，但它挂的内容被内容闸门丢了 → 记 `orphaned`，不静默少一条。

        这是 `fact_content_event` 的条数**不等于**"判为相关的条数"的原因。
        不报出来的话，账上会缺一块，而缺的那块永远查不出来。
        """
        repo, event_id = repo_with_event()

        report = save_judged_events(
            [("https://www.zhihu.com/answer/没入库", event_judged(event_id))],
            repo=repo,
            content_ids={},  # 内容闸门什么都没留下
        )

        assert report.orphaned == 1
        assert report.saved == 0
        assert report.total == 1, "每一档都要有归宿"

    def test_a_stale_event_version_is_refused(self) -> None:
        """⭐ 议题摘要改过版（4.5.4），基于旧版的判定**拒写**。

        ⚠️ 拒写不是"更严格一点"：另一条路是把它改写成库里的当前版本，
        那等于把一条基于旧摘要的判断**伪装成**基于新摘要的——而 4.5.4
        整套"该议题下有 N 条判断基于旧版本"就是靠这一列工作的，
        伪装之后那个数字永远是 0。
        """
        repo, event_id = repo_with_event()
        url, content_id = inserted_content(repo)
        assert repo.update_event(event_id, bump_version=True) is True

        report = save_judged_events(
            [(url, event_judged(event_id, event_version=1))],  # 判定基于第 1 版
            repo=repo,
            content_ids={url: content_id},
        )

        assert report.version_mismatch == 1
        assert report.saved == 0
        assert repo.event_stances_for(content_id) == []

    def test_an_unknown_event_is_counted_not_raised(self) -> None:
        """议题不在库里时记一笔就走——外键会炸，而且炸在一批的中途。"""
        repo, _event_id = repo_with_event()
        url, content_id = inserted_content(repo)

        report = save_judged_events(
            [(url, event_judged(999))],
            repo=repo,
            content_ids={url: content_id},
        )

        assert report.unknown_event == 1
        assert report.saved == 0

    def test_a_bad_stance_is_counted_not_raised(self) -> None:
        """立场取值不合法：库上有 check 约束，挡在这里才能报人话。"""
        repo, event_id = repo_with_event()
        url, content_id = inserted_content(repo)

        report = save_judged_events(
            [(url, event_judged(event_id, stance="支持"))],
            repo=repo,
            content_ids={url: content_id},
        )

        assert report.bad_stance == 1
        assert report.saved == 0
        assert repo.event_stances_for(content_id) == []
