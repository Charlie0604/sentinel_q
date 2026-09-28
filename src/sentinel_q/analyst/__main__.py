"""AI 模块的独立入口（架构文档 7.4）。

    python -m sentinel_q.analyst run --batch 8      # 跑一轮分析
    python -m sentinel_q.analyst run --once         # 只跑一轮就退出

## 事件分类也归这里（原"模块四"，决策 52）

它和内容判定是**同一套机器**——拼 prompt → 调模型 → 解析答复 → 落一张 fact 表，
只差 prompt 内容和落哪张表（`fact_content_event`）。原 `modules/m4_classifier`
的独立入口已随重构删除，将来在这里加一个 `event` 子命令即可。
预筛查询见 4.5.2 ⚠️ 匹配集必须含 `ai_summary`，否则会漏掉全部长内容。

## ⚠️ `prompts pull` / `prompts push` **不在这里**（决策 52）

那两个动作每一行都要连库，所以它们搬去 `storage` 了：

    python -m sentinel_q.storage prompts pull
    python -m sentinel_q.storage prompts push -v 3

这不是"挪个位置"：搬走之后这个模块**一个 Repo 参数都不收**，
于是它能在一个没有数据库、没有网络的环境里完整跑起来——
而那正是"独立测试"这句话的字面意思（架构文档 7.5）。
"""

from __future__ import annotations

import argparse
import logging
import sys

from sentinel_q.shared.config import paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sentinel_q.analyst", description="AI 模块：内容判定")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="跑一轮分析")
    run.add_argument("--batch", type=int, default=8, help="一次推送给 AI 的条数（4.2）")
    run.add_argument("--once", action="store_true", help="只跑一轮就退出，不等新内容")

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    paths().ensure()

    raise NotImplementedError(
        f"分析尚未实现（batch={args.batch}）。任务 A/B 的设计见架构文档第四章。"
    )


if __name__ == "__main__":
    sys.exit(main())
