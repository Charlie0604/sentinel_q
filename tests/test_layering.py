"""依赖方向契约（架构文档 8.3）。

这条测试是"模块可以独立开发"的全部保障。没有它，
`modules/A → modules/B` 的一次顺手 import 就会悄悄把模块粘回一坨，
而且要到很久以后（改一处崩三处）才会发现。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _imports(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # level > 0 是相对 import，解析起来麻烦；
            # 约定包内部也一律用绝对 import（from modules.m2_analyst import ...）
            if node.level == 0 and node.module:
                yield node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name


def _python_files(package: str) -> Iterator[Path]:
    yield from (REPO_ROOT / package).rglob("*.py")


def test_no_cross_module_imports() -> None:
    for path in _python_files("modules"):
        own = path.relative_to(REPO_ROOT / "modules").parts[0]
        for module in _imports(path):
            if not module.startswith("modules."):
                continue
            other = module.split(".")[1]
            assert other == own, (
                f"{path.relative_to(REPO_ROOT)} 跨模块依赖了 {module}——"
                "模块之间只能通过 core/ 或落库传 ID 通信（架构文档 8.3）"
            )


def test_core_never_depends_on_modules() -> None:
    for path in _python_files("core"):
        for module in _imports(path):
            assert not module.startswith("modules"), (
                f"{path.relative_to(REPO_ROOT)} 反向依赖了模块 {module}——"
                "core/ 是依赖方向的终点（架构文档 8.3）"
            )


def test_no_relative_imports() -> None:
    """相对 import 会绕过上面两条检查，所以直接禁掉。"""
    for package in ("core", "modules"):
        for path in _python_files(package):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.level:
                    raise AssertionError(
                        f"{path.relative_to(REPO_ROOT)} 使用了相对 import，"
                        "请改为绝对 import"
                    )
