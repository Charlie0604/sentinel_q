"""模块四独立入口（架构文档 8.4）。

    python -m modules.m4_classifier --event 3     # 对某个事件跑一轮内容归属
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="m4_classifier", description="模块四：事件分类")
    parser.add_argument("--event", type=int, required=True, help="dim_event.event_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raise NotImplementedError(
        f"事件分类尚未实现（event={args.event}）。"
        "预筛查询见架构文档 6.2，注意匹配集必须含 ai_summary，否则会漏掉全部长内容。"
    )


if __name__ == "__main__":
    sys.exit(main())
