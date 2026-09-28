"""滚动加载骨架：一直滚到"没有更多了"。四个能力共用。

刻意**只吃回调、不 import playwright**（见计划里的"驱动与解析分离"）：
于是终止条件这种最容易写错的地方，能用几个 lambda 测干净，不需要浏览器。

    outcome = scroll_until_exhausted(
        scroll  = lambda: page.evaluate("window.scrollBy(0, 900)"),
        at_end  = lambda: end_marker_visible(page),
        collect = lambda: len(new_items_this_round(page)),
        pace    = session.pace,
    )

**评论弹窗要特别注意**：它是独立的滚动容器，`window.scrollBy` 对它无效。
调用方把 `scroll` 换成滚弹窗元素的实现即可——这正是这里收成回调的原因之一。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

log = logging.getLogger(__name__)

PACE_FACTOR = 0.15
"""滚动循环里每轮之间的停顿系数（乘在 `SessionConfig.pace_range` = 3~8 秒上）。

**0.15 → 每轮停 0.45~1.2 秒。** 2026-09-26 之前是 0.5（1.5~4.0 秒），实跑
实测每轮 15 秒、20 轮只采到 52 条就被轮次上限截断。

⚠️ **为什么要动这个值，理由不是"快一点"，是"原来那样根本不像人"**：
`scroll_step` 的节奏是"滚 400px → 冻 3 秒 → 再滚 400px"，重复几十次。
真人浏览不长这样——用户的原话是「我自己手操的时候是疯狂的滚动滚轮这样
每次滚动输出的间隔就是非常小」。所以慢换来的不是安全，是**更显眼的机器节奏**。
配套的改动是 `drive.scroll_flick`（把"一个手势"打包成一次调用）。

⚠️ 但**别调到 0**：`session.pace` 是本系统唯一有反爬含义的停顿
（`session.py` 里写明了）。把每轮压到没有任何间隔，就是拿掉它。
这个系数调的是"停顿多长"，不是"要不要停"。

⚠️ **这个常量住在 `scrolling.py` 而不是原来那个 `search.py`**（2026-09-27 搬的）：
现在搜索（能力一）和评论区（能力三）共用同一套滚动骨架，
"每轮停多久"是骨架的属性，不是某一个能力的属性。`search.PACE_FACTOR`
仍然可以取到（那边是转出），那条路留着是为了不动已有调用点。
"""

class StopReason(StrEnum):
    """为什么停下来。**四种原因的含义完全不同，调用方必须区别对待。**"""

    END_MARKER = "end_marker"
    """看到"没有更多了"——正常的结束，数据是完整的。"""

    STABLE = "stable"
    """连续 N 轮没有新增，而且**没有提供补救手段**（`nudge=None`）。

    多半是到底了，**但也可能是页面卡住或选择器失效**——所以它不能当成和
    END_MARKER 一样可信。调用方应当记一条日志。"""

    GAVE_UP = "gave_up"
    """连续无新增，**上滚下滚重新触发也救不回来**，于是主动放弃。

    这是 2026-09-26 用户报告的真实情况：
    「如果问题很长快速的滚动到底部它会不加载，如果慢一点滚动好像没有这个问题，
      然后如果不加载了往上滚一下再继续往下又可以继续触发加载。」

    所以放弃是**分级的**：先慢慢滚 → 卡住就上滚再下滚 → 反复无效才放弃。
    绝不能一路空转到 max_rounds 卡死在那里（用户原话：**「而不是一直卡着」**）。

    ⚠️ 它比 STABLE 更值得报警：STABLE 至少还剩"真到底了"的可能，
    GAVE_UP 是"我们试过了、没辙了"。数据是不完整的。"""

    MAX_ROUNDS = "max_rounds"
    """撞上轮次上限，**数据是被截断的**。绝不能当成正常结束——
    架构文档反复强调静默截断比报错更糟，因为它看起来像"抓完了"。"""

    EARLY_STOP = "early_stop"
    """**主动**提前收工：调用方判定"已经拿够了"，不用再往下滚。

    这是正常的收场方式，不是异常——所以 `truncated` 和 `suspicious`
    对它都是 False。判据由调用方给（见 `stop_early` 参数），目前两种：

      * **采满声明的条数**（能力四全量、评论）——"声明多少就采多少"；
      * **追上以前采过的内容**（能力四更新，见 `answers.KnownRun`）——
        再往下都是旧的。

    ⚠️ 第二种**只对"单调有序"的列表成立。** 按时间排序时，遇到一条采过的
    就意味着它下面的都更旧、也都采过；按**相关性**排序时没有这个性质，
    一条采过的后面完全可能是没采过的新内容。所以调用方要给对场景，
    给错了就是静默漏采。第一种没有这个问题，它比的是**总数**。"""


@dataclass(frozen=True)
class ScrollOutcome:
    rounds: int
    collected: int
    stop_reason: StopReason

    nudges: int = 0
    """上滚下滚尝试重新触发懒加载的次数。`0` 表示压根没试过。"""

    @property
    def truncated(self) -> bool:
        """是否**确定**漏了数据。`True` 时调用方必须上报，不能静默返回。

        `EARLY_STOP` **不算**截断：那是"下面都是采过的旧内容"，
        是更新采集的正常收场，不是漏。
        """
        return self.stop_reason in (StopReason.MAX_ROUNDS, StopReason.GAVE_UP)

    @property
    def suspicious(self) -> bool:
        """是否值得记一条警告。"""
        return self.stop_reason not in (StopReason.END_MARKER, StopReason.EARLY_STOP)

    def describe(self, what: str = "内容") -> str:
        match self.stop_reason:
            case StopReason.END_MARKER:
                return f"{what}已到末尾：{self.rounds} 轮，共 {self.collected} 条"
            case StopReason.EARLY_STOP:
                # 具体是哪个判据（采满了 / 追上旧的了）由调用方自己记日志，
                # 这里只说"主动收工、不是故障"——两种判据共用这一句。
                return (
                    f"{what}提前收工：调用方判定已经拿够了，没滚到底"
                    f"（{self.rounds} 轮，共 {self.collected} 条）"
                )
            case StopReason.STABLE:
                return (
                    f"⚠️ {what}连续多轮无新增后停止（{self.rounds} 轮，{self.collected} 条）。"
                    "可能是真到底，也可能页面卡住或选择器失效——请核对"
                )
            case StopReason.GAVE_UP:
                return (
                    f"❌ {what}卡住了：连续无新增，上滚下滚重试 {self.nudges} 次也没能"
                    f"再加载出内容（{self.rounds} 轮，{self.collected} 条）。"
                    "**这批数据不完整**，别当成抓完了"
                )
            case StopReason.MAX_ROUNDS:
                return (
                    f"❌ {what}撞上轮次上限被**截断**（{self.rounds} 轮，{self.collected} 条）。"
                    "这批数据不完整，不要当成抓完了"
                )


def scroll_until_exhausted(
    *,
    scroll: Callable[[], None],
    at_end: Callable[[], bool],
    collect: Callable[[], int],
    nudge: Callable[[], None] | None = None,
    stop_early: Callable[[], bool] | None = None,
    pace: Callable[[], None] | None = None,
    stable_rounds: int = 3,
    max_nudges: int = 2,
    max_rounds: int = 500,
    on_round: Callable[[int, int], None] | None = None,
) -> ScrollOutcome:
    """滚到底。**分三级降级，不会一路空转到 max_rounds 卡死。**

        1. 正常滚。每轮 `scroll()` 一次，收一轮新增。
        2. 卡住了（连续 `stable_rounds` 轮无新增）→ 调 `nudge()` 上滚下滚，
           重新触发懒加载，最多 `max_nudges` 次。
        3. nudge 也救不回来 → `GAVE_UP`，**主动放弃并上报**。

    Args:
        scroll: 执行一次滚动。窗口就滚窗口，弹窗就滚弹窗元素。
            ⚠️ **必须是小步滚动，不要一步跳到 `scrollHeight`。**
            实测（用户 2026-09-26 报告）：快速滚到底**不会触发**懒加载，
            慢一点就没这个问题。用 `drive.scroll_step`。
        at_end: 页面上是否已出现"没有更多了"。
            ⚠️ 问题页**永远不会有**这个标记，那边靠 `nudge` + 条数核算兜底。
        collect: 收集本轮内容，**返回本轮新增的条数**（不是总数）。
        nudge: 上滚一小段再滚回来。给不了就退化成旧的 STABLE 行为。
        stop_early: 每轮开头问一次"该收工了吗"。返回 True 就直接返回
            `EARLY_STOP`，**且不再滚动**。给不了就永远是 False。
            ⚠️ 只对单调有序的列表用（见 `StopReason.EARLY_STOP`）。
        pace: 每轮之间的停顿。传 `session.pace` 来保持人类节奏。
            nudge 之后那一下也用它——那一下要留时间让补加载的请求回来，
            但留多久是**网络问题**，目前没有实测数据支持给它单独一个系数。
        stable_rounds: 连续几轮无新增才算"卡住"。默认 3，给慢加载留余量。
        max_nudges: 上滚下滚重试几次就放弃。默认 2。
        max_rounds: 轮次上限，防死循环。**撞上它算截断，会记在 outcome 里。**
        on_round: 每轮回调 `(轮次, 本轮新增)`，用于打进度。

    Returns:
        `ScrollOutcome`——**务必看它的 `stop_reason`**，尤其 `truncated`。
    """
    # 首屏通常已经有内容，先收一轮再开始滚，否则第一屏会被漏掉
    total = collect()
    if on_round:
        on_round(0, total)

    stable = 0
    nudges = 0
    for round_no in range(1, max_rounds + 1):
        # 先看标记再滚动：已经到底了就别白滚一次
        if at_end():
            return ScrollOutcome(round_no - 1, total, StopReason.END_MARKER)

        # 早停排在 at_end 之后：真到底了就是 END_MARKER，那不是"提前"收工，
        # 两者对调用方的含义不同（一个采全了，一个"上面这段是新的"）。
        if stop_early is not None and stop_early():
            return ScrollOutcome(round_no - 1, total, StopReason.EARLY_STOP, nudges=nudges)

        scroll()
        if pace:
            pace()

        new = collect()
        total += new
        if on_round:
            on_round(round_no, new)

        if new:
            stable = 0
            continue

        # 连续无新增计数。注意：慢加载会让某一轮 new=0 但其实还有内容，
        # 所以这里要连续 stable_rounds 轮才判定到底
        stable += 1
        if stable < stable_rounds:
            continue

        # 卡住了。先别下结论——很可能是滚太快没触发懒加载（用户实测的现象），
        # 上滚一屏再滚回来通常就能重新触发。
        if nudge is not None and nudges < max_nudges:
            nudges += 1
            log.info("连续 %d 轮无新增，上滚下滚重试第 %d/%d 次", stable, nudges, max_nudges)
            nudge()
            if pace:
                pace()
            stable = 0
            continue

        return ScrollOutcome(
            round_no,
            total,
            StopReason.GAVE_UP if nudges else StopReason.STABLE,
            nudges=nudges,
        )

    return ScrollOutcome(max_rounds, total, StopReason.MAX_ROUNDS, nudges=nudges)
