"""提示词加载与拼装（架构文档 8.9）。

钉住三件事：

  1. 拼装顺序固定为 A–G，与字典插入顺序无关
  2. hash 稳定——同内容必同 hash，因为它要作为 `prompt_version` 写进库
  3. 三级取值：线上库 → 本地暂存 → `*.example` 模板降级，一级都不能少

顺带证明模块二可以**在没有真实提示词、没有数据库**的情况下被独立测试（8.5）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from core.config import Paths
from core.models import PROMPT_MODULE_ORDER, PromptBundle
from core.repo import FakeRepo

from modules.m2_analyst import prompt_loader

REPO_ROOT = Path(__file__).resolve().parents[3]


def _layout(tmp_path: Path, *, with_examples: bool = False) -> Paths:
    layout = Paths.resolve(tmp_path)
    layout.ensure()
    if with_examples:
        shutil.copytree(REPO_ROOT / "prompts", layout.prompts, dirs_exist_ok=True)
    return layout


def _bundle(version: int, **modules: str) -> PromptBundle:
    full = {name: f"<{name}>" for name in PROMPT_MODULE_ORDER}
    full.update(modules)
    return PromptBundle(version=version, content_hash=prompt_loader.compute_hash(full), modules=full)


# ── 拼装 ────────────────────────────────────────────────────────────


def test_assemble_follows_fixed_order_regardless_of_dict_order() -> None:
    shuffled = dict(reversed(list(_bundle(1, a_role="AAA", g_guardrails="GGG").modules.items())))
    bundle = PromptBundle(version=1, content_hash="x", modules=shuffled)

    assembled = bundle.assemble()

    assert assembled.index("AAA") < assembled.index("GGG")


def test_missing_modules_are_reported() -> None:
    bundle = PromptBundle(version=1, content_hash="x", modules={"a_role": "AAA"})
    assert "b_subject" in bundle.missing_modules()


def test_empty_modules_are_skipped_in_assembly() -> None:
    bundle = PromptBundle(
        version=1, content_hash="x", modules={"a_role": "AAA", "b_subject": "   "}
    )
    assert "b_subject" not in bundle.assemble()


# ── hash ────────────────────────────────────────────────────────────


def test_hash_is_order_independent() -> None:
    forward = {name: name for name in PROMPT_MODULE_ORDER}
    backward = dict(reversed(list(forward.items())))
    assert prompt_loader.compute_hash(forward) == prompt_loader.compute_hash(backward)


def test_hash_changes_when_any_module_changes() -> None:
    before = {name: name for name in PROMPT_MODULE_ORDER}
    after = {**before, "d_rubric": "改了一句话"}
    assert prompt_loader.compute_hash(before) != prompt_loader.compute_hash(after)


# ── 三级取值 ────────────────────────────────────────────────────────


def test_db_bundle_wins_and_is_cached(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    repo = FakeRepo()
    repo.push_prompt_bundle(_bundle(3, a_role="来自线上库"))

    bundle = prompt_loader.fetch_bundle(repo, layout)

    assert bundle.version == 3
    assert bundle.modules["a_role"] == "来自线上库"
    # 暂存到本地，供下次任务开始前离线兜底
    cached = json.loads((layout.prompts_cache / prompt_loader.CACHE_FILE).read_text("utf-8"))
    assert cached["version"] == 3


def test_stale_cache_is_used_when_db_unreachable(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    prompt_loader._write_cache(layout.prompts_cache, _bundle(2, a_role="上次暂存的"))

    bundle = prompt_loader.fetch_bundle(FakeRepo(), layout)  # 库是空的

    assert bundle.version == 2
    assert bundle.modules["a_role"] == "上次暂存的"


def test_falls_back_to_example_templates(tmp_path: Path) -> None:
    """库里没有、本地也没暂存 —— 降到模板，保证模块二仍可独立跑通流程。"""
    layout = _layout(tmp_path, with_examples=True)

    bundle = prompt_loader.fetch_bundle(FakeRepo(), layout)

    assert bundle.version == 0
    assert "降级" in (bundle.note or "")
    assert bundle.missing_modules() == []  # 七个模板齐了
    # 模板里必须写清楚"这不是真实提示词"，否则有人会拿占位内容当真结果
    assert "模板" in bundle.modules["b_subject"]


def test_workdir_bundle_reads_real_files_only(tmp_path: Path) -> None:
    layout = _layout(tmp_path, with_examples=True)
    # 工作区里放一份真实提示词，覆盖掉模板
    (layout.prompts / "d_rubric.txt").write_text("真实的判断标准", encoding="utf-8")

    bundle = prompt_loader.bundle_from_workdir(layout.prompts, version=4, note="改了 D")

    assert bundle.version == 4
    assert bundle.modules["d_rubric"] == "真实的判断标准"
    assert bundle.missing_modules()  # 其余模块只有 .example，工作区里是空的
