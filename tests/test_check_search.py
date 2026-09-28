"""`scripts/check_search.py` 里**纯逻辑部分**的测试。

⚠️ 这个脚本本身要开浏览器，不能在这里调用。但它的"两档差集"是纯计算，
   而那一节正是整份报告的结论所在——算错了会直接误导口径决定，
   所以值得钉住。

   脚本的其余部分（开浏览器、落盘、滚动）由实跑验收，不在这里。
"""

from __future__ import annotations

import pytest

from scripts.check_search import (
    PLANS,
    CaseResult,
    _cases,
    build_parser,
    diff_section,
    overlap_section,
    run_all,
    select_cases,
)
from sentinel_q.collector import search as search_mod
from sentinel_q.collector import selectors
from sentinel_q.collector.scrolling import StopReason


def _result(index: int, urls: list[str], *, stop: StopReason = StopReason.END_MARKER) -> CaseResult:
    return CaseResult(case=_cases()[index - 1], urls=urls, stop_reason=stop)


class TestStalledRounds:
    """空转轮数——**判断"滚太快了"的指标**。

    ⚠️ 为什么它比"总共花了多久"更值得钉：把滚轮拨得更快，总耗时几乎必然下降，
    所以耗时看不出有没有副作用。真正会暴露问题的是空转——
    每一轮跨得太远、冲过了懒加载的触发点，那一轮就白白空转。
    这个数是由 `total_rounds` 和 `rounds` 两个计数器**推**出来的，
    推错方向就会把"跨过头了"读成"很顺"，所以值得单独测。
    """

    def test_counts_the_collect_calls_that_yielded_nothing(self) -> None:
        """`scroll_until_exhausted` 进循环前先收一次首屏，
        所以 collect 总次数 = 轮数 + 1；有产出 3 次、跑 7 轮 → 空转 5 次。
        """
        result = _result(1, ["a"])
        result.rounds = 3  # 有产出的轮
        result.total_rounds = 7

        assert result.stalled_rounds == 5

    def test_a_clean_run_has_no_stalls(self) -> None:
        """每轮都有产出：空转是 0，不是负数。"""
        result = _result(1, ["a"])
        result.rounds = 4
        result.total_rounds = 3  # 首屏那一轮 + 3 轮产出

        assert result.stalled_rounds == 0

    def test_never_goes_negative(self) -> None:
        """数据对不上时给 0，不要给出一个 -1 让报告读起来像 bug。"""
        result = _result(1, ["a"])
        result.rounds = 99
        result.total_rounds = 1

        assert result.stalled_rounds == 0

    def test_shows_up_in_the_report_lines(self) -> None:
        result = _result(1, ["a"])
        result.rounds = 3
        result.total_rounds = 7
        result.elapsed = 42.0

        text = "\n".join(result.lines())

        assert "空转 5 轮" in text
        assert "42 秒" in text


class TestMetadataLine:
    """⭐ 新增字段有没有**真的接上**，报告里必须看得见。

    这是能力一最阴的一种失效：条数对、筛选回读对、每档都写着"采集完成"，
    只有元数据整列是空的。不专门报一行，就只能靠翻 `urls.jsonl` 才发现——
    而那时候人已经在怀疑别的地方了。
    """

    @staticmethod
    def _with(**kw: object) -> CaseResult:
        result = _result(1, ["a", "b"])
        for key, value in kw.items():
            setattr(result, key, value)
        return result

    def test_coverage_shows_up_in_the_report(self) -> None:
        text = "\n".join(
            self._with(with_excerpt=2, with_voteup=1, with_comment=1, top_voteup=1770).lines()
        )

        assert "缩略信息：2/2 条有" in text
        assert "赞同非 0 1 条（最高 1770）" in text
        assert "评论非 0 1 条" in text

    def test_an_all_empty_column_is_called_out_loudly(self) -> None:
        """整列为空必须带 ⚠️ —— 这正是"选择器失效"或"字段没接上"的样子。"""
        assert "⚠️ 一条缩略信息都没有" in "\n".join(self._with().lines())

    def test_a_case_that_collected_nothing_does_not_cry_wolf(self) -> None:
        """一条 URL 都没采到（关键词没内容）是另一回事，不该报"字段没接上"。"""
        assert "一条缩略信息都没有" not in "\n".join(_result(1, []).lines())


class TestCases:
    def test_case_one_does_not_sort(self) -> None:
        """第 1 档 = 页面默认口径：**排序栏一下都不点**。

        所以它的 sort 必须是「综合排序」。填成「最新发布」的话，
        这档就不再是"默认口径"了，两档差集也就失去意义。
        """
        assert _cases()[0].kwargs["sort"] == selectors.FILTER_SORT_DEFAULT
        assert _cases()[0].kwargs["time"] == selectors.FILTER_TIME_UNLIMITED

    def test_case_two_is_the_update_recipe(self) -> None:
        """第 2 档 = 更新采集要的口径。"""
        assert _cases()[1].kwargs["sort"] == selectors.FILTER_SORT_NEWEST
        assert _cases()[1].kwargs["time"] == selectors.FILTER_TIME_DAY

    def test_every_case_uses_measured_option_text(self) -> None:
        """每档填的都必须是**实测的页面原文**——写错了 `SearchSpec` 会拦，
        但那时候已经开完浏览器了。这里在开之前就拦住。
        """
        for plan in PLANS.values():
            for case in plan():
                assert case.kwargs.get("sort") in selectors.FILTER_SORT_OPTIONS, case
                assert case.kwargs.get("time") in selectors.FILTER_TIME_OPTIONS, case
                if "type" in case.kwargs:
                    assert case.kwargs["type"] in selectors.FILTER_TYPE_OPTIONS, case


class TestPlans:
    """`--plan` 的四个计划。⚠️ 它们**直接复用物业代码的组合表**，
    所以这里钉的是"计划有没有把那张表铺全"，不是"组合表对不对"——
    后者在 `test_search.py::TestCombos`。
    """

    def test_backfill_plan_runs_the_whole_table(self) -> None:
        cases = PLANS["backfill"]()

        assert len(cases) == len(search_mod.backfill_specs("甲")) == 12
        assert [c.label for c in cases] == [
            f"{s.type_filter} × {s.time_filter}" for s in search_mod.backfill_specs("甲")
        ]

    def test_update_plan_is_one_combo(self) -> None:
        assert len(PLANS["update"]()) == 1

    def test_all_plan_is_backfill_plus_update(self) -> None:
        assert len(PLANS["all"]()) == 13

    def test_case_indices_are_contiguous_from_one(self) -> None:
        """档号是**报告里唯一能指代某一档的东西**（`urls.jsonl` 里没有档位字段），
        所以它必须连续、从一开始，不能在收窄之后留下空号。
        """
        for name, plan in PLANS.items():
            indices = [c.index for c in plan()]
            assert indices == list(range(1, len(indices) + 1)), name

    def test_backfill_cases_carry_the_type_dimension(self) -> None:
        """⭐ 脚本必须把**类型**那一维也带上。

        漏了它的话，十二档会退化成"同一档跑十二遍"——因为 `--type` 的收窄、
        以及 `SearchSpec` 的类型覆盖都无从谈起。而那种退化**不会报错**：
        每档都采回一百多条，只是全都是同一批。
        """
        kinds = {c.kind for c in PLANS["backfill"]()}

        assert kinds == set(search_mod.BACKFILL_TYPES)

    def test_the_two_case_plan_has_no_type_dimension(self) -> None:
        """`cases` 那两档比的是**排序+时间**，不带类型。

        这个性质有意义是因为 `--type` 拿它收窄会收出一个空清单——
        `select_cases` 必须为此报错而不是空跑。
        """
        assert all(c.kind is None for c in PLANS["cases"]())


class TestSelectCases:
    """`--plan` / `--type` / `--time` 的解析。

    ⚠️ 这一层值得测的理由和别处不同：它**在开浏览器之前**跑，
    所以这里是拦住"白跑一趟"的最后一道——收窄收空了还照跑的话，
    日志会写着"全部完成"，而一条 URL 都没采。
    """

    def _parse(self, argv: list[str]):
        parser = build_parser()
        return parser, parser.parse_args(["--keyword", "甲", *argv])

    def test_default_plan_is_the_two_case_comparison(self) -> None:
        _, args = self._parse([])

        assert [c.index for c in select_cases(args, build_parser())] == [1, 2]

    def test_type_narrows_the_backfill_plan_to_one_ladder(self) -> None:
        """`--type 只看文章` 该剩六档（一个类型 × 六段时间）。"""
        _, args = self._parse(["--plan", "backfill", "--type", selectors.FILTER_TYPE_ARTICLE])

        cases = select_cases(args, build_parser())

        assert len(cases) == len(search_mod.TIME_LADDER)
        assert {c.kind for c in cases} == {selectors.FILTER_TYPE_ARTICLE}

    def test_narrowing_to_nothing_is_a_loud_error(self) -> None:
        """⭐ 收窄收空必须**报错退出**，不能返回空列表。

        空跑一趟的代价不只是浪费一次开浏览器：日志上会写着"全部完成"，
        实际一条没采。这正是架构文档 4.1 点名的那类静默失败。
        """
        parser, args = self._parse(["--plan", "cases", "--type", selectors.FILTER_TYPE_ARTICLE])

        with pytest.raises(SystemExit):
            select_cases(args, parser)

    def test_the_old_case_flag_still_works(self) -> None:
        """`--case 2` 是上个版本用惯的写法，保留兼容。"""
        _, args = self._parse(["--case", "2"])

        assert [c.index for c in select_cases(args, build_parser())] == [2]

    def test_the_old_case_flag_refuses_to_be_silently_ignored(self) -> None:
        """⭐ 旧参数配新 plan 要**报错**，不能当没看见。

        静默忽略的话，`--plan backfill --case 2` 会跑满十二档——
        用户以为只跑一档，坐在那儿等十几分钟。
        """
        parser, args = self._parse(["--plan", "backfill", "--case", "2"])

        with pytest.raises(SystemExit):
            select_cases(args, parser)


class TestRunAll:
    """⭐ **崩在半路不能把前面的诊断数据一起带走。**

    一轮全量十几分钟、十几档。第 10 档崩掉时，前 9 档的逐档条数和筛选回读
    是排查唯一的依据——`urls.jsonl` 里只有原始 URL，不含任何口径信息。
    所以 `run_all` 把异常**接住并返回**，而不是往外抛。
    """

    class _Session:
        def __init__(self) -> None:
            self.paces = 0

        def pace(self, *_a: object, **_kw: object) -> None:
            self.paces += 1

    def _args(self, keyword: str = "甲"):
        return build_parser().parse_args(["--keyword", keyword])

    def test_a_crash_on_the_last_case_keeps_the_earlier_ones(self, monkeypatch) -> None:
        cases = PLANS["backfill"]()[:4]
        calls: list[int] = []

        def fake_run_case(session, case, keyword, store, max_rounds, flick, pace):
            if case.index == 3:
                raise RuntimeError("第 3 档炸了")
            calls.append(case.index)
            return CaseResult(case=case, urls=[f"u{case.index}"])

        monkeypatch.setattr("scripts.check_search.run_case", fake_run_case)

        results, failure = run_all(self._Session(), cases, self._args(), store=None)

        assert calls == [1, 2]  # 第 3 档没跑成，第 4 档没轮到
        assert [r.case.index for r in results] == [1, 2]
        assert isinstance(failure, RuntimeError)

    def test_the_exception_is_returned_not_swallowed(self, monkeypatch) -> None:
        """接住 ≠ 吞掉。返回的异常必须带着，调用方要据此报"这是残缺的一轮"。"""
        monkeypatch.setattr(
            "scripts.check_search.run_case",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("炸了")),
        )

        results, failure = run_all(self._Session(), PLANS["update"](), self._args(), store=None)

        assert results == []
        assert failure is not None and "炸了" in str(failure)

    def test_a_clean_run_returns_no_failure(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "scripts.check_search.run_case",
            lambda session, case, keyword, store, mr, fs, pf: CaseResult(case=case, urls=["a"]),
        )

        results, failure = run_all(self._Session(), PLANS["update"](), self._args(), store=None)

        assert failure is None
        assert len(results) == 1

    def test_keyboard_interrupt_is_caught_too(self, monkeypatch) -> None:
        """⭐ Ctrl-C 是最可能真实发生的那种中断（十几分钟的活没人会一直盯着）。

        只接 `Exception` 的话，Ctrl-C 会**绕过**这里直接往上飞，
        前 9 档的结果照样丢——而那恰恰是最常见的场景。
        """
        monkeypatch.setattr(
            "scripts.check_search.run_case",
            lambda *a, **kw: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

        _results, failure = run_all(self._Session(), PLANS["update"](), self._args(), store=None)

        assert isinstance(failure, KeyboardInterrupt)


class TestDiffSection:
    def test_new_content_only_in_the_newest_run_is_called_out(self) -> None:
        """⭐ 这一节量化的是**已知的覆盖缺口**：只有「最新发布」捞到的内容。

        全量的十二档排序固定在「综合排序」，碰不到这些——它们靠每天一次的
        更新路径兜。所以这个数**不为 0 才是正常的**，报告该说的是
        "缺口有多宽"，而不是"该不该改口径"。
        """
        results = [
            _result(1, ["a", "b"]),
            _result(2, ["a", "b", "新1", "新2"]),
        ]

        text = "\n".join(diff_section(results))

        assert "只在第 2 档：2 条" in text
        assert "新1" in text
        assert "已知覆盖缺口的量化" in text
        assert "更新那一路不能停" in text

    def test_zero_difference_says_the_gap_is_narrow(self) -> None:
        """差集为 0 时给的是相反的结论，不能说成一样的话。"""
        results = [_result(1, ["a", "b"]), _result(2, ["a", "b"])]

        text = "\n".join(diff_section(results))

        assert "只在第 2 档：0 条" in text
        assert "已知覆盖缺口的量化" not in text
        assert "只跑一次不算数" in text  # 一次实跑不足以定案

    def test_one_case_is_not_a_comparison(self) -> None:
        """只跑一档时不能硬凑出一节对比——那会给出"差集为 0"的假结论。"""
        assert diff_section([_result(1, ["a"])]) == []
        assert diff_section([]) == []

    def test_counts_are_deduplicated(self) -> None:
        """页内重复的那几条已经写进 urls.jsonl 了，但这里比的是**集合**：
        同一档里同一个 URL 出现两次，不该被算成"两条内容"。"""
        results = [_result(1, ["a", "a", "b"]), _result(2, ["b"])]

        text = "\n".join(diff_section(results))

        assert "第 1 档（" in text
        assert "：2 条" in text  # a、b —— 不是 3


class TestOverlapSection:
    """⭐ **全量路径唯一能发现"筛选没生效"的地方。**

    `_apply_filters` 的回读验的是"面板上高亮的是哪个标签"。万一知乎做成
    标签变了、结果没变，回读是过得去的——十二档会白跑一遍，而每档的日志
    都写着"采集完成"。只有重合率能看出来。
    """

    def _run(self, url_sets: list[list[str]], stops: list[StopReason] | None = None):
        stops = stops or [StopReason.END_MARKER] * len(url_sets)
        return [
            _result(1 if i % 2 == 0 else 2, urls, stop=stop)
            for i, (urls, stop) in enumerate(zip(url_sets, stops, strict=True))
        ]

    def test_disjoint_combos_are_reported_as_healthy(self) -> None:
        """各档近乎不相交才是正常的（实测约 1%）。"""
        text = "\n".join(overlap_section(self._run([["a", "b"], ["c", "d"]])))

        assert "并集去重：4 条" in text
        assert "重合率正常" in text

    def test_identical_combos_are_called_out_as_a_filter_failure(self) -> None:
        """⭐ 十二档返回同一批 → 这不是"内容都热门"，是筛选没生效。"""
        text = "\n".join(overlap_section(self._run([["a", "b"], ["a", "b"], ["a", "b"]])))

        assert "2 条" in text  # 并集
        assert "筛选没生效" in text
        assert "重合率正常" not in text

    def test_counts_the_union_not_the_sum(self) -> None:
        """产出报的是**并集**。报成各档条数之和的话，数字会随档数虚涨——
        十二档跑完看着像采了一千八百条，实际去重后可能只有八百。
        """
        text = "\n".join(overlap_section(self._run([["a", "b"], ["a", "b"]])))

        assert "各档合计：4 条" in text
        assert "并集去重：2 条" in text

    def test_each_combo_shows_what_it_actually_added(self) -> None:
        """⭐ 逐档要显示**它自己带来了多少新东西**，不只是它采到多少。

        某档采回 150 条、其中 148 条别档已有，它其实只贡献了 2 条——
        而"采到 150 条"这个数完全看不出这件事。判断"梯子够不够长"
        靠的就是这一列。
        """
        text = "\n".join(overlap_section(self._run([["a", "b"], ["a", "b", "c"]])))

        assert "第  1 档" in text
        assert "采到 2 条，新增 2 条（累计 2）" in text
        assert "采到 3 条，新增 1 条（累计 3）" in text

    def test_a_truncated_combo_is_visibly_marked(self) -> None:
        """某一档没采完（轮次上限截断）时要在这一节里看得见。

        ⚠️ 这一节是**横向比较各档**的地方，某一档条数明显低一截，
        到底是"那个时间段本来就没内容"还是"那一档被截断了"，
        不看这个标记分不出来。
        """
        results = self._run(
            [["a", "b"], ["c"]],
            stops=[StopReason.END_MARKER, StopReason.MAX_ROUNDS],
        )

        lines = overlap_section(results)

        assert any(line.startswith("  ✅ 第  1 档") for line in lines)
        assert any(line.startswith("  ⚠️ 第  2 档") for line in lines)

    def test_one_combo_is_not_a_comparison(self) -> None:
        assert overlap_section([_result(1, ["a"])]) == []
        assert overlap_section([]) == []

    def test_no_urls_at_all_does_not_divide_by_zero(self) -> None:
        """一个关键词十二档全是 0 条（关键词完全没内容）时不能崩。

        崩在这里会让整份报告丢掉——而那时候最需要的恰恰是报告：
        到底是"这个词没人提"还是"选择器挂了"，得看每档的筛选回读。
        """
        text = "\n".join(overlap_section(self._run([[], []])))

        assert "重合率 0.0%" in text
        assert "并集去重：0 条" in text


class TestVerdict:
    def test_only_the_end_marker_counts_as_complete(self) -> None:
        """⭐ 只有看到「没有更多了」才算确定采完。

        `STABLE`（连续多轮无新增）**不算**：它可能是真到底，也可能是页面卡住
        或选择器失效，两者在日志上一模一样。把它当成功，就等于把
        静默漏采报成"采集完成"。
        """
        assert _result(1, ["a"], stop=StopReason.END_MARKER).complete is True
        for reason in (StopReason.STABLE, StopReason.GAVE_UP, StopReason.MAX_ROUNDS):
            assert _result(1, ["a"], stop=reason).complete is False, reason

    def test_no_scroll_outcome_is_not_complete(self) -> None:
        """滚动结果缺失（理论上不该发生）时不能默认成功。"""
        assert CaseResult(case=_cases()[0], urls=["a"], stop_reason=None).complete is False
