"""模块五独立入口（架构文档 8.4）。

    python -m modules.m5_console     # 启动后端服务
"""

from __future__ import annotations

import argparse
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="m5_console", description="模块五：用户交互界面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    raise NotImplementedError(
        f"界面尚未实现（{args.host}:{args.port}）。"
        "基础骨架优先做预警看板与内容复核，见架构文档 7.1/7.3。"
    )


if __name__ == "__main__":
    sys.exit(main())
