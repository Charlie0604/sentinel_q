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
"""

from __future__ import annotations

import hashlib
import logging
import re
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
    """建 `schema_migrations`。幂等，每次跑都先来一遍。"""
    with conn.cursor() as cur:
        cur.execute(TRACKING_TABLE)
    conn.commit()


def tracking_exists(conn: Any) -> bool:
    """`schema_migrations` 建了没有。**只读**，`--dry-run` 靠它避免写库。"""
    with conn.cursor() as cur:
        cur.execute("select to_regclass('public.schema_migrations') is not null")
        return bool(cur.fetchone()[0])


def read_applied(conn: Any) -> dict[str, str]:
    """库里已执行的迁移：version → checksum。调用前先 `ensure_tracking`。"""
    with conn.cursor() as cur:
        cur.execute("select version, checksum from schema_migrations order by version")
        return {row[0]: row[1] for row in cur.fetchall()}


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
                cur.execute(
                    "insert into schema_migrations (version, checksum) values (%s, %s)",
                    (migration.version, migration.checksum),
                )
        except Exception:
            conn.rollback()
            log.error("❌ %s 失败，已回滚该文件。", migration.version)
            raise
        conn.commit()
        done.append(migration.version)
        log.info("✅ %s", migration.version)
    return done
