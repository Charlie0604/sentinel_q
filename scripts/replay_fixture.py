"""把一份采集夹具重放一遍：模块2（`analyst`）+ 模块3（`storage`）联调。

    .venv/bin/python scripts/replay_fixture.py
    .venv/bin/python scripts/replay_fixture.py --run <别的 run-id> -v

## ⚠️ 这是一个**临时脚手架**，做完该做的事就该删掉

它干的事情**本来属于 `main/`**——"读产物 → 判 A／判 B → 落库"就是主程序的
接线活（架构文档 7.2）。现在写在这里只有一个原因：**那段接线还没写**。

    collector/__main__.py     ✔ 采集，产出 questions/answers/contents
    analyst/__main__.py       ✘ `run` 还是空壳（文件层没接）
    main/__main__.py          ✔ 有个 `ingest`，但只写 fact_content，不调 AI

所以今天想让"模块2 + 模块3"真的跑起来，只能在这里手工把两头接上。

**它一旦被抄进 `main/`，就会变成第二份真相源**——两边都会改，然后对不上。
`main/ ingest --analyze` 做出来那天，删掉这个文件。

## 它**只走内存库**，线上库一个字节都不动

刻意的。要在真库上跑这条线，那件事必须由 `main/` 来做，理由很具体：
`fact_content.content_id` 是 uuid、`save_analysis` 要拿它，
而 `Repo` 上**没有**按 url 反查 id 的接口（那是 `FakeRepo` 的测试专用方法）。
真库上要拿到这个映射，只能像 `insert_contents` 那样"插一条记一条"——
那就是 `main/` 该有的逻辑，不该在这里抄第二份。

## 判定是谁做的

`analyst.fake` 和本文件里的两个假客户端：**按预设规则回话，一次真实 API 都不调**。
目的是跑通"拼 prompt → 解析 → 落库"这条路，**不是**检验 AI 判得准不准。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from sentinel_q.analyst import judge_content, judge_question
from sentinel_q.analyst.client import LLMClient
from sentinel_q.collector import ops
from sentinel_q.collector.extract import from_document
from sentinel_q.shared.config import paths
from sentinel_q.shared.models import ContentRecord
from sentinel_q.shared.prompts import bundle_from_workdir
from sentinel_q.storage.fake import FakeRepo
from sentinel_q.storage.ingest import insert_contents

log = logging.getLogger("replay")

DEFAULT_RUN = "20260929-090000-fixture"


# ── 夹具的"标准答案" ────────────────────────────────────────────────
#
# 这两张表是**夹具的一部分**，不是判出来的。写成数据而不是写进假客户端的
# if-else 里，是因为它们是给人看的：说明这份夹具想覆盖哪几种情况。

QUESTION_TRUTH: dict[str, tuple[bool, str]] = {
    "1002003001": (True, "点名了企业"),
    "1002003002": (True, "点名了产品"),
    "1002003003": (True, "没点名，但描述里的时间／事件唯一指向青禾乳业"),
    "1002003004": (False, "噪声：青禾中学（同名，不是同一件事）"),
    "1002003005": (False, "泛问行业标准，全篇落不到青禾乳业"),
}

# (内容里的特征词 → 是否相关, 立场, 风险等级, 摘要)
CONTENT_TRUTH: tuple[tuple[str, bool, str, str], ...] = (
    ("人肉", True, "抹黑", "高风险"),
    ("表哥在食药监", True, "抹黑", "低风险"),
    ("把消费者当傻子", True, "中立", "中风险"),
    ("先说结论：从现行国标看", True, "中立", "低风险"),
    ("声明的全文我读了三遍", True, "中立", "低风险"),
    ("放进行业里看", True, "中立", "低风险"),
    ("补充一个事实", True, "中立", "低风险"),
    ("这类事情一般是属地市场监管", True, "中立", "低风险"),
    ("那到底什么叫 0 蔗糖", True, "中立", "低风险"),
    ("公道话", True, "有利", "低风险"),
    ("官方账号说明", True, "有利", "低风险"),
    ("减脂期喝了两个月", True, "有利", "低风险"),
    ("老旧小区改造", False, "不相关", "低风险"),
    # 下面三条说的是**另一桩事**（5 月高钙奶），不是 9 月这桩 0 蔗糖争议。
    # ⚠️ 对**内容任务**（本表）它们是相关的——判的是"跟监测对象有没有关系"；
    #    对**事件任务**才是要挡掉的那一类（同一家企业、另一桩事），
    #    答案在 `runtime/ops/<run>/events.jsonl` 那张表上，不在这张表里。
    ("整件事里有两点值得注意", True, "中立", "低风险"),
    ("其实是没分清这两条", True, "中立", "低风险"),
    ("从通报到整改公示走完才 16 天", True, "中立", "低风险"),
)


class QuestionClient:
    """问题任务的答复来源：**只回一个 `is_relevant`**，别的一个键都不给。

    照 `question_schema` 的样子来。多给几个键也不会被采纳
    （`QuestionJudgment` 上没有那些字段），但那样测的就不是真实形状了。
    """

    model = "fixture-rules"

    def __init__(self, zhihu_qid: str) -> None:
        self.zhihu_qid = zhihu_qid

    def complete(self, *, system: str, user: str) -> str:
        relevant, _why = QUESTION_TRUTH[self.zhihu_qid]
        return json.dumps({"is_relevant": relevant}, ensure_ascii=False)


class ContentClient:
    """内容任务的答复来源：按**正文里出现什么词**分派。

    ⚠️ 不能用 `FakeLLMClient` 的答复列表：那个是**按到达顺序**发的，
    并发下说不准谁拿哪条（`analyst/fake.py` 里专门警告过）。
    这里按内容分派，所以同一份夹具跑几遍结果都一样。
    """

    model = "fixture-rules"

    def complete(self, *, system: str, user: str) -> str:
        for needle, relevant, stance, risk in CONTENT_TRUTH:
            if needle in user:
                return json.dumps(
                    {
                        "is_relevant": relevant,
                        "platform_stance": stance,
                        "stance_confidence": 0.8,
                        "risk_level": risk,
                        "risk_reasoning": f"夹具规则：命中「{needle}」",
                        "ai_summary": f"（夹具）{needle}",
                    },
                    ensure_ascii=False,
                )
        # ⚠️ 兜底**故意给中风险**，和 `g_guardrails` 那条"模糊时默认中风险"同向。
        #    走到这里说明 `CONTENT_TRUTH` 该补一条了，所以要吵。
        log.warning("有一条没命中任何夹具规则，按兜底处理：%s", _one_line(user))
        return json.dumps(
            {
                "is_relevant": True,
                "platform_stance": "中立",
                "stance_confidence": 0.5,
                "risk_level": "中风险",
                "risk_reasoning": "夹具兜底：没有命中任何规则",
                "ai_summary": "（夹具兜底）",
            },
            ensure_ascii=False,
        )


def _one_line(text: str, limit: int = 60) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit] + "…"


# ── 两步 ────────────────────────────────────────────────────────────


def step_questions(run_dir: Path, *, repo: FakeRepo, client_cls=QuestionClient) -> int:
    """第②步：问题 → 判 B → `dim_question`。**必须排在正文前面**（3.3）。

    排在前面的理由是具体的：后面那一步要靠"哪些问题相关"决定采什么，
    而且正文入库时 `question_id` 那一列要去 `dim_question` 找父级——
    问题没先落库，那一条会记成"question_id 空着"。
    """
    path = run_dir / "questions.jsonl"
    if not path.exists():
        log.warning("没有 %s，跳过问题判定", path.name)
        return 0

    bundle = bundle_from_workdir(paths().prompts, version=1)
    count = 0
    for row in _rows(path):
        pending = judge_question.PendingQuestion(
            zhihu_qid=row["zhihu_qid"],
            title=row["title"],
            description=row.get("description"),
        )
        judgment = judge_question.judge_one(
            pending, bundle=bundle, client=client_cls(row["zhihu_qid"])
        )

        # 4.1 的"先插后问"在这里是两个动作：claim 拿主键，再写判定。
        # `is_relevant` 三态里的 `null` 就是这两行之间那个瞬间。
        question_id = repo.claim_question(
            zhihu_qid=judgment.zhihu_qid, url=row["url"], title=row["title"]
        )
        repo.set_question_relevance(question_id, is_relevant=bool(judgment.is_relevant))
        count += 1
        log.info(
            "问题 %s → %-4s %s",
            judgment.zhihu_qid,
            "相关" if judgment.is_relevant else "不相关",
            row["title"][:28],
        )
    return count


def step_contents(
    run_dir: Path, *, repo: FakeRepo, client: LLMClient
) -> dict[str, dict[str, int]]:
    """第⑥步：两个正文文件 → 入库 → 判 A → `fact_analysis`。

    顺序上有个容易看错的地方：**先整批入库、再逐条判 A**，不是反过来。
    `fact_analysis.content_id` 是 `fact_content` 的主键，判定结果得先有 uuid
    才写得进去。决策 51 那句"判完才入库"说的是**两行同批**，
    而判 A 要用的正文本来就在文件里，不需要库给它。
    """
    store = ops.OpsStore.resume(run_dir)
    bundle = bundle_from_workdir(paths().prompts, version=1)
    stats: dict[str, dict[str, int]] = {}

    for name in ops.OpsStore.BODY_FILES:
        # ⚠️ 不在这里过滤 "question"：`contents.jsonl` 末尾那行问题**故意留着**，
        #    让 `from_document` 去拒它（`FACT_CONTENT_TYPES` 里没有问题），
        #    那个"装不成记录 1"就是那道闸门还活着的证据。
        rows = list(store.iter_contents(name=name))
        stat = {"rows": len(rows), "unbuildable": 0, "inserted": 0, "judged": 0, "duplicates": 0}

        # 入库。⚠️ 顺序不能动（父级要先于子级），所以整批一次交给它，
        #    不在这个循环里自己插——那会抄走 `insert_contents` 里
        #    父级映射那段逻辑，抄漏了就是"评论静默挂空"。
        records: list[ContentRecord] = []
        for row in rows:
            record = from_document(row)
            if record is None:
                stat["unbuildable"] += 1
                continue
            records.append(record)
        report = insert_contents(records, repo=repo)
        stat["inserted"] = report.inserted
        stat["duplicates"] = report.duplicates
        log.info("%s：%s", name, report.describe())

        # 判 A。逐条判、判完立刻写 —— 决策 39／51：结果一返回就落盘，
        # 进程崩了不用重问一遍 AI。
        for record in records:
            content_id = repo.content_id_for(record.url)
            if content_id is None:
                continue  # 重复的，库里本来就有，不重判
            # ⚠️ 长正文在 `storage_path` 指的文件里、`content_text` 是空的
            #    （决策 32 提到的那个真实缺陷）。真实的主程序在这里要
            #    **把文件读回来**；夹具里那个文件不存在，所以退回 `text`
            #    （采集时写进去的完整正文）。
            text = _text_of(rows, record.url)
            judgment = judge_content.judge_one(
                judge_content.PendingContent(record=record, text=text),
                bundle=bundle,
                client=client,
            )
            repo.save_analysis(judgment.to_analysis_result(content_id))
            stat["judged"] += 1

        stats[name] = stat

    return stats


def _rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _text_of(rows: list[dict], url: str) -> str:
    for row in rows:
        if row.get("url") == url:
            return row.get("content_text") or row.get("text") or ""
    return ""


# ── 入口 ────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="replay_fixture",
        description="把一份采集夹具重放一遍，联调模块2 + 模块3（**只走内存库**）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="⚠️ 临时脚手架：这段接线将来属于 main/。线上库一个字节都不会动。\n",
    )
    parser.add_argument("--run", default=DEFAULT_RUN, metavar="ID", help="runtime/ops 下的目录名")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    # 日志走 stderr、print 走 stdout。管道里 stdout 是块缓冲的，不加这行的话
    # 报告会整段跑到日志后面去，读起来像"先跑完才打印标题"。
    sys.stdout.reconfigure(line_buffering=True)

    layout = paths()
    run_dir = layout.ops / args.run
    if not (run_dir / "task.json").exists():
        raise SystemExit(f"❌ 找不到任务目录 {run_dir}")

    # ⚠️ 工作区里只有 *.example（占位符）时**当场停下**。拿占位符拼出来的
    #    prompt 跑出来的判断没有任何意义，跑通了反而更糟——它会让人以为
    #    "这条线验过了"。和 `fetch_bundle` 那条降级警告同一个立场。
    if not any(layout.prompts.glob("*.txt")):
        raise SystemExit(
            f"❌ {layout.prompts} 里一个 .txt 都没有，只有 *.example 模板。\n"
            "   占位符拼出来的判断没有意义，所以这里直接停。\n"
            "   先把提示词写进工作区（或 `python -m sentinel_q.storage prompts pull`）。"
        )

    repo = FakeRepo()
    print(f"── run {args.run} ──")
    print(f"提示词工作区：{layout.prompts}")
    print()
    questions = step_questions(run_dir, repo=repo)
    print()
    stats = step_contents(run_dir, repo=repo, client=ContentClient())

    print()
    print("── 账 ─────────────────────────────────────────────")
    print(f"  dim_question    {questions} 行")
    total_in = total_judged = total_rows = 0
    for name, stat in stats.items():
        total_rows += stat["rows"]
        total_in += stat["inserted"]
        total_judged += stat["judged"]
        print(
            f"  {name:9s} 读 {stat['rows']} 行，"
            f"入库 {stat['inserted']}，判 A {stat['judged']}，"
            f"装不成记录 {stat['unbuildable']}"
        )
    print(f"  fact_content    {total_in} 行")
    print(f"  fact_analysis   {total_judged} 行")

    print()
    print("── 问题的判定 ─────────────────────────────────────")
    for row in repo.all_questions():
        mark = "✔" if row.is_relevant else "✘"
        print(f"  {mark} {row.is_relevant!s:5s} {row.title[:30]}")

    print()
    print("── 内容的判定 ─────────────────────────────────────")
    for content_id, url in sorted(repo._content_ids.items(), key=lambda kv: kv[1]):
        analysis = repo.analysis_for(content_id)
        if analysis is None:
            continue
        print(
            f"  {analysis.platform_stance:4s} {analysis.risk_level:4s} "
            f"{(analysis.risk_reasoning or '')[:38]}"
        )

    print()
    print("（内存库：线上库一个字节都没动，进程退出即消失）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
