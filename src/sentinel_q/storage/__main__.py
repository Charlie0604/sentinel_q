"""数据库模块的独立入口（架构文档 7.4）。

    python -m sentinel_q.storage status               # 看迁移跑到哪了
    python -m sentinel_q.storage migrate --dry-run    # 只打印计划，一个字节都不写
    python -m sentinel_q.storage migrate              # 真的推到库上
    python -m sentinel_q.storage prompts pull         # 从线上库拉提示词到本地工作区
    python -m sentinel_q.storage prompts push -v 3    # 把本地工作区推上库，标记版本 3

⚠️ `migrate` 是**不可逆的外部动作**——它往线上库写 DDL。所以先 `--dry-run`
看一眼，尤其是第一次。没有二次确认，是为了能脚本化调用（和采集采集模块致）。

表结构以架构文档第五章为准，落到 `migrations/` 下的编号 SQL 文件；
执行状态记在库里的 `schema_migrations`，不记本地（7.8：本地文件随时可删）。

## 提示词同步为什么在这里（决策 52）

`prompts pull/push` 原本挂在 AI 模块下。它们每一行都要连库，
而 AI 模块的硬边界是"不连数据库"——搬过来之后 AI 模块才能真正独立运行。
详见 `storage/prompts.py` 的模块开头。
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from sentinel_q.shared.config import Settings, paths
from sentinel_q.storage import migrate, prompts

log = logging.getLogger("storage")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def _dsn() -> str:
    """取连接串。**不静默降级**——没有库就没有数据库模块，猜一个默认值只会更难查。"""
    settings = Settings.from_env()
    if not settings.supabase_dsn:
        raise SystemExit(
            "❌ 没有配 SUPABASE_DSN，连不上库。\n"
            "   复制 .env.example 成 .env（已在 .gitignore 里），填上 Supabase 的\n"
            "   **连接池**连接串——端口 6543，用户名是 postgres.<项目ref>。\n"
            "   填好之后 status / migrate 都能读到。"
        )
    return settings.supabase_dsn


def _report(migration: migrate.Migration, applied: dict[str, str]) -> str:
    recorded = applied.get(migration.version)
    if recorded is None:
        return f"  待执行  {migration.version}"
    if recorded != migration.checksum:
        return f"  ⚠️ 已改动  {migration.version}（库里记的是 {recorded[:12]}…）"
    return f"  已执行  {migration.version}"


def cmd_status(args: argparse.Namespace) -> int:
    """列出三个迁移各自的状态。漂移或孤儿时退出码 1，方便脚本判断。"""
    found = migrate.discover(MIGRATIONS_DIR)
    if not found:
        print(f"{MIGRATIONS_DIR} 下没有迁移文件。")
        return 0

    with migrate.connect(_dsn()) as conn:
        applied = migrate.read_applied(conn) if migrate.tracking_exists(conn) else {}
        result = migrate.plan(applied, found)

    print(f"迁移目录：{MIGRATIONS_DIR}")
    for migration in found:
        print(_report(migration, applied))
    if result.orphaned:
        print("  ⚠️ 库里有、文件里没有：" + "、".join(result.orphaned))
    if result.drifted or result.orphaned:
        print("\n⚠️ 本地文件和库对不上了，不要继续 migrate。见 migrations/README.md。")
        return 1
    print("\n本地文件和库一致。")
    return 0


def cmd_migrate(args: argparse.Namespace) -> int:
    found = migrate.discover(MIGRATIONS_DIR)
    if not found:
        print(f"{MIGRATIONS_DIR} 下没有迁移文件，无事可做。")
        return 0

    with migrate.connect(_dsn()) as conn:
        if args.dry_run:
            # ⚠️ 这条分支里**一个写操作都不能有**，包括建 schema_migrations。
            # 所以用只读的 tracking_exists 探一下，而不是 ensure_tracking。
            applied = migrate.read_applied(conn) if migrate.tracking_exists(conn) else {}
            result = migrate.plan(applied, found)
            print("（--dry-run，一个字节都不会写）")
            print(result.describe())
            if result.blocked:
                print("\n⚠️ 上面这些对不上，真跑的话会被拦下。见 migrations/README.md。")
            return 0

        migrate.ensure_tracking(conn)
        applied = migrate.read_applied(conn)
        result = migrate.plan(applied, found)

        if result.blocked:
            print(result.describe())
            raise SystemExit(
                "❌ 已执行过的迁移文件被改动过（或文件被删了），**一个都没执行**。\n"
                "   库和文件已经不是同一份契约，继续跑只会把偏差滚大。\n"
                "   改法：把文件改回原样，或者新增一个编号（migrations/README.md）。"
            )

        if not result.pending:
            print("没有待执行的迁移，库已经是最新的。")
            return 0

        print(result.describe())
        done = migrate.apply(conn, result.pending)

    print(f"\n✅ 执行了 {len(done)} 个迁移：" + "、".join(done))
    return 0


def cmd_prompts(args: argparse.Namespace) -> int:
    """提示词与线上库同步。**权威副本在库里**，本地两份都是副本。"""
    from sentinel_q.storage.supabase import SupabaseRepo

    layout = paths()
    with SupabaseRepo(_dsn()) as repo:
        if args.action == "pull":
            bundle = prompts.pull_to_workdir(repo, layout)
            print(f"已拉取提示词 v{bundle.version}（hash {bundle.content_hash}）到 {layout.prompts}")
            return 0

        if args.version is None:
            raise SystemExit("push 需要 -v 指定版本号")
        bundle = prompts.push_from_workdir(repo, layout, args.version, args.note)

    # ⚠️ 空模块照样推（不然改到一半没东西可推），但**必须说出来**：
    #    推到库上的东西是判断结果的追溯依据，空着一块而没人知道是最坏的情况。
    missing = bundle.missing_modules()
    if missing:
        print(f"⚠️ 以下模块在本地工作区是空的，已推上空内容：{', '.join(missing)}")
    print(f"已推送提示词 v{bundle.version}（hash {bundle.content_hash}）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentinel_q.storage",
        description="数据库模块：表结构 + 提示词，所有数据库通信的收口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "典型顺序：\n"
            "  status  →  migrate --dry-run  →  migrate\n"
            "  prompts pull  →  人工改 prompts/  →  prompts push -v N\n"
            "\n"
            "⚠️ migrate 往线上库写 DDL，不可逆。第一次先 --dry-run 看一眼。\n"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="打印调试日志（默认只打 info 及以上）",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<子命令>")

    p_migrate = sub.add_parser("migrate", help="应用未执行的迁移")
    p_migrate.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划，一个字节都不写（连 schema_migrations 都不建）",
    )
    p_migrate.set_defaults(func=cmd_migrate)

    p_status = sub.add_parser("status", help="列出已执行 / 待执行 / 校验和漂移")
    p_status.set_defaults(func=cmd_status)

    p_prompts = sub.add_parser("prompts", help="提示词与线上库同步")
    p_prompts.add_argument("action", choices=["pull", "push"])
    p_prompts.add_argument("-v", "--version", type=int, help="push 时标记的版本号")
    p_prompts.add_argument("-n", "--note", help="push 时说明这次改了什么")
    p_prompts.set_defaults(func=cmd_prompts)

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
