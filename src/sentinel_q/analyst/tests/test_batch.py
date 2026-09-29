"""并发推 N 条（架构文档 4.2）。

这个文件钉的是两条硬要求：

  1. **一返回就回调**（决策 39）——不是攒完再统一调一遍。第 5 条返回后进程崩了，
     若结果只在内存里，剩下 3 条重启后要重新问一遍 AI，而"重爬一次内容是廉价的，
     重问一次 AI 不是"。
  2. **一条失败不拖垮一批**——单条异常记进 `failed` 并打日志，其余照跑。

⚠️ 这里测的是并发调度本身，`judge` 是个普通函数，一次模型调用都没有。
"""

from __future__ import annotations

import threading

import pytest

from sentinel_q.analyst.batch import BatchReport, judge_many

# ── 账目 ────────────────────────────────────────────────────────────


def test_empty_input_is_a_clean_empty_report() -> None:
    report = judge_many([], lambda item: item, label=str, concurrency=4)

    assert (report.done, report.failed) == (0, ())
    assert report.ok


def test_report_says_what_failed() -> None:
    report = BatchReport(done=2, failed=(("q1", "LLMSchemaError: 缺字段"),))

    assert not report.ok
    assert "q1" in report.describe()
    assert "缺字段" in report.describe()


def test_report_describes_a_clean_run() -> None:
    assert "3" in BatchReport(done=3).describe()


def test_zero_concurrency_is_refused() -> None:
    """并发数写 0 会让 ThreadPoolExecutor 抛一个跟本意无关的错，当场说清楚更好。"""
    with pytest.raises(ValueError):
        judge_many([1], lambda item: item, label=str, concurrency=0)


# ── 一一对应 ────────────────────────────────────────────────────────


def test_every_item_gets_its_own_result() -> None:
    """一次请求只处理一条，所以答复天然与输入一一对应——不存在按 index 拆回去的问题。"""
    items = [f"c{i}" for i in range(20)]
    seen: dict[str, str] = {}

    report = judge_many(
        items,
        lambda item: item.upper(),
        label=str,
        concurrency=8,
        on_result=lambda item, judgment: seen.__setitem__(item, judgment),
    )

    assert report.ok
    assert report.done == 20
    assert seen == {item: item.upper() for item in items}


def test_results_arrive_out_of_order_but_nobody_is_missed() -> None:
    """并发下回来顺序本来就不保证；账要对得上，不是顺序对得上。"""
    order: list[str] = []

    def judge(item: str) -> str:
        if item == "slow":
            threading.Event().wait(0.05)
        return item

    report = judge_many(
        ["slow", "fast"],
        judge,
        label=str,
        concurrency=2,
        on_result=lambda item, judgment: order.append(item),
    )

    assert report.done == 2
    assert sorted(order) == ["fast", "slow"]


# ── 决策 39：一返回就回调 ───────────────────────────────────────────


def test_on_result_fires_the_moment_each_one_returns() -> None:
    """⭐ 攒完再调的话，这条断言必红。

    慢的那条卡在 `release` 上；快的那条回来时**慢的还没结束**，
    回调就应该已经发生了——回调里 `release.set()` 把慢的放行，
    整个流程才走得下去。若实现是"全部跑完再统一回调"，两边会互相等到超时，
    然后 `slow_done` 早就置上了，断言当场失败。
    """
    release = threading.Event()
    slow_done = threading.Event()

    def judge(item: str) -> str:
        if item == "slow":
            release.wait(5)
            slow_done.set()
        return item.upper()

    def on_result(item: str, judgment: str) -> None:
        if item == "fast":
            assert not slow_done.is_set(), "快的这条回来时，慢的那条还没结束"
            release.set()

    report = judge_many(["slow", "fast"], judge, label=str, concurrency=2, on_result=on_result)

    assert report.ok
    assert report.done == 2


def test_a_broken_callback_only_costs_that_one_item() -> None:
    """回调干的是写盘。写盘失败 = 这条结果**没留下来**，算成功就等于骗自己不用重问，
    所以它只进 `failed`、不进 `done`。"""

    def on_result(item: str, judgment: str) -> None:
        if item == "b":
            raise OSError("磁盘满了")

    report = judge_many(
        ["a", "b", "c"], lambda item: item, label=str, concurrency=1, on_result=on_result
    )

    assert report.done == 2
    assert [label for label, _ in report.failed] == ["b"]
    assert "回调失败" in report.failed[0][1]


def test_done_plus_failed_is_exactly_the_batch_size() -> None:
    """⭐ 一条要么判完并落了盘，要么在失败清单里，没有第三种。

    这条比单独断言 done 或 failed 都有用：数目对不上就说明有条被悄悄吞了，
    而"被吞掉的那条"正是这个模块存在的全部理由。
    """

    def judge(item: int) -> int:
        if item % 3 == 0:
            raise ValueError("判不了")
        return item

    def on_result(item: int, judgment: int) -> None:
        if item == 7:
            raise OSError("写盘失败")

    items = list(range(12))
    report = judge_many(items, judge, label=str, concurrency=4, on_result=on_result)

    assert report.done + len(report.failed) == len(items)
    # 0/3/6/9 判不出来，7 判出来了但没写下去
    assert report.done == 7
    assert sorted(label for label, _ in report.failed) == ["0", "3", "6", "7", "9"]


# ── 一条失败不拖垮一批 ──────────────────────────────────────────────


def test_one_failure_does_not_stop_the_rest() -> None:
    def judge(item: int) -> int:
        if item == 3:
            raise ValueError("这条答复不合约")
        return item * 2

    report = judge_many(range(6), judge, label=str, concurrency=3)

    assert report.done == 5
    assert [label for label, _ in report.failed] == ["3"]
    assert "ValueError" in report.failed[0][1]


def test_a_broken_label_still_reports_the_failure() -> None:
    """报告失败这条路上再炸一次，那才是最尴尬的：整批死在报错的半道上。"""

    def label(item: object) -> str:
        raise RuntimeError("取标识失败")

    def judge(item: object) -> object:
        raise ValueError("判不了")

    report = judge_many(["x"], judge, label=label, concurrency=1)

    assert report.done == 0
    assert len(report.failed) == 1
    assert "ValueError" in report.failed[0][1]


def test_an_item_that_cannot_even_be_printed_still_does_not_take_down_the_batch() -> None:
    """`label` 和 `repr` 双双炸掉时也得给出一行——留个占位，别把整批带走。"""

    class Nasty:
        def __repr__(self) -> str:
            raise RuntimeError("连 repr 都炸")

    def label(item: object) -> str:
        raise RuntimeError("取标识失败")

    def judge(item: object) -> object:
        raise ValueError("判不了")

    report = judge_many([Nasty()], judge, label=label, concurrency=1)

    assert report.done == 0
    assert len(report.failed) == 1
    assert "ValueError" in report.failed[0][1]
