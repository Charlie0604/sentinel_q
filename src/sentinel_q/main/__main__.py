"""主程序：唯一把模块接起来的地方（架构文档 2 / 7.2）。

    python -m sentinel_q.main ingest --run 20260928-153000

## ⚠️ 这里只有接线，没有业务逻辑

业务逻辑属于各个模块。一旦这里出现"顺手处理一下"的代码，模块边界就开始烂了——
因为那种代码两边都放得下，最后两边都改。

## 为什么采集和入库分成两趟（决策 52）

采集模块**不连数据库**，它只产 `runtime/ops/<run>/contents.jsonl`（连同快照文件）。
所以入库这件事必须有一个人来做，而那个人只能是这里：

    采集（一趟浏览器，几十分钟） → contents.jsonl → 这里读回来 → storage 入库

拆开之后有两个好处，都不是理论上的：

  1. **采集能在一个没有数据库的环境里跑完。** 库连不上不该让已经采到的东西作废。
  2. **入库能重跑。** 采集崩在第 400 条、或者库临时挂了，直接对着
     `contents.jsonl` 重跑一次入库就行，不用再爬一遍。

代价是查重得分成两半："文件"那一半在采集侧（读这份文档已有的 URL），
"库"那一半只有这里给得出来（`storage.existing_urls()`）——
合起来才是决策 51 那句"文件 ∪ 库"。

## 完整的流水线长这样（现在只接了最后一段）

    collector search   →  urls.jsonl
    collector content  →  contents.jsonl（正文 + 快照）
    analyst run        →  把判定结果写回**同一行**
    main ingest        →  读回这些行，交给 storage 入库          ← 已经能跑

中间那一步还没接线：不变量是"AI 判完才入库"（决策 51），
所以 `ingest` 迟早要顺手把 `fact_analysis` 一起写进去——那也是同一批。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sentinel_q.collector import extract, ops
from sentinel_q.shared.config import Settings, paths
from sentinel_q.storage.fake import FakeRepo
from sentinel_q.storage.ingest import IngestReport, insert_contents
from sentinel_q.storage.repo import Repo
from sentinel_q.storage.supabase import SupabaseRepo

log = logging.getLogger("main")


def _repo() -> Repo:
    """取仓储实现。**不静默降级。**

    DSN 没配就直接退出：这一趟的全部意义就是把采到的东西写进库，
    没有库可写就等于什么都没做。用内存库跑出来的"成功"是最糟的结果——
    它看起来跑通了，而数据在进程退出时一起没了。

    ⚠️ 这是**入库**这一步的规矩，不是采集那一步的。采集侧压根没有"库"这个概念，
    所以"库没配"永远不会让一次已经开始的采集中断。
    """
    settings = Settings.from_env()
    if not settings.supabase_dsn:
        raise SystemExit(
            "❌ 没有配 SUPABASE_DSN，没有库可写，**一条都不会入库**。\n"
            "   采到的东西还在 contents.jsonl 里：配好之后再跑一次这条命令就行，\n"
            "   不用再爬一遍——这正是采集和入库拆成两趟的好处（决策 52）。"
        )
    return SupabaseRepo(settings.supabase_dsn)


def load_rows(run_dir: Path) -> list[dict]:
    """读回一次采集的产物。文件不存在或一行都没有，都返回空列表——不是错误。

    直接复用采集模块的 `OpsStore`，而不是在这里自己开文件读：`contents.jsonl`
    的格式（空行、半截 JSON 怎么处理）是它的知识，抄一份就是第二个真相源。
    """
    return list(ops.OpsStore.resume(run_dir).iter_contents())


def ingest_run(run_dir: Path, *, repo: Repo) -> IngestReport | None:
    """把一次任务的 `contents.jsonl` 读回来，按行序写进 `repo`。

    ⚠️ **顺序不能动**：`fact_content.parent_id` 是 uuid，指向另一行的
    `content_id`，而文档里装的是**知乎那边的 ID**。中间这一跳只能靠
    "同一批里父级先插过"来搭，所以这里必须**按文件行序单趟插入**——
    评论的父级（正文）要排在它自己的评论前面。顺序由采集侧保证
    （见 `collector.__main__._crawl_question`），这里只负责不把它打乱。

    返回 None 表示这份产物是空的——**那不是错误**，是"这次还没采到东西"。

    ⚠️ 这个函数**不知道库是从哪来的**（参数递进来的），所以测试能拿一个
    `FakeRepo` 直接调它，不必去 patch 一个连接串。
    """
    rows = load_rows(run_dir)
    if not rows:
        log.info("%s 里还没有东西，无事可做", run_dir / "contents.jsonl")
        return None

    unbuildable = 0
    records = []
    for row in rows:
        record = extract.from_document(row)
        if record is None:
            unbuildable += 1
            continue
        records.append(record)

    # ⚠️ 全都装不成记录时**在开库之前**退出：连上去再报"入库 0 条"，
    #    看起来像跑通了。
    if not records:
        raise SystemExit(
            f"❌ {len(rows)} 行一条都装不成记录（没有知乎 ID / URL，或者类型认不出来）。"
            "每一行的原因都在上面的警告里。"
        )

    report = insert_contents(records, repo=repo)
    if unbuildable:
        log.warning(
            "⚠️ %d 行（共 %d 行）装不成记录，已跳过——上面有逐行的原因",
            unbuildable,
            len(rows),
        )
    return report


def cmd_ingest(args: argparse.Namespace) -> int:
    """`ingest` 子命令：挑一个仓储，然后交给 `ingest_run`。"""
    run_dir = paths().ops / args.run
    if not (run_dir / "task.json").exists():
        raise SystemExit(
            f"❌ 找不到任务 {run_dir}。用 `python -m sentinel_q.collector status` 看看有哪些。"
        )

    repo: Repo = FakeRepo() if args.dry_run else _repo()
    try:
        report = ingest_run(run_dir, repo=repo)
    finally:
        if isinstance(repo, SupabaseRepo):
            repo.close()  # 长连接必须显式关，见 SupabaseRepo.__exit__ 的说明

    if report is None:
        return 0
    log.info("%s：%s", args.run, report.describe())
    if args.dry_run:
        print("（--dry-run：写的是内存库，线上库一个字节都没动）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentinel_q.main",
        description="主程序：把采集、AI、数据库三个模块接成一条流水线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "典型顺序：\n"
            "  python -m sentinel_q.collector search  --keyword 某公司\n"
            "  python -m sentinel_q.collector content --run <run-id>\n"
            "  python -m sentinel_q.main ingest       --run <run-id>\n"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="打印调试日志")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<子命令>")

    p_ingest = sub.add_parser(
        "ingest",
        help="把一次采集的 contents.jsonl 写进库",
        description=(
            "读 runtime/ops/<run>/contents.jsonl，按**文件行序**写进 fact_content。\n"
            "⚠️ 顺序是硬的：评论的 parent_id 指向正文那一行的 uuid，"
            "父级没先插过就全挂空（而且不报错）。所以不要并行、不要排序。"
        ),
    )
    p_ingest.add_argument(
        "--run", required=True, metavar="ID", help="任务 ID（runtime/ops 下的目录名）"
    )
    p_ingest.add_argument(
        "--dry-run",
        action="store_true",
        help="写内存库，线上库一个字节都不动——但仍然会报出有多少条挂空",
    )
    p_ingest.set_defaults(func=cmd_ingest)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    paths().ensure()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
