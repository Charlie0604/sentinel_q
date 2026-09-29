"""用**真模型**把夹具判一遍——模块2（`analyst`）单独跑，产物落给模块3 用。

    .venv/bin/python scripts/judge_fixture.py --dry-run      # 先看要发多少请求、prompt 长什么样
    .venv/bin/python scripts/judge_fixture.py                # 真判（花钱）
    .venv/bin/python scripts/judge_fixture.py --only events  # 只跑一个任务

## ⚠️ 这个脚本会真的调模型、真的花钱

它**不是 pytest 测试**：`pyproject.toml` 里 `testpaths = ["tests", "src"]`，
所以 `scripts/` 不在收集范围里——`pytest` 永远不会跑到它。
这是刻意的：把它写成测试函数的话，某天有人敲一个 `pytest` 就会顺手花掉一笔钱，
而且离线环境下必然红。跑不跑、什么时候跑，由你决定。

## 三个任务各判什么

| 步骤 | 输入 | 判什么 | 产物 |
|---|---|---|---|
| `questions` | `questions.jsonl` | 问题**是否与监测对象**相关 | `judgments/questions.jsonl` |
| `contents` | `answers.jsonl` + `contents.jsonl` | 正文：相关 + 立场 + 风险 + 摘要 | `judgments/contents.jsonl` |
| `events` | 上面两份正文 × `events.jsonl` 每个议题 | **是否与这个议题相关** + 相对议题的立场 | `judgments/events.jsonl` |

⚠️ **问题和事件判的不是一回事。** 问题那一层问的是"跟监测对象有关吗"；
事件那一层问的是"说的是不是**这一桩事**"——议题是人工确认过的，
"跟监测对象有关"已经定了。`events.jsonl` 里第二个议题（5 月高钙奶）就是
为这件事造的：同一家企业、另一桩事，是这一层最容易判错的那一类。

⚠️ **问题不参与事件判定。** 决策 29 说问题只进 `dim_question`，
而 `fact_content_event.content_id` 是 `references fact_content(content_id)` 的外键——
问题根本没有 `content_id` 可挂。所以"这篇文章或回答或问题是否跟事件相关"这句里，
**"问题"那一项在表结构上就落不下来**。

## 产物是给模块3 用的

`judgments/*.jsonl` 的每一行都带了 `model_version` / `prompt_version`
（决策 47），落库时原样写进 `fact_analysis` / `fact_content_event`。
`events` 那一步里 `is_relevant: false` 的行**照样记下来**（它证明了哪些被挡掉了），
但落库时**不能插进 `fact_content_event`**——那张表没有 `is_relevant` 列，
插进去就等于把"其实不相关"记成"相关且中立"。

## 断点与重跑

判完一条就落盘（决策 39），所以中途 Ctrl-C 不白花钱：重跑会**跳过已经判过的**。
断点靠 `prompt_version` 认——如果输出文件里存在**别的**提示词版本判出来的行，
脚本会当场停下（那是两份提示词的判断混在一个文件里），`--force` 把旧文件改名备份后重来。

## ⚠️ 候选集是"全量 × 全量"，比线上宽

4.5.2 的 `pg_trgm` 预筛还没写，所以 `events` 这一步是**每条正文 × 每个议题**都判，
不是"预筛命中的才判"。对功能测试来说这反而更干净（不用先信一个没测过的预筛），
但它比线上多花钱，条目数按 `正文数 × 议题数` 走。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sentinel_q.analyst import judge_content, judge_event, judge_question
from sentinel_q.analyst.client import HttpLLMClient
from sentinel_q.collector import ops
from sentinel_q.collector.extract import from_document
from sentinel_q.shared.config import Settings, paths
from sentinel_q.shared.models import ContentJudgment, ContentRecord, PromptBundle
from sentinel_q.shared.prompts import bundle_from_workdir

log = logging.getLogger("judge_fixture")

DEFAULT_RUN = "20260929-090000-fixture"
JUDGMENTS = "judgments"
STEPS = ("questions", "contents", "events")


# ── 产物文件 ────────────────────────────────────────────────────────


def _stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


class Output:
    """一份判定结果的落盘。**一行一条、写完即 flush**（决策 39）。

    ⚠️ 写盘要加锁：`batch.judge_many` 是并发回调，多个线程会同时进来。
    每次 `open` / `write` / `flush` / `close` 都在锁里，避免两行交错写坏。
    """

    def __init__(
        self,
        path: Path,
        *,
        key: Callable[[dict], str],
        bundle: PromptBundle,
        force: bool = False,
    ) -> None:
        self.path = path
        self.key = key
        self.rows: dict[str, dict] = {}
        self._lock = threading.Lock()

        if not path.exists():
            return
        existing = _rows(path)
        stale = [r for r in existing if r.get("prompt_version") != bundle.content_hash]
        if stale and not force:
            raise SystemExit(
                f"❌ {path} 里有 {len(stale)} 行是**别的提示词版本**判出来的"
                f"（文件里有 {len({r.get('prompt_version') for r in stale})} 个版本，"
                f"当前工作区是 {bundle.content_hash[:8]}）。\n"
                "   两份提示词的判断混在一个文件里，落库以后分不清哪条是哪版判的——"
                "而 `prompt_version` 正是唯一的追溯依据（决策 47）。\n"
                "   --force 会把旧文件改名备份（不删）后重判。"
            )
        if stale:
            backup = path.with_name(f"{path.name}.bak.{_stamp()}")
            path.rename(backup)
            log.warning("旧提示词的 %d 行已备份到 %s，这个文件从头开始", len(existing), backup.name)
            return
        self.rows = {key(r): r for r in existing}

    def has(self, item_key: str) -> bool:
        return item_key in self.rows

    def write(self, row: dict) -> None:
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                fh.flush()
            self.rows[self.key(row)] = row


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # 崩在最后一行会留下半截 JSON
        if isinstance(row, dict):
            out.append(row)
    return out


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ── 输入 ────────────────────────────────────────────────────────────


@dataclass
class Bodies:
    """两份正文文件读进来的结果。**只读一遍**，正文和事件两步共用。"""

    pairs: list[tuple[dict, ContentRecord]]
    seen: int
    """文件里的总行数，含**装不成记录**的那些——差值就是 `from_document` 挡掉的。"""


def _bodies(run_dir: Path) -> Bodies:
    """两份正文文件 → `(文档行, 记录)`。**装不成记录的跳过。**

    ⚠️ 不做 `content_type` 过滤：`contents.jsonl` 里那行 `question` 是**故意留的**，
    让它去撞 `from_document` 的闸门（`FACT_CONTENT_TYPES` 里没有问题）。
    过滤掉的话就看不出那道闸门还活着没有。

    ⚠️ **只调一次。** 每调一次 `from_document` 就会为同一行再打一遍"装不成记录"
    的警告——正文那步和事件那步各调一次的话，同一句警告会打两遍，
    读起来像有两行坏数据。
    """
    store = ops.OpsStore.resume(run_dir)
    pairs: list[tuple[dict, ContentRecord]] = []
    seen = 0
    for name in ops.OpsStore.BODY_FILES:
        for row in store.iter_contents(name=name):
            seen += 1
            record = from_document(row)
            if record is None:
                continue
            pairs.append((row, record))
    return Bodies(pairs=pairs, seen=seen)


def _body_of(row: dict) -> tuple[str, bool]:
    """取正文，返回 `(正文, 是不是退回了 text)`。

    ⚠️ 长正文的正文在 `storage_path` 指的那个文件里、`content_text` 是空的
    （决策 32 提到的那个真实缺陷）。**真实的主程序在这里要把文件读回来**；
    夹具里那个文件不存在（README「快照文件本体」那条），所以退回 `text`
    ——采集时写进去的完整正文，内容是对的，只是走的不是线上那条路。
    退回的次数会报出来，不静默。
    """
    inline = (row.get("content_text") or "").strip()
    if inline:
        return inline, False
    return (row.get("text") or "").strip(), bool(row.get("storage_path"))


def _event_briefs(run_dir: Path) -> list[judge_event.EventBrief]:
    """`events.jsonl` → `EventBrief`。这是人工建的那张表，采集侧产不出来。"""
    out = []
    for row in _rows(run_dir / "events.jsonl"):
        out.append(
            judge_event.EventBrief(
                event_id=str(row["event_id"]),
                version=int(row.get("version", 1)),
                name=row.get("name") or "",
                summary=row["summary"],
                start_date=_date(row.get("start_date")),
                end_date=_date(row.get("end_date")),
                keywords=tuple(row.get("keywords") or ()),
            )
        )
    return out


def _date(value: Any) -> date | None:
    return date.fromisoformat(value) if value else None


# ── 账 ──────────────────────────────────────────────────────────────


@dataclass
class Plan:
    """一个步骤打算干什么。`--dry-run` 打印的就是它，真跑时也照它报账。"""

    name: str
    total: int
    todo: int
    skipped: int = 0
    unusable: int = 0
    fell_back: int = 0

    def describe(self) -> str:
        parts = [f"{self.total} 条"]
        if self.skipped:
            parts.append(f"跳过已判 {self.skipped}")
        if self.unusable:
            parts.append(f"装不成记录 {self.unusable}")
        if self.fell_back:
            parts.append(f"长正文退回 text {self.fell_back}")
        return f"{self.name}：要发 {self.todo} 次请求（{'，'.join(parts)}）"


# ── 三个步骤 ────────────────────────────────────────────────────────


def plan_questions(run_dir: Path, out: Output) -> tuple[Plan, list]:
    rows = _rows(run_dir / "questions.jsonl")
    pendings = [
        judge_question.PendingQuestion(
            zhihu_qid=r["zhihu_qid"], title=r["title"], description=r.get("description")
        )
        for r in rows
    ]
    todo = [p for p in pendings if not out.has(p.zhihu_qid)]
    done = len(pendings) - len(todo)
    return Plan("questions", len(pendings), len(todo), skipped=done), todo


def plan_contents(bodies: Bodies, out: Output) -> tuple[Plan, list]:
    todo, fell_back = [], 0
    for row, record in bodies.pairs:
        if out.has(record.url):
            continue
        text, fallback = _body_of(row)
        fell_back += int(fallback)
        todo.append(judge_content.PendingContent(record=record, text=text))

    plan = Plan("contents", len(bodies.pairs), len(todo), fell_back=fell_back)
    plan.unusable = bodies.seen - len(bodies.pairs)
    return plan, todo


def plan_events(bodies: Bodies, run_dir: Path, out: Output) -> tuple[Plan, list]:
    """每条正文 × 每个议题。⚠️ 线上是"预筛命中的才判"，这里**全判**（见模块开头）。"""
    briefs = _event_briefs(run_dir)

    todo, fell_back = [], 0
    for row, record in bodies.pairs:
        text, fallback = _body_of(row)
        # ⚠️ 只数一次：下面那个循环是按议题展开的，写在里面会一条正文数 N 遍
        fell_back += int(fallback)
        for brief in briefs:
            if out.has(f"{record.url}#{brief.event_id}"):
                continue
            todo.append(judge_event.PendingEvent(record=record, text=text, event=brief))

    total = len(bodies.pairs) * len(briefs)
    return Plan("events", total, len(todo), skipped=total - len(todo), fell_back=fell_back), todo


# ── 入口 ────────────────────────────────────────────────────────────


def _preview(items: list, build: Callable[[Any, PromptBundle], tuple[str, str]], bundle) -> None:
    """打印第一条的 system 尾部——花钱之前先看一眼这次到底问了什么。"""
    if not items:
        return
    system, user = build(items[0], bundle)
    print(f"    system {len(system)} 字 / user {len(user)} 字；system 尾部：")
    for line in system.splitlines()[-6:]:
        print(f"      │ {line}")
    head = user.splitlines()[0] if user.splitlines() else ""
    print(f"    user 开头：{head}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="judge_fixture",
        description="用真模型把夹具判一遍（模块2 单独跑）。**会花钱。**",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="产物落在 <run>/judgments/，供模块3 的测试直接读。\n",
    )
    parser.add_argument("--run", default=DEFAULT_RUN, metavar="ID", help="runtime/ops 下的目录名")
    parser.add_argument("--only", choices=STEPS, action="append", help="只跑这几步（可重复）")
    parser.add_argument("--concurrency", type=int, default=None, help="默认取 AI_BATCH_SIZE")
    parser.add_argument("--force", action="store_true", help="旧提示词的产物改名备份后重判")
    parser.add_argument("--dry-run", action="store_true", help="只报账、打印 prompt，不发请求")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if not args.verbose else logging.DEBUG,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    sys.stdout.reconfigure(line_buffering=True)

    layout = paths()
    run_dir = layout.ops / args.run
    if not (run_dir / "task.json").exists():
        raise SystemExit(f"❌ 找不到任务目录 {run_dir}")

    # ⚠️ 工作区里只有 *.example 时**当场停下**。占位符拼出来的判断没有意义，
    #    而且这次要为它花钱——和 `replay_fixture.py` 同一条规矩。
    if not any(layout.prompts.glob("*.txt")):
        raise SystemExit(
            f"❌ {layout.prompts} 里一个 .txt 都没有，只有 *.example 模板。\n"
            "   占位符拼出来的 prompt 不值得花钱，所以这里直接停。"
        )
    bundle = bundle_from_workdir(layout.prompts, version=1)

    settings = Settings.from_env()
    concurrency = args.concurrency or settings.batch_size
    steps = [s for s in STEPS if not args.only or s in args.only]

    out_dir = run_dir / JUDGMENTS
    outputs = {
        "questions": Output(
            out_dir / "questions.jsonl",
            key=lambda r: r["zhihu_qid"],
            bundle=bundle,
            force=args.force,
        ),
        "contents": Output(
            out_dir / "contents.jsonl", key=lambda r: r["url"], bundle=bundle, force=args.force
        ),
        "events": Output(
            out_dir / "events.jsonl",
            key=lambda r: f"{r['url']}#{r['event_id']}",
            bundle=bundle,
            force=args.force,
        ),
    }

    print(f"── run {args.run} ──")
    print(f"提示词工作区：{layout.prompts}")
    print(f"提示词版本：  {bundle.content_hash[:12]}（空模块 {bundle.missing_modules() or '无'}）")

    # ⚠️ 这两行 **dry-run 也要打**：它们合起来就是"这次打算按什么档位花钱"的
    #    全部答案，而 dry-run 的用途正是在花钱之前看一眼。所以取的是 `settings`
    #    而不是 `client`——客户端要连（缺 key 会当场报错），dry-run 不该被它挡住。
    if args.dry_run:
        print(f"模型：        {settings.llm_model}（--dry-run，不连）")
    else:
        # 缺 LLM_BASE_URL / LLM_API_KEY 会在这里当场报错，说清该填哪两个变量
        client = HttpLLMClient.from_settings(settings)
        print(f"模型：        {client.model} @ {client.base_url}")
    # ⚠️ 空串 = 不传该字段、用服务端默认，而服务端默认往往是最高档——
    #    打印成"（服务端默认）"让人一眼看见自己正把这条留白。
    print(f"思考强度：    {settings.llm_reasoning_effort or '（服务端默认）'}")
    print(f"并发：        {concurrency}")
    print()

    # ── 报账 ────────────────────────────────────────────────────────
    bodies = _bodies(run_dir)
    plans: dict[str, Plan] = {}
    todos: dict[str, list] = {}
    for name in steps:
        if name == "questions":
            plans[name], todos[name] = plan_questions(run_dir, outputs[name])
        elif name == "contents":
            plans[name], todos[name] = plan_contents(bodies, outputs[name])
        else:
            plans[name], todos[name] = plan_events(bodies, run_dir, outputs[name])
        print(f"  {plans[name].describe()}")

    total = sum(p.todo for p in plans.values())
    print(f"  ── 合计 {total} 次请求")
    if plans.get("events") and not (run_dir / "events.jsonl").exists():
        print("  ⚠️ 没有 events.jsonl，事件那一步没什么可判的")

    # ── 预览 ────────────────────────────────────────────────────────
    print()
    print("── 这次要问什么 ────────────────────────────────────")
    if "questions" in todos:
        _preview(todos["questions"], judge_question.build_messages, bundle)
    if "contents" in todos:
        _preview(todos["contents"], judge_content.build_messages, bundle)
    if "events" in todos:
        _preview(todos["events"], judge_event.build_messages, bundle)

    if args.dry_run:
        print()
        print("（--dry-run：一次请求都没发，一个字节都没写）")
        return 0

    if not total:
        print()
        print("✅ 没有待判的条目（都判过了）。要重判就删掉 judgments/ 里对应的文件。")
        return 0

    # ── 真判 ────────────────────────────────────────────────────────
    out_dir.mkdir(parents=True, exist_ok=True)
    # ⚠️ `PendingQuestion` 上**没有 url**：那个类是"判相关性"的输入，
    #    知乎问题链接是库里/夹具里的事，不该塞进 AI 模块（决策 52 那条边界）。
    #    可产物里要带上 url，模块3 才好对上 `claim_question(zhihu_qid, url, title)`——
    #    所以在**这一层**按 qid 建个映射，不往上加字段。
    q_urls = {r["zhihu_qid"]: r.get("url") for r in _rows(run_dir / "questions.jsonl")}
    reports: dict[str, str] = {}

    for name in steps:
        todo = todos[name]
        if not todo:
            continue
        print()
        print(f"── {name}：{len(todo)} 条 ─────────────────────────────")
        out = outputs[name]

        if name == "questions":

            def on_question(pending, judgment, _out=out) -> None:
                _out.write(
                    {
                        "zhihu_qid": judgment.zhihu_qid,
                        "url": q_urls.get(judgment.zhihu_qid),
                        "title": pending.title,
                        "is_relevant": judgment.is_relevant,
                        "model_version": judgment.model_version,
                        "prompt_version": judgment.prompt_version,
                        "judged_at": _now(),
                    }
                )

            report = judge_question.judge_many(
                todo, bundle=bundle, client=client, concurrency=concurrency, on_result=on_question
            )
        elif name == "contents":

            def on_content(pending, judgment: ContentJudgment, _out=out) -> None:
                _out.write(_content_row(pending.record, judgment))

            report = judge_content.judge_many(
                todo, bundle=bundle, client=client, concurrency=concurrency, on_result=on_content
            )
        else:

            def on_event(pending, judgment, _out=out) -> None:
                _out.write(
                    {
                        "url": pending.record.url,
                        "zhihu_id": pending.record.zhihu_id,
                        "content_type": pending.record.content_type,
                        "event_id": int(judgment.event_id),
                        "event_name": pending.event.name,
                        "event_version": judgment.event_version,
                        "is_relevant": judgment.is_relevant,
                        "stance": judgment.stance,
                        "confidence": judgment.confidence,
                        "model_version": judgment.model_version,
                        "prompt_version": judgment.prompt_version,
                        "judged_at": _now(),
                    }
                )

            report = judge_event.judge_many(
                todo, bundle=bundle, client=client, concurrency=concurrency, on_result=on_event
            )

        reports[name] = report.describe()
        print(f"  {report.describe()}")

    # ── 落一份 meta，供模块3 的测试认版本 ───────────────────────────
    meta = {
        "run_id": args.run,
        "model": client.model,
        "prompt_version": bundle.content_hash,
        "prompt_modules": len(bundle.modules),
        "concurrency": concurrency,
        "finished_at": _now(),
        "steps": {name: plans[name].describe() for name in steps},
        "reports": reports,
    }
    (out_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    print()
    print(f"── 落盘 ──  {out_dir}")
    for name in steps:
        path = out_dir / f"{name}.jsonl"
        if path.exists():
            print(f"  {path.name:16s} {len(_rows(path))} 行")
    print("  meta.json")
    return 0


def _content_row(record: ContentRecord, judgment: ContentJudgment) -> dict:
    """正文判定 → 一行。字段与 `fact_analysis` 的列对齐（决策 31）。"""
    return {
        "url": record.url,
        "zhihu_id": record.zhihu_id,
        "content_type": record.content_type,
        # ⚠️ `is_relevant` 不落 `fact_analysis`（那张表没这一列），
        #    它是"这条要不要入库"的裁决输入（决策 28）。这里记下来是为了
        #    模块3 的测试能核对，落库时由主程序决定怎么用。
        "is_relevant": judgment.is_relevant,
        "ai_summary": judgment.ai_summary,
        "platform_stance": judgment.platform_stance,
        "stance_confidence": judgment.stance_confidence,
        "risk_level": judgment.risk_level,
        "risk_reasoning": judgment.risk_reasoning,
        "model_version": judgment.model_version,
        "prompt_version": judgment.prompt_version,
        "judged_at": _now(),
    }


if __name__ == "__main__":
    sys.exit(main())
