"""提示词与线上库同步（架构文档 7.9）。

## 为什么这个文件在 `storage/` 而不是 `analyst/`

因为它**每一行都要连库**。AI 模块读提示词是"从本地文件读"——
它拿到的永远是一个已经落好的 `PromptBundle`，不需要知道那东西是怎么来的、
更不需要一个 `Repo`（决策 52：模块之间不互相 import，
`analyst` 更不该为了读一个文件去 import `storage`）。

所以分界线是：

    storage/prompts.py   ← 连库、拉取、推送、写本地缓存   （只有这里能碰 Repo）
    shared/prompts.py    ← 读缓存、拼装、读 *.example     （纯本地，两边共用）

## 权威副本在线上库

提示词**不进 git**（`/prompts/*` 是 gitignore 的第三条，见 .gitignore）。
所以线上库是唯一能找到它、也是唯一能追溯"这条判断当时用的哪版提示词"的地方。
本地工作区（`prompts/`）是人工编辑的地方，本地缓存（`runtime/prompts/current.json`）
是运行期读的地方——**三个副本，库是权威**。
"""

from __future__ import annotations

import logging

from sentinel_q.shared import prompts as prompt_files
from sentinel_q.shared.config import Paths
from sentinel_q.shared.models import PromptBundle
from sentinel_q.storage.repo import Repo

log = logging.getLogger(__name__)


def fetch_bundle(repo: Repo, paths: Paths) -> PromptBundle:
    """任务开始时调用一次，取到这一整次任务要用的提示词。

    三级降级，每一级都比上一级差，而且**每一级都会说出来**：

      1. 线上库当前生效的那一版      —— 正常路径
      2. 本地暂存的上一版            —— 库连不上时；结果照旧可追溯（自带 version/hash）
      3. 仓库里的 `*.example` 模板   —— 从没推过提示词时；内容是占位符，**判断结果没有意义**

    ⚠️ 第 3 级只应该出现在"第一次跑通流程"的时候。它出现在生产日志里
    意味着 AI 正在拿占位符做判断——所以那条 log 是 warning 而不是 info。
    """
    bundle = repo.active_prompt_bundle()
    if bundle is not None:
        prompt_files.write_cache(paths.prompts_cache, bundle)
        log.info("提示词 v%s 已从线上库拉取", bundle.version)
        return bundle

    if cached := prompt_files.read_cache(paths.prompts_cache):
        # 沿用旧版是安全的：bundle 自带 version/hash，判断结果记录的就是它实际用的那版
        log.warning("线上库没有生效的提示词，沿用上次暂存的 v%s", cached.version)
        return cached

    log.warning(
        "线上库与本地暂存都没有提示词，降级使用 *.example 模板——"
        "内容是占位符，判断结果没有意义，仅供跑通流程"
    )
    return prompt_files.load_examples(paths.prompts)


def pull_to_workdir(repo: Repo, paths: Paths) -> PromptBundle:
    """`prompts pull`：把线上库那一版写进**工作区**，供人工编辑。

    ⚠️ 目标是工作区（`prompts/<name>.txt`），不是运行缓存——
    工作区才是人改的地方，缓存是程序读的地方。混成一个的话，
    下一次 `fetch_bundle` 会把人工改到一半的东西当成权威版本用。
    """
    bundle = fetch_bundle(repo, paths)
    for name, body in bundle.modules.items():
        (paths.prompts / f"{name}.txt").write_text(body, encoding="utf-8")
    return bundle


def push_from_workdir(
    repo: Repo, paths: Paths, version: int, note: str | None = None
) -> PromptBundle:
    """`prompts push`：把工作区推上库，并标记版本号。

    ⚠️ **版本号由人来给**（`-v`），不自动递增：提示词的版本是判断结果的追溯依据，
    自动递增会让"这次改动值不值得记一个版本"这个判断从人手里溜走。
    """
    bundle = prompt_files.bundle_from_workdir(paths.prompts, version, note)
    repo.push_prompt_bundle(bundle)
    return bundle
