"""迁移执行器的**离线**用例——不连库、不装 psycopg。

引擎只认"一个连接对象"，所以这里喂一个假连接进去，把执行过的 SQL 记下来。
思路和 `FakeRepo` 一样（7.5：能独立测试的关键不在测试代码，而在数据库访问是否可以替换）。

要钉住的全是**错了不会报错**的那几处：

  1. 排序——`0002` 跑到 `0001` 前面会因外键指向不存在的表而失败，
     但那要等到真连库才看得见
  2. 校验和漂移**拒绝执行**，而且是一个都不执行（不能留下半截状态）
  3. 每个文件**一个事务**：中途失败时只有它自己回滚
  4. `--dry-run` 真的一个字节都不写

⚠️ 这里不验"DDL 本身对不对"——那是 SQL 的事，只能靠真连一次库来确认。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Self

import pytest

from sentinel_q.storage import __main__ as cli
from sentinel_q.storage import migrate
from sentinel_q.storage.migrate import Migration

# ── 假连接 ────────────────────────────────────────────────────────
#
# 记录的是**事件序列**（exec / commit / rollback），不是最终状态——
# "每个文件一个事务"只有看序列才验得出来。


class FakeCursor:
    def __init__(self, conn: FakeConn) -> None:
        self._conn = conn
        self._rows: list = []

    def execute(self, sql: str, params: object = None) -> None:
        self._conn.events.append(("exec", sql, params))
        if self._conn.fail_on and self._conn.fail_on in sql:
            raise RuntimeError("模拟中途失败")
        # 每次 execute 从队首取一批行喂给 fetchone / fetchall
        self._rows = self._conn.queue.pop(0) if self._conn.queue else []

    def fetchall(self) -> list:
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class FakeConn:
    def __init__(
        self,
        *,
        rows: list | None = None,
        fail_on: str | None = None,
        autocommit: bool = False,
    ) -> None:
        self.autocommit = autocommit
        self.events: list[tuple] = []
        self.queue = list(rows or [])
        self.fail_on = fail_on

    def cursor(self, *args: object, **kwargs: object) -> FakeCursor:
        return FakeCursor(self)

    def commit(self) -> None:
        self.events.append(("commit",))

    def rollback(self) -> None:
        self.events.append(("rollback",))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    @property
    def kinds(self) -> list[str]:
        """事件序列，形如 `["exec", "commit", "exec", "commit"]`。"""
        return [event[0] for event in self.events]

    @property
    def sqls(self) -> list[str]:
        return [event[1] for event in self.events if event[0] == "exec"]


def _migration(version: str, sql: str = "select 1;") -> Migration:
    return Migration(version=version, path=Path(f"{version}.sql"), sql=sql)


# ── 一、discover：按文件名排序 ─────────────────────────────────────
#
# 排序错了的代价不对称：`0002` 先跑，它引用的 `dim_prompt` 外键指向还没建的
# `dim_author`，Postgres 会报错——这还好。真正糟的是 `0000`（扩展）跑到后面，
# 于是所有建表语句里用到 pg_trgm 的地方静默换了一种解释。


def test_按文件名排序而不是文件系统顺序(tmp_path: Path) -> None:
    """故意乱序写文件，discover 必须按名字排回来。"""
    for name in ("0002_prompt.sql", "0000_extensions.sql", "0001_business.sql"):
        (tmp_path / name).write_text("select 1;", encoding="utf-8")

    found = migrate.discover(tmp_path)

    assert [m.version for m in found] == ["0000_extensions", "0001_business", "0002_prompt"]


def test_编号跨十位也排得对(tmp_path: Path) -> None:
    """⭐ 这条钉的是"四位定宽"这个前提。

    编号定宽四位，字符串排序才等于数字排序。要是哪天有人写成 `10_x.sql`，
    它会排在 `2_x.sql` **前面**，而文件系统顺序恰好又是对的——
    于是这个 bug 只在换一台机器时才冒出来。
    """
    for name in ("0010_c.sql", "0002_a.sql", "0009_b.sql", "0000_z.sql"):
        (tmp_path / name).write_text("select 1;", encoding="utf-8")

    found = migrate.discover(tmp_path)

    assert [m.version for m in found] == ["0000_z", "0002_a", "0009_b", "0010_c"]


def test_非_sql_文件不算迁移(tmp_path: Path) -> None:
    """README.md 跟迁移放在同一个目录，不能被当成迁移。"""
    (tmp_path / "README.md").write_text("说明", encoding="utf-8")
    (tmp_path / "0001_business.sql").write_text("select 1;", encoding="utf-8")

    assert [m.version for m in migrate.discover(tmp_path)] == ["0001_business"]


def test_空目录是空的而不是报错(tmp_path: Path) -> None:
    """只有一个 README 的目录也要能跑——`--dry-run` 那时该说"无事可做"。"""
    (tmp_path / "README.md").write_text("说明", encoding="utf-8")

    assert migrate.discover(tmp_path) == []


def test_名字不合规的文件直接报错(tmp_path: Path) -> None:
    """⭐⭐ 编辑器另存出来的 `0001_business copy.sql` 会被当成一个新迁移。

    后果是往线上库灌一段没人看过的 DDL，而且**它不会报任何错**。
    所以这里必须炸，不能跳过——跳过等于悄悄少跑一个迁移。
    """
    (tmp_path / "0001_business copy.sql").write_text("select 1;", encoding="utf-8")

    with pytest.raises(ValueError, match="不合规"):
        migrate.discover(tmp_path)


def test_校验和跟着内容走(tmp_path: Path) -> None:
    a = _migration("0001_a", "select 1;")
    b = _migration("0001_a", "select 1;")
    c = _migration("0001_a", "select 2;")

    assert a.checksum == b.checksum
    assert a.checksum != c.checksum


def test_换行差异不算改动() -> None:
    """⭐ 同一次 checkout 在不同系统上行尾可能不一样。

    不归一化的话，一次纯粹的行尾差异会触发"迁移被改过、拒绝执行"——
    一个把人拦在门外、却什么都没告诉他的告警。
    """
    unix = _migration("0001_a", "select 1;\nselect 2;\n")
    windows = _migration("0001_a", "select 1;\r\nselect 2;\r\n")

    assert unix.checksum == windows.checksum


# ── 二、plan：已执行的不再出现，对不上的拒绝执行 ────────────────────


def test_已执行过的不会再出现() -> None:
    """重复执行 `create table` 会炸，而且炸在**第二次**运行时——
    也就是最像"第一次一切正常"的那次之后。"""
    all_ = [_migration("0001_a"), _migration("0002_b")]
    applied = {"0001_a": all_[0].checksum}

    result = migrate.plan(applied, all_)

    assert [m.version for m in result.pending] == ["0002_b"]
    assert result.blocked is False


def test_全部跑过就是空的() -> None:
    all_ = [_migration("0001_a")]
    result = migrate.plan({"0001_a": all_[0].checksum}, all_)

    assert result.pending == []
    assert result.blocked is False


def test_内容被改过就是漂移() -> None:
    all_ = [_migration("0001_a", "select 1;")]
    result = migrate.plan({"0001_a": "旧内容的哈希"}, all_)

    assert [m.version for m, _ in result.drifted] == ["0001_a"]
    assert result.blocked is True
    assert "已改动" in result.describe()


def test_文件被删了也是漂移() -> None:
    """库里有、文件里没有——本地和库已经不是同一份契约了，必须拦。"""
    result = migrate.plan({"0001_a": "任意"}, [])

    assert result.orphaned == ["0001_a"]
    assert result.blocked is True
    assert "文件里没有" in result.describe()


def test_漂移时待执行的那部分也一并作废() -> None:
    """⭐⭐ 只拦下被改的那个文件是不够的。

    如果 `0001` 漂移、`0002` 是干净的，只跳过 `0001` 就会跑出一个
    "0001 是旧的、0002 是新的"的库——比整个停下来更难修。
    `blocked` 是整体判断，调用方据此一个都不执行。
    """
    all_ = [_migration("0001_a", "select 1;"), _migration("0002_b")]
    result = migrate.plan({"0001_a": "旧哈希"}, all_)

    assert [m.version for m in result.pending] == ["0002_b"]
    assert result.blocked is True, "pending 非空不代表可以跑——blocked 才是判据"


# ── 三、apply：一个文件一个事务 ────────────────────────────────────


def test_每个文件一个事务() -> None:
    """⭐ 一个文件一个事务，不是全部包在一起。

    全部包在一起的话，第 3 个文件失败会把前 2 个一起回滚——而它们本来已经
    是对的，回滚掉等于白跑，还得重来。
    """
    conn = FakeConn()
    done = migrate.apply(conn, [_migration("0001_a"), _migration("0002_b")])

    assert done == ["0001_a", "0002_b"]
    # exec(文件) → exec(记账) → commit，两组
    assert conn.kinds == ["exec", "exec", "commit", "exec", "exec", "commit"]


def test_记账和建表在同一个事务里() -> None:
    """⭐ 顺序错了会留下"表建好了但没记账"的状态，下次重跑就炸在重复建表上。

    所以每个事务的最后一条必须是记账插入。
    """
    conn = FakeConn()
    migrate.apply(conn, [_migration("0001_a", "create table t (x int);")])

    assert conn.sqls[0] == "create table t (x int);"
    assert "insert into schema_migrations" in conn.sqls[1]


def test_中途失败只回滚那一个文件() -> None:
    """第二个文件炸了：第一个已经提交的**不能**被回滚掉，下次接着跑即可。"""
    conn = FakeConn(fail_on="select 2;")
    with pytest.raises(RuntimeError):
        migrate.apply(conn, [_migration("0001_a"), _migration("0002_b", "select 2;")])

    assert conn.kinds == ["exec", "exec", "commit", "exec", "rollback"]


def test_记账失败也算这个文件失败() -> None:
    """建表成功但记账失败，也必须整体回滚——否则下次重跑会重复建表。"""
    conn = FakeConn(fail_on="insert into schema_migrations")
    with pytest.raises(RuntimeError):
        migrate.apply(conn, [_migration("0001_a")])

    assert conn.kinds == ["exec", "exec", "rollback"]


def test_autocommit_开着就拒绝执行() -> None:
    """⭐ autocommit 开着时"一个文件一个事务"是假的：每条语句各自提交，回滚回不来。

    这种连接不会报任何错，只是失败时留下半截 DDL。
    """
    conn = FakeConn(autocommit=True)

    with pytest.raises(ValueError, match="autocommit"):
        migrate.apply(conn, [_migration("0001_a")])


def test_建表语句先于任何迁移() -> None:
    """⭐ `schema_migrations` 不由编号文件建，由执行器建——否则第一次运行有鸡生蛋问题。

    所以它必须排在所有迁移前面，不然记不了账。
    """
    conn = FakeConn()
    migrate.ensure_tracking(conn)
    migrate.apply(conn, [_migration("0001_a")])

    assert "schema_migrations" in conn.sqls[0]
    assert conn.kinds == ["exec", "commit", "exec", "exec", "commit"]


def test_读已执行的是_version_到_checksum() -> None:
    conn = FakeConn(rows=[[("0001_a", "aaa"), ("0002_b", "bbb")]])

    assert migrate.read_applied(conn) == {"0001_a": "aaa", "0002_b": "bbb"}


def test_探建表只读不写() -> None:
    """`tracking_exists` 是 `--dry-run` 能"一个字节都不写"的前提。"""
    conn = FakeConn(rows=[[(True,)]])

    assert migrate.tracking_exists(conn) is True
    assert conn.kinds == ["exec"]


# ── 四、CLI：--dry-run 是唯一的"不会误伤线上库"的保障 ──────────────
#
# 这一段用真的 `migrations/` 目录（三个文件），只把连接和配置换成假的。


def _patch_env(monkeypatch: pytest.MonkeyPatch, conn: FakeConn, dsn: str | None) -> None:
    class _Settings:
        @staticmethod
        def from_env() -> SimpleNamespace:
            return SimpleNamespace(supabase_dsn=dsn)

    monkeypatch.setattr(cli, "Settings", _Settings)
    monkeypatch.setattr(cli.migrate, "connect", lambda _dsn: conn)


_WRITE_PREFIXES = ("insert", "update", "delete", "create", "alter", "drop", "truncate")


def test_dry_run_一条写语句都没有(monkeypatch: pytest.MonkeyPatch) -> None:
    """⭐⭐ 这条是整个文件里最重要的一个断言。

    `--dry-run` 是人敢在第一次推之前先看一眼的唯一理由。它只要漏了一条
    `create table`，那个承诺就没了——而且没有第二次机会，库已经被动过了。
    """
    conn = FakeConn(rows=[[(False,)]])  # 还没有 schema_migrations
    _patch_env(monkeypatch, conn, "postgresql://x")

    assert cli.main(["migrate", "--dry-run"]) == 0

    writes = [
        sql for sql in conn.sqls if sql.strip().lower().startswith(_WRITE_PREFIXES)
    ]
    assert writes == [], f"--dry-run 写了库：{writes}"
    assert conn.kinds == ["exec"], "dry-run 只该有一条只读查询"


def test_dry_run_不建记账表(monkeypatch: pytest.MonkeyPatch) -> None:
    """⭐ 尤其不能建 `schema_migrations`——它是"写"里最隐蔽的一条，
    看着像准备工作，实际已经在库里留了东西。"""
    conn = FakeConn(rows=[[(False,)]])
    _patch_env(monkeypatch, conn, "postgresql://x")

    cli.main(["migrate", "--dry-run"])

    assert all("schema_migrations" not in sql or "to_regclass" in sql for sql in conn.sqls)


def test_漂移时一个文件都不执行(monkeypatch: pytest.MonkeyPatch) -> None:
    """真跑的时候：漂移 → 报错退出，**不执行任何 pending**。"""
    found = migrate.discover(cli.MIGRATIONS_DIR)
    conn = FakeConn(rows=[[(True,)], [(found[0].version, "旧哈希")]])
    _patch_env(monkeypatch, conn, "postgresql://x")

    with pytest.raises(SystemExit, match="一个都没执行"):
        cli.main(["migrate"])

    assert not any("insert into schema_migrations" in sql for sql in conn.sqls)
    assert "create table dim_author" not in " ".join(conn.sqls)


def test_库已是最新时不动它(monkeypatch: pytest.MonkeyPatch) -> None:
    found = migrate.discover(cli.MIGRATIONS_DIR)
    rows = [[(True,)], [(m.version, m.checksum) for m in found]]
    conn = FakeConn(rows=rows)
    _patch_env(monkeypatch, conn, "postgresql://x")

    assert cli.main(["migrate"]) == 0

    assert not any(sql.lstrip().startswith("create table dim") for sql in conn.sqls)
    assert not any("insert into schema_migrations" in sql for sql in conn.sqls)


def test_status_对得上就退_0(monkeypatch: pytest.MonkeyPatch) -> None:
    found = migrate.discover(cli.MIGRATIONS_DIR)
    conn = FakeConn(rows=[[(True,)], [(m.version, m.checksum) for m in found]])
    _patch_env(monkeypatch, conn, "postgresql://x")

    assert cli.main(["status"]) == 0


def test_status_漂移时退_1(monkeypatch: pytest.MonkeyPatch) -> None:
    found = migrate.discover(cli.MIGRATIONS_DIR)
    conn = FakeConn(rows=[[(True,)], [(found[0].version, "旧哈希")]])
    _patch_env(monkeypatch, conn, "postgresql://x")

    assert cli.main(["status"]) == 1


def test_没配_DSN_时干净报错(monkeypatch: pytest.MonkeyPatch) -> None:
    """没配 DSN 就给一句人话，**不要**静默降级成某种默认连接。

    降级的后果是"看着跑了、其实连的是别的库"，而反馈出来只有一句成功日志。
    """
    conn = FakeConn()
    _patch_env(monkeypatch, conn, None)

    with pytest.raises(SystemExit, match="SUPABASE_DSN"):
        cli.main(["status"])


def test_三个迁移文件都在() -> None:
    """⭐ 反过来钉一下：上面所有用例都建立在"目录里有这三个文件"上。

    文件被删掉的话，`discover` 返回空、`plan` 空、CLI 说"无事可做"，
    全部用例照样绿——**而库其实什么都没建**。
    """
    found = migrate.discover(cli.MIGRATIONS_DIR)

    assert [m.version for m in found] == ["0000_extensions", "0001_business", "0002_prompt"]
