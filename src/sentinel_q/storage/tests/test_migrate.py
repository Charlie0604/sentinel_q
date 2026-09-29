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

import hashlib
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


def _state_rows(
    applied: dict[str, str],
    *,
    stored: dict[str, str] | None = None,
    columns: tuple[str, ...] = ("version", "checksum", "applied_sql", "applied_at"),
) -> list[list]:
    """喂给 FakeConn 的行批次，对应一次**非 dry-run** 的 migrate 会发的查询。

    每个 `execute` 吃一批（见 `FakeCursor.execute`），所以顺序是死的：

      0. `ensure_tracking`  —— 建表语句，不取行，但照样吃掉一批
      1. `tracking_exists`  —— `to_regclass`，fetchone
      2. `read_applied`     —— version / checksum，fetchall
      3. `tracking_columns` —— 列名，fetchall（给 `applied_sql` 就算它有这一列）

    第 4 批只有给了 `stored` 才喂——不喂就是"库里存着正文列、但一行都没存过"，
    也就是老库补正文之前的样子。
    """
    rows: list[list] = [
        [],
        [(True,)],
        list(applied.items()),
        [(name,) for name in columns],
    ]
    if stored is not None:
        rows.append(list(stored.items()))
    return rows


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
    conn = FakeConn(rows=_state_rows({found[0].version: "旧哈希"}))
    _patch_env(monkeypatch, conn, "postgresql://x")

    with pytest.raises(SystemExit, match="一个都没执行"):
        cli.main(["migrate"])

    assert not any("insert into schema_migrations" in sql for sql in conn.sqls)
    assert "create table dim_author" not in " ".join(conn.sqls)


def test_库已是最新时不动它(monkeypatch: pytest.MonkeyPatch) -> None:
    found = migrate.discover(cli.MIGRATIONS_DIR)
    stored = {m.version: m.sql for m in found}
    conn = FakeConn(rows=_state_rows({m.version: m.checksum for m in found}, stored=stored))
    _patch_env(monkeypatch, conn, "postgresql://x")

    assert cli.main(["migrate"]) == 0

    # 建 schema_migrations / 补正文都不算"动它"——这里管的是**业务表**和记账行
    writes = [
        sql
        for sql in conn.sqls
        if sql.strip().lower().startswith(("insert", "update", "delete"))
    ]
    assert writes == [], f"库已是最新，不该写任何东西：{writes}"
    assert not any(sql.lstrip().startswith("create table dim") for sql in conn.sqls)


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


def test_该有的迁移文件一个都不少() -> None:
    """⭐ 反过来钉一下：上面所有用例都建立在"目录里就这几个文件"上。

    文件被删掉的话，`discover` 返回空、`plan` 空、CLI 说"无事可做"，
    全部用例照样绿——**而库其实什么都没建**。

    ⚠️ 这里是**严格相等**，所以新增一个迁移文件会让它红。那是有意的：
    加文件的人必须当场回答一次"这个文件该不该在列表里"，
    而不是让它悄悄溜进 `discover` 的结果、再由某次线上 `migrate` 第一次执行。
    """
    found = migrate.discover(cli.MIGRATIONS_DIR)

    assert [m.version for m in found] == [
        "0000_extensions",
        "0001_business",
        "0002_prompt",
        "0003_reviewed_by",
        "0004_question_home",
        "0005_question_metrics",
    ]


# ── 五、接受漂移：把"我确认过 DDL 没变"记进库 ───────────────────────
#
# 漂移本身是**正常会发生的**——重构改一行注释里的路径就会触发，`0002_prompt`
# 就是这么漂的。但这个判断只有人能下：引擎手里只有一个哈希，从哈希看
# "注释里改了个字"和"DDL 改了、线上表已经对不上文件"长得一模一样。
#
# 所以 `--accept-drift` 记的**不是事实，是一个人的判断 + 理由**。下面这些用例
# 钉的就是两件事：这个判断被如实记下来了，以及它没有被放宽成"一把梭"。


def test_探列只读不写() -> None:
    """`tracking_columns` 是 `status` / `--dry-run` 读 `applied_sql` 的前提，
    那两条路径不能建列，所以它必须是纯读的。"""
    conn = FakeConn(rows=[[("version",), ("checksum",)]])

    assert migrate.tracking_columns(conn) == {"version", "checksum"}
    assert conn.kinds == ["exec"]


def test_read_state_老库上没有正文列时返回空字典() -> None:
    """⭐ `--dry-run` / `status` 在老库上不炸的保障。

    `applied_sql` 是后加的列，老库上还没有。那两条路径**不能**加列（写操作），
    所以只能读到"没有"就认了——而不是去 SELECT 一个不存在的列。
    """
    conn = FakeConn(
        rows=[[(True,)], [("0001_a", "aaa")], [("version",), ("checksum",)]]
    )

    applied, stored = migrate.read_state(conn)

    assert applied == {"0001_a": "aaa"}
    assert stored == {}
    assert conn.kinds == ["exec", "exec", "exec"]


def test_read_state_表还没建时什么都不查() -> None:
    conn = FakeConn(rows=[[(False,)]])

    assert migrate.read_state(conn) == ({}, {})
    assert conn.kinds == ["exec"]


def test_记账时连正文一起存() -> None:
    """`apply` 存的正文就是文件原文——`checksum` 算的时候自己归一化换行，
    两边不会打架。"""
    conn = FakeConn()
    migration = _migration("0001_a", "select 1;")

    migrate.apply(conn, [migration])

    assert conn.events[1][2] == (migration.version, migration.checksum, "select 1;")


def test_存进去的正文和校验和对得上() -> None:
    """⭐ 不变量：`checksum == sha256(applied_sql)`（换行归一化后）。

    这条不变量是 `applied_sql` 敢叫"库现在认定生效的那一版"的全部依据。
    三个写入点（apply / 补齐 / 接受漂移）都要维持它。
    """
    conn = FakeConn()
    migrate.apply(conn, [_migration("0001_a", "select 1;\r\nselect 2;\r\n")])

    _, checksum, stored = conn.events[1][2]
    normalized = stored.replace("\r\n", "\n").encode("utf-8")

    assert hashlib.sha256(normalized).hexdigest() == checksum


def test_只给校验和对得上的老行补正文() -> None:
    """⭐⭐ 已经漂移的行**不补**。

    补了就是把没跑过的正文说成跑过的——恰好是这个功能要防的那件事，
    而且它会静默地把一次真漂移洗成"没问题"。
    """
    good = _migration("0001_a", "select 1;")
    bad = _migration("0002_b", "select 2;")
    conn = FakeConn()

    filled = migrate.backfill_applied_sql(
        conn, [good, bad], {"0001_a": good.checksum, "0002_b": "旧哈希"}, {}
    )

    assert filled == ["0001_a"], "只有哈希对得上的那条能补"
    assert conn.sqls == [
        "update schema_migrations set applied_sql = %s where version = %s"
    ]
    assert conn.events[0][2] == ("select 1;", "0001_a"), "补的是文件原文"
    assert conn.kinds == ["exec", "commit"]


def test_已经有正文的行不重复补() -> None:
    good = _migration("0001_a", "select 1;")
    conn = FakeConn()

    filled = migrate.backfill_applied_sql(
        conn, [good], {"0001_a": good.checksum}, {"0001_a": "select 1;"}
    )

    assert filled == []
    assert conn.kinds == [], "没什么可补的时候一条语句都不该发"


def test_接受漂移必须写理由() -> None:
    """⭐ 理由是这条记录里**唯一不能自动生成**的东西。

    哈希、时间、旧值都能自己填，只有"为什么"必须由人给。所以空白的 note
    在**发任何 SQL 之前**就要炸掉，不能写出一条没有理由的接受记录。
    """
    conn = FakeConn()

    with pytest.raises(ValueError, match="理由"):
        migrate.accept_drift(conn, _migration("0001_a"), note="   ", recorded="旧哈希")

    assert conn.kinds == []


def test_接受漂移把旧校验和挪进另一列() -> None:
    """⭐ 光把 checksum 换成新的，等于把"这里漂移过"这件事抹掉了。"""
    target = _migration("0001_a", "select 1;")
    conn = FakeConn()

    migrate.accept_drift(conn, target, note="  只改了注释  ", recorded="旧哈希")

    assert conn.sqls[0].startswith("update schema_migrations")
    assert "accepted_from_checksum" in conn.sqls[0]
    assert conn.events[0][2] == (
        target.checksum,
        "select 1;",
        "只改了注释",
        "旧哈希",
        "0001_a",
    ), "理由要去掉首尾空白再存"
    assert conn.kinds == ["exec", "commit"]


def test_接受漂移只碰一个版本() -> None:
    """⭐ "不能一把梭"的机械化版本：`WHERE` 里必须带 version。"""
    conn = FakeConn()

    migrate.accept_drift(conn, _migration("0002_prompt"), note="理由", recorded="旧哈希")

    assert "where version = %s" in conn.sqls[0]
    assert conn.events[0][2][-1] == "0002_prompt"


def test_diff_看得出注释级改动() -> None:
    """`0002_prompt` 那次漂移正是这种：只改了一行注释里的路径。"""
    stored = "-- 见 core/repo/supabase.py\ncreate table t (x int);\n"
    current = "-- 见 storage/supabase.py\ncreate table t (x int);\n"

    diff = migrate.sql_diff(stored, current, version="0002_prompt")

    assert "库（0002_prompt，执行时）" in diff
    assert "--- 见 core/repo/supabase.py" in diff, "删掉的那行"
    assert "+-- 见 storage/supabase.py" in diff, "加上的那行"
    assert " create table t (x int);" in diff, "没变的那行是上下文，不是差异"


def test_diff_不把最后一行粘起来() -> None:
    """⭐ 文件最后一行没有换行符时，`difflib` 会把 diff 的两行拼成一行——
    看上去像"改了内容"，实际只是打字机式的错位。

    ⚠️ 删掉的那行前缀是**一个** `-`（`---` 是文件头），别把两者看混。
    """
    diff = migrate.sql_diff("select 1;", "select 2;", version="0001_a")

    assert "-select 1;\n" in diff
    assert "+select 2;\n" in diff


def test_没存正文时不硬编差异() -> None:
    """⭐⭐ 这一段里最重要的断言。

    库里没存正文时，拿现有文件跟自己 diff 会得到一个**空的差异**——
    而那看起来和"确认过没问题"一模一样，等于替人签了字。
    所以必须明说"打不出差异"，并且一个 diff 头都不能出现。
    """
    report = migrate.drift_report(
        _migration("0002_prompt", "create table t (x int);\n"),
        recorded="73075a1dd22501a7d8830e3eff42abfd0d5b0a0072405c62157ad74a8f8cf1ac",
        stored_sql=None,
    )

    assert "打不出差异" in report
    assert "---" not in report and "+++" not in report
    assert "73075a1d" in report


def test_报告里有照抄能跑的命令行() -> None:
    """报告的唯一用途就是让人照着做，所以命令必须**完整可复制**。"""
    report = migrate.drift_report(
        _migration("0002_prompt", "select 2;"),
        recorded="旧哈希",
        stored_sql="select 1;",
    )

    assert "migrate --accept-drift 0002_prompt" in report
    assert "-n" in report, "理由必填，命令里就得带着 --note"


def test_accept_drift_不给理由就报错(monkeypatch: pytest.MonkeyPatch) -> None:
    """⭐ 理由必填，和 `prompts push -n` 同一条规矩。"""
    conn = FakeConn()
    _patch_env(monkeypatch, conn, "postgresql://x")

    with pytest.raises(SystemExit, match="note"):
        cli.main(["migrate", "--accept-drift", "0002_prompt"])

    assert conn.kinds == [], "理由都没给，连库都不该连"


def test_accept_drift_版本号不存在就报错(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn()
    _patch_env(monkeypatch, conn, "postgresql://x")

    with pytest.raises(SystemExit, match="没有 0009_nope"):
        cli.main(["migrate", "--accept-drift", "0009_nope", "-n", "理由"])

    assert conn.kinds == []


def test_accept_drift_没漂移就拒绝(monkeypatch: pytest.MonkeyPatch) -> None:
    """⭐ 空操作不算成功：脚本里 `|| exit 1` 不该被它骗过去。"""
    found = migrate.discover(cli.MIGRATIONS_DIR)
    conn = FakeConn(rows=_state_rows({m.version: m.checksum for m in found}))
    _patch_env(monkeypatch, conn, "postgresql://x")

    with pytest.raises(SystemExit, match="没有漂移可接受"):
        cli.main(["migrate", "--accept-drift", "0002_prompt", "-n", "理由"])

    assert not any("update schema_migrations" in sql for sql in conn.sqls)


def test_accept_drift_没执行过就拒绝(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn(rows=_state_rows({}))
    _patch_env(monkeypatch, conn, "postgresql://x")

    with pytest.raises(SystemExit, match="还没执行过"):
        cli.main(["migrate", "--accept-drift", "0002_prompt", "-n", "理由"])


def test_accept_drift_真的写库(monkeypatch: pytest.MonkeyPatch) -> None:
    """真跑：一条 UPDATE + commit，且**一个业务表都不碰**。"""
    found = migrate.discover(cli.MIGRATIONS_DIR)
    target = found[0]
    conn = FakeConn(rows=_state_rows({target.version: "旧哈希"}))
    _patch_env(monkeypatch, conn, "postgresql://x")

    assert cli.main(["migrate", "--accept-drift", target.version, "-n", "只改了注释"]) == 0

    update = next(
        event for event in conn.events if event[0] == "exec" and event[1].startswith("update")
    )
    assert update[2] == (
        target.checksum,
        target.sql,
        "只改了注释",
        "旧哈希",
        target.version,
    )
    assert "create table dim_author" not in " ".join(conn.sqls)


def test_accept_drift_不会顺手把迁移也跑了(monkeypatch: pytest.MonkeyPatch) -> None:
    """⭐ "我确认过"和"库真的变了"是两件事，分开做、各自留痕。"""
    found = migrate.discover(cli.MIGRATIONS_DIR)
    conn = FakeConn(rows=_state_rows({found[0].version: "旧哈希"}))
    _patch_env(monkeypatch, conn, "postgresql://x")

    cli.main(["migrate", "--accept-drift", found[0].version, "-n", "理由"])

    assert not any("insert into schema_migrations" in sql for sql in conn.sqls)


def test_accept_drift_dry_run_一个写语句都没有(monkeypatch: pytest.MonkeyPatch) -> None:
    """⭐ 和 `migrate --dry-run` 同一条纪律：不可逆的外部动作，先看一眼。"""
    found = migrate.discover(cli.MIGRATIONS_DIR)
    conn = FakeConn(
        rows=[[(True,)], [(found[0].version, "旧哈希")], [("version",), ("checksum",)]]
    )
    _patch_env(monkeypatch, conn, "postgresql://x")

    code = cli.main(
        ["migrate", "--accept-drift", found[0].version, "--dry-run", "-n", "理由"]
    )

    assert code == 0
    writes = [sql for sql in conn.sqls if sql.strip().lower().startswith(_WRITE_PREFIXES)]
    assert writes == [], f"--dry-run 写了库：{writes}"
    assert not any("schema_migrations" in sql and "insert" in sql for sql in conn.sqls)


def test_status_漂移时打出真实差异(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`status` 是唯一会主动跑的诊断命令，差异必须出现在这里——
    否则人就得先接受漂移、再回头才发现改的是什么。"""
    found = migrate.discover(cli.MIGRATIONS_DIR)
    target = found[0]
    conn = FakeConn(
        rows=[
            [(True,)],
            [(target.version, "旧哈希")],
            [("version",), ("checksum",), ("applied_sql",)],
            [(target.version, "-- 当初那一版的注释\n")],
        ]
    )
    _patch_env(monkeypatch, conn, "postgresql://x")

    assert cli.main(["status"]) == 1

    out = capsys.readouterr().out
    assert "逐行差异" in out
    assert "--- 当初那一版的注释" in out
    assert "--accept-drift" in out
