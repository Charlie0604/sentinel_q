"""模块二独立入口（架构文档 8.4）。

    python -m modules.m2_analyst run --batch 8     # 跑一轮分析
    python -m modules.m2_analyst prompts pull      # 从线上库拉提示词到本地工作区
    python -m modules.m2_analyst prompts push -v 3 # 把本地工作区推上库，标记版本 3
"""

from __future__ import annotations

import argparse
import logging
import sys

from core.config import Settings, paths
from core.repo.supabase import SupabaseRepo

from modules.m2_analyst import prompt_loader


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="m2_analyst", description="模块二：AI 分析")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="跑一轮分析")
    run.add_argument("--batch", type=int, default=8, help="一次推送给 AI 的条数（4.2）")
    run.add_argument("--once", action="store_true", help="只跑一轮就退出，不等新内容")

    prompts = sub.add_parser("prompts", help="提示词与线上库同步")
    prompts.add_argument("action", choices=["pull", "push"])
    prompts.add_argument("-v", "--version", type=int, help="push 时标记的版本号")
    prompts.add_argument("-n", "--note", help="push 时说明这次改了什么")

    return parser


def _open_repo() -> SupabaseRepo:
    settings = Settings.from_env()
    if not settings.supabase_dsn:
        raise SystemExit("缺少 SUPABASE_DSN，见仓库根目录 .env.example")
    return SupabaseRepo(settings.supabase_dsn)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    layout = paths()

    if args.command == "prompts":
        repo = _open_repo()
        if args.action == "pull":
            bundle = prompt_loader.fetch_bundle(repo, layout)
            # pull 的目标是**工作区**，不是运行缓存——工作区才是人工编辑的地方
            for name, body in bundle.modules.items():
                (layout.prompts / f"{name}.txt").write_text(body, encoding="utf-8")
            print(f"已拉取提示词 v{bundle.version}（hash {bundle.content_hash}）到 {layout.prompts}")
            return 0

        if args.version is None:
            raise SystemExit("push 需要 -v 指定版本号")
        bundle = prompt_loader.bundle_from_workdir(layout.prompts, args.version, args.note)
        missing = bundle.missing_modules()
        if missing:
            print(f"⚠️ 以下模块在本地工作区是空的，将推上空内容：{', '.join(missing)}")
        repo.push_prompt_bundle(bundle)
        print(f"已推送提示词 v{bundle.version}（hash {bundle.content_hash}）")
        return 0

    raise NotImplementedError(
        f"分析尚未实现（batch={args.batch}）。任务 A/B 的设计见架构文档第四章。"
    )


if __name__ == "__main__":
    sys.exit(main())
