"""主程序接线测试：`contents.jsonl` → 库。

⚠️ **这里不碰浏览器，也不碰真库。** 采集那一半由 `collector` 自己的测试负责，
入库那一半由 `storage` 自己的测试负责——这个文件测的是**两者之间那条缝**：

    runtime/ops/<run>/contents.jsonl  →  from_document  →  insert_contents

这条缝上有三件事只有测试拦得住，而且失败时都不报错：

  1. **父级顺序**：评论的 `parent_id` 靠"同一批里父级先插过"来搭。
     把行序打乱，子级全部挂空，日志正常，只有 `dangling` 计数器会动。
  2. **装不成的行**：一行读不出必填字段就要跳过并报数——静默丢一条
     就是静默丢一份证据。
  3. **空产物 / 全坏产物**：前者不是错误，后者**必须在开库之前**退出。
     连上去再报"入库 0 条"，看起来像跑通了。

⚠️ 目录一律指到 `tmp_path`（`cli.paths`），别往仓库的 `runtime/ops/` 里堆东西。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sentinel_q.main import __main__ as cli
from sentinel_q.shared.config import Paths
from sentinel_q.storage.fake import FakeRepo

ANSWER_URL = "https://www.zhihu.com/question/123/answer/456"
COMMENT_URL = "https://www.zhihu.com/question/123/answer/456/comment/c1"


def _row(
    *,
    url: str,
    zhihu_id: str,
    content_type: str = "answer",
    parent_id: str | None = None,
    question_id: str | None = "123",
    text: str = "正文",
    author_url: str | None = "https://www.zhihu.com/people/someone",
) -> dict:
    """一行 `contents.jsonl`。字段名照抄 `extract.to_document` 的产物。

    ⚠️ 这里手写而不是调 `to_document`：那个函数会往磁盘上写快照文件，
    而这个文件测的是**读回来之后**的事。文档的形状由
    `collector/tests/test_extract.py` 的往返测试钉着。
    """
    return {
        "url": url,
        "content_type": content_type,
        "zhihu_id": zhihu_id,
        "parent_id": parent_id,
        "question_id": question_id,
        "title": None,
        "author_name": "某人",
        "author_url": author_url,
        "text": text,
        "text_length": len(text),
        "content_text": text,
        "storage_path": None,
        "voteup_count": 1,
        "comment_count": 0,
        "published_at": "2020-11-25T10:17:52+00:00",
        "published_text": None,
        "raw_content_hash": "deadbeef",
        "snapshot_path": None,
        "keyword": "某公司",
        "collected_at": "2026-09-28T15:30:00+00:00",
    }


def _seed_run(root: Path, run_id: str, rows: list[dict]) -> Path:
    """造一个任务目录：`task.json`（`ingest` 靠它判断任务存不存在）+ `contents.jsonl`。"""
    run_dir = Paths.resolve(root).ops / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "task.json").write_text(
        json.dumps({"run_id": run_id, "mode": "update", "cursor": {}}), encoding="utf-8"
    )
    (run_dir / "contents.jsonl").write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8"
    )
    return run_dir


def _body_then_comments() -> list[dict]:
    return [
        _row(url=ANSWER_URL, zhihu_id="456"),
        _row(url=COMMENT_URL, zhihu_id="c1", content_type="comment", parent_id="456"),
        _row(
            url=COMMENT_URL + "2",
            zhihu_id="c2",
            content_type="comment",
            parent_id="456",
        ),
    ]


class TestIngestRun:
    def test_a_body_lands_before_its_comments(self, tmp_path: Path) -> None:
        """⭐ 评论的 `parent_id` 必须接上正文那一行的 uuid。

        这一跳只在"父级先插过"时才成立。行序一乱，`parent_id` 全变 None——
        **而且不报错**，只有 `dangling_parents` 计数器会动。所以这里两头都断言。
        """
        run_dir = _seed_run(tmp_path, "20260101-000000", _body_then_comments())
        repo = FakeRepo()

        report = cli.ingest_run(run_dir, repo=repo)

        assert report is not None
        assert report.inserted == 3
        assert report.dangling_parents == 0, "顺序对了就不该有挂空的父级"

        body_id = repo.content_id_for(ANSWER_URL)
        assert body_id is not None
        for comment_url in (COMMENT_URL, COMMENT_URL + "2"):
            comment_id = repo.content_id_for(comment_url)
            assert comment_id is not None
            assert repo.resolved[comment_id]["parent_id"] == body_id

    def test_the_zhihu_parent_id_is_translated_not_copied(self, tmp_path: Path) -> None:
        """⚠️ 文档里的 `parent_id` 是**知乎的 ID**（`456`），不是库里的 uuid。

        直接把 `"456"` 写进 `parent_id` 列的错误很隐蔽：那一列是 uuid 类型，
        有的库配置会接受任意字符串，于是父子关系永远查不出来，
        而每一行单看都"对"。
        """
        run_dir = _seed_run(tmp_path, "20260101-000000", _body_then_comments())
        repo = FakeRepo()

        cli.ingest_run(run_dir, repo=repo)

        comment_id = repo.content_id_for(COMMENT_URL)
        assert comment_id is not None
        assert repo.resolved[comment_id]["parent_id"] != "456", "不能照抄知乎的 ID"
        assert repo.resolved[comment_id]["parent_id"] == repo.content_id_for(ANSWER_URL)

    def test_an_empty_run_is_not_an_error(self, tmp_path: Path) -> None:
        """一行都没有 = "这次还没采到东西"，不是失败。"""
        run_dir = _seed_run(tmp_path, "20260101-000000", [])
        assert cli.ingest_run(run_dir, repo=FakeRepo()) is None

    def test_a_missing_contents_file_is_not_an_error_either(self, tmp_path: Path) -> None:
        """采集中途崩了，`contents.jsonl` 可能压根还没建——同样不算失败。"""
        run_dir = Paths.resolve(tmp_path).ops / "20260101-000000"
        run_dir.mkdir(parents=True)
        (run_dir / "task.json").write_text('{"run_id": "20260101-000000", "mode": "update"}')

        assert cli.ingest_run(run_dir, repo=FakeRepo()) is None

    def test_a_row_that_cannot_be_built_is_counted_not_dropped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """⭐ 认不出的类型要**报出来**，不能静默跳过。

        静默丢一行 = 静默丢一份证据。这里的类型是 `pin`（知乎有这个东西，
        但库上的 check 约束不认），所以它装不成记录。
        """
        rows = _body_then_comments() + [_row(url="https://zhihu.com/pin/9", zhihu_id="9", content_type="pin")]
        run_dir = _seed_run(tmp_path, "20260101-000000", rows)
        repo = FakeRepo()

        with caplog.at_level("WARNING"):
            report = cli.ingest_run(run_dir, repo=repo)

        assert report is not None
        assert report.inserted == 3, "好的那三条照旧入库"
        assert len(repo.contents) == 3
        assert "pin" in caplog.text

    def test_a_run_where_everything_is_unbuildable_refuses_to_open_the_db(
        self, tmp_path: Path
    ) -> None:
        """⭐ 全坏就**别连库**。

        连上去再报"入库 0 条"，看起来像跑通了——而这个项目里最危险的失败
        恰恰是"命令正常退出、报告写着成功、实际什么都没做"。
        """
        rows = [_row(url="", zhihu_id="", content_type="pin")]
        run_dir = _seed_run(tmp_path, "20260101-000000", rows)

        with pytest.raises(SystemExit, match="装不成记录"):
            cli.ingest_run(run_dir, repo=FakeRepo())

    def test_re_running_ingest_hits_the_url_constraint(self, tmp_path: Path) -> None:
        """重跑一次入库不该写第二遍——`url` 的唯一约束兜着（决策 52 的"入库能重跑"）。

        这正是拆成两趟的意义：采集中途崩了，重跑的是这一条命令，不是那一趟浏览器。
        """
        run_dir = _seed_run(tmp_path, "20260101-000000", _body_then_comments())
        repo = FakeRepo()

        cli.ingest_run(run_dir, repo=repo)
        second = cli.ingest_run(run_dir, repo=repo)

        assert second is not None
        assert second.inserted == 0
        assert second.duplicates == 3
        assert len(repo.contents) == 3


class TestCmdIngest:
    def test_a_missing_run_dir_exits_before_touching_anything(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(cli, "paths", lambda: Paths.resolve(tmp_path))
        args = cli.build_parser().parse_args(["ingest", "--run", "没有这个任务"])

        with pytest.raises(SystemExit, match="找不到任务"):
            cli.cmd_ingest(args)

    def test_dry_run_needs_no_database_at_all(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """⚠️ `--dry-run` 走内存库，所以**没配 DSN 也能跑**。

        这是它唯一的用途：在没接库的机器上把"这份产物能不能入库"先验一遍。
        真去读 DSN 的话，这个开关就变成"有库才能验"，等于没用。
        """

        def _explode() -> object:
            raise AssertionError("--dry-run 不该去要一个真仓储")

        monkeypatch.setattr(cli, "paths", lambda: Paths.resolve(tmp_path))
        monkeypatch.setattr(cli, "_repo", _explode)
        _seed_run(tmp_path, "20260101-000000", _body_then_comments())

        args = cli.build_parser().parse_args(["ingest", "--run", "20260101-000000", "--dry-run"])
        assert cli.cmd_ingest(args) == 0

    def test_without_a_dsn_it_refuses_instead_of_pretending(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """⭐ 没配库就**退出**，绝不悄悄用内存库顶上。

        内存库跑出来的"成功"是最糟的结果：它看起来跑通了，
        而数据在进程退出时一起没了。消息还要说清楚**采到的东西没丢**——
        否则下一个人会以为得重爬一遍。
        """
        monkeypatch.setattr(cli, "paths", lambda: Paths.resolve(tmp_path))
        monkeypatch.delenv("SUPABASE_DSN", raising=False)
        monkeypatch.setattr("sentinel_q.shared.config.REPO_ROOT", tmp_path)  # 别读仓库根的 .env
        _seed_run(tmp_path, "20260101-000000", _body_then_comments())

        args = cli.build_parser().parse_args(["ingest", "--run", "20260101-000000"])
        with pytest.raises(SystemExit) as caught:
            cli.cmd_ingest(args)

        message = str(caught.value)
        assert "SUPABASE_DSN" in message
        assert "contents.jsonl" in message, "要讲清楚采到的东西还在，不用重爬"
