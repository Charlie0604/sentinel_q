"""能力一实跑验收：搜一个关键词，把每个 URL 立刻落盘，报完整性。

⚠️ **这个脚本会打开真实浏览器、用真实账号访问知乎。**
   它**不是**单元测试——单元测试在 `src/sentinel_q/collector/tests/` 下，全部离线跑。
   这个脚本是给人手动跑的，一个能力一个，用来回答"它到底能不能work"。

用法：

    # 最常用：只跑全量的前两档，验一下筛选和滚动没坏（约 1 分钟）
    .venv/bin/python scripts/check_search.py --keyword 考研 --plan cases

    # ⭐ 全量十二档全跑，验组合表（约 10~15 分钟）
    .venv/bin/python scripts/check_search.py --keyword 考研 --plan backfill

    # 只想验一个类型维度的六档时间梯子（约 5 分钟）
    .venv/bin/python scripts/check_search.py --keyword 考研 --plan backfill --type 只看文章

    # 更新的单档
    .venv/bin/python scripts/check_search.py --keyword 考研 --plan update

    # 先拿小轮次上限试水，确认页面没改版再放开
    .venv/bin/python scripts/check_search.py --keyword 考研 --plan cases --max-rounds 20

调滚动速度（2026-09-26：原来每轮 400px + 停 3 秒，实跑 15 秒/轮）
    .venv/bin/python scripts/check_search.py --keyword 考研 --flick-steps 3
    .venv/bin/python scripts/check_search.py --keyword 考研 --pace 0.25

    ⚠️ 看"空转轮数"，不是看总耗时。跨得更远如果冲过了懒加载的触发点，
    总耗时反而会涨——那时候该把 `--flick-steps` 调小，不是把车倒回去。

`--plan` 的三个值
-----------------
    cases     前两档口径对比，**最小可跑的验收**（原来叫 `--case both`）
    backfill  全量的十二档：2 类型 × 6 时间段，排序固定「综合排序」
    update    更新的单档：「最新发布」+「一天内」+「不限类型」

    `--type` / `--time` 可以把任何一个 plan 收窄，用来只跑关心的那几档。

跑完看什么
----------
脚本最后给两节：

**逐档对比**（≥2 档时）——每档采到多少、并起来多少、重合率多少。
⚠️ **重合率是这一节的重点，不是覆盖率。** 各档近乎不相交才是正常的
（2026-09-26 实测两档共有 4 条 / 共 333 条）。重合率**高**只说明一件事：
筛选没生效，每一档都拿回了同一批。那时候这十二档等于白跑，
而日志上每档都写着"采集完成"。

**两档差集**（恰好 2 档时）——`--plan cases` 的两档，回答的是
"综合排序漏掉了多少当天新内容"。这是**已知覆盖缺口**的量化：
那些内容靠每天一次的更新路径（「最新发布」+「一天内」）兜。

产出（都在 `runtime/ops/<run-id>/` 下，已被 .gitignore 覆盖）：

    urls.jsonl    每条 URL 落盘即 flush，进程崩了也只丢最后一行
    report.md     人看的：每档采到多少、怎么停的、筛选项回读值、重合率
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sentinel_q.collector import drive, ops, selectors
from sentinel_q.collector import search as search_mod
from sentinel_q.collector.scrolling import StopReason
from sentinel_q.collector.session import BrowserSession, SessionConfig
from sentinel_q.shared.config import paths

log = logging.getLogger("check_search")

DEFAULT_PROFILE = "secrets/browser-profile"


@dataclass(frozen=True)
class Case:
    """一档口径，就是构造一个 `SearchSpec` 要的那些显式覆盖。"""

    index: int
    label: str
    kwargs: dict[str, str]

    #: 收窄用。`--type` / `--time` 拿它过滤。
    @property
    def kind(self) -> str | None:
        return self.kwargs.get("type")

    @property
    def span(self) -> str | None:
        return self.kwargs.get("time")


def _cases() -> list[Case]:
    """`--plan cases`：两档口径对比。

    ⚠️ 这两档是**显式覆盖**跑的，不走 `SearchSpec` 的 `mode` 默认值——
    对比实验必须能单独动一个维度，否则"结果不一样"归不了因。

    第 1 档排序栏**一下都不点**（「综合排序」就是页面默认），
    所以它同时也是"筛选逻辑会不会误点、把默认档点坏"的验收。
    """
    return [
        Case(
            1,
            "综合排序 + 不限时间",
            {"sort": selectors.FILTER_SORT_DEFAULT, "time": selectors.FILTER_TIME_UNLIMITED},
        ),
        Case(
            2,
            "最新发布 + 一天内",
            {"sort": selectors.FILTER_SORT_NEWEST, "time": selectors.FILTER_TIME_DAY},
        ),
    ]


def _backfill_cases() -> list[Case]:
    """`--plan backfill`：全量的十二档，**直接照 `search.backfill_specs` 铺**。

    ⚠️ 这里刻意**不重新拼一遍组合**，而是复用 `BACKFILL_TYPES` 和 `TIME_LADDER`。
    脚本自己写一份组合的话，实跑验的就不是线上要跑的那张表了——
    改了物业代码的组合表、脚本却还照着旧表跑，是最容易骗过验收的一种偏差。
    """
    cases: list[Case] = []
    for kind in search_mod.BACKFILL_TYPES:
        for span in search_mod.TIME_LADDER:
            cases.append(
                Case(
                    index=len(cases) + 1,
                    label=f"{kind} × {span}",
                    kwargs={
                        "sort": selectors.FILTER_SORT_DEFAULT,
                        "type": kind,
                        "time": span,
                    },
                )
            )
    return cases


def _update_cases(start: int = 1) -> list[Case]:
    """`--plan update`：更新路径的**唯一**一档。

    `start` 是给 `--plan all` 用的——那边的档号要接在全量十二档后面。
    ⚠️ 档号**必须连续**：它是报告里唯一能指代某一档的东西
    （`urls.jsonl` 里没有档位字段，见 `search.backfill` 的说明），
    所以 `all` 里冒出第二个"第 1 档"会让报告直接读不通。
    """
    spec = search_mod.update_spec("")
    return [
        Case(
            start,
            f"{spec.type_filter} × {spec.time_filter}",
            {"sort": spec.sort_filter, "type": spec.type_filter, "time": spec.time_filter},
        )
    ]


def _all_cases() -> list[Case]:
    """`--plan all`：全量十二档 + 更新一档 = 十三档。"""
    cases = _backfill_cases()
    return cases + _update_cases(start=len(cases) + 1)


PLANS: dict[str, Callable[[], list[Case]]] = {
    "cases": _cases,
    "backfill": _backfill_cases,
    "update": _update_cases,
    "all": _all_cases,
}


@dataclass
class CaseResult:
    """一档跑完之后留下的、写进 report.md 的东西。"""

    case: Case
    urls: list[str] = field(default_factory=list)
    readback: tuple[str | None, ...] = ()
    duplicates: int = 0
    skipped_no_link: int = 0
    skipped_not_content: int = 0
    rounds: int = 0  # 有产出的轮数（on_batch 被调到的次数）
    total_rounds: int = 0  # 滚动循环实际跑过的轮数，**含空转轮**
    elapsed: float = 0.0
    stop_reason: StopReason | None = None
    scroll_desc: str = ""
    empty_result: bool = False

    #: 搜索页白送的元数据的覆盖情况。**全是 0 就是没接上**——
    #: 那正是"新增字段有没有生效"要盯的东西，所以写进报告而不是只躺在 jsonl 里。
    with_excerpt: int = 0
    with_voteup: int = 0
    with_comment: int = 0
    top_voteup: int = 0

    @property
    def complete(self) -> bool:
        """⭐ 只有看到「没有更多了」才算**确定**采完了。

        `STABLE` 不算：它是"连续多轮无新增"，可能是真到底，也可能是
        页面卡住或选择器失效——这两种情况在日志上一模一样，不能当成功。
        """
        return self.stop_reason is StopReason.END_MARKER

    @property
    def stalled_rounds(self) -> int:
        """空转的轮数——**这是判断"滚太快了"的指标，不是总耗时。**

        `scroll_until_exhausted` 在进循环之前先 `collect()` 一次（首屏），
        所以 collect 的总调用次数是 `total_rounds + 1`；其中真有产出的
        `rounds` 次，剩下的就是空转。

        ⚠️ 为什么这个数比"总共花了多久"重要：轮数少一半可能只是把滚轮调快了，
        而空转变多说明**每一轮跨得太远、冲过了懒加载的触发点**——
        那正是"快"会付出的代价，也是唯一需要往回退一步的信号。
        """
        return max(0, self.total_rounds + 1 - self.rounds)

    def lines(self) -> list[str]:
        mark = "✅" if self.complete else "⚠️"
        out = [
            f"  筛选回读：{' / '.join(t or '?' for t in self.readback)}",
            (
                "  → 这三个值必须和情况名对得上。对不上说明筛选没生效，"
                "这批数据的口径是错的。"
            ),
            f"  采到 URL：{len(self.urls)} 条",
            (
                f"  缩略信息：{self.with_excerpt}/{len(self.urls)} 条有"
                f"｜赞同非 0 {self.with_voteup} 条（最高 {self.top_voteup}）"
                f"｜评论非 0 {self.with_comment} 条"
            ),
            f"  页内重复：{self.duplicates} 条（滚动时 DOM 重渲染，正常）",
            f"  无链接卡片：{self.skipped_no_link} 个 ⚠️ 非 0 要查选择器",
            f"  非内容链接：{self.skipped_not_content} 条（话题/用户页，正常）",
            (
                f"  用时：{self.elapsed:.0f} 秒，共 {self.total_rounds} 轮"
                f"（有产出 {self.rounds} 轮，空转 {self.stalled_rounds} 轮）"
            ),
            f"  滚动：{self.scroll_desc}",
            f"  {mark} {'完整（页面上出现了结尾标记）' if self.complete else '不确定/不完整，看上面那行滚动是怎么停的'}",
        ]
        if self.urls and not self.with_excerpt:
            # ⭐ 响亮地报出来。这个失效是**静默**的：条数、筛选回读全都正常，
            #    只有元数据整列为空——不看这一行根本发现不了。
            out.append(
                "  ⚠️ 一条缩略信息都没有。要么新字段没接上，"
                "要么 `.RichText` 选择器失效了（页面改版）"
            )
        if self.empty_result:
            out.append(
                "  ❌ 命中了「内容发现」——这个关键词**没有搜索结果**，"
                "采到的是知乎塞的推荐内容"
            )
        return out


def run_case(
    session: BrowserSession,
    case: Case,
    keyword: str,
    store: ops.OpsStore,
    max_rounds: int,
    flick_steps: int,
    pace_factor: float,
) -> CaseResult:
    """跑一档，URL 一边采一边落盘。"""
    log.info("")
    log.info("═══ 第 %d 档：%s ═══", case.index, case.label)
    log.info("  轮次上限 %d，每轮拨 %d 下滚轮，轮间停顿系数 %.2f", max_rounds, flick_steps, pace_factor)

    spec = search_mod.SearchSpec(keyword, **case.kwargs)
    result = CaseResult(case=case)
    started = time.monotonic()
    last = started

    def on_batch(batch: list[ops.UrlEntry]) -> None:
        nonlocal last
        # ⭐ 成本与崩溃的边界就在这一行：append_urls 内部**逐行 flush**，
        #    所以这里返回时，这一批已经在磁盘上了。
        written = store.append_urls(batch)
        result.rounds += 1
        now = time.monotonic()
        log.info(
            "  第 %d 轮：新增 %d 条，累计 %d 条（距上一轮 %.1f 秒）",
            result.rounds,
            written,
            len(result.urls) + written,
            now - last,
        )
        last = now
        result.urls.extend(entry.url for entry in batch)
        # 搜索页白送的元数据，逐条累计——报告里那一行就是靠这几个数
        for entry in batch:
            result.with_excerpt += bool(entry.excerpt)
            result.with_voteup += bool(entry.voteup_count)
            result.with_comment += bool(entry.comment_count)
            result.top_voteup = max(result.top_voteup, entry.voteup_count)

    report = search_mod.search(
        session,
        spec,
        on_batch=on_batch,
        max_rounds=max_rounds,
        flick_steps=flick_steps,
        pace_factor=pace_factor,
    )
    result.elapsed = time.monotonic() - started

    result.readback = report.filters_readback
    result.duplicates = report.duplicates
    result.skipped_no_link = report.skipped_no_link
    result.skipped_not_content = report.skipped_not_content
    result.empty_result = report.empty_result
    if report.scroll is not None:
        result.stop_reason = report.scroll.stop_reason
        result.total_rounds = report.scroll.rounds
        result.scroll_desc = report.scroll.describe("搜索结果")
    return result


def overlap_section(results: list[CaseResult]) -> list[str]:
    """逐档条数 + 并集 + **重合率**。跑了两档以上就出这一节。

    ⚠️ **这一节的重点是重合率，不是覆盖率。** 各档近乎不相交才是正常的：
    2026-09-26 实测两档共有 4 条 / 合计 333 条，约 1%。因为搜索结果页
    是个推荐接口，换一档筛选**成员就重洗一遍**（见 `search.py` 模块开头）。

    于是重合率**高**只有一种解释：筛选没生效，每档都拿回同一批。
    那时候十二档等于白跑一遍，而每档的日志都写着"采集完成"。
    这是全量路径**唯一**能发现这种故障的地方。
    """
    if len(results) < 2:
        return []
    sets = [set(r.urls) for r in results]
    union: set[str] = set().union(*sets)
    raw_total = sum(len(s) for s in sets)
    overlap = raw_total - len(union)
    ratio = overlap / raw_total if raw_total else 0.0

    lines = ["", "── 逐档对比 " + "─" * 50]
    # 累计去重的并集一路滚下去，于是每档显示的是**它自己带来了多少新东西**。
    # ⚠️ 这比"这一档采到多少条"有用得多：某档采回 150 条、其中 148 条别档已有，
    #    那它其实只贡献了 2 条——而只看"采到 150 条"是完全看不出来的。
    running: set[str] = set()
    fresh_counts: list[int] = []
    for result in results:
        mine = set(result.urls)
        fresh = len(mine - running)
        fresh_counts.append(fresh)
        running |= mine
        mark = "✅" if result.complete else "⚠️"
        lines.append(
            f"  {mark} 第 {result.case.index:>2} 档  {result.case.label}："
            f"采到 {len(mine)} 条，新增 {fresh} 条（累计 {len(running)}）"
        )
    lines += [
        "",
        f"  各档合计：{raw_total} 条（不去重）",
        f"  并集去重：{len(union)} 条  ← ⭐ 全量的实际产出",
        f"  重合：{overlap} 条，重合率 {ratio:.1%}",
        "",
    ]
    if ratio > search_mod.OVERLAP_ALARM:
        lines += [
            (
                f"  ❌ 重合率 {ratio:.1%} 远超正常（实测约 1%）。这不像"
                "「内容都热门」，更像**筛选没生效**——"
            ),
            "     每一档都拿回了同一批结果。先看每档的「筛选回读」那行对不对得上。",
        ]
    else:
        lines += [
            "  ✅ 重合率正常，各档拿回的是不同的样本——组合策略是有效的。",
        ]

    # ⚠️ 这一节回答的是另一个问题：**梯子够不够长**。
    #    如果后面几档的"新增"掉到接近 0，说明再加档就是白跑——
    #    全量的组合表该收，不该继续加。反过来如果最后一档还新增一大堆，
    #    那这张表就是**明显没铺满**，值得回头补档。
    if len(results) >= 4:
        tail_fresh = sum(fresh_counts[-2:])
        lines += [
            "",
            f"  最后两档一共新增：{tail_fresh} 条",
        ]
        if tail_fresh == 0:
            lines.append("  ⚠️ 最后一档一条新东西都没带来——梯子可能已经铺满，可以考虑收档。")
        elif tail_fresh > len(union) * 0.2:
            lines.append("  ⚠️ 最后两档还贡献了超过两成的内容——**这张表多半没铺满**，值得补档。")
        else:
            lines.append("  ✅ 尾部还有稳定新增但不算多，梯子长度大致合适。")
    return lines


def diff_section(results: list[CaseResult]) -> list[str]:
    """恰好两档时的差集。`--plan cases` 用它量化**已知的覆盖缺口**。"""
    if len(results) < 2:
        return []
    first, second = results[0], results[1]
    a, b = set(first.urls), set(second.urls)
    only_second = b - a
    lines = [
        "",
        "── 两档差集 " + "─" * 50,
        f"  第 {first.case.index} 档（{first.case.label}）：{len(a)} 条",
        f"  第 {second.case.index} 档（{second.case.label}）：{len(b)} 条",
        f"  两档都有：{len(a & b)} 条",
        f"  只在第 {first.case.index} 档：{len(a - b)} 条",
        f"  只在第 {second.case.index} 档：{len(only_second)} 条  ← ⭐ 看这个数",
        "",
    ]
    if only_second:
        lines += [
            (
                f"  ⭐ 「{second.case.label}」多出来这 {len(only_second)} 条，就是"
                "「最新发布」能捞到、而「综合排序」捞不到的**当天新内容**。前 10 条："
            ),
            *[f"       {u}" for u in sorted(only_second)[:10]],
            "",
            "     这个数就是**已知覆盖缺口的量化**：全量的十二档排序固定在",
            "     「综合排序」，碰不到这些内容。它们是靠**每天一次的更新路径**",
            "     （「最新发布」+「一天内」）兜住的——所以更新那一路不能停。",
            "     这个数要是很大（比如几百条），说明缺口比预期宽，值得回头再看。",
        ]
    else:
        lines += [
            "  ⭐ 这一档没有多出任何内容——「综合排序」把「最新发布」的结果全包住了。",
            "     若是这样，全量口径的覆盖缺口就小；但**只跑一次不算数**，",
            "     换个关键词、或者过一段内容更新鲜的时候可能就不同。",
        ]
    return lines


def write_report(
    path: Path,
    keyword: str,
    plan: str,
    run_dir: Path,
    results: list[CaseResult],
    rounds_cap: int,
    flick_steps: int,
    pace_factor: float,
) -> None:
    body = [
        "# 能力一实跑：搜索 URL 提取",
        "",
        f"- 关键词：`{keyword}`",
        f"- 计划：`{plan}`（{len(results)} 档）",
        f"- 时间：{datetime.now(UTC).astimezone().isoformat(timespec='seconds')}",
        f"- 运行目录：`{run_dir}`",
        f"- 轮次上限：{rounds_cap}",
        f"- 滚动：每轮拨 {flick_steps} 下滚轮（`drive.scroll_flick`），轮间停顿系数 {pace_factor}",
        "",
        "> ⚠️ 这份报告由 `scripts/check_search.py` 生成，是**一次实跑的记录**，",
        "> 不是自动化测试。单元测试在 `src/sentinel_q/collector/tests/`。",
    ]
    for result in results:
        body += ["", f"## 第 {result.case.index} 档：{result.case.label}", "", *result.lines()]
    body += overlap_section(results)
    body += diff_section(results)
    body += [
        "",
        "── URL 清单 " + "─" * 50,
        "",
        f"全部 URL 在 `{run_dir / 'urls.jsonl'}`，**每写一行就 flush**，",
        "所以这个脚本中途崩掉，前面采到的也都在。",
        "⚠️ 所有档写的是**同一个文件**，而文件里**没有「哪一档采的」这个字段**",
        "（刻意不加，见 `search.backfill` 的说明）。要按档拆开看，",
        "只能看上面逐档对比里的条数——或者一档一档单独跑。",
        "",
    ]
    path.write_text("\n".join(body) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="check_search",
        description="能力一实跑验收：搜关键词 → 滚到底 → URL 逐行落盘 → 报完整性",
        epilog=(
            "⚠️ 会打开真实浏览器、用真实账号访问知乎。\n"
            "关键词没有默认值——一次忘了传参的运行会静默地采回来一堆别的东西。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--keyword",
        required=True,
        help="测试关键词。要中性，别拿真实的监测对象试（关键词清单的敏感度和提示词同级）",
    )
    parser.add_argument(
        "--plan",
        choices=sorted(PLANS),
        default="cases",
        help=(
            "跑哪个计划（默认 cases）。"
            "cases=两档口径对比；backfill=全量的十二档（2 类型 × 6 时间段，约 10~15 分钟）；"
            "update=更新的单档；all=backfill+update"
        ),
    )
    parser.add_argument(
        "--type",
        dest="kind",
        choices=selectors.FILTER_TYPE_OPTIONS,
        help="只跑这个类型档（收窄用）。不给就跑计划里全部",
    )
    parser.add_argument(
        "--time",
        dest="span",
        choices=selectors.FILTER_TIME_OPTIONS,
        help="只跑这个时间段档（收窄用）。不给就跑计划里全部",
    )
    parser.add_argument("--max-rounds", type=int, default=500, help="每档的滚动轮次上限（默认 500）")
    parser.add_argument(
        "--flick-steps",
        type=int,
        default=drive.FLICK_STEPS,
        help=(
            f"每轮拨几下滚轮（默认 {drive.FLICK_STEPS}）。"
            "调大=每轮跨得更远、轮数更少；⚠️ 判断有没有调过头看**空转轮数**，不是总耗时"
        ),
    )
    parser.add_argument(
        "--pace",
        type=float,
        default=search_mod.PACE_FACTOR,
        help=(
            f"轮间停顿系数，乘在 3~8 秒上（默认 {search_mod.PACE_FACTOR}）。"
            "⚠️ 别填 0——那是拿掉本系统唯一有反爬含义的停顿"
        ),
    )
    parser.add_argument(
        "--case",
        choices=("1", "2", "both"),
        default=None,
        help="（旧参数，保留兼容）等价于 --plan cases 再收窄到第 N 档。"
        "⚠️ 只能和 --plan cases 一起用；配别的 plan 会直接报错，不会静默忽略",
    )
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="浏览器 profile 目录（登录态）")
    parser.add_argument("--headless", action="store_true", help="无头模式。⚠️ 登录和过验证码会看不见窗口")
    parser.add_argument(
        "--no-stealth",
        action="store_true",
        help="关掉 navigator.webdriver 抑制。⚠️ 关掉后知乎的验证码会加载不出来",
    )
    return parser


def select_cases(args: argparse.Namespace, parser: argparse.ArgumentParser) -> list[Case]:
    """把 `--plan` / `--case` / `--type` / `--time` 解析成要跑的档清单。

    ⚠️ **收窄到空清单时直接报错退出，不返回空列表。**
    空跑一趟的代价不只是浪费一次开浏览器：日志上会写着"全部完成"，
    而实际上一条 URL 都没采——那是本项目最怕的那类静默失败，
    在 CLI 这一层就该断掉。
    """
    if args.case is not None:
        if args.plan != "cases":
            parser.error(f"--case 只能和 --plan cases 一起用，现在是 --plan {args.plan}")
        wanted = (1, 2) if args.case == "both" else (int(args.case),)
        return [c for c in _cases() if c.index in wanted]

    cases = PLANS[args.plan]()
    total = len(cases)
    if args.kind:
        cases = [c for c in cases if c.kind == args.kind]
    if args.span:
        cases = [c for c in cases if c.span == args.span]
    if not cases:
        given = " / ".join(
            f"--{name} {value}"
            for name, value in (("type", args.kind), ("time", args.span))
            if value
        )
        parser.error(
            f"--plan {args.plan} 的 {total} 档里，没有一档符合 {given}。"
            "（比如 `cases` 那两档带的是 排序+时间，没有类型维度）"
        )
    if len(cases) < total:
        log.info("按 --type/--time 收窄：%d 档 → %d 档", total, len(cases))
    return cases


def run_all(
    session: BrowserSession,
    cases: list[Case],
    args: argparse.Namespace,
    store: ops.OpsStore,
) -> tuple[list[CaseResult], BaseException | None]:
    """逐档跑，**把异常接住并连同已完成的结果一起返回**，不往外抛。

    ⚠️ 这是刻意的：一轮全量十几分钟、十几档，第 10 档崩掉时前 9 档的
    诊断数据（逐档条数、筛选回读值）是排查时唯一能用的东西——
    `urls.jsonl` 里虽然每行都落了盘，但那是原始 URL，**不含任何口径信息**。
    抛出去的话调用方拿不到 `results`，报告只剩一片空白。

    返回的第二个值是异常本身（`BaseException`，连 Ctrl-C 一起接住），
    由调用方在**写完报告之后**决定怎么处置——绝不能在这里吞掉。
    """
    results: list[CaseResult] = []
    try:
        for case in cases:
            results.append(
                run_case(
                    session,
                    case,
                    args.keyword,
                    store,
                    args.max_rounds,
                    args.flick_steps,
                    args.pace,
                )
            )
            session.pace()
    except BaseException as exc:  # noqa: BLE001
        return results, exc
    return results, None


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    cases = select_cases(args, parser)

    layout = paths()
    layout.ensure()
    run_id = datetime.now(UTC).strftime("%Y%m%d-%H%M%S") + "-check-search"
    store = ops.OpsStore.new_run(
        layout.ops,
        mode="backfill",  # ⚠️ 不是真在跑 backfill——见下面 note
        run_id=run_id,
        note=f"能力一实跑验收（plan={args.plan}，{len(cases)} 档），不是正式采集任务",
    )

    log.info("关键词：%s", args.keyword)
    log.info("运行目录：%s", store.run_dir)
    log.info("将要跑：%d 档 —— %s", len(cases), "；".join(f"第{c.index}档 {c.label}" for c in cases))
    if len(cases) >= 6:
        # 全量十几档是十几分钟的事，先说清楚，别让人以为是卡住了
        log.info("⚠️ %d 档是十几次完整搜索串起来的，会跑十几分钟。中途可以 Ctrl-C，已采的 URL 都在盘上。", len(cases))
    log.info("⚠️ 接下来会打开真实浏览器访问知乎。若弹出登录/验证码，窗口里手动处理，脚本会等。")

    store.set_status("running")
    results: list[CaseResult] = []
    failure: BaseException | None = None
    try:
        with BrowserSession(
            SessionConfig(
                profile_dir=Path(args.profile),
                headless=args.headless,
                suppress_webdriver_flag=not args.no_stealth,
            )
        ) as session:
            session.ensure_logged_in()
            results, failure = run_all(session, cases, args, store)
    except BaseException:
        # 开浏览器 / 等登录这一步崩了，还没轮到跑档，没有部分结果可救
        store.set_status("failed")
        raise
    if failure is not None:
        store.set_status("failed")

    report_path = store.run_dir / "report.md"
    write_report(
        report_path,
        args.keyword,
        args.plan,
        store.run_dir,
        results,
        args.max_rounds,
        args.flick_steps,
        args.pace,
    )

    print()
    for result in results:
        print(f"── 第 {result.case.index} 档：{result.case.label}")
        for line in result.lines():
            print(line)
    for line in overlap_section(results):
        print(line)
    for line in diff_section(results):
        print(line)
    print()
    if failure is not None:
        # 部分结果也照样打完，然后**响亮地说清楚这是残缺的一轮**
        done = len(results)
        print(f"❌ 第 {done + 1} 档崩了，上面只有前 {done} 档的数据（共 {len(cases)} 档）。")
        print(f"   异常：{type(failure).__name__}: {failure}")
        print(f"   报告（残缺）：{report_path}")
        print(f"   URL 清单：{store.urls_path}（共 {store.url_count()} 行）")
        raise failure

    print(f"报告：{report_path}")
    print(f"URL 清单：{store.urls_path}（共 {store.url_count()} 行）")
    print("⚠️ urls.jsonl 里**没有去重**（每档各自追加），所以这个行数是各档之和，")
    print("   不是全量的产出——真实产出看上面「并集去重」那一行。")

    store.set_status("done")
    return 0 if all(r.complete for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
