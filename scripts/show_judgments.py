"""把 `judgments/` 里的判定和**它当时读的那段正文**摆在一起，一条一条看。

    .venv/bin/python scripts/show_judgments.py                  # 正文判定，逐条翻页
    .venv/bin/python scripts/show_judgments.py --task events    # 事件判定（按正文归并）
    .venv/bin/python scripts/show_judgments.py --task questions
    .venv/bin/python scripts/show_judgments.py --all            # 一次全打，不分页
    .venv/bin/python scripts/show_judgments.py --only 8801006 8801007

**只读。** 不写任何文件、不连模型、不碰数据库。重判的唯一方式是删掉
`judgments/` 里对应的文件（或 `judge_fixture.py --force`）。

## 为什么正文要在这里现拼

`judgments/*.jsonl` 里**没有正文**，只有 `url` / `zhihu_id` 这些回指。正文在
`answers.jsonl` / `contents.jsonl` 里，而且长正文那条（`8801006`）的正文压根不在
`content_text` 里——它走 `storage_path`，而夹具里那个文件不存在，得退回 `text`。
这条规矩和判的时候**必须是同一条**，否则你比对的是"另一段文字"，而它看起来
一模一样。所以这里直接复用 `judge_fixture._body_of`，不另写一份。

## 事件任务按正文归并，不按判定行平铺

一个议题一行的话，同一条正文会被打两遍，而这两遍**只有判定不同**——那正好是
最该并排看的东西（4.5.2 那类"同一家企业、另一桩事"的差别就在这两行之间）。
所以事件任务是 16 页，每页一条正文 + 它对着每个议题的判定。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from judge_fixture import DEFAULT_RUN, JUDGMENTS, _bodies, _body_of, _rows

WIDTH = 78

TASKS = ("contents", "events", "questions")


# ── 翻页 ────────────────────────────────────────────────────────────


class Pager:
    """一条一停，回车继续。

    ⚠️ **输出不是终端时自动退化成一次性全打**——`show_judgments.py > 判定.txt`
    或 `| less` 都该照常工作。分页是给人眼看的，不是给管道的。
    """

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    @property
    def quit(self) -> bool:
        return self._quit

    _quit = False

    def next(self) -> None:
        if not self.enabled:
            return
        try:
            answer = input("  ── 回车继续，q 退出 ── ")
        except EOFError:
            self.enabled = False
            return
        if answer.strip().lower() in ("q", "quit"):
            self._quit = True


# ── 画 ──────────────────────────────────────────────────────────────


def _rule(left: str = "") -> None:
    head = f"── {left} " if left else ""
    print(head + "─" * max(0, WIDTH - len(head)))


def _flag(value: bool | None) -> str:
    if value is None:
        return "（没判）"
    return "✔ 相关" if value else "✘ 不相关"


def _panel(fields: list[tuple[str, Any]]) -> None:
    """判定那一块。`┃` 起头是为了和上面的正文一眼分开——正文里也可能有这些词。"""
    rows = [(k, "—" if v is None or v == "" else str(v)) for k, v in fields]
    width = max(len(k) for k, _ in rows)
    print("┃")
    for key, value in rows:
        print(f"┃ {key.ljust(width)}  {value}")
    print("┃")


def _body(store: dict[str, tuple[dict, Any]], url: str) -> tuple[str, bool]:
    """按 `url` 取回判定当时读的那段正文。取不到就返回空串，由调用方报出来。"""
    pair = store.get(url)
    if pair is None:
        return "", False
    return _body_of(pair[0])


# ── 三个任务各画各的 ────────────────────────────────────────────────


def _page_question(row: dict, _store: dict, pager: Pager) -> None:
    """问题只有一项判断，而且它已经在标题行上了——再画一个面板就是同一句话说两遍。

    （也没别的字段可画：`QuestionJudgment` 上就一个 `is_relevant`。
    模型要是多返回了立场，那个值落不到这里。）
    """
    _rule(f"{row['zhihu_qid']}　{_flag(row['is_relevant'])}")
    print(f"  {row['title']}")
    print(f"  {row.get('url') or ''}")
    pager.next()


def _page_content(row: dict, store: dict, pager: Pager) -> None:
    text, fell_back = _body(store, row["url"])
    _rule(f"{row['zhihu_id']}　{row['content_type']}　{len(text)} 字　{_flag(row['is_relevant'])}")
    print(f"  {row['url']}")
    if fell_back:
        # ⚠️ 报出来，不静默：退回 text 意味着这段正文不是从线上那条路来的
        print("  ⚠️ content_text 为空，这段正文是从 text 字段退回来的（长正文分流）")
    print()
    print(text or "（取不到正文）")
    print()
    _panel(
        [
            ("is_relevant", _flag(row["is_relevant"])),
            ("platform_stance", row.get("platform_stance")),
            ("stance_confidence", row.get("stance_confidence")),
            ("risk_level", row.get("risk_level")),
            ("risk_reasoning", row.get("risk_reasoning")),
            ("ai_summary", row.get("ai_summary")),
        ]
    )
    pager.next()


def _page_event(url: str, rows: list[dict], store: dict, pager: Pager) -> None:
    text, fell_back = _body(store, url)
    head = rows[0]
    _rule(f"{head['zhihu_id']}　{head['content_type']}　{len(text)} 字　× {len(rows)} 个议题")
    print(f"  {url}")
    if fell_back:
        print("  ⚠️ content_text 为空，这段正文是从 text 字段退回来的（长正文分流）")
    print()
    print(text or "（取不到正文）")
    print()
    print("┃")
    # 议题名可能很长，单独一行；判定另起一行，不然挤成一坨看不出来差别
    for row in rows:
        print(f"┃ 议题 {row['event_id']}　{row['event_name']}")
        print(
            f"┃   {_flag(row['is_relevant'])}　立场 {row.get('stance')}"
            f"　置信 {row.get('confidence')}"
        )
    print("┃")
    pager.next()


# ── 收尾的账 ────────────────────────────────────────────────────────


def _counts(values: list[Any]) -> str:
    return "　".join(f"{k} {v}" for k, v in Counter(values).most_common())


def _tally(task: str, rows: list[dict]) -> None:
    print()
    _rule("合计")
    if task == "questions":
        print(f"  相关性　{_counts([_flag(r['is_relevant']) for r in rows])}")
        return
    if task == "contents":
        print(f"  相关性　{_counts([_flag(r['is_relevant']) for r in rows])}")
        print(f"  立场　　{_counts([r.get('platform_stance') for r in rows])}")
        print(f"  风险　　{_counts([r.get('risk_level') for r in rows])}")
        return
    # 事件：按议题拆开，不然 32 行混在一起看不出哪个议题收得多
    by_event: dict[Any, list[dict]] = {}
    for row in rows:
        by_event.setdefault((row["event_id"], row["event_name"]), []).append(row)
    for (event_id, name), group in by_event.items():
        hit = [r for r in group if r["is_relevant"]]
        print(f"  议题 {event_id}　{name}")
        # ⚠️ 立场**只统计相关的那几条**。不相关的行立场是提示词要求"照填中立"的
        #    占位值，把它算进来会让"这个议题下的立场分布"整体偏向中立——
        #    而落库时这些行根本不进 `fact_content_event`（那张表没有 is_relevant 列）。
        print(f"    相关 {len(hit)} / {len(group)}　立场 {_counts([r.get('stance') for r in hit])}")


# ── 入口 ────────────────────────────────────────────────────────────


def _load(path: Path) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"❌ 找不到 {path}\n   先跑 judge_fixture.py。")
    rows = _rows(path)
    if not rows:
        raise SystemExit(f"❌ {path} 是空的。")
    return rows


def _filters(args: argparse.Namespace, row: dict, *keys: str) -> bool:
    if not args.only:
        return True
    haystack = " ".join(str(row.get(k) or "") for k in keys)
    return any(needle in haystack for needle in args.only)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="show_judgments",
        description="把判定和它读的正文摆在一起，逐条核对。**只读。**",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="输出不是终端时自动一次全打（可以 > 文件 或 | less）。\n",
    )
    parser.add_argument("--run", default=DEFAULT_RUN, metavar="ID")
    parser.add_argument("--task", choices=TASKS, default="contents")
    parser.add_argument("--only", nargs="+", metavar="关键词", help="只显示匹配的条目")
    parser.add_argument("--all", action="store_true", help="不分页，一次全打")
    args = parser.parse_args(argv)

    run_dir = _run_dir(args.run)
    out_dir = run_dir / JUDGMENTS
    rows = _load(out_dir / f"{args.task}.jsonl")

    _header(args, run_dir, out_dir, rows)

    # 正文现拼：和判的时候走同一条路（见模块开头）。
    # ⚠️ 问题任务**不读正文**——它判的是标题和描述，读一遍只会白打一条
    #    "装不成记录"的警告（那份正文文件里那行 question 是故意留的）。
    store = (
        {}
        if args.task == "questions"
        else {record.url: (row, record) for row, record in _bodies(run_dir).pairs}
    )
    pager = Pager(enabled=not args.all and sys.stdout.isatty() and sys.stdin.isatty())

    shown = 0
    if args.task == "events":
        grouped: dict[str, list[dict]] = {}
        for row in rows:
            if _filters(args, row, "zhihu_id", "url"):
                grouped.setdefault(row["url"], []).append(row)
        for url, group in grouped.items():
            _page_event(url, group, store, pager)
            shown += len(group)
            if pager.quit:
                break
        kept = [r for g in grouped.values() for r in g]
    else:
        drawer = _page_question if args.task == "questions" else _page_content
        kept = []
        for row in rows:
            if not _filters(args, row, "zhihu_id", "zhihu_qid", "url", "title"):
                continue
            drawer(row, store, pager)
            kept.append(row)
            shown += 1
            if pager.quit:
                break

    _tally(args.task, kept)
    if shown != len(rows):
        print(f"\n  （共 {len(rows)} 条，显示了 {shown} 条）")
    return 0


def _run_dir(run_id: str) -> Path:
    from sentinel_q.shared.config import paths

    run_dir = paths().ops / run_id
    if not (run_dir / "task.json").exists():
        raise SystemExit(f"❌ 找不到任务目录 {run_dir}")
    return run_dir


def _header(args: argparse.Namespace, run_dir: Path, out_dir: Path, rows: list[dict]) -> None:
    _rule(f"{args.task}　{args.run}")
    print(f"  {out_dir}")
    meta_path = out_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        # ⚠️ 这两项要打出来：判定长什么样由它们决定（决策 47），
        #    看到怪结果时第一个要确认的就是"这是哪版提示词、哪个模型判的"。
        print(f"  模型 {meta.get('model')}　提示词 {str(meta.get('prompt_version'))[:12]}"
              f"　并发 {meta.get('concurrency')}")
    print(f"  {len(rows)} 条判定")
    versions = {r.get("prompt_version") for r in rows}
    if len(versions) > 1:
        print(f"  ⚠️ 这批判定混了 {len(versions)} 个提示词版本：{sorted(map(str, versions))}")
        print("     落库以后分不清哪条是哪版判的（决策 47 的追溯就断了）")


if __name__ == "__main__":
    sys.exit(main())
