"""`SupabaseRepo` 的连接必须是 autocommit —— 否则**写进去的东西一条都留不下**。

## 这个文件是为什么存在的

2026-09-29 真库第一次入库，报账全对、8 张表全是 0 行。原因是 psycopg 默认
不开 autocommit：

    读一次（裸 cursor）        → 连接停在 INTRANS（隐式开了个事务）
    with conn.transaction():  → 不是开事务，是开 **SAVEPOINT**
    退出                      → release savepoint，**不提交外层那个事务**
    close()                   → 整个事务回滚，一条都不剩

触发条件低得可怕：**只要在读之后写就行**。而这一层每个读方法都用裸 cursor，
`insert_content` 内部更是先读后写，所以它自己也中招。

**它为什么能藏这么久**：报账是拿 `IngestReport` 的计数器做的，而那些计数器
数的是"`insert` 返回了几行"——回滚之前，那些行**确实存在**，同一条连接上
读得回来。所以彩排、回读、账目，三样全是绿的。

`FakeRepo` 永远测不出这件事（它没有"事务"这个概念），所以这里是**静态断言**：
抓 `__init__` 里那个 `psycopg.connect(...)` 的关键字参数。真正的行为验证
在下面那条 `@pytest.mark.integration` 里，默认不跑——但它是唯一能证明
"读之后连接还是 IDLE"的东西。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from sentinel_q.shared.config import Settings
from sentinel_q.storage import supabase

SOURCE = Path(supabase.__file__).read_text(encoding="utf-8")


def _connect_kwargs() -> set[str]:
    """`SupabaseRepo.__init__` 里那个 `psycopg.connect(...)` 传了哪些关键字。"""
    for node in ast.walk(ast.parse(SOURCE)):
        if not isinstance(node, ast.FunctionDef) or node.name != "__init__":
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "connect"
            ):
                return {kw.arg for kw in call.keywords if kw.arg}
    raise AssertionError("没在 SupabaseRepo.__init__ 里找到 psycopg.connect(...)")


def test_the_connection_is_autocommit() -> None:
    """⭐ 少了这个关键字，**每一次写入都会在 close() 时静默回滚**。

    它不是一个可以"按需打开"的调优项：关掉它，`with self._conn.transaction()`
    在读之后就不再是事务而是 savepoint，而这一层**每个读方法都用裸 cursor**。
    """
    assert "autocommit" in _connect_kwargs(), (
        "SupabaseRepo 的连接必须 autocommit=True。\n"
        "不加的话，任何『先读后写』的路径（含 insert_content 自己）都会在\n"
        "close() 时被整个回滚——而所有计数器仍然显示成功。见本文件开头。"
    )


def test_prepare_threshold_is_still_disabled() -> None:
    """6543 端口（pgbouncer 事务模式）下预编译语句会失效，这条别被顺手删掉。"""
    assert "prepare_threshold" in _connect_kwargs()


@pytest.mark.integration
def test_a_read_does_not_leave_the_connection_in_a_transaction() -> None:
    """⭐ 行为验证：读一次之后，连接必须回到 IDLE。

    这是上面那条静态断言想表达的**事实本身**。它需要真库，所以默认不跑：

        .venv/bin/python -m pytest -m integration -q

    ⚠️ 它是这个文件里唯一能证明"写真的落盘了"的东西。静态断言只能挡住
    "有人删掉了 autocommit"，挡不住"psycopg 换了语义"。
    """
    dsn = Settings.from_env().supabase_dsn
    if not dsn:
        pytest.skip("没有配 SUPABASE_DSN")
    from psycopg.pq import TransactionStatus

    repo = supabase.SupabaseRepo(dsn)
    try:
        repo.all_urls()  # 一个纯读，走的就是那些裸 cursor
        assert repo._conn.info.transaction_status == TransactionStatus.IDLE, (
            "读完之后连接停在 INTRANS —— 后面所有 transaction() 都会退化成 "
            "savepoint，close() 时整个回滚"
        )
    finally:
        repo.close()
