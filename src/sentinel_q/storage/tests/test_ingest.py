"""批量入库的测试。

⭐ **这一组原来住在 `collector/tests/test_extract.py` 里**，测的是
`extract.ingest()`。决策 52 之后入库整个搬到了 `storage/`，
测试也跟着搬——`collector` 现在连 `content_id` 是什么都不知道，
留在这里的每一条都测不了。

搬过来时改了两处，都是"因为搬了家"而不是"因为改了行为"：

  1. 喂进去的从 `ParsedItem` 变成了 `ContentRecord`——
     `insert_contents` 拿到的是**已经装配好的记录**（`from_document` 装配的），
     它既不认识 `ParsedItem`，也不知道 `contents.jsonl`。
  2. 用真的 `FakeRepo`（`storage/fake.py`），不再自己搭一个。
     那个替身本来就实现了 `insert_content` 的维表同步，
     自己再搭一个会在两处各漂各的。

⚠️ `report.skipped` 那一条**没有搬过来**：那个计数器已经删了。
"装配失败"是 `extract.from_document()` 的事（采集模块那边），入库这一层只看得见
`ContentRecord`，**根本遇不到装不出来的条目**——留一个恒为 0 的
计数器在那里，只会让人以为这一层也能丢东西。
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sentinel_q.shared.models import ContentRecord
from sentinel_q.storage.fake import FakeRepo
from sentinel_q.storage.ingest import insert_contents


def record(
    *,
    zhihu_id: str = "123",
    content_type: str = "answer",
    url: str | None = None,
    question_zhihu_id: str | None = None,
    parent_zhihu_id: str | None = None,
    author_zhihu_id: str | None = "someone",
    author_name: str | None = "某人",
    author_url: str | None = "https://www.zhihu.com/people/someone",
    snapshot_path: str | None = "snap/answer/123.html.gz",
) -> ContentRecord:
    return ContentRecord(
        content_type=content_type,
        zhihu_id=zhihu_id,
        url=url or f"https://www.zhihu.com/{content_type}/{zhihu_id}",
        author_zhihu_id=author_zhihu_id,
        author_name=author_name,
        author_url=author_url,
        question_zhihu_id=question_zhihu_id,
        parent_zhihu_id=parent_zhihu_id,
        snapshot_path=snapshot_path,
        published_at=datetime(2020, 11, 25, 10, 17, 52, tzinfo=UTC),
    )


def repo_with_question(zhihu_qid: str) -> FakeRepo:
    """一个已经问过 `zhihu_qid` 的库——`claim_question` 是唯一的注入口。"""
    repo = FakeRepo()
    repo.claim_question(zhihu_qid, None, None)
    return repo


# ── 父级：知乎 ID → uuid ────────────────────────────────────────────


class TestParentResolution:
    def test_parent_zhihu_id_resolves_to_the_parent_uuid(self) -> None:
        """`fact_content.parent_id` 是 **uuid**，记录里装的是知乎 ID。

        中间这一跳就是 `IdIndex` 存在的理由。
        """
        repo = FakeRepo()
        parent = record(zhihu_id="100", content_type="comment", parent_zhihu_id=None)
        child = record(zhihu_id="200", content_type="comment", parent_zhihu_id="100")

        report = insert_contents([parent, child], repo=repo)

        assert report.inserted == 2
        assert report.dangling_parents == 0
        # `FakeRepo` 发的是真 uuid，所以按 URL 反查，不假设它的形状
        parent_id = repo.content_id_for(parent.url)
        child_id = repo.content_id_for(child.url)
        assert parent_id is not None and child_id is not None
        assert repo.resolved[parent_id]["parent_id"] is None
        assert repo.resolved[child_id]["parent_id"] == parent_id, "子级必须指向父级的 uuid"

    def test_unresolvable_parent_is_counted_not_guessed(self) -> None:
        """⭐ 父级找不到时 `parent_id` 留 None 并**计数**，不猜。

        静默留 None 的后果：评论看起来都采到了，只是**层级全塌成平的**——
        而"这条评论是在回复谁"恰恰是网暴取证里最关键的一环。
        所以断链必须变成一个报得出来的数字。
        """
        repo = FakeRepo()
        orphan = record(zhihu_id="200", content_type="comment", parent_zhihu_id="不存在")

        report = insert_contents([orphan], repo=repo)

        assert report.inserted == 1
        assert report.dangling_parents == 1
        assert repo.resolved[repo.content_id_for(orphan.url)]["parent_id"] is None

    def test_top_level_items_never_count_as_dangling(self) -> None:
        """顶层内容（回答、一级评论）本来就没有父级，不该被算成断链。

        "取不到"和"本来就没有"必须分开——混在一起的话，
        断链计数永远是满的，等于没有这个指标。
        """
        repo = FakeRepo()
        report = insert_contents(
            [record(zhihu_id="1"), record(zhihu_id="2", content_type="comment")],
            repo=repo,
        )
        assert report.dangling_parents == 0

    def test_cross_type_parent_still_resolves(self) -> None:
        """评论的父级是**回答/文章**（另一种类型），也要能接上。

        一条评论挂在回答下时，父级的 zhihu_id 是回答 ID——按
        `(content_type, zhihu_id)` 严格查表是查不到的，必须有兜底。
        """
        repo = FakeRepo()
        answer = record(zhihu_id="1594809785", content_type="answer")
        comment = record(
            zhihu_id="11541681936", content_type="comment", parent_zhihu_id="1594809785"
        )

        report = insert_contents([answer, comment], repo=repo)

        assert report.dangling_parents == 0
        assert (
            repo.resolved[repo.content_id_for(comment.url)]["parent_id"]
            == repo.content_id_for(answer.url)
        )

    def test_an_already_known_parent_does_not_break_its_children(self) -> None:
        """⭐ 父级**已经在库里**时，子级照样挂空——这是已知的、报得出来的缺口。

        跨轮才有这种情况：上一次运行插的父级，这一次不在映射里。
        这里钉住的是"它会被数出来"，不是"它会被修好"——
        修法要等 `Repo` 给出一个按 URL 查 `content_id` 的查询。
        """
        repo = FakeRepo()
        # 父级这一批插进去过一次
        first = insert_contents([record(zhihu_id="100", content_type="comment")], repo=repo)
        assert first.inserted == 1

        # 新的一批：父级又出现了一次（这时会被 url 唯一约束挡掉），子级跟着来
        second = insert_contents(
            [
                record(zhihu_id="100", content_type="comment"),
                record(zhihu_id="200", content_type="comment", parent_zhihu_id="100"),
            ],
            repo=repo,
        )

        assert second.duplicates == 1
        assert second.dangling_parents == 1, "挂不上必须报出来，不能安静地塌成平的"


# ── 报告：把"跑完了但缺东西"变成数字 ────────────────────────────────


class TestReport:
    def test_duplicate_url_is_not_a_failure(self) -> None:
        """重复是正常的（查重在调用方做，这里是并发兜底），不是错误。"""
        repo = FakeRepo()
        one = record(url="https://www.zhihu.com/answer/123")
        assert insert_contents([one], repo=repo).inserted == 1

        report = insert_contents([one], repo=repo)

        assert report.inserted == 0
        assert report.duplicates == 1

    def test_missing_snapshots_are_counted(self) -> None:
        """快照缺了必须报——这是"将来删帖就取不回来"的那批内容。"""
        repo = FakeRepo()
        report = insert_contents([record(snapshot_path=None)], repo=repo)

        assert report.no_snapshot == 1
        assert "删帖取不回来" in report.describe()

    def test_known_gaps_are_summarised_once(self) -> None:
        """author/question 两个缺口**合并成一句**，不逐条刷屏。

        一批 500 条的话，逐条报就是 500 行噪声，人会直接忽略掉——
        那等于没报。
        """
        repo = FakeRepo()  # 一个问题都没问过，所以 question_id 全查不到
        report = insert_contents(
            [
                record(question_zhihu_id="19581646", author_zhihu_id=None),
                record(
                    zhihu_id="2", question_zhihu_id="1", author_zhihu_id=None
                ),
            ],
            repo=repo,
        )

        assert report.unresolved_authors == 2
        assert report.unresolved_questions == 2
        gaps = report.describe_known_gaps()
        assert gaps is not None
        assert gaps.count("；") == 1, "两个缺口合成一句，不是两行"

    def test_no_author_is_not_a_gap(self) -> None:
        """匿名回答**本来就没有作者**，不该算进缺口。

        和 `dangling_parents` 是同一条道理：把"本来就没有"混进"没取到"，
        这个指标就永远是满的。
        """
        repo = FakeRepo()
        report = insert_contents(
            [record(author_zhihu_id=None, author_name=None, author_url=None)], repo=repo
        )
        assert report.unresolved_authors == 0
        assert report.describe_known_gaps() is None

    def test_a_question_already_in_dim_question_is_not_a_gap(self) -> None:
        """问过的那个问题要把 `question_id` 落上，而且不算缺口。"""
        repo = repo_with_question("19581646")
        report = insert_contents([record(question_zhihu_id="19581646")], repo=repo)

        assert report.unresolved_questions == 0
        row = record(question_zhihu_id="19581646")
        assert repo.resolved[repo.content_id_for(row.url)]["question_id"] == 1


# ── 维度表同步：只在 storage 这一层发生 ─────────────────────────────


class TestDimensionSync:
    def test_the_author_actually_reaches_the_row(self) -> None:
        """⭐ 同一个人在这批里发两条，两个 `author_id` 必须指向**同一行**。

        这就是 `ensure_author` 用 upsert 而不是 insert 的理由：
        "同一用户在不同内容下的发言"要能聚到一起。若每条内容各建一个作者，
        数字看着都对（没有 null、没有报错），而按作者聚合时每个人只有一条。
        """
        repo = FakeRepo()
        report = insert_contents(
            [record(zhihu_id="1"), record(zhihu_id="2")], repo=repo
        )

        assert report.inserted == 2
        assert report.unresolved_authors == 0
        author_ids = {resolved["author_id"] for resolved in repo.resolved.values()}
        assert len(author_ids) == 1
        assert len(repo.authors) == 1

    def test_the_record_carries_no_database_keys(self) -> None:
        """⭐ 采集侧的记录里**一个库里主键都没有**（决策 52）。

        三个主键全是入库那一刻在 `storage` 里翻译出来的。
        往记录上加回任何一个，采集模块就又要开始知道库长什么样了。
        """
        fields = set(ContentRecord.__dataclass_fields__)
        for leaked in ("author_id", "question_id", "parent_id", "content_id"):
            assert leaked not in fields, f"{leaked} 不该出现在 ContentRecord 上"
        for expected in ("author_zhihu_id", "question_zhihu_id", "parent_zhihu_id"):
            assert expected in fields


def test_ingest_module_does_not_import_the_collector() -> None:
    """⭐ 入库这一层不认识 `ParsedItem`：装配是采集侧的事，两边不许互相 import。

    这条是 `tests/test_layering.py` 那条规则的具体化——
    写在这里是因为它一旦被破坏，最先炸的就是这个文件里的测试。
    """
    source = (Path(__file__).resolve().parents[1] / "ingest.py").read_text(encoding="utf-8")
    assert "collector" not in source, "storage 不许 import collector（架构文档 7.3 规则一）"
