"""能力四的**决策逻辑**测试——不需要浏览器、不需要网络。

`answers.extract_answers()` 本身是驱动代码（导航、点击、滚动），只能靠实跑校准。
但它里面有两块**纯算术**，恰恰是最容易写错、而且**错了完全不报错**的地方：

  1. `AnswerSpec` 的口径（哪种模式点哪个排序、哪种模式能早停）
  2. `AnswersReport` 的条数核算（采到多少 / 声明多少 / 算不算采全）
  3. `KnownRun` 的连续计数（早停判据）

这三块错了的表现都是"采集顺利完成、条数看着正常、实际漏了一大片"，
正是架构文档 4.1 列为**不可接受**的那类失败。所以它们必须用用例钉死，
而不是靠"跑一遍看看有没有报错"。

⚠️ 这里断言的是**算术和口径**，不是"真实页面上会怎样"。
真实条数（15/15/0、91/89/6、1735/5/15）来自三份快照实测，
见 `test_parse.py` 的第三部分和 `answers.py` 的模块开头。
"""

from __future__ import annotations

import pytest

from sentinel_q.collector import answers, parse, selectors
from sentinel_q.collector.answers import AnswerSpec, AnswersReport, KnownRun

# ── 一、口径：哪种模式点哪个排序、能不能早停 ────────────────────────
#
# 这两条是 2026-09-26 用户改的口径，**推翻**了更早的两处说法：
#
#     全量（backfill）  →  默认排序（= 相关热度，为了采到"最值得看的那些"）
#     更新（update）    →  按时间排序 + 追上旧内容就早停
#
# 口径写错的后果不对称：把全量写成按时间排序，会漏掉所有高赞老回答；
# 把更新写成默认排序，早停判据就不成立了（下面 `test_只有更新模式能早停` 正是
# 钉这个耦合）。


def test_全量模式点默认排序() -> None:
    """全量要的是"最值得看的那些"，而默认排序就是相关的热度排序。

    实测这**通常一次都不用点**：默认排序本来就是知乎的初始状态，
    所以 `_select_sort` 走的是"回读确认"那一支。
    """
    spec = AnswerSpec(question_id="34450022", mode="backfill")

    assert spec.sort_text == selectors.QUESTION_SORT_DEFAULT_TEXT
    assert spec.sort_text == "默认排序"


def test_更新模式点按时间排序() -> None:
    """更新要的是"上次之后新增的"，只有按时间排序才能一条条往回追。"""
    spec = AnswerSpec(question_id="34450022", mode="update")

    assert spec.sort_text == selectors.QUESTION_SORT_NEWEST_TEXT
    assert spec.sort_text == "按时间排序"


def test_只有更新模式能早停() -> None:
    """⭐ **早停和排序方式是一对，不能拆开。**

    早停的推理是"遇到一条采过的 ⇒ 下面的都更旧、也都采过"。
    这个推理**只在单调有序的列表上成立**——按时间排序成立，
    按相关性排序**不成立**（一条采过的后面完全可能是没采过的新内容）。

    所以 `early_stop` 是**由模式推导出来的**，不是一个可以单独设的开关。
    如果哪天有人让全量模式也能早停，采到一半就会停，而且不会有任何报错。
    """
    assert AnswerSpec(question_id="1", mode="update").early_stop is True
    assert AnswerSpec(question_id="1", mode="backfill").early_stop is False


def test_两种模式的排序口径确实相反() -> None:
    """把两个模式放在一起断言——免得以后有人"统一"成一个值。"""
    backfill = AnswerSpec(question_id="1", mode="backfill")
    update = AnswerSpec(question_id="1", mode="update")

    assert backfill.sort_text != update.sort_text


def test_问题网址按_id_拼() -> None:
    spec = AnswerSpec(question_id="34450022")

    assert spec.url == "https://www.zhihu.com/question/34450022"


def test_默认模式是全量() -> None:
    """不写模式时按全量算——**保守**的那一边：多采不会丢数据，少采会。"""
    assert AnswerSpec(question_id="1").mode == "backfill"


# ── 二、早停判据：连续 N 条已在库里 ─────────────────────────────────
#
# 判据是"**连续**"而不是"碰见一条就停"。库里可能缺了中间几条
# （上一轮卡住了、或者按热度采的时候跳过了一些），连续计数能容忍个别空洞，
# 不会因为撞见一条旧的就把后面新的全丢掉。


def _known(urls: set[str]):
    """造一个查重回调：URL 在集合里就算"库里已经有了"。"""
    return lambda url: url in urls


def test_没有查重回调就永远不早停() -> None:
    """全量模式传的就是 `None`——没有判据，就不该停。"""
    run = KnownRun(is_known=None, threshold=2)

    assert run.observe("u1") is False
    assert run.observe("u1") is False
    assert run.tripped is False


def test_连续条数够了才早停() -> None:
    """阈值 3：第 1、2 条只记数，第 3 条才收工。"""
    run = KnownRun(is_known=_known({"a", "b", "c"}), threshold=3)

    assert run.observe("a") is False
    assert run.observe("b") is False
    assert run.observe("c") is True


def test_撞见一条新的就把计数清零() -> None:
    """⭐ 这是"连续"两个字的全部意义。

    库里有断档时（上一轮卡住过），旧的、旧的、**新的**、旧的、旧的……
    如果只数总数，这串会在第 5 条误判成"追上旧内容了"，
    而后面那些**从没采过**的回答一条都拿不到。
    """
    run = KnownRun(is_known=_known({"a", "b", "d", "e"}), threshold=3)

    assert run.observe("a") is False  # 旧 1
    assert run.observe("b") is False  # 旧 2
    assert run.observe("c") is False  # 新的 → 清零
    assert run.observe("d") is False  # 旧 1（重新数）
    assert run.observe("e") is False  # 旧 2
    assert run.tripped is False, "断了之后只数到 2 条，不该早停"


def test_首条网址记的是本轮连续的起点() -> None:
    """早停时报出"从哪条开始都是旧的"，人工核对这个判断对不对全靠它。

    ⚠️ 必须是**这一轮连续**的第一条，不是有史以来第一条——
    报错了会让人去核对一段根本不相干的区间。
    """
    run = KnownRun(is_known=_known({"a", "b", "c"}), threshold=2)

    run.observe("a")
    assert run.first_url == "a"

    run.observe("new")  # 断了
    run.observe("b")

    assert run.first_url == "b", "重启的连续段要重新记起点"


def test_阈值默认是五条() -> None:
    """`EARLY_STOP_AFTER` 是**连续**条数，不是"总共有 5 条旧的"。

    定成 1 太脆（库里有个空洞就早停，丢掉后面全部新内容），
    定得太大又白滚很久。5 是权衡的结果，改它要连着这份用例一起改。
    """
    assert answers.EARLY_STOP_AFTER == 5
    run = KnownRun(is_known=_known({"x"}), threshold=answers.EARLY_STOP_AFTER)

    assert [run.observe("x") for _ in range(4)] == [False] * 4
    assert run.observe("x") is True


def test_阈值可以调() -> None:
    run = KnownRun(is_known=_known({"x"}), threshold=1)

    assert run.observe("x") is True


# ── 三、条数核算：采到多少 / 声明多少 / 算不算采全 ──────────────────
#
# 问题页**没有**"没有更多了"（实测 0 次），滚动循环只可能以 GAVE_UP 收场，
# 而 GAVE_UP 同时对应"真采完了"和"卡在半路"。所以完整性**只能**另找判据。
#
# 下面三组数字不是编的，是三份快照的实测值。


def _report(declared: int | None, collected: int, collapsed: int = 0) -> AnswersReport:
    report = AnswersReport(question_id="34450022", url="u", mode="backfill")
    report.declared = declared
    report.collapsed = collapsed
    report.items = [_item(i) for i in range(collected)]
    return report


def _item(n: int) -> parse.ParsedItem:
    return parse.ParsedItem(
        url=f"https://www.zhihu.com/question/34450022/answer/{n}",
        content_type="answer",
        zhihu_id=str(n),
        question_id="34450022",
    )


def test_条数对上就是采全了() -> None:
    """`question_bottom.html` 实测：声明 15、采到 15、折叠 0。"""
    report = _report(declared=15, collected=15)

    assert report.accounted == 15
    assert report.complete is True
    assert report.missing == 0
    assert report.ok is True


def test_多出来也算采全() -> None:
    """⭐ `large_question.html` 实测：声明 91、采到 89、折叠 6 → **95 > 91**。

    知乎那个「N 个回答」是个**缓存的计数**，跟页面真实渲染出来的条数对不齐。
    所以判据必须是 `>=` 而不是 `==`——写成 `==` 会**永远**报"不完整"，
    而一个永远在报警的告警等于没有告警。
    """
    report = _report(declared=91, collected=89, collapsed=6)

    assert report.accounted == 95
    assert report.complete is True, "95 >= 91，多出来是正常的"
    assert report.missing == 0, "不可能是负数"
    assert report.ok is True


def test_条数差得远就是没采全() -> None:
    """`question_newest.html` 实测：声明 1735、采到 5、折叠 15。

    这是**只采了首屏**的样子——要按错误上报，绝不能当成功。
    """
    report = _report(declared=1735, collected=5, collapsed=15)

    assert report.accounted == 20
    assert report.complete is False
    assert report.missing == 1715
    assert report.ok is False
    assert "不完整" in report.describe()


def test_折叠的算进总数() -> None:
    """⭐ 折叠回答不点开（用户 2026-09-26 决定），但**必须数出来**。

    它们算在声明总数里。不数的话完整性判据永远差那几条，
    每次采集都误报"没采全"。
    """
    without = _report(declared=20, collected=18)
    with_folded = _report(declared=20, collected=18, collapsed=2)

    assert without.complete is False
    assert with_folded.complete is True, "18 采到的 + 2 折叠的 = 20，够了"


def test_页面没给总数时是不知道而不是没问题() -> None:
    """⭐ `declared is None` 是**"不知道"**，不能当成"没问题"。

    这是本模块最容易被写成 `return True` 的岔路：没有总数就没法核算，
    那时候报"成功"等于把"不知道"伪装成"没问题"——
    恰恰是架构文档 4.1 说的那类静默失败。

    所以 `complete` 是三态的：True / False / None，`ok` 才算成功与否。
    """
    report = _report(declared=None, collected=5)

    assert report.complete is None
    assert report.missing == 0, "核算不了时 missing 没有意义，给 0 并由 complete 表达"
    assert report.ok is False, "核算不了不许报成功"
    assert "无法核算" in report.describe()


def test_早停时即使条数对不上也算正常收场() -> None:
    """⭐ 更新模式的正常结局：上面这段是新的，下面都是采过的。

    这时候报"不完整"是**错的**——漏掉的那些上一次就采过，
    报错会让每一次正常的更新采集都飘红，然后所有人学会忽略这个报错。
    """
    report = _report(declared=1735, collected=3)
    report.early_stop = True
    report.early_stop_at = "https://www.zhihu.com/question/34450022/answer/1"

    assert report.complete is False
    assert report.ok is True, "早停是主动收工，不是失败"


def test_早停的文案说清楚是从哪条开始追上的() -> None:
    """报出起点，人工才核得动"这个早停判断对不对"。

    ⚠️ 早停时**不能**再喊"不完整"：那是设计内的收场，
    而一个每次更新都会喊的告警会训练人忽略告警。
    """
    report = _report(declared=1735, collected=3)
    report.early_stop = True
    report.early_stop_at = "https://www.zhihu.com/question/34450022/answer/777"

    text = report.describe()

    assert "早停" in text
    assert "answer/777" in text
    assert "不完整" not in text


def test_描述里带上排序回读() -> None:
    """排序回读是"排序真的生效了"的唯一证据，要留在报告里。"""
    report = _report(declared=15, collected=15)
    report.sort_readback = "按时间排序"

    assert "按时间排序" in report.describe()


def test_描述里报出解析失败的条数() -> None:
    """`unparsed` 是**校准信号**——正常应该是 0，冒出来就是选择器选窄了。"""
    report = _report(declared=15, collected=15)
    report.unparsed = 3

    assert "3" in report.describe()
    assert "解析不出" in report.describe()


@pytest.mark.parametrize(
    ("declared", "collected", "collapsed", "complete"),
    [
        (15, 15, 0, True),  # question_bottom.html
        (91, 89, 6, True),  # large_question.html —— 多出来也算全
        (1735, 5, 15, False),  # question_newest.html —— 只采了首屏
        (0, 0, 0, True),  # 一个问题都没回答：0 >= 0
    ],
)
def test_三份快照的实测条数(
    declared: int, collected: int, collapsed: int, complete: bool
) -> None:
    """把三份真实快照的数字摆在一起，免得以后有人把 `>=` 改成 `==`。"""
    assert _report(declared, collected, collapsed).complete is complete


# ── 四、滚动节奏：跟能力一（搜索页）对齐 ────────────────────────────
#
# 2026-09-27 用户要求「使用能力一的滚动逻辑更加快速一点」。
# 换掉的是循环里那两个参数（怎么滚、滚完怎么等），**不是循环本身**——
# 完整性核算、早停、nudge 全都留在 `scrolling.scroll_until_exhausted` 里。
#
# ⚠️ 这一条值得测，是因为它**换了也不会报错**：还是滚、还是能采到东西，
#    只是每轮从 0.35 秒变成几秒。一整个问题页差的是十几分钟和"像不像人"，
#    而日志上完全看不出来。唯一能钉住它的地方就是这里。


class _FakeSession:
    """只够 `extract_answers` 跑到滚动那一步。"""

    def __init__(self) -> None:
        self.page = object()
        self.opened: list[str] = []
        self.paces: list[float | None] = []

    def open(self, url: str) -> None:
        self.opened.append(url)

    def pace(self, factor: float | None = None) -> None:
        self.paces.append(factor)


def _drive_to_the_scroll_block(monkeypatch: pytest.MonkeyPatch) -> dict:
    """把 `extract_answers` 除了滚动以外的部分全换掉，返回捕获到的滚动参数。"""
    caught: dict = {}

    monkeypatch.setattr(answers.selectors, "require_calibrated", lambda *a: None)
    monkeypatch.setattr(answers, "_select_sort", lambda session, spec: "默认排序")
    monkeypatch.setattr(answers.drive, "page_html", lambda page: "")
    monkeypatch.setattr(answers.parse, "parse_question_total", lambda html: None)
    monkeypatch.setattr(answers.parse, "parse_collapsed_text", lambda text: 0)
    monkeypatch.setattr(answers.drive, "Harvester", lambda *a, **k: (lambda: 0))
    monkeypatch.setattr(answers.drive, "text_of", lambda *a, **k: "")
    monkeypatch.setattr(answers.drive, "has_text", lambda *a, **k: False)
    monkeypatch.setattr(answers.drive, "nudge", lambda *a, **k: None)
    monkeypatch.setattr(
        answers.drive, "scroll_flick", lambda page, **k: caught.setdefault("flick", k)
    )

    def fake_scroll(**kwargs):
        caught["scroll"] = kwargs
        kwargs["scroll"]()  # 真的调一次，才能看出它调的是谁
        kwargs["pace"]()
        kwargs["collect"]()
        return answers.scrolling.ScrollOutcome(
            1, 0, answers.scrolling.StopReason.STABLE
        )

    monkeypatch.setattr(answers.scrolling, "scroll_until_exhausted", fake_scroll)
    return caught


def test_滚动用的是能力一那套拨轮子(monkeypatch: pytest.MonkeyPatch) -> None:
    """⭐ 每轮是「连拨几下滚轮」，不是「滚一下停一拍」。

    拨的是 `drive.scroll_flick`，步数默认 `drive.FLICK_STEPS`。
    """
    caught = _drive_to_the_scroll_block(monkeypatch)
    session = _FakeSession()

    answers.extract_answers(session, AnswerSpec("123", "backfill"))

    assert caught["flick"] == {"steps": answers.drive.FLICK_STEPS}


def test_滚动步数可以调(monkeypatch: pytest.MonkeyPatch) -> None:
    """调大 = 每轮跨得远、跑得快，也更容易跨过没触发懒加载的那一段。"""
    caught = _drive_to_the_scroll_block(monkeypatch)

    answers.extract_answers(_FakeSession(), AnswerSpec("123", "backfill"), flick_steps=9)

    assert caught["flick"] == {"steps": 9}


def test_每轮的停顿也跟能力一一致(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ 停顿时长**乘的是 `scrolling.PACE_FACTOR`**，不是 `session` 里那个默认值。

    不乘的话每轮要停 3~8 秒，一圈问题页下来比原来那套还慢——而
    `session.pace()` 不传参**照样能被调通**，所以错了不会有任何报错。
    """
    _drive_to_the_scroll_block(monkeypatch)
    session = _FakeSession()

    answers.extract_answers(session, AnswerSpec("123", "backfill"))

    assert session.paces == [answers.scrolling.PACE_FACTOR]
    assert answers.scrolling.PACE_FACTOR < 1, "这个系数就是用来把停顿压短的"


def test_换掉的是节奏不是循环(monkeypatch: pytest.MonkeyPatch) -> None:
    """完整性核算、终点判据、nudge 一个都不能少——它们才是"采全了没有"的依据。

    ⚠️ 尤其 `stop_early`：`scrolling` 那边靠它区分"滚不动了"和"够了"，
    换成 `None` 之后循环照跑、条数照采，只是**永远滚到最后才停**。
    """
    caught = _drive_to_the_scroll_block(monkeypatch)

    answers.extract_answers(_FakeSession(), AnswerSpec("123", "backfill"))

    for name in ("at_end", "collect", "nudge", "stop_early"):
        assert callable(caught["scroll"][name]), name
