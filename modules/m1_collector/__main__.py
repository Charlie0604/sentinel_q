"""模块一独立入口（架构文档 8.4）。

    python -m modules.m1_collector --mode backfill     # 全域全时间搜索，首次建库
    python -m modules.m1_collector --mode update       # 日常增量
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="m1_collector", description="模块一：内容采集")
    parser.add_argument("--mode", choices=["backfill", "update"], required=True)
    parser.add_argument("--workers", type=int, default=1, help="并行采集进程数（1~4）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raise NotImplementedError(
        f"采集尚未实现（mode={args.mode}, workers={args.workers}）。"
        "推进顺序见架构文档 8.6：先冻结 core/ 与 migrations，再填模块内部。"
    )


if __name__ == "__main__":
    sys.exit(main())
