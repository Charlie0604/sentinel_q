"""入口测试。

⚠️ **这里只测"命令怎么被解析成一次调用"，绝不真的调用。**

`sentinel_q.collector` 的每一个子命令都会打开一个真实浏览器、用真实账号访问知乎。
所以这个文件里的测试全部只碰三样东西：

  1. `_is_probe()` —— `probe` 的参数转发判断（纯字符串运算）
  2. `build_parser()` 的**参数校验** —— 缺必填项时报不报错
  3. `cmd_content` 的**编排顺序**与**落盘产物** —— 谁先谁后、开关开了才走哪条路、
     `contents.jsonl` 里写进去的是什么
     （每一个会碰浏览器的函数都被换成假的，见 `_Run`）

⚠️ **落盘那部分用的是真 `OpsStore`、真 `_ContentsDoc`**，只把目录指到 `tmp_path`——
`contents.jsonl` 的格式和去重是这次要交付的东西，用假对象测等于没测。
每次跑测试都会往仓库的 `runtime/ops/` 里新建一个任务目录（那些目录不入 git，
但堆着也难受），所以 `cli.paths` 一律指到 tmp。

真正的采集逻辑由各模块自己的测试负责（那些用快照固件，不用浏览器）。
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path

import pytest

from sentinel_q.collector import __main__ as cli
from sentinel_q.collector import answers, comments, content, extract, ops, parse
from sentinel_q.shared.config import Paths

# ── probe 的参数转发 ────────────────────────────────────────────────


class TestIsProbe:
    """⭐ 这一组防的是一个**具体的、已经踩过的坑**。

    最初用 `nargs=argparse.REMAINDER` 转发 `probe` 的参数，结果
    `probe --list` 被顶层解析器报成 `unrecognized arguments: --list`——
    因为 REMAINDER 碰到以 `-` 开头的 token 就停止捕获，
    而 `probe` 的**每一个**参数都是 `-` 开头的。

    改完之后 `probe` 在 argparse 之前就被拦下来。下面钉住两个方向：
    该拦的拦住，**不该拦的绝不误拦**。
    """

    def test_plain_probe_is_forwarded(self) -> None:
        assert cli._is_probe(["probe"]) == 0

    def test_probe_flags_are_forwarded(self) -> None:
        """`--list` / `--check` 这些全是以 `-` 开头的，一个都不能掉。"""
        assert cli._is_probe(["probe", "--list"]) == 0
        assert cli._is_probe(["probe", "--check", "--force"]) == 0
        assert cli._is_probe(["probe", "--only", "answer,article"]) == 0

    def test_global_verbose_does_not_hide_probe(self) -> None:
        assert cli._is_probe(["-v", "probe", "--list"]) == 1

    def test_keyword_named_probe_is_not_hijacked(self) -> None:
        """⭐ 一个**恰好叫 "probe" 的关键词**不能被当成子命令。

        用 `"probe" in argv` 判断就会踩这个坑：`search --keyword probe`
        会整个被转发给 probe.py，然后**拿着 `--keyword probe` 去开浏览器**。
        所以判断的是"第一个位置参数"，不是"有没有出现过"。
        """
        assert cli._is_probe(["search", "--keyword", "probe"]) is None

    def test_url_containing_probe_is_not_hijacked(self) -> None:
        assert cli._is_probe(["content", "--url", "https://www.zhihu.com/pin/probe"]) is None

    def test_other_subcommands_are_not_probe(self) -> None:
        for command in ("search", "content", "comments", "answers", "status"):
            assert cli._is_probe([command]) is None, command

    def test_empty_argv_is_not_probe(self) -> None:
        assert cli._is_probe([]) is None


# ── 子命令与参数校验（只解析，不执行）──────────────────────────────


class TestParser:
    def test_all_subcommands_are_listed(self) -> None:
        """验收项：`--help` 能列出全部子命令（计划里的验收条件）。"""
        parser = cli.build_parser()
        actions = [
            action
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        assert len(actions) == 1
        assert set(actions[0].choices) == {
            "search",
            "content",
            "comments",
            "answers",
            "probe",
            "status",
        }

    def test_search_needs_a_keyword(self) -> None:
        """⭐ `search` **没有默认关键词**，而且这是刻意的。

        有默认值的话，一次忘了传参的调用会静默地拿着某个词去采回来一堆
        别的东西——采到的条数正常、URL 合法、只不过内容是无关的。
        缺参数应当报错，然后由人来决定搜什么。
        """
        args = cli.build_parser().parse_args(["search"])
        assert args.keyword is None
        with pytest.raises(SystemExit, match="没有关键词"):
            cli.cmd_search(args)

    def test_search_keyword_accumulates(self) -> None:
        args = cli.build_parser().parse_args(
            ["search", "--keyword", "甲", "--keyword", "乙"]
        )
        assert args.keyword == ["甲", "乙"]

    def test_answers_needs_a_question(self) -> None:
        """也顺带钉住**校验顺序**：命令行的错必须在"碰环境"之前报出来。

        反过来的话，忘了给 `--question` 的人会先看到浏览器起不来，
        跑去查浏览器、再跑一遍，才发现是命令行的问题——而那是两件无关的事。
        """
        args = cli.build_parser().parse_args(["answers"])
        with pytest.raises(SystemExit, match="没给问题 ID"):
            cli.cmd_answers(args)

    def test_content_and_comments_need_a_url(self) -> None:
        parser = cli.build_parser()
        with pytest.raises(SystemExit, match="没给 URL"):
            cli.cmd_content(parser.parse_args(["content"]))
        with pytest.raises(SystemExit, match="没给 URL"):
            cli.cmd_comments(parser.parse_args(["comments"]))

    def test_mode_only_exists_where_it_means_something(self) -> None:
        """`--mode` 只出现在有口径之分的能力上。

        全量/更新的区别只对**搜索**和**问题回答**有意义（排序与时间范围），
        对"打开一条 URL 取正文"没有意义。给它也加一个 `--mode` 会让人以为
        改它有效果。
        """
        parser = cli.build_parser()
        assert parser.parse_args(["search", "--mode", "update"]).mode == "update"
        assert parser.parse_args(["answers", "--mode", "update"]).mode == "update"
        with pytest.raises(SystemExit):
            parser.parse_args(["content", "--mode", "update"])

    def test_the_collector_has_no_database_switch_at_all(self) -> None:
        """⭐ 采集模块**一个库开关都没有**（决策 52）。

        以前有 `--no-db`：默认关，给了就用内存库冒烟跑。那个开关现在没有
        意义了——**这个模块压根不连库**，采到的东西只有 `contents.jsonl`
        一条出路，由主程序统一入库。

        ⚠️ 这条不只是"参数没了"，它盯住的是一个**曾经踩过的坑**：
        库配错时那个开关会让人以为"用 --no-db 跑通了就等于流程没问题"，
        而实际跑通的是另一条路。现在没有第二条路了。

        真正的机械检查在 `tests/test_layering.py`（扫 `psycopg` / SQL），
        这里只管命令行那一面。
        """
        parser = cli.build_parser()
        for command in ("search", "content", "comments", "answers"):
            assert not hasattr(parser.parse_args([command]), "no_db"), command
            with pytest.raises(SystemExit):
                parser.parse_args([command, "--no-db"])

    def test_verbose_is_global(self) -> None:
        assert cli.build_parser().parse_args(["-v", "status"]).verbose is True
        assert cli.build_parser().parse_args(["status"]).verbose is False


# ── content 子命令的编排：评论是个可选项 ────────────────────────────


ANSWER = "https://www.zhihu.com/question/123/answer/456"

SECOND_ANSWER = "https://www.zhihu.com/question/123/answer/789"
"""第二个回答。⭐ **URL 必须和 `ANSWER` 不同**——能力四是按 URL 推作用域的，
两条回答共用一个地址的话，"逐条采"会退化成"反复采同一条"，而测试看不出来。"""


class _FakeBrowserSession:
    """够 `cmd_content` 用的假会话——它只用 `with` / `ensure_logged_in` / `pace`。"""

    def __init__(self, config) -> None:
        self.config = config

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def ensure_logged_in(self) -> None:
        pass

    def pace(self) -> None:
        pass


class _Run:
    """一次假的 `cmd_content`：记录谁被调用、按什么顺序、入库的又是什么。

    `calls` 记的是**调用顺序**，`ingested` 记的是**入库顺序**——两者是分开的
    两件事，而且都各有一条硬约束（见下面两个测试）。

    ⚠️ `_open_run` **没有换成假的**：任务目录、`task.json`、`contents.jsonl`
    都是这次要交付的真东西。只把 `cli.paths` 指到 tmp，别让它写进仓库。
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.comment_kwargs: dict = {}
        self.ingested: list[parse.ParsedItem] = []
        self.content_fails = False
        self.comments_fail = False
        self.answer = _answer_item()

    def install(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(cli, "paths", lambda: Paths.resolve(tmp_path))
        monkeypatch.setattr(cli, "_require_calibrated", lambda: None)
        monkeypatch.setattr(cli, "BrowserSession", _FakeBrowserSession)
        # ⚠️ 必须是**真**策略，不能是一个 `object()` 占位：`to_document` 现在
        #    自己调 `store()` 落盘（决策 52），而它会读 `inline_limit`、往
        #    `policy.root` 写文件。占位对象会让每一条落盘用例都 AttributeError。
        #    指到 tmp 是为了别把快照写进仓库的 `runtime/`。
        monkeypatch.setattr(
            cli, "_policy", lambda: extract.SnapshotPolicy(root=tmp_path / "snapshots")
        )
        monkeypatch.setattr(cli, "_page_html", lambda session: "")
        monkeypatch.setattr(cli, "_cross_check", self._cross_check)
        monkeypatch.setattr(content, "extract_content", self._content)
        monkeypatch.setattr(comments, "extract_comments", self._comments)

    def run(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *argv: str) -> int:
        self.install(monkeypatch, tmp_path)
        args = cli.build_parser().parse_args(["content", "--url", ANSWER, *argv])
        return cli.cmd_content(args)

    def _cross_check(self, items, *, page_html=None, what="") -> int:
        """站位:交叉验证在入库搬走之后是**唯一**按批发生的事。

        这里记的是"验证过哪些条、按什么顺序"——`ingested` 这个名字保留着，
        因为它测的那条约束（顺序）一点没变。
        """
        self.ingested.extend(items)
        return 0

    def _content(self, session, url):
        self.calls.append("content")
        item = None if self.content_fails else self.answer
        return content.ContentReport(url=url, item=item)

    def _comments(self, session, url, **kwargs):
        self.calls.append("comments")
        self.comment_kwargs = kwargs
        items = [_comment_item(zhihu_id) for zhihu_id in ("c1", "c2", "c3")]
        # 复刻真实现的逐条回调——入库顺序就从这儿来
        for item in items:
            if kwargs.get("on_item"):
                kwargs["on_item"](item)
        return comments.CommentsReport(url=url, opened=not self.comments_fail, items=items)


class TestContentWithComments:
    """⭐ 这一组钉住的是**评论是可选项**。

    `cmd_content` 是唯一把两个能力串起来的地方，所以"不点评论按钮"这件事
    只有在这一层才验得出来：不给 `--with-comments` 时，`extract_comments`
    必须**一次都没被调用**。评论是最贵的动作（开弹窗、滚几百下、逐条展开），
    将来整合时"什么条件下才采评论"就写在这个循环里。
    """

    def test_without_the_flag_comments_are_never_touched(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        run = _Run()
        assert run.run(monkeypatch, tmp_path) == 0

        assert run.calls == ["content"], "没给开关就该只有正文那一次调用"
        assert run.comment_kwargs == {}
        assert [i.zhihu_id for i in run.ingested] == ["456"]

    def test_comments_run_after_the_body_is_ingested(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """顺序是硬的：正文 → 正文入库 → 评论 → 评论入库。

        ⚠️ 一级评论的 `parent_id` 指向正文那一行的 uuid，而 `extract.IdIndex`
        只认"本次运行里插过的父级"——正文没先入库，评论的 `parent_id` 全挂空，
        整棵对话结构**塌成平的，而且不报错**。所以断言的是 `ingested` 的顺序，
        不只是"两个都调了"。
        """
        run = _Run()
        assert run.run(monkeypatch, tmp_path, "--with-comments") == 0

        assert run.calls == ["content", "comments"]
        assert run.comment_kwargs["reuse_open_page"] is True, (
            "正文刚打开过这条 URL，评论不该再导航一次"
        )
        assert [i.zhihu_id for i in run.ingested] == ["456", "c1", "c2", "c3"]

    def test_a_body_that_was_not_collected_skips_comments(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """正文没取到就没有父级可挂，评论也一并跳过。

        照样开弹窗去采的话，这一批评论的 `parent_id` 会全指向空气。
        """
        run = _Run()
        run.content_fails = True

        assert run.run(monkeypatch, tmp_path, "--with-comments") == 1
        assert run.calls == ["content"]
        assert run.ingested == []

    def test_comment_failures_are_not_hidden_by_a_healthy_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """正文全成功、评论全失败时，退出码仍要非 0。

        评论漏采是实打实的证据缺失，被正文的成功盖过去就没人会去看。
        """
        run = _Run()
        run.comments_fail = True

        assert run.run(monkeypatch, tmp_path, "--with-comments") == 1

    def test_a_url_already_collected_skips_both(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """⭐ `_dedup` 挡掉的 URL，正文和评论同进同退，**一条都不采**。

        挡它的是"这个任务的 `contents.jsonl` 里已经有这一条了"——
        现在是那份产物在当去重台账（决策 51 的"文件 ∪ 库"里"文件"那一半；
        "库"那一半只有主程序给得出来，见 `_dedup` 的说明）。
        """
        run = _Run()
        _seed_run(tmp_path, "20260101-000000", [ops.UrlEntry(url=ANSWER)])
        _seed_contents(tmp_path, "20260101-000000", [{"url": ANSWER, "zhihu_id": "456"}])

        code = run.run(monkeypatch, tmp_path, "--with-comments", "--run", "20260101-000000")

        assert code == 0
        assert run.calls == [], "已经采过就不该再开浏览器"
        assert run.ingested == []


def _answer_item(
    *,
    text: str = "正文",
    excerpt: str | None = None,
    zhihu_id: str | None = "456",
    url: str = ANSWER,
) -> parse.ParsedItem:
    return parse.ParsedItem(
        url=url,
        content_type="answer",
        zhihu_id=zhihu_id,
        question_id="123",
        text=text,
        excerpt=excerpt,
        author_name="某人",
        author_url="https://www.zhihu.com/people/someone",
    )


def _comment_item(zhihu_id: str) -> parse.ParsedItem:
    return parse.ParsedItem(
        url=f"https://www.zhihu.com/comment/{zhihu_id}",
        content_type="comment",
        zhihu_id=zhihu_id,
        question_id=None,
        parent_id="456",
        text="评论",
        author_name="某人",
        author_url="https://www.zhihu.com/people/someone",
    )


# ── contents.jsonl：落盘的是**产物**，不是状态 ──────────────────────


def _ops_dir(root: Path) -> Path:
    return root / "runtime" / "ops"


def _contents_rows(root: Path) -> list[dict]:
    """把这次任务落盘的正文读回来。**断言的是文件，不是内存里的对象。**

    一条都没采到的时候文件**根本不会创建**（`append_contents` 只在有东西可写时
    才开文件），所以这里按"至多一个"处理。
    """
    files = sorted(_ops_dir(root).glob("*/contents.jsonl"))
    assert len(files) <= 1, f"应该有至多一次任务目录，实际 {files}"
    if not files:
        return []
    return [json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()]


def _seed_run(root: Path, run_id: str, entries: list[ops.UrlEntry]) -> None:
    """先造一个能力一跑完的任务目录，给 `--run` 用。"""
    run_dir = _ops_dir(root) / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "task.json").write_text(
        json.dumps({"run_id": run_id, "mode": "backfill", "status": "done"}),
        encoding="utf-8",
    )
    (run_dir / "urls.jsonl").write_text(
        "".join(json.dumps(asdict(entry), ensure_ascii=False) + "\n" for entry in entries),
        encoding="utf-8",
    )


def _seed_contents(root: Path, run_id: str, rows: list[dict]) -> None:
    """往某个任务的 `contents.jsonl` 里预置几行——**去重台账就是这个文件**。

    只写 `url` / `zhihu_id` 就够：`_ContentsDoc` 按
    `(content_type, zhihu_id)` 挡重复写、按 `url` 挡重复采。
    """
    path = _ops_dir(root) / run_id / "contents.jsonl"
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


class TestContentsDocument:
    """⭐ 能力二的产物：每采完一条就追加进 `contents.jsonl`。

    它是"采集 → 分析"的衔接面（决策 51）：AI 判定结果与断点标记都写在同一行。
    它必须能重建，见 `ops.py` 的模块开头。
    """

    def test_the_body_lands_before_its_comments(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """文档里的顺序**就是入库顺序**：正文在前，它的评论随后。

        两头的顺序必须一致，否则核对的人对着文件找库里的行会对不上。
        """
        run = _Run()
        assert run.run(monkeypatch, tmp_path, "--with-comments") == 0

        rows = _contents_rows(tmp_path)
        assert [r["zhihu_id"] for r in rows] == ["456", "c1", "c2", "c3"]
        assert [r["content_type"] for r in rows] == [
            "answer",
            "comment",
            "comment",
            "comment",
        ]
        assert rows[1]["parent_id"] == "456", "评论挂的是**知乎那边的**内容 ID"

    def test_the_document_carries_the_full_text_not_the_excerpt(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """⚠️ 落盘的是**完整正文**，不是 `urls.jsonl` 里那种被截断的 `excerpt`。

        库里为了省额度会把长正文分流到快照文件，`content_text` 留空；
        这份文档给人看，必须直接读得出来。所以这一行里**两个都在**：
        `text` 是完整正文，`content_text` 是真正要进库的那一列。
        """
        run = _Run()
        run.answer = _answer_item(text="很长的正文" * 50, excerpt="很长的正文…")

        assert run.run(monkeypatch, tmp_path) == 0

        row = _contents_rows(tmp_path)[0]
        assert row["text"] == "很长的正文" * 50
        assert row["text_length"] == len("很长的正文" * 50)
        assert row["raw_content_hash"], "要有原文哈希，重抓时才比得出对方改没改过"
        assert row["snapshot_path"] is None, "这条没有 html，就没有快照"
        assert row["collected_at"], "人工核对时要看得出这是哪一轮采的"

    def test_the_keyword_rides_along_from_the_url_list(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`urls.jsonl` 里的关键词要一路带到文档里。

        不带上就说不清"这条是哪次搜索捞出来的"——人工核对时最常问的就是这个。
        """
        run = _Run()
        _seed_run(tmp_path, "20260101-000000", [ops.UrlEntry(url=ANSWER, keyword="某公司")])

        assert run.run(monkeypatch, tmp_path, "--run", "20260101-000000") == 0

        assert _contents_rows(tmp_path)[0]["keyword"] == "某公司"

    def test_a_body_collected_twice_is_written_once(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """⚠️ 同一个 `(content_type, zhihu_id)` **不写第二遍**。

        验收流程本身就是「先 `--limit 20` 冒烟、再全量」——两次跑的是同一批 URL，
        不去重的话前 20 条会各留两行，"行数 == 清单条数"这条验收判据当场失效。

        ⚠️ 挡的是**重复写**，不是重复采：第二次照样会去爬（浪费几十分钟，
        但结果正确）。这份文档不该有能力影响采集行为——那正是它作为
        "可重建的产物"和"第二个真相源"之间的分界线。
        """
        run = _Run()
        _seed_run(tmp_path, "20260101-000000", [ops.UrlEntry(url=ANSWER)])
        assert run.run(monkeypatch, tmp_path, "--run", "20260101-000000") == 0
        first = _contents_rows(tmp_path)

        assert run.run(monkeypatch, tmp_path, "--run", "20260101-000000") == 0
        assert _contents_rows(tmp_path) == first, "重跑一遍，行数不该变"

    def test_a_failed_body_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """正文没取到的 URL **不留行**——留一行空的比没有更难核对。"""
        run = _Run()
        run.content_fails = True

        assert run.run(monkeypatch, tmp_path) == 1
        assert _contents_rows(tmp_path) == []


# ── 能力四：只采回答，采到一条就落盘 ────────────────────────────────


class _QuestionRun:
    """一次假的 `cmd_answers`：记录回答怎么被回调、落到了哪里。

    ⚠️ **评论这一趟在 2026-09-27 被整个删掉了**（项目所有者定的，理由是
    问题页上评论入口的分支判据不稳、而评论是全系统最贵的动作）。所以这个
    假对象的头号职责反过来：盯住 `comments` 里的**任何**采集函数都不许被
    调到。删干净和"没配开关所以没走"在日志上长得一样，只有这里分得清。
    """

    def __init__(self) -> None:
        self.comment_calls: list[str] = []
        self.answers_items = [_answer_item(), _answer_item(zhihu_id="789", url=SECOND_ANSWER)]
        # 页面那行「N 个回答」声明了多少。默认跟着采到的条数走（= 采全了），
        # 要造"没采全"的场景就把它调高。
        self.declared: int | None = None

    def install(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(cli, "paths", lambda: Paths.resolve(tmp_path))
        monkeypatch.setattr(cli, "_require_calibrated", lambda: None)
        monkeypatch.setattr(cli, "BrowserSession", _FakeBrowserSession)
        # ⚠️ 必须是**真**策略，不能是一个 `object()` 占位：`to_document` 现在
        #    自己调 `store()` 落盘（决策 52），而它会读 `inline_limit`、往
        #    `policy.root` 写文件。占位对象会让每一条落盘用例都 AttributeError。
        #    指到 tmp 是为了别把快照写进仓库的 `runtime/`。
        monkeypatch.setattr(
            cli, "_policy", lambda: extract.SnapshotPolicy(root=tmp_path / "snapshots")
        )
        monkeypatch.setattr(cli, "_page_html", lambda session: "")
        monkeypatch.setattr(cli, "_cross_check", lambda *a, **k: 0)
        monkeypatch.setattr(answers, "extract_answers", self._answers)
        monkeypatch.setattr(comments, "extract_answer_comments", self._refuse_comments)
        monkeypatch.setattr(comments, "extract_comments", self._refuse_comments)

    def run(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *argv: str) -> int:
        self.install(monkeypatch, tmp_path)
        args = cli.build_parser().parse_args(
            ["answers", "--question", "123", *argv]
        )
        return cli.cmd_answers(args)

    def _answers(self, session, spec, **kwargs):
        report = answers.AnswersReport(
            question_id=spec.question_id, url=spec.url, mode=spec.mode
        )
        # ⚠️ 逐条回调，**复刻真实现**：落盘是在采到的那一刻发生的，
        #    不是等 `extract_answers` 返回之后补一遍。在这里攒着最后一起发，
        #    测出来的顺序就永远是对的，也就测不到"中途崩了全没了"。
        for item in self.answers_items:
            if kwargs.get("on_item"):
                kwargs["on_item"](item)
        report.items = list(self.answers_items)
        # `ok` 要求条数对得上（`complete is True`）——不然 `_verdict` 直接给非 0，
        # 这一组的断言就全被退出码挡在外面了
        report.declared = (
            len(report.items) if self.declared is None else self.declared
        )
        return report

    def _refuse_comments(self, *_args, **_kwargs):
        self.comment_calls.append("comments")
        raise AssertionError(
            "能力四 2026-09-27 起**不采评论**了。要评论请走能力二"
            "（content --with-comments / comments 子命令）。"
        )


class TestAnswersDocument:
    """⭐ 能力四的产物：跟能力二一样，采到一条就写进 `contents.jsonl`。

    以前能力四只有入库那条路，于是 `--no-db`（实跑冒烟一直是这么跑的）
    跑完**什么都不剩**——采了几十分钟、界面上一片正常、产物是空的。
    """

    def test_every_collected_answer_lands_in_the_document(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        run = _QuestionRun()
        assert run.run(monkeypatch, tmp_path) == 0

        rows = _contents_rows(tmp_path)
        assert [r["zhihu_id"] for r in rows] == ["456", "789"]
        assert {r["content_type"] for r in rows} == {"answer"}
        assert rows[0]["text"] == "正文", "落的是完整正文"
        assert rows[0]["keyword"] is None, (
            "回答不是搜出来的，`urls.jsonl` 那套关键词在这里没有来源"
        )

    def test_a_rerun_does_not_write_the_same_answer_twice(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """⚠️ 验收流程是「先跑一次、再跑一次全量」，两次撞的是同一批回答。"""
        run = _QuestionRun()
        _seed_run(tmp_path, "20260101-000000", [])
        assert run.run(monkeypatch, tmp_path, "--run", "20260101-000000") == 0
        first = _contents_rows(tmp_path)

        assert run.run(monkeypatch, tmp_path, "--run", "20260101-000000") == 0
        assert _contents_rows(tmp_path) == first, "重跑一遍，行数不该变"

    def test_the_document_lands_in_the_run_it_was_given(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`--run` 指到哪，回答就写进哪个任务目录（跟能力二同一条口径）。

        ⚠️ 能力四常常是接着一次能力一的任务跑的，写错目录就等于**验收时
        在清单旁边找不到产物**，而那两样本来就是对着看的。
        """
        run = _QuestionRun()
        _seed_run(tmp_path, "20260101-000000", [])

        assert run.run(monkeypatch, tmp_path, "--run", "20260101-000000") == 0

        assert sorted(_ops_dir(tmp_path).iterdir()) == [
            _ops_dir(tmp_path) / "20260101-000000"
        ], "不该另开一个任务目录"
        assert _contents_rows(tmp_path), "产物要落在指定的那个任务里"

    def test_an_answer_that_was_never_collected_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """一条都没采到就**不留文件**——空文件比没有更难核对。

        ⚠️ 声明数要留着（页面确实说了有 15 条），否则「0 采到 / 0 声明」
        会被核算判成**采全了**，退出码 0，这一条就测不到东西了。
        """
        run = _QuestionRun()
        run.answers_items = []
        run.declared = 15

        assert run.run(monkeypatch, tmp_path) == 1, "没采全要报失败"
        assert _contents_rows(tmp_path) == []


class TestAnswersNeverCollectsComments:
    """⭐ 2026-09-27 的删除：能力四**只采回答**。

    删掉的是一整条支路（`--with-comments` 开关、`_crawl_answer_comments`、
    `_crawl_comments` 的 `answer_id` 分支），不是"默认关着"。这个区别很要紧：
    默认关着的开关会在某次调参时被重新打开，而注释里那句理由是当时才想起来的。
    """

    def test_no_comment_collector_is_ever_reached(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        run = _QuestionRun()
        assert run.run(monkeypatch, tmp_path) == 0
        assert run.comment_calls == []

    def test_the_old_flag_is_refused_rather_than_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`--with-comments` 现在**报错**，不是被静静吃掉。

        悄悄忽略的坏处：跑的人以为评论采上了，日志里一条评论都没有，
        而退出码是 0。宁可当场解析失败。
        """
        args = ["answers", "--question", "123", "--with-comments"]
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(args)
