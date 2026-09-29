"""把夹具的**判定**和**内容**一起写进真库——模块3 的第一次真实入库。

    .venv/bin/python scripts/ingest_fixture.py --dry-run   # 走内存库，先看账对不对
    .venv/bin/python scripts/ingest_fixture.py             # ⚠️ 真写线上库

## ⚠️ 这是一个**临时脚手架**，做完该做的事就该删掉

它干的接线活**本来属于 `main/`**（架构文档 7.2）：读产物 → 落库 → 记账。
现在写在这里只有一个原因：`main/ ingest` 读的只有 `contents.jsonl`，而 AI 的判定
要写回**同一行**的那层文件逻辑还没接上（决策 51 第①条后果）。
`main/ ingest --analyze` 做出来那天，删掉这个文件。

## ⚠️ 它会真的写线上库

这是这个仓库里**第一个真的往库里写业务数据的东西**——`prompts push` 只写提示词，
`migrate` 只改结构。所以它有一套别处没有的守卫：

  - **空库哨兵**（第 0 步第 4 条）：库里只要有任何一行，当场停下。
    `create_event` 没有幂等键（`dim_event` 上没有唯一约束），重跑一次
    就是两个议题，而议题是**人工确认过**的东西，多出来的那些永远查不出源头。
  - **全有或全无的预检**：所有检查在**写第一个字节之前**跑完。中途发现漏一条
    判定就退出，等于库里留下半批内容——那正是决策 51 要挡的状态。
  - **不包 try/except**：失败就带着栈退出。这个脚本没有"部分成功"这种结果
    值得挽救，硬撑着往下走只会把状态搞得更难收拾。

## 账对不上就非零退出

`EXPECTED` 那张表是从这份夹具**实算出来**的，不是"大致差不多"。数字对不上
说明有一段逻辑和你以为的不一样——那时候最该做的不是调数字，是把差异查清楚。

## 它只走 `Repo`，不 import 驱动、不写 SQL

`scripts/` 不受 `tests/test_layering.py` 那四条硬规则的约束（那个测试不扫这里），
所以这条规矩得自己守。理由和那边一样：直连驱动的脚本没法用内存库彩排，
而**没法彩排的脚本不该拿去写生产库**。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

from sentinel_q.collector import ops
from sentinel_q.collector.extract import from_document
from sentinel_q.shared.config import Settings, paths
from sentinel_q.shared.models import ContentJudgment, ContentRecord, EventJudgment
from sentinel_q.shared.prompts import bundle_from_workdir
from sentinel_q.storage.events import EventStanceReport, save_judged_events
from sentinel_q.storage.fake import FakeRepo
from sentinel_q.storage.ingest import JudgedReport, insert_judged
from sentinel_q.storage.repo import Repo
from sentinel_q.storage.supabase import SupabaseRepo

log = logging.getLogger("ingest_fixture")

DEFAULT_RUN = "20260929-090000-fixture"

# ── 验收数字 ────────────────────────────────────────────────────────
#
# 从这份夹具实算出来的，逐条核对过。**不是"跑出来多少就记多少"**——
# 那样这张表就没有意义了。

EXPECTED_ROWS: dict[str, int] = {
    "dim_question": 5,
    "dim_author": 12,
    "dim_event": 2,
    "fact_content": 13,
    "fact_analysis": 13,
    "fact_content_event": 12,  # ⚠️ 不是 13，见下面 EXPECTED_EVENTS.orphaned
    "fact_evidence": 0,
}

EXPECTED_UNBUILDABLE = 1
"""`contents.jsonl` 里那条 `content_type: "question"`。**预期之内**，不是坏了。"""

EXPECTED_GATE: dict[str, int] = {
    "kept_by_parent": 2,  # 9001005 / 9001007，都挂在 1002003003 下
    "dropped_irrelevant": 3,  # 8801003 / 8801005 / 8801007，没有所属问题
    "dropped_unknown_parent": 0,
    "unjudged": 0,
    "rejected_questions": 0,
    "stance_conflict": 0,
}

EXPECTED_EVENTS: dict[str, int] = {
    "saved": 12,
    "skipped_irrelevant": 19,
    "orphaned": 1,  # ⚠️ zhuanlan/p/8801007 × 议题2：判定相关，但内容被闸门丢了
    "unknown_event": 0,
    "version_mismatch": 0,
    "bad_stance": 0,
}


# ── 读夹具 ──────────────────────────────────────────────────────────


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


@dataclass
class Fixture:
    """一份夹具里，这次入库要用的全部输入。"""

    records: list[ContentRecord] = field(default_factory=list)
    """装得成的记录，**按文件序**（answers 先、contents 后）。"""

    unbuildable: int = 0
    dropped_rows: list[str] = field(default_factory=list)
    """装不成记录的那些行的 url，报账用。"""

    content_judgments: dict[str, ContentJudgment] = field(default_factory=dict)
    """`url` → 判定。⚠️ 键是 url 不是 `zhihu_id`，理由见 `insert_judged` 的说明。"""

    event_judgments: list[tuple[str, EventJudgment]] = field(default_factory=list)

    questions: list[dict] = field(default_factory=list)
    """`questions.jsonl` 的原始行——`claim_question` 要 url / title / description。"""

    question_relevance: dict[str, bool] = field(default_factory=dict)
    """`zhihu_qid` → 判为相关与否（来自 `judgments/questions.jsonl`）。"""

    events: list[dict] = field(default_factory=list)

    prompt_version: str | None = None

    @property
    def relevant_questions(self) -> set[str]:
        """⭐ §3.3 第⑥步的【更新问题列表】：**本轮新登记、且判为相关**的问题。

        这份夹具是首次采集，所以"本轮新登记的"就是全部 5 个。
        线上不是这样：那里要按 `first_seen_at >= started_at` 过滤
        （`Repo.follow_up_questions`），把上一轮的问题排除掉。
        """
        return {qid for qid, relevant in self.question_relevance.items() if relevant}


def load_fixture(run_dir: Path) -> Fixture:
    """读一份夹具。**装不成的行照旧跳过并计数**，不在这一层报错。"""
    fx = Fixture()

    store = ops.OpsStore.resume(run_dir)
    for name in ops.OpsStore.BODY_FILES:
        # ⚠️ 顺序不能动：answers 里的回答挂在问题下，contents 里的文章没有父级，
        #    但两份必须是**同一次 `insert_judged` 调用**（见那个函数的说明）。
        for row in store.iter_contents(name=name):
            record = from_document(row)
            if record is None:
                # 「装不成」的原因 `from_document` 已经逐行打过警告了。
                # ⚠️ 这里**不做类型判断**——那正是 `from_document` 的活，
                #    在这一层再认一次就是第二个真相源。
                fx.unbuildable += 1
                fx.dropped_rows.append(str(row.get("url")))
                continue
            fx.records.append(record)

    judgments_dir = run_dir / "judgments"
    for row in _rows(judgments_dir / "contents.jsonl"):
        fx.content_judgments[row["url"]] = ContentJudgment(
            zhihu_id=row["zhihu_id"],
            is_relevant=row["is_relevant"],
            ai_summary=row.get("ai_summary"),
            platform_stance=row.get("platform_stance"),
            stance_confidence=row.get("stance_confidence"),
            risk_level=row.get("risk_level"),
            risk_reasoning=row.get("risk_reasoning"),
            model_version=row.get("model_version"),
            prompt_version=row.get("prompt_version"),
        )

    for row in _rows(judgments_dir / "events.jsonl"):
        fx.event_judgments.append(
            (
                row["url"],
                EventJudgment(
                    zhihu_id=row["zhihu_id"],
                    # ⚠️ 判定里是 int，`EventJudgment` 上是 str（AI 模块原样回带
                    #    调用方给的标识，不解释它是什么）。在这里转一道。
                    event_id=str(row["event_id"]),
                    event_version=row["event_version"],
                    is_relevant=row["is_relevant"],
                    stance=row.get("stance"),
                    confidence=row.get("confidence"),
                    model_version=row.get("model_version"),
                    prompt_version=row.get("prompt_version"),
                ),
            )
        )

    fx.questions = _rows(run_dir / "questions.jsonl")
    for row in _rows(judgments_dir / "questions.jsonl"):
        fx.question_relevance[row["zhihu_qid"]] = row["is_relevant"]

    fx.events = _rows(run_dir / "events.jsonl")

    meta = judgments_dir / "meta.json"
    if meta.exists():
        fx.prompt_version = json.loads(meta.read_text(encoding="utf-8")).get("prompt_version")

    return fx


# ── 第 0 步：预检（一个字节都不写）────────────────────────────────────


def preflight(fx: Fixture, *, repo: Repo, dry_run: bool) -> None:
    """所有守卫都在这里。**它在任何写操作之前跑完**——这是这个函数存在的全部意义。"""
    layout = paths()

    # 1. 提示词工作区里只有占位符时当场停下。判定是拿那套提示词做出来的，
    #    占位符拼出来的判断没有意义，而"用另一套提示词判出来的结果入库"
    #    会让 `fact_analysis.prompt_version` 指向一个不存在的东西。
    if not any(layout.prompts.glob("*.txt")):
        raise SystemExit(
            f"❌ {layout.prompts} 里一个 .txt 都没有，只有 *.example 模板。\n"
            "   判定是拿工作区里那套提示词做出来的，先把它补上再跑。"
        )

    # 2. 一条都装不成 → 在开库之前退出。对齐 `main.ingest_run` 的同一条守卫：
    #    连上去再报"入库 0 条"，看起来像跑通了。
    if not fx.records:
        raise SystemExit("❌ 一条记录都装不成，没有东西可写。上面有逐行的原因。")

    # 3. 每条能装成的记录都必须有判定。**缺一条就整体停下**，不是跳过那一条：
    #    跳过会让库里少一行，而报告上只会多一个 `unjudged`——那看起来像
    #    "模型还没判完"，实际是这份 judgments/ 和这份产物对不上。
    missing = [r.url for r in fx.records if r.url not in fx.content_judgments]
    if missing:
        lines = "\n".join(f"     {url}" for url in missing[:10])
        more = f"\n     …还有 {len(missing) - 10} 条" if len(missing) > 10 else ""
        raise SystemExit(
            f"❌ {len(missing)} 条记录没有判定，先跑 `scripts/judge_fixture.py`：\n{lines}{more}"
        )

    # 4. ⭐ 空库哨兵。`create_event` 没有幂等键，重跑一次就是两个议题，
    #    而议题是人工确认过的东西——多出来的那些永远查不出源头。
    _refuse_if_not_empty(fx, repo=repo, dry_run=dry_run)

    _check_prompt_provenance(fx, repo=repo, dry_run=dry_run)


def _refuse_if_not_empty(fx: Fixture, *, repo: Repo, dry_run: bool) -> None:
    urls = [r.url for r in fx.records]
    qids = [q["zhihu_qid"] for q in fx.questions]
    already = repo.existing_urls(urls) | repo.existing_question_ids(qids)
    total = len(repo.all_urls())
    if already or total:
        # ⚠️ 顺带报一下"抢占了但没判完"的问题（4.1「先插后问」中间那个瞬间）。
        #    它本来就该被上面那条挡住，所以**不单独设一道守卫**——那种守卫
        #    在这里永远走不到，是死代码。但它的成因值得写进这句提示里：
        #    库里躺着一个 `is_relevant` 还是 null 的问题时，它下面的回答
        #    谁也不敢碰（`insert_judged` 会当成"没有相关问题"整个丢掉）。
        pending = [q.zhihu_qid for q in repo.all_questions() if q.is_relevant is None]
        hint = ""
        if pending:
            hint = (
                f"\n   其中 {len(pending)} 个问题是「已抢占、没判完」的状态"
                f"（is_relevant 还是 null）：{'、'.join(pending[:5])}。"
                "\n   那种问题下面的回答会被内容闸门整个丢掉，先把它们判完。"
            )
        raise SystemExit(
            f"❌ 库里已经有东西了（已采 URL {total} 条，这批里重复的 {len(already)} 条），"
            "这个脚本只在**空库**上跑。\n"
            "   理由：`create_event` 没有幂等键，重跑会再建一遍议题；\n"
            "   而议题是人工确认过的，多出来的那几行事后认不出来。\n"
            "   要在非空库上试，请走 `main/` 的正式入库路径，不是这个脚手架。"
            f"{hint}"
        )
    if dry_run:
        log.info("（内存库，本来就是空的；库侧那几条检查没有真跑）")


def _check_prompt_provenance(fx: Fixture, *, repo: Repo, dry_run: bool) -> None:
    """判定里的 `prompt_version` 要能在库里找到对应的那一版提示词（决策 47）。

    ⚠️ `--dry-run` 走的是内存库，里面**没有**提示词——所以这个检查在彩排时
    是**跳过**的，脚本最后会明说这件事。别把彩排通过当成"这条验过了"。
    """
    if dry_run:
        return

    bundle = repo.active_prompt_bundle()
    if bundle is None:
        local = bundle_from_workdir(paths().prompts, version=1)
        raise SystemExit(
            "❌ 库里没有生效的提示词版本，而判定里的 prompt_version 必须有处可查（决策 47）。\n"
            f"   工作区那份的 hash 是 {local.content_hash}。先推上去：\n"
            "     .venv/bin/python -m sentinel_q.storage prompts push -v 1"
        )
    if fx.prompt_version and bundle.content_hash != fx.prompt_version:
        raise SystemExit(
            f"❌ 判定是用提示词 {fx.prompt_version} 做的，库里生效的是 {bundle.content_hash}。\n"
            "   拿另一套提示词判出来的结果入库，等于把 prompt_version 那一列写成一个谎。\n"
            "   要么把这一版推上库（prompts push -v N），要么用当前那版重判一遍。"
        )
    log.info("提示词出处对上了：%s", bundle.content_hash)


# ── 第 1–4 步：写 ───────────────────────────────────────────────────


def write_questions(fx: Fixture, *, repo: Repo) -> dict[str, int]:
    """第②步：问题登记 + 相关性判定。**必须排在正文前面**（3.3）。

    排在前面的理由是具体的：正文入库时 `question_id` 那一列要去 `dim_question`
    找父级，问题没先落库，那一条会记成"question_id 空着"——
    而且 `insert_judged` 的内容闸门也会把它的回答当成"没有相关问题"丢掉。
    """
    ids: dict[str, int] = {}
    for row in fx.questions:
        qid = row["zhihu_qid"]
        # 4.1 的"先插后问"在这里是两个动作：claim 拿主键，再写判定。
        # `is_relevant` 三态里的 `null` 就是这两行之间那个瞬间。
        question_id = repo.claim_question(
            qid,
            row.get("url"),
            row.get("title"),
            description=row.get("description"),
        )
        if question_id is None:
            raise SystemExit(f"❌ 问题 {qid} 已经在库里了——这个脚本只在空库上跑。")
        if qid not in fx.question_relevance:
            raise SystemExit(f"❌ 问题 {qid} 没有判定，先跑 `scripts/judge_fixture.py`。")
        repo.set_question_relevance(question_id, is_relevant=fx.question_relevance[qid])
        ids[qid] = question_id
        log.info(
            "问题 %s → %-4s %s",
            qid,
            "相关" if fx.question_relevance[qid] else "不相关",
            str(row.get("title"))[:28],
        )
    return ids


def write_events(fx: Fixture, *, repo: Repo) -> dict[int, int]:
    """议题入库（4.5：**由人工确认后录入**，采集侧永远产不出这些行）。

    ⚠️ 这里**只能新建**：`dim_event` 上没有唯一约束，没有 `on conflict` 可用。
    这正是"空库哨兵"存在的理由（见 `_refuse_if_not_empty`）。

    返回 `{夹具里的 event_id: 库里的 event_id}`。
    """
    mapping: dict[int, int] = {}
    for row in fx.events:
        event_id = repo.create_event(
            name=row["name"],
            summary=row["summary"],
            keywords=row["keywords"],
            event_type=row["event_type"],
            start_date=date.fromisoformat(row["start_date"]) if row.get("start_date") else None,
            end_date=date.fromisoformat(row["end_date"]) if row.get("end_date") else None,
        )
        created = repo.event_by_id(event_id)
        # ⚠️ 断言版本号：判定是**对着某一版摘要**做的（4.5.4）。
        #    对不上时 `save_judged_events` 会整批拒写，那还不如在这里就说清楚。
        if created is None or created.version != row["version"]:
            raise SystemExit(
                f"❌ 议题 {row['name']!r} 建出来是第 {created and created.version} 版，"
                f"夹具里写的是第 {row['version']} 版。"
            )
        mapping[row["event_id"]] = event_id
        log.info("议题 %s → 库里 %s（第 %s 版）", row["event_id"], event_id, created.version)
    return mapping


# ── 第 5 步：回读校验 ───────────────────────────────────────────────


def read_back(
    fx: Fixture,
    *,
    repo: Repo,
    report: JudgedReport,
    event_report: EventStanceReport,
    event_ids: dict[int, int],
) -> list[str]:
    """写完之后**再读一遍**，用同一批 `Repo` 原语。

    它只调 `HONEST` 里的方法，所以内存库和真库跑的是同一段代码——
    彩排验过的检查，真跑时会一模一样地再跑一遍。

    返回问题清单；空列表 = 全部通过。
    """
    problems: list[str] = []

    # ① 决策 51：插进去的每一行都要有分析，一条不落。
    for url, content_id in report.content_ids.items():
        if repo.analysis_for(content_id) is None:
            problems.append(f"内容 {url} 入库了，但没有 fact_analysis（决策 51 被破坏）")

    # ② 作者维度：同一个知乎用户 ID 必须落到**同一行** dim_author，
    #    匿名（没有知乎 ID）的那条必须是 NULL。这是 `ensure_author` 用 upsert 的理由。
    by_author: dict[str, set[int]] = {}
    for record in fx.records:
        content_id = report.content_ids.get(record.url)
        if content_id is None:
            continue
        row = repo.content_by_id(content_id)
        if row is None:
            problems.append(f"内容 {record.url} 刚插进去就查不到了")
            continue
        if record.author_zhihu_id:
            if row.author_id is None:
                problems.append(f"内容 {record.url} 的 author_id 是空的（它有知乎用户 ID）")
            else:
                by_author.setdefault(record.author_zhihu_id, set()).add(row.author_id)
        elif row.author_id is not None:
            problems.append(f"内容 {record.url} 没有知乎用户 ID，author_id 却不空")
    for zhihu_user_id, ids in by_author.items():
        if len(ids) > 1:
            problems.append(f"作者 {zhihu_user_id} 落成了 {len(ids)} 行 dim_author（upsert 坏了）")

    # ③ 议题判定：写进去的每一对都要能读回来。
    saved_pairs = 0
    for url, judgment in fx.event_judgments:
        if not judgment.is_relevant:
            continue
        content_id = report.content_ids.get(url)
        if content_id is None:
            continue  # orphaned，`save_judged_events` 已经计过一笔了
        event_id = event_ids.get(int(judgment.event_id))
        stances = {row.event_id: row for row in repo.event_stances_for(content_id)}
        if event_id not in stances:
            problems.append(f"内容 {url} × 议题 {event_id} 的判定没落库")
        elif stances[event_id].stance != judgment.stance:
            problems.append(
                f"内容 {url} × 议题 {event_id} 的立场是 {stances[event_id].stance!r}，"
                f"判定里是 {judgment.stance!r}"
            )
        else:
            saved_pairs += 1

    if saved_pairs != event_report.saved:
        problems.append(
            f"议题判定写进去 {event_report.saved} 条，读回来只有 {saved_pairs} 条"
        )

    # ④ 问题：三态必须是 true/false，不许留 null（那是"抢占后没回来"）。
    questions = {q.zhihu_qid: q for q in repo.all_questions()}
    for qid, relevant in fx.question_relevance.items():
        row = questions.get(qid)
        if row is None:
            problems.append(f"问题 {qid} 没进 dim_question")
        elif row.is_relevant is None:
            problems.append(f"问题 {qid} 的 is_relevant 还是 null（抢占之后没判完）")
        elif row.is_relevant != relevant:
            problems.append(f"问题 {qid} 的判定是 {row.is_relevant}，夹具里是 {relevant}")

    return problems


# ── 账 ──────────────────────────────────────────────────────────────


def tally(
    fx: Fixture,
    *,
    repo: Repo,
    report: JudgedReport,
    event_report: EventStanceReport,
    question_count: int,
    event_count: int,
) -> list[str]:
    """把实际发生的数字和 `EXPECTED_*` 对一遍。返回对不上的那些。"""
    problems: list[str] = []

    def compare(label: str, actual: int, expected: int) -> None:
        if actual != expected:
            problems.append(f"{label}：实际 {actual}，预期 {expected}")

    # `dim_author` 没有枚举用的原语（`Repo` 上没有 all_authors），
    # 所以它是从**插入行各自的 author_id** 数出来的——和直接 count 等价，
    # 而且顺手验了"同一个人只占一行"。查重口径见 read_back 的 ②。
    author_ids = set()
    for record in fx.records:
        content_id = report.content_ids.get(record.url)
        if content_id is None:
            continue
        row = repo.content_by_id(content_id)
        if row is not None and row.author_id is not None:
            author_ids.add(row.author_id)

    actual_rows = {
        "dim_question": question_count,
        "dim_author": len(author_ids),
        "dim_event": event_count,
        "fact_content": report.ingest.inserted,
        "fact_analysis": report.ingest.inserted,  # 决策 51：同批同数
        "fact_content_event": event_report.saved,
        "fact_evidence": 0,  # 这条线这次不碰
    }
    for table, expected in EXPECTED_ROWS.items():
        compare(table, actual_rows[table], expected)

    compare("装不成记录", fx.unbuildable, EXPECTED_UNBUILDABLE)

    for name, expected in EXPECTED_GATE.items():
        compare(name, getattr(report, name), expected)

    for name, expected in EXPECTED_EVENTS.items():
        compare(name, getattr(event_report, name), expected)

    return problems


def print_report(
    fx: Fixture,
    *,
    report: JudgedReport,
    event_report: EventStanceReport,
    question_count: int,
    event_count: int,
    problems: list[str],
    dry_run: bool,
) -> None:
    print()
    print("── 账 ─────────────────────────────────────────────")
    print(f"  读物产      {len(fx.records) + fx.unbuildable} 行"
          f"（装不成记录 {fx.unbuildable} → 可入库 {len(fx.records)}）")
    print(f"  内容闸门    自身相关或所属问题相关 → 留 {report.ingest.inserted}"
          f"（其中靠所属问题留下 {report.kept_by_parent}）")
    print(f"              丢弃 {report.dropped_irrelevant}"
          f" + {report.dropped_unknown_parent}（所属问题不在库里）"
          f" + {report.unjudged}（没有判定）"
          f" + {report.rejected_questions}（问题类型）")
    print(f"  {report.ingest.describe()}")
    print(f"  {event_report.describe()}")
    print()
    print("── 表 ─────────────────────────────────────────────")
    print(f"  dim_question       {question_count}")
    print(f"  dim_event          {event_count}")
    print(f"  fact_content       {report.ingest.inserted}")
    print(f"  fact_analysis      {report.ingest.inserted}")
    print(f"  fact_content_event {event_report.saved}")
    if report.ingest.unresolved_authors:
        print(f"  （dim_author 有 {report.ingest.unresolved_authors} 条内容没挂上——匿名回答，正常）")
    print()
    if problems:
        print(f"── ❌ 对不上（{len(problems)} 项）─────────────────────")
        for item in problems:
            print(f"  · {item}")
    else:
        print("── ✅ 全部对上 ─────────────────────────────────────")
    print()
    if dry_run:
        print("（--dry-run：写的是内存库，**线上库一个字节都没动**，进程退出即消失）")
        print("（⚠️ 因此库侧那几项检查——空库哨兵、提示词出处——**没有真跑**）")


# ── 入口 ────────────────────────────────────────────────────────────


def _repo(dry_run: bool) -> Repo:
    """取仓储。**不静默降级**——与 `main._repo()` 同一条规矩。"""
    if dry_run:
        return FakeRepo()
    settings = Settings.from_env()
    if not settings.supabase_dsn:
        raise SystemExit(
            "❌ 没有配 SUPABASE_DSN，没有库可写。\n"
            "   想先看账长什么样就加 --dry-run（走内存库）。"
        )
    return SupabaseRepo(settings.supabase_dsn)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ingest_fixture",
        description="把夹具的判定和内容一起写进库（模块3 的真实入库测试）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="⚠️ 临时脚手架：这段接线将来属于 main/。不加 --dry-run 就是真写线上库。\n",
    )
    parser.add_argument("--run", default=DEFAULT_RUN, metavar="ID", help="runtime/ops 下的目录名")
    parser.add_argument(
        "--dry-run", action="store_true", help="走内存库彩排（库侧检查会跳过，脚本会说明）"
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    # 日志走 stderr、print 走 stdout。管道里 stdout 是块缓冲的，不加这行的话
    # 报告会整段跑到日志后面去，读起来像"先跑完才打印标题"。
    sys.stdout.reconfigure(line_buffering=True)

    run_dir = paths().ops / args.run
    if not (run_dir / "task.json").exists():
        raise SystemExit(f"❌ 找不到任务目录 {run_dir}")

    print(f"── run {args.run} ──")
    print(f"提示词工作区：{paths().prompts}")
    if not args.dry_run:
        print("⚠️ 真写线上库。")
    print()

    fx = load_fixture(run_dir)
    if args.verbose:
        for url in fx.dropped_rows:
            log.debug("装不成记录：%s", url)

    repo = _repo(args.dry_run)
    try:
        # ⭐ 先跑完全部守卫，再写第一个字节。中途发现漏一条判定就退出的话，
        #    库里会留下半批内容——那正是决策 51 要挡的状态。
        preflight(fx, repo=repo, dry_run=args.dry_run)

        question_ids = write_questions(fx, repo=repo)
        event_ids = write_events(fx, repo=repo)

        report = insert_judged(
            fx.records,
            fx.content_judgments,
            repo=repo,
            relevant_questions=fx.relevant_questions,
        )
        log.info("%s", report.ingest.describe())

        # 议题标识：夹具里的 event_id → 库里刚建出来的那个。
        # 这一步必须在 create_event 之后，而 `save_judged_events` 只认库里的 id。
        remapped = [
            (url, _with_event_id(judgment, event_ids))
            for url, judgment in fx.event_judgments
        ]
        event_report = save_judged_events(
            remapped, repo=repo, content_ids=report.content_ids
        )
        log.info("%s", event_report.describe())

        problems = read_back(
            fx,
            repo=repo,
            report=report,
            event_report=event_report,
            event_ids=event_ids,
        )
        problems += tally(
            fx,
            repo=repo,
            report=report,
            event_report=event_report,
            question_count=len(question_ids),
            event_count=len(event_ids),
        )
    finally:
        if isinstance(repo, SupabaseRepo):
            repo.close()  # 长连接必须显式关，见 SupabaseRepo.__exit__ 的说明

    print_report(
        fx,
        report=report,
        event_report=event_report,
        question_count=len(question_ids),
        event_count=len(event_ids),
        problems=problems,
        dry_run=args.dry_run,
    )
    return 1 if problems else 0


def _with_event_id(judgment: EventJudgment, event_ids: dict[int, int]) -> EventJudgment:
    """把判定里的**夹具**议题号换成**库里**的议题号。

    这份夹具是空库上跑的第一批，两边的号恰好一样；但把它们当成一回事
    是错的——`dim_event.event_id` 是库自己发的，第二次跑就不是 1、2 了。
    """
    return replace(judgment, event_id=str(event_ids[int(judgment.event_id)]))


if __name__ == "__main__":
    sys.exit(main())
