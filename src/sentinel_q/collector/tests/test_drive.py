"""`drive.py` 里**不碰浏览器就能测**的那部分。

⚠️ 这里测**不是** JS 脚本本身——`_STEP` / `_SCROLL_TO_BOTTOM` 那几段要真浏览器
   才跑得动，由实跑验收（`scripts/check_search.py`）。这里测的是**调用序列**：
   拨滚轮到底拨了几下、每下多少像素、参数有没有按约定包成数组。

   为什么这层值得测：`scroll_flick` 存在的全部理由是"像人一样连着拨好几下"，
   而它退化成"一下跳到底"**不会报错**——只会安静地少采内容。
   这正是本项目最怕的那类失败，所以锚住调用序列是有意义的。
"""

from __future__ import annotations

import pytest

from sentinel_q.collector import drive, selectors


class FakeTarget:
    """够 `_scroll_by` 用的假页面：只记下每次 `evaluate` 收到了什么。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def evaluate(self, script: str, arg: object = None) -> None:
        self.calls.append((script, arg))

    @property
    def offsets(self) -> list[int]:
        """每次滚动请求的像素数——直接从参数数组里取。"""
        return [arg[0] for _, arg in self.calls]  # type: ignore[index]


@pytest.fixture
def slept(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """把 `time.sleep` 换成记账，免得测试真的睡半天。"""
    recorded: list[float] = []
    monkeypatch.setattr(drive.time, "sleep", recorded.append)
    return recorded


class TestScrollFlick:
    def test_flick_is_several_separate_scrolls_not_one_jump(self, slept: list[float]) -> None:
        """⭐ 这是整个函数的存在理由：**连着拨好几下**，不是一步跳到底。

        一步跳到底的问题用户实测过：「如果问题很长快速的滚动到底部它会
        **不加载**」。所以这条钉的不是"快慢"，是"有没有跳"。
        """
        page = FakeTarget()

        drive.scroll_flick(page, steps=5, step=400, gap=0.07)

        assert len(page.calls) == 5
        assert all(script == drive._STEP for script, _ in page.calls)
        assert drive._SCROLL_TO_BOTTOM not in [script for script, _ in page.calls]

    def test_each_flick_covers_its_step(self, slept: list[float]) -> None:
        page = FakeTarget()

        drive.scroll_flick(page, steps=4, step=350, gap=0.05)

        assert page.offsets == [350, 350, 350, 350]
        assert sum(page.offsets) == 1400  # 一轮总共跨过的距离

    def test_parameters_are_wrapped_in_an_array(self, slept: list[float]) -> None:
        """⭐ 双形态契约：参数**一律包成数组**再传。

        写死成 `(px) => …` 的那种，在元素上会把 `px` 绑到元素本身，
        `scrollBy(0, <元素>)` **不报错也不滚**——弹窗滚的就是元素，
        于是它安静地一次都不滚。数组是唯一能同时分清两种形态的写法。
        """
        page = FakeTarget()

        drive.scroll_flick(page, steps=1, step=400, gap=0.0)

        assert page.calls[0][1] == [400]
        assert isinstance(page.calls[0][1], list)

    def test_gap_is_spent_between_every_pair(self, slept: list[float]) -> None:
        """每下之间都要有间隔——那是手势的形状，不是节流。"""
        page = FakeTarget()

        drive.scroll_flick(page, steps=3, step=400, gap=0.07)

        assert slept == [0.07, 0.07, 0.07]

    def test_zero_steps_is_refused(self, slept: list[float]) -> None:
        """⭐ `steps=0` 必须抛，不能静默返回。

        滚 0 次 = 一次都没滚，而滚动循环会一路空转到 `max_rounds`，
        最后报一个看起来像"知乎改版了"的错。静默失败要在这里就断掉。
        """
        with pytest.raises(ValueError, match="至少要滚一下"):
            drive.scroll_flick(FakeTarget(), steps=0)

    def test_negative_steps_is_refused(self, slept: list[float]) -> None:
        with pytest.raises(ValueError, match="至少要滚一下"):
            drive.scroll_flick(FakeTarget(), steps=-1)


class TestFlickDefaults:
    def test_defaults_are_a_human_sized_burst(self) -> None:
        """默认值是一轮滚多远。**别为了"更慢更安全"把它调小**——
        调小就是回到 `scroll_step` 那种"滚一下停三秒"的机器节奏。
        """
        assert drive.FLICK_STEPS >= 3
        assert drive.FLICK_STEP >= 300
        assert drive.FLICK_GAP < 0.2  # 手势内部，不是停顿

    def test_default_covers_more_than_a_single_step(self) -> None:
        """一轮至少得跨过 `scroll_step` 的旧默认值，否则白改。"""
        assert drive.FLICK_STEPS * drive.FLICK_STEP > 400


class FakePageWithValue:
    """够 `text_length` 用的假页面：记下参数，返回值由调用方给定。"""

    def __init__(self, value: object = 0) -> None:
        self.value = value
        self.calls: list[tuple[str, object]] = []

    def evaluate(self, script: str, arg: object = None) -> object:
        self.calls.append((script, arg))
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class TestTextLength:
    """`text_length()` —— "等渲染完"的轮询判据（能力二的提速就是靠它）。"""

    def test_candidates_keep_their_written_order(self) -> None:
        """⭐ 候选要**按书写顺序**传给 JS，和 `parse._pick` 完全一致。

        顺序被吃掉（比如传 `css()` 拼好的整组）的话，命中的是**文档里靠前**
        的那个而不是我们字面上靠前的那个——文章页上那三个 `.RichText` 里
        有两个是评论输入框，判据就会看着输入框的字数说"正文渲染好了"。
        """
        page = FakePageWithValue(7)

        drive.text_length(page, selectors.RICH_TEXT)

        _, arg = page.calls[0]
        assert arg[1] is None, "没给 scope 就该传 None"
        assert arg[0] == [c.strip() for c in selectors.RICH_TEXT.split(",")]

    def test_uncalibrated_marker_is_stripped_before_reaching_js(self) -> None:
        """没校准的候选带着 `TODO-` 前缀，那是**标记不是选择器**，进 CSS 会语法错。"""
        page = FakePageWithValue(0)

        drive.text_length(page, "TODO-.Post-RichText, .RichText")

        assert page.calls[0][1][0] == [".Post-RichText", ".RichText"]

    def test_scope_rides_along(self) -> None:
        page = FakePageWithValue(3)

        drive.text_length(page, selectors.RICH_TEXT, scope=".AnswerItem[name='1']")

        assert page.calls[0][1][1] == ".AnswerItem[name='1']"

    @pytest.mark.parametrize("value", [-1, 0, 42])
    def test_value_passes_through(self, value: int) -> None:
        """三态原样返回，由调用方解释（见 `content._body_ready`）。"""
        assert drive.text_length(FakePageWithValue(value), selectors.RICH_TEXT) == value

    def test_broken_selector_degrades_to_zero(self) -> None:
        """选择器写错是校准期的常态，不该在轮询里抛异常打断整轮采集。"""
        page = FakePageWithValue(RuntimeError("invalid selector"))

        assert drive.text_length(page, "!!!") == 0
