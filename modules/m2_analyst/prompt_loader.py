"""提示词加载与拼装（架构文档 8.9）。

权威副本在线上库的 `dim_prompt` 表——提示词不进 git，**线上库是唯一能找到它、
也是唯一能追溯"这条判断当时用的哪版提示词"的地方**。

一次任务开始时拉一次、暂存本地，下一次任务重新拉。也就是说
**提示词在一次任务内是冻结的**——这正是 `prompt_version` 能作为
`fact_analysis` 单一追溯依据的前提。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from core.config import Paths
from core.models import PROMPT_MODULE_ORDER, PromptBundle
from core.repo.base import Repo

log = logging.getLogger(__name__)

CACHE_FILE = "current.json"


def compute_hash(modules: dict[str, str]) -> str:
    """按固定顺序规范化后取 hash，与字典插入顺序无关——同内容必同 hash。

    这个值就是写进 `fact_analysis.prompt_version` 的东西。
    """
    canonical = json.dumps(
        {name: modules.get(name, "") for name in PROMPT_MODULE_ORDER},
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def fetch_bundle(repo: Repo, paths: Paths) -> PromptBundle:
    """任务开始时调用一次，取到这一整次任务要用的提示词。"""
    bundle = repo.active_prompt_bundle()
    if bundle is not None:
        _write_cache(paths.prompts_cache, bundle)
        log.info("提示词 v%s 已从线上库拉取", bundle.version)
        return bundle

    if cached := _read_cache(paths.prompts_cache):
        # 沿用旧版是安全的：bundle 自带 version/hash，判断结果记录的就是它实际用的那版
        log.warning("线上库没有生效的提示词，沿用上次暂存的 v%s", cached.version)
        return cached

    log.warning(
        "线上库与本地暂存都没有提示词，降级使用 *.example 模板——"
        "内容是占位符，判断结果没有意义，仅供跑通流程"
    )
    return load_examples(paths.prompts)


def load_examples(prompts_dir: Path) -> PromptBundle:
    """从仓库里入库的 *.example 模板拼一版。

    它存在的意义是让模块二在**没有真实提示词、没有数据库**的情况下也能独立测试。
    真实提示词缺失时回退到这里，并打一条显眼的警告。
    """
    modules: dict[str, str] = {}
    for name in PROMPT_MODULE_ORDER:
        real = prompts_dir / f"{name}.txt"
        example = prompts_dir / f"{name}.txt.example"
        path = real if real.exists() else example
        modules[name] = path.read_text(encoding="utf-8") if path.exists() else ""
    return PromptBundle(
        version=0,
        content_hash=compute_hash(modules),
        modules=modules,
        note="降级：来自 *.example 模板",
    )


def bundle_from_workdir(prompts_dir: Path, version: int, note: str | None = None) -> PromptBundle:
    """把本地工作区（`prompts/`）的提示词打包，供 push 到线上库。

    工作区是人工编辑的地方，不是运行期读取的地方——运行期只认库里拉下来的那份。
    """
    modules = {}
    for name in PROMPT_MODULE_ORDER:
        path = prompts_dir / f"{name}.txt"
        modules[name] = path.read_text(encoding="utf-8") if path.exists() else ""
    return PromptBundle(
        version=version,
        content_hash=compute_hash(modules),
        modules=modules,
        note=note,
    )


# ── 本地暂存 ────────────────────────────────────────────────────────


def _write_cache(cache_dir: Path, bundle: PromptBundle) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / CACHE_FILE).write_text(
        json.dumps(
            {
                "version": bundle.version,
                "content_hash": bundle.content_hash,
                "modules": bundle.modules,
                "note": bundle.note,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _read_cache(cache_dir: Path) -> PromptBundle | None:
    path = cache_dir / CACHE_FILE
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return PromptBundle(
        version=data["version"],
        content_hash=data["content_hash"],
        modules=data["modules"],
        note=data.get("note"),
    )
