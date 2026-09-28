"""依赖方向契约（架构文档 7.3）。

这组测试是"模块可以独立开发、独立测试"的全部保障。没有它，
一次顺手的跨模块 import 就会把模块粘回一坨，而且要到很久以后
（改一处崩三处）才会发现。
"""

from __future__ import annotations

import ast
import re
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG = REPO_ROOT / "src" / "sentinel_q"

MODULES = ("collector", "analyst", "storage", "main")
"""全部四个包。扫描文件时用这个。"""

ENGINE_MODULES = ("collector", "analyst", "storage")
"""三个**模块**。`main/` 不在其中——它是接线层，按 7.3 的图它可以 import 三者。"""

SHARED = "shared"


def _imports(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # level > 0 是相对 import，解析起来麻烦；
            # 约定包内部也一律用绝对 import（from sentinel_q.collector import ...）
            if node.level == 0 and node.module:
                yield node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name


def _python_files(package: str) -> Iterator[Path]:
    yield from (PKG / package).rglob("*.py")


def _rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


def test_no_cross_module_imports() -> None:
    """规则 1：三个模块之间不互相 import，只依赖 shared/。

    ⚠️ **`main/` 不在这条规则里**（架构文档 7.3 的图：`main/ ──→ 三者皆可`）。
    它就是接线的那一层，不 import 另外三个的话它没有任何东西可接。
    所以这里扫的是 `ENGINE_MODULES`——三个模块——而 `main/` 有自己的
    一条约束：**只有它能 import `storage`**（规则 3，见下面那条测试）。
    """
    for package in ENGINE_MODULES:
        for path in _python_files(package):
            for module in _imports(path):
                if not module.startswith("sentinel_q."):
                    continue
                other = module.split(".")[1]
                assert other in (package, SHARED), (
                    f"{_rel(path)} 跨模块依赖了 {module}——"
                    "模块之间只能传 shared/models.py 里的数据结构（架构文档 7.3）"
                )


def test_shared_never_depends_outward() -> None:
    """规则 1 的另一半：shared/ 是依赖方向的终点。"""
    for path in _python_files(SHARED):
        for module in _imports(path):
            if not module.startswith("sentinel_q."):
                continue
            other = module.split(".")[1]
            assert other == SHARED, (
                f"{_rel(path)} 反向依赖了 {module}——"
                "shared/ 不依赖任何模块（架构文档 7.3）"
            )


def test_no_relative_imports() -> None:
    """规则 4：相对 import 会绕过前三条检查，所以直接禁掉。"""
    for package in (*MODULES, SHARED):
        for path in _python_files(package):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level:
                    raise AssertionError(
                        f"{_rel(path)} 使用了相对 import，请改为绝对 import"
                    )


# ── 规则 2：只有 storage/ 能碰数据库 ────────────────────────────────

DB_DRIVERS = ("psycopg", "psycopg2", "supabase", "postgrest", "asyncpg", "sqlalchemy")

SQL = re.compile(
    r"\b(insert\s+into|select\s+.+\s+from|update\s+\w+\s+set|delete\s+from"
    r"|create\s+table|alter\s+table|drop\s+table"
    r"|on\s+conflict|returning\s+\w)\b",
    re.IGNORECASE,
)
"""SQL 的形状。**故意宽一点**：漏报比误报贵得多——
误报只是让人把字符串写得绕一点，漏报是一条直连库的 SQL 悄悄活下来。"""


def _prose(tree: ast.AST) -> set[int]:
    """所有**当文档用的**字符串常量节点的 id——扫描时要跳过它们。

    ⚠️ 这不是为了放水。这个项目里每个模块的文档都在讲**边界在哪、为什么**，
    于是"`Repo.existing_urls()` 那种查库的事不在这里做"这种句子遍地都是。
    按原文扫字符串的话，**越是把边界写清楚的模块越会被判违规**——
    那会逼着人把解释删掉，正好反了。

    认的是"**作为独立语句出现的字符串**"（`ast.Expr` 包着一个常量），
    它同时覆盖两种写法：模块/类/函数开头的那段文档，以及紧跟在赋值后面、
    给字段写说明的**属性文档**（`storage/ingest.py` 的计数器几乎每个都带一段）。

    ⚠️ 用 `Q = <一句 SQL>` 这种**赋值**藏是躲不掉的——
    那是 `ast.Assign`，不是独立语句。
    """
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }


def test_only_storage_touches_the_database() -> None:
    """⭐ 规则 2：`storage/` 是**唯一**允许出现 SQL / 驱动的地方（决策 52）。

    这是用户那句"它们不需要也不应该去询问服务器任何东西"的机械化。
    查两样东西，都按 AST 而不是按原文：

      - **import 了数据库驱动** —— 完整名匹配，`psycopg` 不会误伤一个叫
        `psycopg_helpers` 的本地变量
      - **代码里有 SQL 字符串** —— 跳过文档字符串（见 `_prose`）

    ⚠️ 不扫"连接串环境变量名"那类词：规则 3 已经拦住了"去找 storage 要连接"，
    而这里再加一层只会把文档里的解释一起扫进来。
    """
    # ⚠️ 扫的是 `storage/` **以外**的三个包。`storage/` 自己当然要用 psycopg——
    #    它是这条规则要保护的那个例外，不是要检的那一边。
    for package in (*ENGINE_MODULES, "main"):
        if package == "storage":
            continue
        for path in _python_files(package):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))

            for module in _imports(path):
                root = module.split(".")[0]
                assert root not in DB_DRIVERS, (
                    f"{_rel(path)} import 了数据库驱动 {module!r}——"
                    "只有 storage/ 能碰数据库（架构文档 7.3 规则二）"
                )

            skip = _prose(tree)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                if id(node) in skip:
                    continue
                if hit := SQL.search(node.value):
                    raise AssertionError(
                        f"{_rel(path)} 里出现了 SQL（{hit.group(0)!r}，第 {node.lineno} 行）"
                        "——只有 storage/ 能写 SQL（架构文档 7.3 规则二）"
                    )


# ── 规则 3：只有 main/ 能 import storage ────────────────────────────


def test_only_main_calls_storage() -> None:
    """⭐ 规则 3：模块不能自己去找库，得由主程序递过去（决策 52）。

    规则 2 管的是"别自己连"，这条管的是"别自己找人连"。
    合起来的效果是：`collector` / `analyst` 拿到的一切都来自参数——
    于是它们真的能在一个**什么都没有**的目录里被单独跑起来。
    """
    # ⚠️ 只有 collector / analyst 要查：`storage` 当然可以 import 自己，
    #    而 `main` 正是那个**被允许**调 storage 的地方（7.3 的图）。
    for package in ("collector", "analyst"):
        for path in _python_files(package):
            for module in _imports(path):
                if not module.startswith("sentinel_q."):
                    continue
                assert module.split(".")[1] != "storage", (
                    f"{_rel(path)} import 了 {module}——"
                    "只有 main/ 能调 storage，模块之间靠 shared/ 的结构传数据"
                    "（架构文档 7.3 规则三）"
                )
