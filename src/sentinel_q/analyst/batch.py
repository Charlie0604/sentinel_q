"""并发推 N 条（架构文档 4.2）。

爬虫比 AI 快，串行"推一条等一条"会让模型响应时间成为整条流水线的瓶颈，
所以**一次推多条、回来一条处理一条**。

## ⚠️ 一次请求还是只处理一条

"并发推 8 条"是 8 个**并发请求**，不是把 8 条拼进一个 prompt（4.3 明确禁止拼接：
长上下文里内容会被忽略导致漏判）。所以这里并发的是**调用**，
每条各自拼 prompt、各自解析，答复天然与输入一一对应。

## 两条来自 4.2 的硬要求

1. **一返回就回调**（决策 39）：并发推 8 条，第 5 条返回后进程崩了，若结果只在
   内存里，剩下 3 条重启后要重新问一遍 AI。"重爬一次内容是廉价的，重问一次 AI 不是。"
   所以 `on_result` 是**每条到手立刻**调，不是在最后统一调一遍——
   写盘那件事由调用方在回调里做（这个模块不碰文件）。
2. **一条失败不拖垮一批**：单条异常记进 `failed` 并打日志，其余照跑。
   和 `collector/extract.py::_write` 同款策略——单条失败是常态（网络抖一下、
   模型偶尔吐一段不合约的 JSON），整批停下才是真的贵。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import TypeVar

log = logging.getLogger(__name__)

Item = TypeVar("Item")
Judgment = TypeVar("Judgment")


@dataclass(frozen=True)
class BatchReport:
    """这一批的账。**失败的条目不能被吞掉**——它就靠这个结构浮出来。"""

    done: int
    failed: tuple[tuple[str, str], ...] = ()  # (标识, 错误信息)

    @property
    def ok(self) -> bool:
        return not self.failed

    def describe(self) -> str:
        if not self.failed:
            return f"✅ {self.done} 条全部判完"
        listing = "；".join(f"{label}（{error}）" for label, error in self.failed)
        return f"⚠️ 判完 {self.done} 条，{len(self.failed)} 条失败：{listing}"


def judge_many(
    items: Iterable[Item],
    judge: Callable[[Item], Judgment],
    *,
    label: Callable[[Item], str],
    concurrency: int,
    on_result: Callable[[Item, Judgment], None] | None = None,
) -> BatchReport:
    """并发跑完一批。**不抛异常**——失败都在 `BatchReport.failed` 里。

    ⚠️ **`done + len(failed)` 恰好等于总条数**：一条要么判完并落了盘，要么在失败
    清单里，没有第三种。所以"回调里写盘失败"那条不进 `done`——它没留下来，
    算成功就等于骗自己下次不用重问（决策 39 的整条理由就是这个）。

    `label` 是每条的身份，用来在失败清单和日志里指出是哪一条（AI 模块不知道
    数据库主键，所以这个标识由调用方给）。**必填**：默认值只能靠猜属性名，
    而猜错的表现是"日志里全是同一条"，比没有标识还难查。

    `on_result` 在**每条返回的那一刻**被调用，不是攒完再调（决策 39）。
    回调里抛异常同样只影响那一条——它是用户代码，不该有能力炸掉整批。
    """
    if concurrency < 1:
        raise ValueError(f"并发数得是正数，拿到的是 {concurrency}")

    done = 0
    failed: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(judge, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                judgment = future.result()
            except Exception as exc:  # noqa: BLE001 - 单条失败不该拖垮一批
                name = _safe_label(label, item)
                failed.append((name, f"{type(exc).__name__}: {exc}"))
                log.error("❌ %s 判定失败：%s", name, exc)
                continue
            if on_result is None:
                done += 1
                continue
            if _deliver(on_result, item, judgment, label, failed):
                done += 1

    return BatchReport(done=done, failed=tuple(failed))


def _deliver(
    on_result: Callable[[Item, Judgment], None],
    item: Item,
    judgment: Judgment,
    label: Callable[[Item], str],
    failed: list[tuple[str, str]],
) -> bool:
    """调一次回调，返回这条算不算真的成了。**回调自己炸了也只算那一条失败。**

    它干的是写盘（决策 39），而写盘失败意味着这条结果**没留下来**——
    算成功就等于骗自己下次不用重问，所以它**不进 `done`**、只进 `failed`。
    """
    try:
        on_result(item, judgment)
    except Exception as exc:  # noqa: BLE001 - 回调是用户代码，不该有能力炸掉整批
        name = _safe_label(label, item)
        failed.append((name, f"回调失败 {type(exc).__name__}: {exc}"))
        log.error("❌ %s 的结果没能落盘：%s", name, exc)
        return False
    return True


def _safe_label(label: Callable[[Item], str], item: Item) -> str:
    """取标识。连取标识都炸了也得给出行，不能让它把整批带走。

    ⚠️ `repr` 那一步**也在 try 里**。它看着不可能炸，但只要它炸了，
    异常就是从"报告失败"这条路上抛出去的——那时候整批已经跑完，
    却死在写账的半道上，比原本那条失败难查得多。
    """
    try:
        return label(item)
    except Exception:  # noqa: BLE001 - 调用方给的那个标识取不出来，退到 repr
        return _repr_or_placeholder(item)


def _repr_or_placeholder(item: Item) -> str:
    """连 `repr` 都炸了（对，这世上真有这种对象）也得给出一行。"""
    try:
        return repr(item)
    except Exception:  # noqa: BLE001 - 见上
        return "<取不出标识的那一条>"
