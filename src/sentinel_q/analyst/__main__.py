"""AI 模块的独立入口（架构文档 7.4）。

    python -m sentinel_q.analyst run --batch 8      # 跑一轮分析
    python -m sentinel_q.analyst run --once         # 只跑一轮就退出

## ⚠️ `run` **还没接上**——但库已经在了

这一轮的边界是"只做纯函数库"：三个任务的判定机器写完了，缺的是**文件层**。

    client.py         模型调用 + 答复解析（httpx 延迟 import，见文件开头）
    batch.py          并发推 N 条 + 一返回就回调（4.2 / 决策 39）
    judge_content.py  任务 A：正文 → fact_analysis
    judge_question.py 任务 B：问题 → dim_question.is_relevant
    judge_event.py    事件任务（原"模块四"）→ fact_content_event
    fake.py           不联网的模型客户端：离线跑通一条判定就靠它

离线自测长这样（**一次真实 API 都不调**）：

    from sentinel_q.analyst import judge_content
    from sentinel_q.analyst.fake import FakeLLMClient, full_bundle
    judgment = judge_content.judge_one(pending, bundle=full_bundle(),
                                       client=FakeLLMClient('{"is_relevant": true, ...}'))

`run` 要干的是"读一份待判清单 → 逐条判 → 把结果写回同一行"（决策 51：
断点与 AI 结果写在 `contents.jsonl` 同一行）。**那一头一尾都是文件操作，
而 `contents.jsonl` 的读写现在在 `collector/ops.py` 里**——`analyst` import 它
会当场破掉"模块之间不互相 import"这条硬规则（7.3 规则一）。
所以文件层留在主程序那边，这一轮不接。

## 提示词：一份 bundle，按任务拼

三个任务的模块共用一份 bundle，靠模块名前缀区分（见 `shared/models.py` 的
`PROMPT_MODULES`）。`PromptBundle.assemble("question")` 拼出来的串里
一个 `c_tasks` 都不会有。

## ⚠️ `prompts pull` / `prompts push` **不在这里**（决策 52）

那两个动作每一行都要连库，所以它们搬去 `storage` 了：

    python -m sentinel_q.storage prompts pull
    python -m sentinel_q.storage prompts push -v 3

这不是"挪个位置"：搬走之后这个模块**一个 Repo 参数都不收**，
于是它能在一个没有数据库、没有网络的环境里完整跑起来——
而那正是"独立测试"这句话的字面意思（架构文档 7.5）。
"""

from __future__ import annotations

import argparse
import logging
import sys

from sentinel_q.shared.config import paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sentinel_q.analyst", description="AI 模块：内容判定")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="跑一轮分析")
    run.add_argument("--batch", type=int, default=8, help="一次推送给 AI 的条数（4.2）")
    run.add_argument("--once", action="store_true", help="只跑一轮就退出，不等新内容")

    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args(argv)
    paths().ensure()

    raise NotImplementedError(
        f"run 还没接上（batch={args.batch}）：三个任务的判定机器已经写好了"
        "（judge_content / judge_question / judge_event），缺的是文件层——"
        "读待判清单、把结果写回 contents.jsonl 同一行，那两件事都在主程序那边，"
        "因为这个模块不碰 collector 的本地文件（7.3 规则一）。"
        "要离线试判定逻辑，直接调 judge_*.judge_one 并喂一个假客户端。"
    )


if __name__ == "__main__":
    sys.exit(main())
