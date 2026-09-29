"""迁移执行器的引擎（架构文档 7.6）。

和 CLI 分开是为了能**离线单测**：引擎只认"一个连接对象"，所以测试可以喂一个
记录 SQL 的假连接进去，不需要真库，也不需要装 psycopg。

## 断点就是 `schema_migrations` 表

不由本地文件记——这是 7.8 的不变量：本地文件必须任何时候都能删掉。
所以"哪些迁移跑过了"这个状态只存在于库里，换台机器、删掉整个 `runtime/` 都还在。

表由执行器自己建，**不进编号文件**：它是执行器的基础设施，不是业务 schema。
放进编号文件会有"先有鸡还是先有蛋"的问题——第一次运行时它自己还没被建出来。

## 校验和 = 机械地兜住 README 的「只增不改」

每个迁移文件的 sha256 记在库里。下次跑的时候对不上就**拒绝执行**，不是警告。
理由：文件被改过，说明库和文件已经对不上了，继续往下跑只会把偏差滚大。

对不上时有**三条**出路（README 也写了）：把文件改回原样、新增一个编号、
以及 `migrate --accept-drift <版本号> -n "<理由>"`。

第三条不是"把检查关掉"。哈希照旧严格比对，变的只是**对不上之后**：
`--accept-drift` 把"我看过差异、确认表结构没变"这个**判断本身**变成库里的一条记录
（新旧校验和 + 理由 + 时间）。它存在是因为引擎手里只有一个哈希——从哈希看，
"注释里改了个字"和"DDL 改了、线上表已经对不上文件"长得一模一样，
所以这个判断只能由人做，而人做过的事得留下痕迹。

要让这个判断**有依据**，库里得存着"当初那版正文"才打得出差异（`applied_sql`）。
这条迁移跑在这个功能之前时那一列是空的，第一次接受就只能看到两个哈希——
这一点 `drift_report` 必须**说出来**，不能假装差异是空的。
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("sentinel_q.storage.migrate")

TRACKING_TABLE = """
create table if not exists schema_migrations (
  version    text primary key,
  checksum   text not null,
  applied_at timestamptz not null default now()
);
alter table schema_migrations enable row level security;

-- 下面四列给 `migrate --accept-drift` 用。加在这里而不是新增一个编号文件，
-- 是因为接受漂移是"让 migrate 能跑"的前提——它依赖的列如果要等 migrate
-- 才加上，就成了死锁。schema_migrations 本来就是执行器自己的基础设施。
--
-- applied_sql            库**现在认定生效**的那一版文件正文。
--                        不变量：checksum == sha256(applied_sql)（换行归一化后），
--                        apply / 补齐 / 接受漂移三处一起写。
--                        ⚠️ 记的是"库现在认哪一版"，**不是**"当初跑的那一版"——
--                        接受漂移时它连正文一起换成新文件的。没接受过漂移时两者
--                        是一回事；漂移过之后，"当初那一版"只剩哈希可查。别拿它
--                        当执行日志用。
-- accepted_at            最近一次人工接受漂移的时间。null = 从没接受过。
-- accepted_note          那次接受的理由。这是将来唯一能回答「当初为什么放行」的地方。
-- accepted_from_checksum 被替换掉的那个校验和。⚠️ 只留最近一次（决策 5：不保留
--                        修改历史），但没有它的话，"接受过漂移"这件事本身没痕迹。
alter table schema_migrations
  add column if not exists applied_sql            text,
  add column if not exists accepted_at            timestamptz,
  add column if not exists accepted_note          text,
  add column if not exists accepted_from_checksum text;
"""

# 0000_extensions.sql 这种形状。编号定宽四位，字符串排序才等于数字排序。
_FILENAME = re.compile(r"^\d{4}_[a-z0-9_]+\.sql$")


@dataclass(frozen=True)
class Migration:
    version: str  # 文件名去后缀，如 "0001_business"
    path: Path
    sql: str

    @property
    def checksum(self) -> str:
        """文件内容的 sha256。

        先把换行统一成 `\\n` 再算：不同系统上 checkout 出来的行尾可能不一样，
        而那种差异会触发一次**看着很吓人的漂移告警**，实际什么都没有变。

        做成 property 而不是字段，是为了不存在"checksum 和 sql 对不上"的实例。
        """
        normalized = self.sql.replace("\r\n", "\n")
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def discover(migrations_dir: Path) -> list[Migration]:
    """列出目录下的迁移，**按文件名排序**。

    排序按文件名而不是文件系统给的顺序：`os.listdir` 的顺序是未定义的，
    而 `0002` 跑到 `0001` 前面会因为外键指向还不存在的表而失败。

    名字不合规的文件直接报错，不跳过。`0001_business copy.sql` 这种
    编辑器产物如果被当成新迁移执行，就是往线上库灌一段没人看过的 DDL。
    """
    paths = sorted(migrations_dir.glob("*.sql"), key=lambda p: p.name)
    migrations = []
    for path in paths:
        if not _FILENAME.match(path.name):
            raise ValueError(
                f"迁移文件名不合规：{path.name}\n"
                "   要求形如 0001_business.sql（四位编号 + 小写下划线 + .sql）。\n"
                "   不合规的文件不会被跳过——那等于悄悄少跑一个迁移。"
            )
        migrations.append(
            Migration(version=path.stem, path=path, sql=path.read_text(encoding="utf-8"))
        )
    return migrations


@dataclass
class Plan:
    """待执行 / 漂移 / 孤儿。`blocked` 为真时**一个文件都不许执行**。"""

    pending: list[Migration] = field(default_factory=list)
    drifted: list[tuple[Migration, str]] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)  # 库里有、文件里没有

    @property
    def blocked(self) -> bool:
        return bool(self.drifted or self.orphaned)

    def describe(self) -> str:
        lines = []
        for migration in self.pending:
            lines.append(f"  待执行  {migration.version}")
        for migration, recorded in self.drifted:
            lines.append(f"  ⚠️ 已改动  {migration.version}（库里记的是 {recorded[:12]}…）")
        for version in self.orphaned:
            lines.append(f"  ⚠️ 库里有、文件里没有  {version}")
        return "\n".join(lines) if lines else "  （没有待执行的迁移）"


def connect(dsn: str) -> Any:
    """连库。**延迟导入 psycopg**——没装 `db` extra 时，本模块的纯逻辑仍然可用。

    两个参数都不是可选的：

    - `cursor_factory=ClientCursor`：迁移文件是多条语句，默认 cursor 走扩展协议，
      一个 `execute()` 只能放一条，而它**不报错**——只执行第一条。见 `apply`。
    - `prepare_threshold=None`：DSN 走 6543 连接池端口（pgbouncer 事务模式），
      预编译语句在那里会失效。
    """
    import psycopg

    return psycopg.connect(
        dsn,
        cursor_factory=psycopg.ClientCursor,
        prepare_threshold=None,
    )


def ensure_tracking(conn: Any) -> None:
    """建 `schema_migrations`，并把后加的四列补齐。幂等，每次跑都先来一遍。

    ⚠️ 那四列是 `add column if not exists`，所以老库上这一句就是**升级动作**。
    `--accept-drift` 依赖 `applied_sql`，所以它必须先跑这一句——
    `status` 和 `--dry-run` 则相反，**一次都不能跑**（它们只读）。
    """
    with conn.cursor() as cur:
        cur.execute(TRACKING_TABLE)
    conn.commit()


def tracking_exists(conn: Any) -> bool:
    """`schema_migrations` 建了没有。**只读**，`--dry-run` 靠它避免写库。"""
    with conn.cursor() as cur:
        cur.execute("select to_regclass('public.schema_migrations') is not null")
        return bool(cur.fetchone()[0])


def tracking_columns(conn: Any) -> set[str]:
    """`schema_migrations` 现有哪些列。**只读**。

    为什么需要它：`status` 和 `--dry-run` 都**不能**调 `ensure_tracking`（那是写操作），
    但它们要读 `applied_sql`——而那一列是后加的，老库上还没有。所以先只读地问一句
    "有哪些列"，再决定 SELECT 带不带它。

    ⚠️ **存在性判据不是"这个集合空不空"**，是 `tracking_exists`。
    `information_schema.columns` 在"表不存在"和"没权限"两种情况下都返回空，
    混在一起会让 `migrate` 以为所有迁移都要重跑——那是往线上库重灌 DDL。
    """
    with conn.cursor() as cur:
        cur.execute(
            "select column_name from information_schema.columns"
            " where table_schema = 'public' and table_name = 'schema_migrations'"
        )
        return {row[0] for row in cur.fetchall()}


def read_applied_sql(conn: Any) -> dict[str, str]:
    """库里存着的正文：version → sql。**没存过的行不在字典里**（不是空串）。

    调用前必须确认那一列存在（`tracking_columns`）——它是后加的，直接 SELECT
    会在 `--dry-run` / `status` 这两条不能建列的路径上炸掉。走 `read_state` 更省事。
    """
    with conn.cursor() as cur:
        cur.execute(
            "select version, applied_sql from schema_migrations"
            " where applied_sql is not null order by version"
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def read_applied(conn: Any) -> dict[str, str]:
    """库里已执行的迁移：version → checksum。调用前先确认表在（走 `read_state`）。"""
    with conn.cursor() as cur:
        cur.execute("select version, checksum from schema_migrations order by version")
        return {row[0]: row[1] for row in cur.fetchall()}


def read_state(conn: Any) -> tuple[dict[str, str], dict[str, str]]:
    """一次读齐：（version → checksum，version → 存着的正文）。

    **只读**，`status` 与 `--dry-run` 走这条——它自己判断表和列在不在，
    一个字节都不写。老库上还没有 `applied_sql` 时返回空字典，那是"没存过正文"，
    不是错误。
    """
    if not tracking_exists(conn):
        return {}, {}
    applied = read_applied(conn)
    if "applied_sql" not in tracking_columns(conn):
        return applied, {}
    return applied, read_applied_sql(conn)


def backfill_applied_sql(
    conn: Any,
    migrations: list[Migration],
    applied: dict[str, str],
    stored: dict[str, str],
) -> list[str]:
    """给"checksum 对得上、但没存正文"的老行补上正文。返回补了哪些版本。

    ⚠️ **只在 checksum 完全相等时才补。** 哈希相等就是"文件与当初执行的那一版
    逐字节相同"的证明（换行归一化后），所以补进去的**确实是当初跑的那一版**，
    不是猜的。对不上的（已经漂移的）一律不补——补了就是把没跑过的正文说成跑过的，
    恰好是这个功能要防的那件事。

    补完一次 commit（不逐行提交）：这不是 DDL，逐行提交只会把调用方
    "一个文件一个事务"的边界搅乱。

    ⚠️ 只在**写路径**上调（`cmd_migrate` / `--accept-drift`），而且必须在
    `ensure_tracking` 之后——`applied_sql` 那一列是它加的，没有那一列时
    这里的 UPDATE 会直接报错。
    """
    filled: list[str] = []
    for migration in migrations:
        if stored.get(migration.version) is not None:
            continue
        if applied.get(migration.version) != migration.checksum:
            continue
        with conn.cursor() as cur:
            cur.execute(
                "update schema_migrations set applied_sql = %s where version = %s",
                (migration.sql, migration.version),
            )
        filled.append(migration.version)
    if filled:
        conn.commit()
    return filled


def plan(applied: dict[str, str], migrations: list[Migration]) -> Plan:
    """算出要跑什么，以及能不能跑。"""
    by_version = {m.version: m for m in migrations}
    result = Plan(
        pending=[m for m in migrations if m.version not in applied],
        drifted=[
            (by_version[version], recorded)
            for version, recorded in applied.items()
            if version in by_version and by_version[version].checksum != recorded
        ],
        # 文件删了但库里记着：说明本地和库已经不是同一份契约了
        orphaned=sorted(version for version in applied if version not in by_version),
    )
    return result


def _diff_lines(text: str) -> list[str]:
    """按 `\\n` 切，并保证每一行都以 `\\n` 收尾。

    `difflib` 不补行尾，少了这一步，"最后一行没有换行符"的文件会把 diff 的两行
    粘成一行。按 `\\n` 切（而不是 `splitlines()`）是刻意的：那样 `\\r` 会留在行尾，
    于是行尾差异是**看得见的**，而不是被悄悄抹平。
    """
    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()  # 文件以换行结尾时 split 会多出一个空串
    return [part + "\n" for part in parts]


def sql_diff(stored: str, current: str, *, version: str) -> str:
    """两版迁移文件的逐行差异（unified diff）。**纯函数**，离线可测。"""
    return "".join(
        difflib.unified_diff(
            _diff_lines(stored),
            _diff_lines(current),
            fromfile=f"库（{version}，执行时）",
            tofile=f"本地（{version}.sql）",
            n=2,
        )
    )


def drift_report(
    migration: Migration, *, recorded: str, stored_sql: str | None
) -> str:
    """一条漂移的详细说明：新旧哈希、能打就打的逐行差异、以及接受漂移的命令行。

    **纯函数**（只吃字符串），离线可测。

    ⚠️ `stored_sql is None` 时**必须说"打不出差异"**。拿现有文件跟自己 diff
    会得到一个空的差异，而那看起来和"确认过没问题"一模一样——等于替人签了字。
    """
    lines = [
        f"  ⚠️ 已改动  {migration.version}",
        f"       库里记的是 {recorded}",
        f"       本地文件是 {migration.checksum}",
    ]
    if stored_sql is None:
        lines += [
            (
                "       库里没存这一版执行时的正文（这条迁移跑在「存正文」之前），"
                "打不出差异，"
            ),
            (
                "       只能确认两个哈希不一样。接受之后当前正文就存进去了，"
                "以后能看差异。"
            ),
        ]
    else:
        lines += [
            "       与库里存的那一版逐行差异：",
            textwrap.indent(
                sql_diff(stored_sql, migration.sql, version=migration.version),
                "       ",
            ),
        ]
    lines += [
        "       确认过表结构没变的话，照着这个跑（理由必填）：",
        f'         migrate --accept-drift {migration.version} -n "<一句理由>"',
    ]
    return "\n".join(lines)


def apply(conn: Any, migrations: list[Migration]) -> list[str]:
    """逐个执行，**每个文件一个事务**，返回执行成功的版本号。

    一个文件一个事务而不是全部包在一起：这样中途失败时，前面成功的那些留住了，
    下次接着跑就行。包在一起的话，第 3 个文件失败会把前 2 个一起回滚，
    而它们本来已经是对的。

    ⚠️ `conn.autocommit` 必须是关的，否则"一个文件一个事务"就是假的——
    每条语句各自提交，回滚回不来。
    """
    if getattr(conn, "autocommit", False):
        raise ValueError("连接开了 autocommit，迁移会失去事务保护，拒绝执行。")

    done: list[str] = []
    for migration in migrations:
        try:
            with conn.cursor() as cur:
                # 整个文件一次执行。cursor 必须是 ClientCursor（见 connect），
                # 否则这里只会跑第一条语句，而且不报错。
                cur.execute(migration.sql)
                # 连正文一起存（applied_sql）：以后这条文件要是漂移了，
                # 才拿得出"当初那一版"跟现在这版打差异。
                cur.execute(
                    "insert into schema_migrations (version, checksum, applied_sql)"
                    " values (%s, %s, %s)",
                    (migration.version, migration.checksum, migration.sql),
                )
        except Exception:
            conn.rollback()
            log.error("❌ %s 失败，已回滚该文件。", migration.version)
            raise
        conn.commit()
        done.append(migration.version)
        log.info("✅ %s", migration.version)
    return done


def accept_drift(
    conn: Any, migration: Migration, *, note: str, recorded: str
) -> None:
    """把"我确认过表结构没变"记进库。

    ⚠️ `note` 空白就抛 `ValueError`。这条记录是将来**唯一**能回答
    「当初为什么放行」的地方，而"接受了但不知道为什么"跟没记一样。

    ⚠️ **一次只改一个版本**。"不能一把梭"是这个功能的明确要求：每个版本都要
    单独看一眼差异、单独写一句理由。调用方负责先确认它确实在漂移
    （走 `plan().drifted`，`recorded` 就是库里那条旧 checksum）。

    ⚠️ 旧 checksum 挪进 `accepted_from_checksum`，**不是丢掉**——否则
    "接受过漂移"这件事本身就没留下任何痕迹，而它恰恰是最该留痕的。

    ⚠️ `applied_sql` 一起换成**新文件**的正文，所以它记的是"库现在认哪一版"。
    不变量 `checksum == sha256(applied_sql)` 由 apply / 补齐 / 这里三处一起维持。
    """
    if not note or not note.strip():
        raise ValueError(
            "接受漂移必须写一句理由（-n/--note）——"
            "它是将来唯一能回答「当初为什么放行」的地方。"
        )
    with conn.cursor() as cur:
        cur.execute(
            "update schema_migrations"
            "   set checksum = %s, applied_sql = %s,"
            "       accepted_at = now(), accepted_note = %s,"
            "       accepted_from_checksum = %s"
            " where version = %s",
            (
                migration.checksum,
                migration.sql,
                note.strip(),
                recorded,
                migration.version,
            ),
        )
    conn.commit()
