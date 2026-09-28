"""能力一（搜索）的离线测试。

⚠️ **这里只测纯逻辑，不测"能不能真的搜到东西"。**
   真正去搜要开浏览器（`search.search` 里全是 `drive.*` 的调用），
   那一层由 `scripts/check_search.py` 实跑验收，不在这里。
   这也是整个模块的分工：能离线的都离线测，剩下那层靠实跑。

本文件测两样：

    `SearchSpec`     —— 纯数据：口径推理 + 入参校验
    `_apply_filters` —— 筛选点击的**重试与回读配对**。用假面板替换掉
                        `_read_group` / `_click_in_group` / `ensure_panel_open`
                        三个浏览器助手，于是"点完到底读没读"这件事
                        完全可以在离线环境里钉住。

⚠️ 本文件**不测**"两种模式各用什么排序"该不该是现在这样——那边还没定案，
   见 `SearchSpec.sort_filter` 的注释。
"""

from __future__ import annotations

import pytest

from sentinel_q.collector import drive, ops, selectors
from sentinel_q.collector import search as search_mod
from sentinel_q.collector.search import SearchSpec


class TestDefaults:
    """`mode` 推出来的口径。"""

    def test_backfill_is_unlimited_time(self) -> None:
        assert SearchSpec("甲").time_filter == selectors.FILTER_TIME_UNLIMITED

    def test_update_is_one_day(self) -> None:
        assert SearchSpec("甲", "update").time_filter == selectors.FILTER_TIME_DAY

    def test_backfill_keeps_the_page_default_sort(self) -> None:
        """⭐ 全量用「综合排序」——**这一档本身就是一份信号**。

        用户 2026-09-26 的理由：综合排序背后是知乎的推荐算法，
        它反映了大众对哪篇东西的关注度。换成「最新发布」就把这份信息冲掉了，
        而全量恰恰是最该留住它的那一路。

        ⚠️ 这条**以前是反的**（两种模式都钉「最新发布」）。改动是刻意的，
        不是修 bug——所以这条测试的注释要能让人看出来它翻过面。
        """
        assert SearchSpec("甲").sort_filter == selectors.FILTER_SORT_DEFAULT

    def test_update_must_use_newest(self) -> None:
        """更新**必须**点「最新发布」：今天发的内容今天搜不到，明天再说，
        监测就断了。这一档也是「最新发布」唯一的入口——全量的十档里
        排序是固定的，碰不到它。
        """
        assert SearchSpec("甲", "update").sort_filter == selectors.FILTER_SORT_NEWEST

    def test_both_modes_default_to_no_type_filter(self) -> None:
        """不传类型 = 「不限类型」。对更新尤其要紧：
        它是唯一能看到**新问题**（只有 `/question/<id>`、还没有回答）的一档。
        """
        assert SearchSpec("甲").type_filter == selectors.FILTER_TYPE_UNLIMITED
        assert SearchSpec("甲", "update").type_filter == selectors.FILTER_TYPE_UNLIMITED


class TestOverrides:
    """显式覆盖：组合表按单一维度逐档采数就靠它。"""

    def test_sort_override_wins_over_mode(self) -> None:
        spec = SearchSpec("甲", "update", sort=selectors.FILTER_SORT_DEFAULT)
        assert spec.sort_filter == selectors.FILTER_SORT_DEFAULT

    def test_time_override_wins_over_mode(self) -> None:
        spec = SearchSpec("甲", "backfill", time=selectors.FILTER_TIME_DAY)
        assert spec.time_filter == selectors.FILTER_TIME_DAY

    def test_type_override_wins_over_the_default(self) -> None:
        spec = SearchSpec("甲", "backfill", type=selectors.FILTER_TYPE_ARTICLE)
        assert spec.type_filter == selectors.FILTER_TYPE_ARTICLE

    def test_overriding_one_leaves_the_others_alone(self) -> None:
        """⭐ 一档组合要能归因，就必须能**只动一个维度**。

        三个都覆盖的话，"结果不一样"到底是类型、排序还是时间造成的，
        就分不出来了。
        """
        spec = SearchSpec("甲", "update", type=selectors.FILTER_TYPE_ANSWER)
        assert spec.type_filter == selectors.FILTER_TYPE_ANSWER
        assert spec.sort_filter == selectors.FILTER_SORT_NEWEST  # 仍由 mode 推
        assert spec.time_filter == selectors.FILTER_TIME_DAY  # 仍由 mode 推

    def test_the_page_default_is_a_real_option(self) -> None:
        """「综合排序」必须在实测选项表里——它是"不点排序"时页面给的那一档。"""
        assert selectors.FILTER_SORT_DEFAULT in selectors.FILTER_SORT_OPTIONS
        assert selectors.FILTER_SORT_NEWEST in selectors.FILTER_SORT_OPTIONS


class TestValidation:
    """⭐ 写错一个字要在**开浏览器之前**就报出来。

    不校验的话，这个错要等到十分钟后 `_apply_filters` 才变成
    `FilterNotApplied`——那时候浏览器已经开了、页面已经翻了，
    而错误信息还得你自己从页面文案里对照。
    """

    def test_typo_in_sort_is_refused(self) -> None:
        with pytest.raises(ValueError, match="不是页面上的选项"):
            SearchSpec("甲", sort="最新")  # 实测是「最新发布」，不是「最新」

    def test_typo_in_time_is_refused(self) -> None:
        with pytest.raises(ValueError, match="不是页面上的选项"):
            SearchSpec("甲", time="一天")  # 实测带后缀，是「一天内」

    def test_typo_in_type_is_refused(self) -> None:
        with pytest.raises(ValueError, match="不是页面上的选项"):
            SearchSpec("甲", type="文章")  # 实测带前缀，是「只看文章」

    def test_the_three_month_option_has_no_measure_word(self) -> None:
        """⭐ **口述和页面原文在这里差一个字**：用户说的是「三个月内」，
        页面上是「**三月内**」。

        这条值得单独钉，因为它是"校验表为什么必须照快照抄"的活例子——
        照人口述写常量的话，这一档会在实跑到第 9 档时才炸，
        而那时候前面 8 档的时间已经花掉了。
        """
        assert selectors.FILTER_TIME_QUARTER == "三月内"
        assert "三个月内" not in selectors.FILTER_TIME_OPTIONS
        with pytest.raises(ValueError, match="不是页面上的选项"):
            SearchSpec("甲", time="三个月内")

    def test_the_error_lists_the_real_options(self) -> None:
        """报错要说清楚**能填什么**，不能只说"你填错了"。

        ⚠️ 这条不是吹毛求疵：早先三处筛选文案全是猜的，每一处都猜错了
        （见 selectors.py 里那些「⚠️ 不叫XX」的记录）。所以"错的时候
        能看到正确值"是这份代码里最实用的一条性质。
        """
        with pytest.raises(ValueError) as excinfo:
            SearchSpec("甲", sort="综合排序 ")  # 尾部多个空格
        message = str(excinfo.value)
        assert selectors.FILTER_SORT_DEFAULT in message
        assert selectors.FILTER_SORT_NEWEST in message

    def test_none_is_not_a_typo(self) -> None:
        """不传 ≠ 传错。不传要按 `mode` 推，而不是报错。"""
        assert SearchSpec("甲").sort is None
        assert SearchSpec("甲").sort_filter == selectors.FILTER_SORT_DEFAULT


class TestPaceFactor:
    """滚动循环的停顿系数。

    ⚠️ 这个值可以调，但**有一条不能跨的线**：它是乘在 `session.pace` 上的，
    而那个停顿是本系统唯一有反爬含义的东西（`session.py` 写明了）。
    调到 0 就等于把它拿掉了——而拿掉之后采集照样跑得完、日志照样干净，
    所以这条得靠测试拦，不能靠人记得。
    """

    def test_pace_is_not_switched_off(self) -> None:
        assert search_mod.PACE_FACTOR > 0

    def test_pace_is_much_shorter_than_the_random_pause_it_scales(self) -> None:
        """实跑实测每轮 15 秒，其中大头就是原来那个 0.5（=1.5~4.0 秒）。
        所以这个系数必须明显小于 1，否则等于没改。
        """
        assert search_mod.PACE_FACTOR < 0.5

    def test_search_accepts_the_speed_knobs(self) -> None:
        """两个旋钮要能**只调一个**：拨的距离和停的时间是两件事，
        混成一个参数就没法归因"是变快了还是跨过头了"。
        """
        import inspect

        params = inspect.signature(search_mod.search).parameters
        assert "flick_steps" in params
        assert "pace_factor" in params
        assert params["flick_steps"].default == drive.FLICK_STEPS
        assert params["pace_factor"].default == search_mod.PACE_FACTOR


def test_url_encodes_the_keyword() -> None:
    """关键词直接进 query，必须转义——不然带空格/&/中文的词会把 URL 撕开。"""
    assert "q=%E7%94%B2" in SearchSpec("甲").url
    assert SearchSpec("甲 乙").url.count("&") == 1  # 空格不能变成第二个参数


# ── 筛选点击的重试与回读配对 ─────────────────────────────────────────


class _FakePanel:
    """假的三组筛选面板。`read` 读状态，`click` 改状态。"""

    def __init__(
        self,
        groups: dict[int, str | None],
        *,
        click_works: bool = True,
        none_after_click: int = 0,
    ) -> None:
        self.groups = dict(groups)
        self.click_works = click_works
        #: 开头几次点击之后的第一次回读会得到 None。
        #: ⚠️ 是**总共**几次，不是每次点击都重来——否则模型就变成
        #: "永远读不到"，那测的是另一种失败了。
        self.none_clicks_left = none_after_click
        self.clicks: list[tuple[int, str]] = []
        self._none_pending = 0

    def read(self, session: object, index: int) -> str | None:
        if self._none_pending:
            # 复刻实跑现象：点完之后面板收起重渲染，此刻读出来是 None
            self._none_pending -= 1
            return None
        return self.groups.get(index)

    def click(self, session: object, index: int, text: str) -> str | None:
        self.clicks.append((index, text))
        if self.click_works:
            self.groups[index] = text
        if self.none_clicks_left > 0:
            self._none_pending = 1
            self.none_clicks_left -= 1
        return text


class _FakeSession:
    """够 `_apply_filters` 用的假会话——三个浏览器助手都被替换掉了，
    所以它只需要接住 `guard` / `settle`。"""

    def __init__(self) -> None:
        self.guards = 0
        self.settles = 0

    def guard(self) -> None:
        self.guards += 1

    def settle(self) -> None:
        self.settles += 1


def correct_panel(spec: SearchSpec) -> dict[int, str | None]:
    """`spec` 那三档都摆对时，面板该是什么状态。

    ⚠️ **由 spec 推，不写死。** 写死的话，将来再动一次口径
    （比如全量的默认排序又翻回去），这一片重试测试会**集体报错**——
    而它们测的明明是"点完有没有回读"，和口径毫无关系。
    口径变了却在这里炸，会让人以为自己改坏了重试逻辑。
    """
    return {
        selectors.FILTER_GROUP_TYPE: spec.type_filter,
        selectors.FILTER_GROUP_SORT: spec.sort_filter,
        selectors.FILTER_GROUP_TIME: spec.time_filter,
    }


#: 一个"上次跑更新剩下的"排序档。拿它当错误的初始值正好——
#: 它是个真实存在的选项，而且正是账号级残留会留下的那种状态。
_RESIDUAL_SORT = selectors.FILTER_SORT_NEWEST


@pytest.fixture
def panel(monkeypatch: pytest.MonkeyPatch):
    """装一个假面板，返回 `install(groups, **kw) -> _FakePanel`。"""

    def install(groups: dict[int, str | None], **kwargs: object) -> _FakePanel:
        fake = _FakePanel(groups, **kwargs)  # type: ignore[arg-type]
        monkeypatch.setattr(search_mod, "_read_group", fake.read)
        monkeypatch.setattr(search_mod, "_click_in_group", fake.click)
        monkeypatch.setattr(search_mod, "ensure_panel_open", lambda session: None)
        # 报错信息要问"这一组有哪些可选项"，那条路要真的读 DOM。
        # 它是纯粹给日志服务的（见 `_group_options` 的 docstring），
        # 这里给个固定值就够了，不必为它编一个能跑 JS 的假页面。
        monkeypatch.setattr(
            search_mod,
            "_group_options",
            lambda session, index: list(selectors.FILTER_SORT_OPTIONS),
        )
        return fake

    return install


class TestApplyFiltersRetry:
    """⭐ 点完必须**立刻回读**——点几次就读几次。

    早先的循环是在每一轮**开头**读，于是两次读取都排在两次点击之前，
    最后一次点击的结果永远不会被看到。后果不是漏报而是**错报**：
    筛选明明点成功了也照样抛异常（2026-09-26 实跑撞上，错误信息里
    写着「现在还是「最新发布」」，而「最新发布」正是要切过去的那一档）。
    """

    def test_a_click_that_works_is_not_reported_as_a_failure(self, panel) -> None:
        spec = SearchSpec("甲")  # 全量：要的是「综合排序」
        fake = panel({**correct_panel(spec), 1: _RESIDUAL_SORT})

        readback = search_mod._apply_filters(_FakeSession(), spec)

        assert fake.clicks == [(1, spec.sort_filter)]
        assert readback[1] == spec.sort_filter

    def test_already_correct_groups_are_never_clicked(self, panel) -> None:
        """三组都对时一下都不点——避免"点了同名的那个把它取消掉"这类反向操作。

        ⚠️ 全量口径下这条尤其常见：组1 要的就是页面默认的「综合排序」，
        所以**正常跑起来组1 本来就不会被点**。
        """
        spec = SearchSpec("甲")
        fake = panel(correct_panel(spec))

        search_mod._apply_filters(_FakeSession(), spec)

        assert fake.clicks == []

    def test_survives_a_read_that_comes_back_none_right_after_a_click(self, panel) -> None:
        """点完之后面板收起重渲染，第一次回读会得到 `None`（实跑就是这么崩的）。

        `None` 是"没读到"，**不是"没切成功"**——重试一次就能读到真值。
        """
        spec = SearchSpec("甲")
        fake = panel({**correct_panel(spec), 1: _RESIDUAL_SORT}, none_after_click=1)

        search_mod._apply_filters(_FakeSession(), spec)

        assert len(fake.clicks) == 2  # 第一次的空读不该让整件事崩掉
        assert fake.groups[1] == spec.sort_filter

    def test_a_click_that_never_takes_effect_gives_up_loudly(self, panel) -> None:
        """⭐ 点击真的不生效时**必须抛**，不能继续跑。

        筛选没生效不会报错，它只是返回一份口径错误的结果集：采集如实完成、
        条数正常、入库正常，只是内容是几个月前的。属于架构文档 4.1
        明确列为"不可接受"的静默失败。
        """
        spec = SearchSpec("甲")
        fake = panel({**correct_panel(spec), 1: _RESIDUAL_SORT}, click_works=False)

        with pytest.raises(search_mod.FilterNotApplied):
            search_mod._apply_filters(_FakeSession(), spec)

        assert len(fake.clicks) == search_mod._CLICK_ATTEMPTS

    def test_stops_at_the_first_attempt_that_works(self, panel) -> None:
        """第一次就点通就不要再点了——多点的每一下都是一次真实请求。"""
        spec = SearchSpec("甲")
        fake = panel({**correct_panel(spec), 1: _RESIDUAL_SORT})

        search_mod._apply_filters(_FakeSession(), spec)

        assert len(fake.clicks) == 1
        assert search_mod._CLICK_ATTEMPTS > 1  # 留了重试余量，只是这次没用上

    def test_every_group_is_checked_not_just_the_first(self, panel) -> None:
        """三组都要管。组0（类型）是账号级残留状态——上次剩下的「只看回答」
        会让文章和想法一条都进不来，而日志上一切正常。"""
        spec = SearchSpec("甲")
        fake = panel(
            {
                selectors.FILTER_GROUP_TYPE: selectors.FILTER_TYPE_ANSWER,
                selectors.FILTER_GROUP_SORT: _RESIDUAL_SORT,
                selectors.FILTER_GROUP_TIME: selectors.FILTER_TIME_DAY,
            }
        )

        readback = search_mod._apply_filters(_FakeSession(), spec)

        assert [index for index, _ in fake.clicks] == [0, 1, 2]
        assert readback == (spec.type_filter, spec.sort_filter, spec.time_filter)

    def test_a_type_locked_combo_is_actually_clicked(self, panel) -> None:
        """⭐ 全量十档里，**组0 从「不限类型」切到「只看文章」这一步真的会被点**。

        这条是组合表能不能工作的分水岭：如果 `_apply_filters` 还像早先那样
        把组0 写死成「不限类型」，「只看文章」那几档就会**采回和「不限类型」
        一样的东西**，而且一声不吭——十二档跑完，结果是一条新内容都没多。
        """
        spec = SearchSpec("甲", "backfill", type=selectors.FILTER_TYPE_ARTICLE)
        # 面板上组的初始状态是**上一档留下的**「不限类型」
        fake = panel({**correct_panel(spec), selectors.FILTER_GROUP_TYPE: selectors.FILTER_TYPE_UNLIMITED})

        readback = search_mod._apply_filters(_FakeSession(), spec)

        assert fake.clicks == [(selectors.FILTER_GROUP_TYPE, selectors.FILTER_TYPE_ARTICLE)]
        assert readback[selectors.FILTER_GROUP_TYPE] == selectors.FILTER_TYPE_ARTICLE


class TestCombos:
    """⭐ 全量的组合表——**整份全量能力就是这张表**。

    它算错的方式全都不出声：少一档就是少采一批内容，多一档就是白花一轮，
    而两种情况的日志都写着"采集完成"。
    """

    def test_backfill_is_types_times_windows(self) -> None:
        specs = search_mod.backfill_specs("甲")

        assert len(specs) == len(search_mod.BACKFILL_TYPES) * len(search_mod.TIME_LADDER)
        assert len(specs) == 12

    def test_every_combo_is_distinct(self) -> None:
        """⭐ 十二档必须**两两不同**。有重复的话，那一轮就是白跑的——
        它拿回的东西上一轮已经拿过了，而日志上看不出任何异常。
        """
        specs = search_mod.backfill_specs("甲")
        keys = [(s.type_filter, s.time_filter) for s in specs]

        assert len(set(keys)) == len(keys)

    def test_backfill_sort_is_pinned_across_every_combo(self) -> None:
        """⭐ 排序在十二档里**固定不动**。用户 2026-09-26 的设计：
        综合排序本身是一份信号（推荐算法背后是大众关注度），
        全量的十几种组合只在类型和时间两个维度上铺开。
        """
        assert all(s.sort_filter == selectors.FILTER_SORT_DEFAULT for s in search_mod.backfill_specs("甲"))

    def test_both_types_and_all_windows_are_covered(self) -> None:
        specs = search_mod.backfill_specs("甲")

        assert {s.type_filter for s in specs} == set(search_mod.BACKFILL_TYPES)
        assert {s.time_filter for s in specs} == set(search_mod.TIME_LADDER)

    def test_video_is_not_collected(self) -> None:
        """⭐ 「只看视频」**不采**——`shared.models` 的 content_type 枚举里
        没有 video，采回来存不进库。

        ⚠️ 这一条不是洁癖：视频档**在页面上是真实存在的**，
        所以"要不要采它"看起来像个人选择。它其实不是选择，
        是下游存不下——把这条理由钉在这里，省得将来有人"顺手补上"。
        """
        assert selectors.FILTER_TYPE_VIDEO in selectors.FILTER_TYPE_OPTIONS  # 选项确实存在
        assert selectors.FILTER_TYPE_VIDEO not in search_mod.BACKFILL_TYPES  # 但我们不采

    def test_the_ladder_goes_wide_to_narrow_and_ends_at_a_week(self) -> None:
        """梯子从宽到窄，最后一档是「一周内」。

        ⚠️ 「一天内」**不在梯子里**：那是更新路径的档，全量跑它等于
        只采最近一天，而全量要的恰恰是老内容。
        """
        ladder = search_mod.TIME_LADDER

        assert ladder[0] == selectors.FILTER_TIME_UNLIMITED
        assert ladder[-1] == selectors.FILTER_TIME_WEEK
        assert selectors.FILTER_TIME_DAY not in ladder

    def test_the_ladder_fills_the_gap_between_a_week_and_three_months(self) -> None:
        """⭐ 用户口述的梯子在「一周内」和「三月内」之间空了一大段，
        这里补了「一月内」。

        补它的理由不是"多一档多一份覆盖"这么笼统——那一段正是
        **已经有点热、但还没老**的内容，也恰恰是综合排序最容易漏的。
        """
        assert selectors.FILTER_TIME_MONTH in search_mod.TIME_LADDER

    def test_every_combo_uses_measured_option_text(self) -> None:
        """十二档填的都得是**实测的页面原文**。

        ⚠️ `SearchSpec.__post_init__` 已经会拦，所以这条测试看着多余——
        但它拦的前提是"这张表真的构造出来了"。表要是写错成两处都不一致的值，
        报错会发生在**运行到那一档的时候**，也就是前面几档的时间已经花掉之后。
        这里在构造之前先把整张表验一遍。
        """
        for spec in search_mod.backfill_specs("甲"):
            assert spec.type_filter in selectors.FILTER_TYPE_OPTIONS, spec
            assert spec.time_filter in selectors.FILTER_TIME_OPTIONS, spec
            assert spec.sort_filter in selectors.FILTER_SORT_OPTIONS, spec

    def test_update_is_exactly_one_combo(self) -> None:
        """更新**只有一档**：「最新发布」+「一天内」+「不限类型」。

        ⚠️ 用户明确说了更新只跑一种组合、只滚一次。多跑几档不会报错，
        只会让每天的更新慢十几倍。
        """
        spec = search_mod.update_spec("甲")

        assert spec.sort_filter == selectors.FILTER_SORT_NEWEST
        assert spec.time_filter == selectors.FILTER_TIME_DAY
        assert spec.type_filter == selectors.FILTER_TYPE_UNLIMITED

    def test_update_sees_bare_questions(self) -> None:
        """⭐ 更新那档必须是「不限类型」，因为它**是唯一能看到新问题的档**。

        只有 `/question/<id>`、还没有任何回答的新问题，只在「不限类型」下
        才会出现。换成「只看回答」的话这批内容会整批消失，
        而日志上一切正常——新问题被漏掉是监测系统最不该犯的错。
        """
        assert search_mod.update_spec("甲").type_filter == selectors.FILTER_TYPE_UNLIMITED


class TestParseBatchCarriesTheFreeMetadata:
    """⭐ 搜索页白送的元数据必须在**驱动层这一跳**被带上。

    解析层取到了、`UrlEntry` 也有字段，但中间 `_parse_batch` 不传的话，
    它们就静默停在半路：`urls.jsonl` 里全是空，而日志一切正常、
    每档都写着"采集完成"。这种"两头都改好了、中间漏了"的漏法
    在这类管道里最常见，所以单独钉一条。
    """

    @staticmethod
    def _card(
        *, excerpt: str = "不点阅读全文就能看到的那段", voteup: int = 117, comments: int = 19
    ) -> str:
        """一张最小可解析的搜索卡片。结构照抄实测的 `search_filtered.html`。

        ⚠️ 赞同数**只在 `aria-label` 里**（按钮本身没有文本，只有一个 SVG），
        评论数是按钮文本。两者都放进去，才测得出有没有取混。
        """
        return f"""
        <div class="ContentItem">
          <h2 class="ContentItem-title"><a href="/question/1/answer/2">标题</a></h2>
          <span class="RichText ztext">{excerpt}</span>
          <div class="ContentItem-actions">
            <button aria-label="赞同 {voteup} "></button>
            <button class="ContentItem-action">{comments} 条评论</button>
          </div>
        </div>
        """

    def test_metadata_reaches_the_url_entry(self) -> None:
        spec = SearchSpec("考研")
        report = search_mod.SearchReport(keyword="考研")

        (entry,) = search_mod._parse_batch([self._card()], spec, report)

        assert entry.title == "标题"
        assert entry.excerpt == "不点阅读全文就能看到的那段"
        assert entry.voteup_count == 117
        assert entry.comment_count == 19

    def test_the_two_counts_do_not_get_mixed_up(self) -> None:
        """⭐ 赞同 117、评论 19 —— 两个数必须各归各位。

        操作栏那段文本里两个数字挨在一起（`赞同 117  19 条评论`），
        取第一个数的写法会让 `comment_count` 变成 117，和 `voteup_count` 相等。
        断言"两个数不相等"是最直接能抓住它的判据。
        """
        spec = SearchSpec("考研")
        report = search_mod.SearchReport(keyword="考研")

        (entry,) = search_mod._parse_batch([self._card(voteup=117, comments=19)], spec, report)

        assert (entry.voteup_count, entry.comment_count) == (117, 19)

    def test_a_stale_line_without_the_metadata_still_works(self) -> None:
        """页面改版/取不到时元数据为空，但 URL 必须照常采到。

        ⚠️ 分诊信息是**锦上添花**，不能因为它缺失就把整条内容丢掉。
        所以这里断言的是"照常产出条目"，不是"字段齐全"。
        """
        spec = SearchSpec("考研")
        report = search_mod.SearchReport(keyword="考研")
        # 只有标题和链接，没有缩略信息、没有赞同/评论按钮
        bare = (
            '<div class="ContentItem">'
            '<h2 class="ContentItem-title"><a href="/question/1/answer/2">标题</a></h2>'
            "</div>"
        )

        entries = search_mod._parse_batch([bare], spec, report)

        assert [e.url for e in entries] == ["https://www.zhihu.com/question/1/answer/2"]
        assert entries[0].excerpt is None


class _StubSearch:
    """替掉 `search.search`：按档位返回预先编排好的 URL，不碰浏览器。"""

    def __init__(self, plan: dict[tuple[str, str], list[str]]) -> None:
        self.plan = plan
        self.calls: list[tuple[str, str]] = []

    def __call__(self, session: object, spec: SearchSpec, **kwargs: object) -> object:
        self.calls.append((spec.type_filter, spec.time_filter))
        urls = [ops.UrlEntry(url=u, keyword=spec.keyword) for u in self.plan.get((spec.type_filter, spec.time_filter), [])]
        # `search()` 的契约是：每轮把新 URL 交给 on_batch，同时自己也攒一份
        if kwargs.get("on_batch"):
            kwargs["on_batch"](urls)  # type: ignore[operator]
        return search_mod.SearchReport(keyword=spec.keyword, urls=list(urls))


@pytest.fixture
def fake_session() -> object:
    """够 `backfill()` 用的假会话——它只调 `pace()`。"""

    class _S:
        def __init__(self) -> None:
            self.paces = 0

        def pace(self, *_a: object, **_kw: object) -> None:
            self.paces += 1

    return _S()


class TestBackfillDedup:
    """⭐ 跨组合去重——`backfill()` 真正新增的那点逻辑。

    为什么值得测：去重错了不会报错。少了就是同一批内容在
    `urls.jsonl` 里出现十二次（下游按 URL 查重时会当成一条，不出错，
    但阶段一的产出直接失去意义）；多了就是把别档的内容当成重复丢掉，
    **静默漏采**。
    """

    def test_the_same_url_in_two_combos_is_written_once(self, monkeypatch, fake_session) -> None:
        stub = _StubSearch(
            {
                (selectors.FILTER_TYPE_ARTICLE, selectors.FILTER_TIME_UNLIMITED): ["a", "b"],
                (selectors.FILTER_TYPE_ARTICLE, selectors.FILTER_TIME_YEAR): ["b", "c"],
            }
        )
        monkeypatch.setattr(search_mod, "search", stub)
        written: list[str] = []

        report = search_mod.backfill(
            fake_session,
            "甲",
            on_batch=lambda batch: written.extend(e.url for e in batch),
            combos=[
                SearchSpec("甲", type=selectors.FILTER_TYPE_ARTICLE, time=selectors.FILTER_TIME_UNLIMITED),
                SearchSpec("甲", type=selectors.FILTER_TYPE_ARTICLE, time=selectors.FILTER_TIME_YEAR),
            ],
        )

        assert [e.url for e in report.urls] == ["a", "b", "c"]
        assert written == ["a", "b", "c"]  # ⭐ 落盘的也只有新增的那些
        assert report.duplicates == 1
        assert report.raw_total == 4  # 各档合计不去重

    def test_on_batch_is_never_called_with_an_empty_batch(self, monkeypatch, fake_session) -> None:
        """一整批都被去重掉时**不要回调**。

        `ops.append_urls` 是逐行落盘的，但"被调了一次"本身在下游有意义
        （计数、进度），空批次会把这些数搅浑。
        """
        stub = _StubSearch(
            {
                (selectors.FILTER_TYPE_ARTICLE, selectors.FILTER_TIME_UNLIMITED): ["a"],
                (selectors.FILTER_TYPE_ARTICLE, selectors.FILTER_TIME_YEAR): ["a"],
            }
        )
        monkeypatch.setattr(search_mod, "search", stub)
        batches: list[list[str]] = []

        search_mod.backfill(
            fake_session,
            "甲",
            on_batch=lambda batch: batches.append([e.url for e in batch]),
            combos=list(search_mod.backfill_specs("甲"))[:2],
        )

        assert batches == [["a"]]

    def test_every_combo_is_actually_run(self, monkeypatch, fake_session) -> None:
        stub = _StubSearch({})
        monkeypatch.setattr(search_mod, "search", stub)

        report = search_mod.backfill(fake_session, "甲")

        assert len(stub.calls) == 12
        assert len(report.combos) == 12

    def test_an_empty_combo_list_returns_empty_without_touching_the_browser(
        self, monkeypatch, fake_session
    ) -> None:
        """空清单不报错，但要**一档都不跑**，并且留一条 warning。

        静默返回一个空报告的话，调用方会以为"这个关键词没内容"。
        """
        stub = _StubSearch({})
        monkeypatch.setattr(search_mod, "search", stub)

        report = search_mod.backfill(fake_session, "甲", combos=[])

        assert stub.calls == []
        assert report.found == 0
        assert report.raw_total == 0

    def test_overlap_ratio_flags_filters_that_stopped_mattering(self, monkeypatch, fake_session) -> None:
        """⭐ 十二档全都返回同一批东西 → 重合率接近 1。

        这是**全量路径特有的探针**。`_apply_filters` 验的是"面板上高亮的是哪个
        标签"，万一知乎做成"标签变了、结果没变"，那个回读是过得去的；
        只有这里能看出来。
        """
        same = ["a", "b", "c", "d"]
        stub = _StubSearch({(t, w): same for t in search_mod.BACKFILL_TYPES for w in search_mod.TIME_LADDER})
        monkeypatch.setattr(search_mod, "search", stub)

        report = search_mod.backfill(fake_session, "甲")

        assert report.overlap_ratio > search_mod.OVERLAP_ALARM
        assert report.found == 4  # 去重后还是 4 条

    def test_a_normal_run_does_not_trip_the_alarm(self, monkeypatch, fake_session) -> None:
        """正常口径（各档近乎不相交）重合率该远低于阈值——否则这个报警
        天天响，响了就没人看了。
        """
        stub = _StubSearch(
            {
                (t, w): [f"{t}-{w}-{i}" for i in range(10)]
                for t in search_mod.BACKFILL_TYPES
                for w in search_mod.TIME_LADDER
            }
        )
        monkeypatch.setattr(search_mod, "search", stub)

        report = search_mod.backfill(fake_session, "甲")

        assert report.overlap_ratio == 0.0
        assert report.found == 120

    def test_overlap_ratio_of_nothing_is_zero_not_a_crash(self) -> None:
        """一条都没采到时是 0.0，不是 ZeroDivisionError。"""
        assert search_mod.BackfillReport(keyword="甲").overlap_ratio == 0.0


class TestPanelWait:
    def test_panel_wait_gives_up_instead_of_hanging(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """面板一直不出现时要**返回 False**，不能永远转下去。

        ⚠️ 这条钉的是"别把超时做成死循环"：`_wait_for_panel` 在
        `search()` 里是同步调用的，转不出来就是整个采集卡死。
        """
        monkeypatch.setattr(search_mod, "_groups", lambda session: [])
        monkeypatch.setattr(search_mod.time, "sleep", lambda _: None)

        assert search_mod._wait_for_panel(_FakeSession(), timeout=0.0) is False

    def test_panel_wait_returns_true_as_soon_as_it_appears(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(search_mod, "_groups", lambda session: ["组0", "组1", "组2"])

        assert search_mod._wait_for_panel(_FakeSession()) is True
