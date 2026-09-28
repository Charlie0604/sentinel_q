"""提示词加载与拼装（架构文档 7.9）。

钉住三件事：

  1. 拼装顺序固定为 A–G，与字典插入顺序无关
  2. hash 稳定——同内容必同 hash，因为它要作为 `prompt_version` 写进库
  3. 运行期取值：本地暂存 → `*.example` 模板降级，一级都不能少

⚠️ **"从线上库拉"那一段不在这里测了**（决策 52）：它搬去了
`storage/prompts.py`，那边有库可连。这一组测试从头到尾**没有 Repo**——
这正是搬家的目的，见下面 `test_loading_never_needs_a_repository`。

⚠️ 这个文件原来住在 `analyst/tests/` 下，模块也跟着搬到了 `shared/`——
它现在被 `storage` 和 `analyst` 两边共用，留在任何一个模块里都会
让另一个模块去 import 它（破 7.3 规则一）。

顺带证明 AI 模块可以**在没有真实提示词、没有数据库**的情况下被独立测试（7.5）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from sentinel_q.shared import prompts as prompt_loader
from sentinel_q.shared.config import REPO_ROOT, Paths
from sentinel_q.shared.models import PROMPT_MODULE_ORDER, PromptBundle


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


def test_the_cached_bundle_is_what_gets_loaded(tmp_path: Path) -> None:
    """主程序拉下来的那一版暂存在本地，运行期读的就是它。"""
    layout = _layout(tmp_path)
    prompt_loader.write_cache(layout.prompts_cache, _bundle(3, a_role="来自线上库"))

    bundle = prompt_loader.load_local(layout)

    assert bundle.version == 3
    assert bundle.modules["a_role"] == "来自线上库"


def test_the_cache_round_trips_through_json(tmp_path: Path) -> None:
    """写下去再读回来必须一模一样——version/hash 是判断结果的追溯依据。"""
    layout = _layout(tmp_path)
    original = _bundle(7, d_rubric="改过的标准")

    prompt_loader.write_cache(layout.prompts_cache, original)
    cached = json.loads((layout.prompts_cache / prompt_loader.CACHE_FILE).read_text("utf-8"))
    back = prompt_loader.read_cache(layout.prompts_cache)

    assert cached["version"] == 7
    assert back is not None
    assert (back.version, back.content_hash, back.modules) == (
        original.version,
        original.content_hash,
        original.modules,
    )


def test_falls_back_to_example_templates(tmp_path: Path) -> None:
    """本地没暂存 —— 降到模板，保证 AI 模块仍可独立跑通流程。"""
    layout = _layout(tmp_path, with_examples=True)

    bundle = prompt_loader.load_local(layout)

    assert bundle.version == 0
    assert "降级" in (bundle.note or "")
    assert bundle.missing_modules() == []  # 七个模板齐了
    # 模板里必须写清楚"这不是真实提示词"，否则有人会拿占位内容当真结果
    assert "模板" in bundle.modules["b_subject"]


def test_loading_never_needs_a_repository(tmp_path: Path) -> None:
    """⭐ 这个模块**收不到也拿不到**一个 Repo —— 那正是它能独立测试的原因。

    决策 52 把"从线上库拉提示词"搬去了 `storage`。只要这里还有一个
    `repo` 参数，每个测试就得先搭一个假库出来（`FakeRepo` 当初存在的
    最大理由就是这个），而"独立测试"就退化成了"换个库测试"。
    """
    import inspect

    for name in ("load_local", "load_examples", "bundle_from_workdir", "compute_hash"):
        params = inspect.signature(getattr(prompt_loader, name)).parameters
        assert "repo" not in params, f"{name} 不该收一个仓储"

    # ⚠️ 这里**不扫源码文本**：文件里提到 `storage/prompts.py` 是给人看的指路，
    #    不是依赖。真正的机械检查在 `tests/test_layering.py`
    #    （它按 AST 取 import，认得清"提到"和"导入"）。


def test_workdir_bundle_reads_real_files_only(tmp_path: Path) -> None:
    layout = _layout(tmp_path, with_examples=True)
    # 工作区里放一份真实提示词，覆盖掉模板
    (layout.prompts / "d_rubric.txt").write_text("真实的判断标准", encoding="utf-8")

    bundle = prompt_loader.bundle_from_workdir(layout.prompts, version=4, note="改了 D")

    assert bundle.version == 4
    assert bundle.modules["d_rubric"] == "真实的判断标准"
    assert bundle.missing_modules()  # 其余模块只有 .example，工作区里是空的
