"""提示词的本地形态：拼装、缓存、降级兜底（架构文档 7.9）。

## ⚠️ 这个文件**不连数据库**，也不属于任何一个模块（决策 52）

它住在 `shared/`，因为**两个模块都要用它**：

    storage/prompts.py  拉取/推送那一半（连库），写完缓存在这里
    analyst/            读缓存在这里，拼装也在里

放在 `analyst/` 下的话 `storage` 就得 import `analyst`，
放在 `storage/` 下就反过来——两边都破了"模块之间不互相 import"（7.3 规则一）。
`shared/` 是唯一两个都不破的位置。

## 提示词在一次任务内是冻结的

任务开始时拉一次、暂存本地，下一次任务重新拉。这正是 `prompt_version`
能作为 `fact_analysis` 单一追溯依据的前提——判断结果记录的是它**实际用的那版**。
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from sentinel_q.shared.config import Paths
from sentinel_q.shared.models import PROMPT_MODULE_ORDER, PromptBundle

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


def load_examples(prompts_dir: Path) -> PromptBundle:
    """从仓库里入库的 *.example 模板拼一版。

    它存在的意义是让AI 模块在**没有真实提示词、没有数据库**的情况下也能独立测试。
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


def load_local(paths: Paths) -> PromptBundle:
    """运行期读提示词：本地缓存优先，没有就用 `*.example` 兜底。

    ⚠️ **这是 AI 模块唯一的读入口**，而且它读的是文件——所以AI 模块
    在没有数据库、没有网络的情况下也能完整跑起来（架构文档 7.5）。

    缓存是谁写的？主程序跑 `storage.prompts.fetch_bundle()` 那一步写的。
    没人写过（第一次跑、或者主程序还没接上）就退到模板，
    并打一条 warning——**拿占位符做的判断没有意义**，必须吵。
    """
    if cached := read_cache(paths.prompts_cache):
        return cached
    log.warning(
        "本地没有暂存的提示词（%s），降级使用 *.example 模板——"
        "内容是占位符，判断结果没有意义，仅供跑通流程",
        paths.prompts_cache / CACHE_FILE,
    )
    return load_examples(paths.prompts)


# ── 本地暂存 ────────────────────────────────────────────────────────


def write_cache(cache_dir: Path, bundle: PromptBundle) -> None:
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


def read_cache(cache_dir: Path) -> PromptBundle | None:
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
