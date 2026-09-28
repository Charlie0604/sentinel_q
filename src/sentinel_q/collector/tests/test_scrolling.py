"""滚动骨架的测试（架构文档 3.3 / 3.4）。

不碰浏览器：`scroll_until_exhausted` 只吃回调，所以这里用几个闭包就能把
终止条件测干净。要钉住的是几条容易写错、而且**错了会静默漏数据**的规则：

  1. 看到"没有更多了"就停
  2. 连续 N 轮无新增 → 先**上滚下滚重试**，不要立刻下结论
  3. 重试也没用 → `GAVE_UP` 放弃并上报，**不许卡在那里**
  4. 撞上 max_rounds 必须**标记为截断**，不能伪装成正常结束
  5. `EARLY_STOP` 是**主动收工**，不是故障——不能算截断，也不能被 END_MARKER 顶掉

第 2、3 条是 2026-09-26 用户实跑之后加的，原话：

    如果问题很长快速的滚动到底部它会不加载，如果慢一点滚动好像没有这个问题，
    然后如果不加载了往上滚一下再继续往下又可以继续触发加载。
    ……当滚不动了等待了一段时间没有加载，上滚下滚的操作也无效后应该放弃任务，
    而不是一直卡着。

第 5 条同一天加的，来自更新采集的口径：

    更新采集时按照时间排序，但是如果发现这篇内容已经是采集过的（下面的内容都是老内容）
    那么就直接早停。
"""

from __future__ import annotations

import pytest

from sentinel_q.collector.scrolling import (
    ScrollOutcome,
    StopReason,
    scroll_until_exhausted,
)


class FakePage:
    """一个假页面：每次滚动吐出一批条数，直到吐完为止。"""

    def __init__(self, batches: list[int], end_marker_after: int | None = None) -> None:
        self._batches = list(batches)
        self._end_marker_after = end_marker_after
        self.initial = self._batches.pop(0) if self._batches else 0  # 首屏单独拿出来
        self._here = 0  # 已经吐出了多少条
        self.scrolls = 0

    def collect(self) -> int:
        """返回**本轮新增**条数。首屏算第一批。"""
        if self._here == 0:
            self._here = self.initial
            return self.initial
        if not self._batches:
            return 0
        new = self._batches.pop(0)
        self._here += new
        return new

    def scroll(self) -> None:
        self.scrolls += 1

    def at_end(self) -> bool:
        if self._end_marker_after is None:
            return False
        return self._here >= self._end_marker_after

    @property
    def seen(self) -> int:
        """已经吐出的总条数。早停判据的用例要读它（"追到第几条了"）。"""
        return self._here


def _run(page: FakePage, **kwargs: object) -> ScrollOutcome:
    """跑一轮滚动。

    `nudge` 从页面上自动取——`StallingPage` 有它就接上，`FakePage` 没有就传
    `None`（退化成旧的 STABLE 行为）。这样两种假页面共用同一个入口。
    """
    return scroll_until_exhausted(
        scroll=page.scroll,
        at_end=page.at_end,
        collect=page.collect,
        nudge=kwargs.pop("nudge", getattr(page, "nudge", None)),  # type: ignore[arg-type]
        stable_rounds=kwargs.pop("stable_rounds", 3),  # type: ignore[arg-type]
        max_rounds=kwargs.pop("max_rounds", 500),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


def test_stops_at_end_marker() -> None:
    """看到"没有更多了"就停，且这一轮的内容不能漏。"""
    page = FakePage(batches=[10, 10, 10, 10], end_marker_after=30)

    outcome = _run(page)

    assert outcome.stop_reason is StopReason.END_MARKER
    assert outcome.collected == 30  # 前后两轮都算进去了
    assert not outcome.truncated


def test_stops_after_stable_rounds_without_marker() -> None:
    """文案改版、页面上再也没有"没有更多了"时，靠连续无新增兜底。

    这是**必需的**保底：只认文案的话，采集会一路空转到 max_rounds 才停，
    期间每轮都在白等。
    """
    page = FakePage(batches=[10, 10])  # 吐两批就没了，且没有结束标记

    outcome = _run(page, stable_rounds=3)

    assert outcome.stop_reason is StopReason.STABLE
    assert outcome.collected == 20
    assert page.scrolls <= 5  # 没有空转到 max_rounds


def test_stable_rounds_tolerates_a_slow_round() -> None:
    """中间偶尔一轮没加载出来，不能就此判定到底——这是 stable_rounds 存在的意义。"""
    page = FakePage(batches=[10, 0, 10])  # 第二轮空转，第三轮又有了

    outcome = _run(page, stable_rounds=3)

    assert outcome.collected == 20  # 第三轮那 10 条没有因为第二轮空转而丢掉


def test_max_rounds_is_reported_as_truncation() -> None:
    """撞上上限必须标记为截断——静默截断看起来像"抓完了"，是最糟的失败模式。"""
    page = FakePage(batches=[1] * 100)  # 永远吐不完

    outcome = _run(page, max_rounds=5, stable_rounds=99)

    assert outcome.stop_reason is StopReason.MAX_ROUNDS
    assert outcome.truncated is True
    assert outcome.suspicious is True
    assert "截断" in outcome.describe()
    assert "不完整" in outcome.describe()


def test_stable_is_suspicious_but_not_truncated() -> None:
    """无标记命中只是"可疑"，不能和"截断"混为一谈——两种情况的处置不一样。"""
    page = FakePage(batches=[5])

    outcome = _run(page)

    assert outcome.suspicious is True
    assert outcome.truncated is False


def test_first_screen_is_collected_before_any_scroll() -> None:
    """首屏内容已经在了，第一轮必须先收再滚——否则会漏掉开头的几条。"""
    page = FakePage(batches=[10], end_marker_after=10)

    outcome = _run(page)

    assert outcome.collected == 10
    assert page.scrolls == 0  # 首屏就到底了，一次都不用滚


def test_pace_is_called_once_per_round() -> None:
    """节奏控制每轮调一次——漏调会让滚动变成全速，那正是风控最敏感的行为。"""
    page = FakePage(batches=[10, 10])
    calls = []

    _run(page, pace=lambda: calls.append(1))

    assert len(calls) == page.scrolls


def test_on_round_reports_progress() -> None:
    """进度回调要如实反映每轮新增，以便观察"卡在第几轮"。"""
    page = FakePage(batches=[10, 5, 0, 0, 0])
    seen: list[tuple[int, int]] = []

    _run(page, on_round=lambda r, n: seen.append((r, n)))

    assert seen[0] == (0, 10)  # 首屏
    assert seen[1] == (1, 5)
    assert sum(n for _, n in seen) == 15


@pytest.mark.parametrize("stable_rounds", [1, 2, 5])
def test_configurable_stable_rounds(stable_rounds: int) -> None:
    page = FakePage(batches=[1])

    outcome = _run(page, stable_rounds=stable_rounds)

    # 首屏 + stable_rounds 轮空转
    assert outcome.rounds == stable_rounds


# ── 卡住 → 上滚下滚重试 → 放弃 ──────────────────────────────────────
#
# 这一组复现的是**用户实跑时报告的现象**，不是想象中的边界情况：
# 知乎的懒加载在滚动太快时不触发，上滚一下再往下就能继续。


class StallingPage:
    """一个**必须被 nudge 才肯继续加载**的假页面。

    滚多少轮都吐不出新内容，只有 `nudge()` 之后下一轮才有货——
    正是"滚太快不加载、上滚下滚才继续"那个现象的最小模型。
    """

    def __init__(self, batches: list[int], *, max_rescues: int = 999) -> None:
        self.batches = list(batches)
        self.max_rescues = max_rescues
        self.nudges = 0
        self.scrolls = 0
        self._armed = True  # 首屏直接给，不用 nudge

    def collect(self) -> int:
        if not self._armed:
            return 0
        self._armed = False
        return self.batches.pop(0) if self.batches else 0

    def scroll(self) -> None:
        self.scrolls += 1

    def nudge(self) -> None:
        """上滚下滚一次。`max_rescues` 用完之后就救不回来了。"""
        self.nudges += 1
        if self.nudges <= self.max_rescues and self.batches:
            self._armed = True

    def at_end(self) -> bool:
        return False  # 问题页**永远没有**"没有更多了"，这是重点


def test_nudge_rescues_a_stalled_page() -> None:
    """⭐ **卡住 ≠ 到底。** 先上滚下滚试一次，内容就出来了。

    没有这一步的话，一个 1735 回答的问题页会在第 3 轮被判成"到底了"，
    安静地只采到十几条。

    ⚠️ 注意终态就是 `GAVE_UP`，**而且这是对的**：这份假页面
    `at_end()` 恒为 False，复刻的正是问题页——**那儿永远没有「没有更多了」**。
    所以循环只可能以"放弃"收场，`stop_reason` 在这里**区分不了**"采全了"
    和"卡住了"。

    这正是 `parse.parse_question_total()` 存在的理由：问题页的完整性**只能**
    靠"采到的条数 vs 声明的总数"来判断，不能靠滚动循环自己报。
    下一组用例钉住这个分工。
    """
    page = StallingPage([10, 20, 30])

    outcome = _run(page, stable_rounds=3)

    assert outcome.collected == 60, "上滚下滚之后的三批内容都要采到"
    assert page.nudges >= 2
    # 没有结束标记的页面，放弃是唯一的收场方式——不是 bug
    assert outcome.stop_reason is StopReason.GAVE_UP
    assert outcome.truncated is True, "循环**自认为**不完整，最终结论交给条数核算"


def test_scroll_loop_cannot_tell_complete_from_stuck() -> None:
    """⭐ 问题页的完整性判据**必须**来自条数核算，滚动循环给不了。

    同一个循环、同一个终态 `GAVE_UP`，一个采全了、一个卡在半路——
    光看 `ScrollOutcome` 分不出来。所以 `answers.extract_answers()` 必须：

        采到 + 折叠 >= 声明总数   →  才算成功

    这条用例把这个分工钉死，免得以后有人看到 `GAVE_UP` 就一律当失败报错。
    """
    complete = _run(StallingPage([10, 20, 30]), stable_rounds=3)
    stuck = _run(StallingPage([10, 20, 30], max_rescues=0), stable_rounds=3)

    assert complete.stop_reason is stuck.stop_reason is StopReason.GAVE_UP
    assert complete.collected == 60
    assert stuck.collected == 10, "卡住的只采到首屏——但终态和采全的那次一模一样"


def test_gives_up_instead_of_hanging_forever() -> None:
    """⭐⭐ **重试无效就放弃，不许一直卡着——用户的原话。**

    放弃必须是**明确的失败**（`GAVE_UP` + `truncated`），
    不能伪装成"抓完了"。用户明确说了漏一点无伤大雅，但**不能不说**。
    """
    page = StallingPage([10], max_rescues=0)  # nudge 永远救不回来

    outcome = _run(page, stable_rounds=2, max_nudges=3)

    assert outcome.stop_reason is StopReason.GAVE_UP
    assert outcome.truncated is True
    assert page.nudges == 3, "重试次数要受 max_nudges 约束，不能无限试"
    assert page.scrolls < 20, "必须真的放弃，不能一路空转到 max_rounds"


def test_default_is_two_retries_before_giving_up() -> None:
    """默认重试 **2** 次（2026-09-27 从 3 调小）。

    项目所有者的反馈是「到底了判断是否到底的这个过程」太慢——试 3 次
    上滚下滚，每一次都要重新滚一轮、重新收一遍，长评论区的收尾就拖在那里。
    少试一次，代价是**更早宣布"救不回来"**（= 报截断，不是静默少采）。
    """
    page = StallingPage([10], max_rescues=0)  # 怎么救都没用

    outcome = _run(page, stable_rounds=2)

    assert page.nudges == 2, "默认重试次数"
    assert outcome.stop_reason is StopReason.GAVE_UP
    assert outcome.truncated is True, "少试一次换来的是**报出来**，不是少报"


def test_nudge_gets_the_same_pause_as_a_round() -> None:
    """上滚下滚之后照常停一拍。

    这一下不是反爬节奏（人不会因为页面卡住就按固定节奏停），是**等补加载的
    请求回来**——所以不能因为它"不是给风控看的"就省掉，省掉下一轮必然收空。
    """
    calls: list[str] = []
    page = StallingPage([10], max_rescues=0)

    def nudge() -> None:
        page.nudge()
        calls.append("nudge")

    _run(page, stable_rounds=2, nudge=nudge, pace=lambda: calls.append("pace"))

    assert page.nudges == 2
    # 只数 nudge 后面**紧跟着**那一下：轮次之间也调同一个 `pace`，光数总数分不出来
    after = [calls[i + 1] for i, c in enumerate(calls) if c == "nudge"]
    assert after == ["pace", "pace"], "每次 nudge 之后都要停一拍"


def test_giving_up_says_what_happened() -> None:
    """上报的文案要让人一眼看出"数据不完整、且我们试过了"。"""
    page = StallingPage([10], max_rescues=0)

    text = _run(page, stable_rounds=2, max_nudges=2).describe("回答")

    assert "不完整" in text
    assert "上滚下滚" in text
    assert "2" in text  # 试了几次要写出来，便于判断是不是选择器坏了


def test_without_a_nudge_callback_it_falls_back_to_stable() -> None:
    """给不出 nudge 就退化成"连续稳定即到底"，别炸。

    ⚠️ 这是**评论区正在走的路**（2026-09-27 起刻意不给 nudge），
    不是"还没实现"的临时状态——STABLE 和 GAVE_UP 对调用方的含义不同：
    前者只是记一条"请核对"，后者会把整条判成截断。
    """
    page = FakePage(batches=[5])

    outcome = _run(page)  # 不传 nudge

    assert outcome.stop_reason is StopReason.STABLE
    assert outcome.nudges == 0


# ── 早停：主动收工 ≠ 出故障 ─────────────────────────────────────────
#
# 更新采集按**时间**排序，所以"遇到一条采过的"意味着它下面的都更旧、
# 也都采过。这是唯一能用早停的顺序——按相关性排序没有这个性质
# （一条采过的后面完全可能是没采过的新内容），调用方给错场景就是静默漏采。
#
# 这一组要钉住的是**语义**：EARLY_STOP 是正常收场，不能和 GAVE_UP 混着报。
# 早停**判据本身**（连续 N 条都在库里）在 answers.KnownRun 里，另有用例。


def test_early_stop_collects_the_first_screen_then_stops() -> None:
    """⭐ 一轮都不滚就收工——但**首屏必须先收**。

    首屏那批正是"最新的一批"，是这次更新采集唯一要拿的东西。
    顺手把顺序写反（先判早停再收首屏）就会一条不剩地空手而归，
    而且看起来完全正常：`EARLY_STOP`、无报错、退出码 0。
    """
    page = FakePage(batches=[10, 10, 10])

    outcome = _run(page, stop_early=lambda: True)

    assert outcome.stop_reason is StopReason.EARLY_STOP
    assert outcome.collected == 10, "首屏必须收进来"
    assert outcome.rounds == 0
    assert page.scrolls == 0, "既然立刻收工，就一次都不该滚"


def test_early_stop_fires_exactly_when_the_old_content_starts() -> None:
    """滚到"旧内容"的边界就停，边界之前的内容一条不少。"""
    page = FakePage(batches=[10, 10, 10, 10, 10])

    def stop_early() -> bool:
        return page.seen >= 30  # 第 3 批之后全是采过的

    outcome = _run(page, stop_early=stop_early)

    assert outcome.stop_reason is StopReason.EARLY_STOP
    assert outcome.collected == 30, "边界前的内容必须全在"
    # 判据是**滚动之前**问的，所以第 3 批到手之后就地收工，不会再多滚一次
    assert page.scrolls == 2
    assert page.seen == 30


def test_early_stop_is_neither_truncation_nor_suspicious() -> None:
    """⭐⭐ **早停不是故障。** 报成 GAVE_UP 会让每次正常的更新采集都飘红。

    `truncated` 是"**确定**漏了数据"的意思。早停时下面那些没采的内容
    不是漏——是"上一次已经采过了"。所以两个标志位都必须是 False。
    """
    page = FakePage(batches=[10, 10])

    outcome = _run(page, stop_early=lambda: True)

    assert outcome.truncated is False
    assert outcome.suspicious is False


def test_early_stop_says_why_it_stopped() -> None:
    """文案要能自证清白：让人看出这是"主动收工"，不是"页面坏了"。

    ⚠️ 只说"拿够了"、**不说具体是哪个判据**——`stop_early` 现在有两类调用方
    （采满声明数 / 追上旧内容），具体理由由它们自己记日志。
    """
    page = FakePage(batches=[10, 10])

    text = _run(page, stop_early=lambda: True).describe("回答")

    assert "提前收工" in text
    assert "拿够" in text
    assert "截断" not in text and "❌" not in text, "正常收场不能看着像故障"


def test_end_marker_beats_early_stop() -> None:
    """⭐ 两个判据同时成立时报 `END_MARKER`。

    含义不同，必须分开：`END_MARKER` 是"**这一批是全的**"，
    `EARLY_STOP` 是"上面这段是新的、下面没看"。两者对调用方的后续动作
    不一样（一个可以不看，一个得去查库里有没有断档），所以顺序不能反。
    """
    page = FakePage(batches=[10, 10], end_marker_after=10)

    outcome = _run(page, stop_early=lambda: True)

    assert outcome.stop_reason is StopReason.END_MARKER


def test_early_stop_is_asked_every_round_not_once() -> None:
    """判据是**每轮重算**的，不是开局问一次。

    "追上旧内容"是滚动过程中才逐渐成立的：开局时页面上全是新的，
    几轮之后才碰到库里已有的。只在开头问一次，早停就永远不会触发——
    更新采集会一路把整个问题页采完（1735 条那个问题要滚十几分钟）。
    """
    page = FakePage(batches=[10, 10, 10])
    asked: list[int] = []

    duration = _run(page, stop_early=lambda: (asked.append(page.seen), False)[1])

    assert len(asked) >= 3, "每轮开头都要问一次"
    assert asked[0] == 10  # 首屏已经收完了才问第一遍
    assert duration.stop_reason is not StopReason.EARLY_STOP  # 一直说"不"，就一路采到底


def test_no_stop_early_callback_means_never_early_stop() -> None:
    """给不出判据的场景（全量模式、别的能力）退化成不早停，别炸。

    全量模式按**默认排序**（相关性）采，**本来就不该早停**——
    那种顺序下"碰到采过的"后面还可能有没采过的。
    """
    page = FakePage(batches=[5])

    outcome = _run(page)  # 不传 stop_early

    assert outcome.stop_reason is StopReason.STABLE
