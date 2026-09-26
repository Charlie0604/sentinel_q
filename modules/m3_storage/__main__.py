"""模块三独立入口（架构文档 8.4）。

    python -m modules.m3_storage migrate     # 应用 migrations/ 下未执行的迁移
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="m3_storage", description="模块三：数据存储层")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="应用未执行的迁移")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raise NotImplementedError(
        f"迁移执行器尚未实现（command={args.command}）。"
        "表结构以架构文档第五章为准，落到 migrations/ 下的编号 SQL 文件。"
    )


if __name__ == "__main__":
    sys.exit(main())
