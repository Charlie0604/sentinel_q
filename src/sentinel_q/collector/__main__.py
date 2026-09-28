"""采集模块的独立入口（架构文档 7.4）。

    python -m sentinel_q.collector search   --keyword 某公司 --mode backfill
    python -m sentinel_q.collector content  --run 20260926-153000
    python -m sentinel_q.collector comments --url https://www.zhihu.com/question/1/answer/2
    python -m sentinel_q.collector answers  --question 19581646 --mode backfill
    python -m sentinel_q.collector probe    --check
    python -m sentinel_q.collector status

## ⚠️ 采集模块**不连数据库**（决策 52）

这个文件里没有 `psycopg`，没有 SQL，也没有 `storage` 的 import。
采到的东西**只落 `runtime/ops/<run_id>/contents.jsonl`**，
由主程序读走、统一入库（`storage.ingest.insert_contents`）。

代价是独立跑 `content` / `answers` 时**问不到库**，也就没法把库里已有的挡掉。
这条代价是**刻意选的**，因为它的错误方向是安全的：不挡 = 白采一遍（浪费几十分钟），
挡错 = 静默漏采（永远补不回来）。宁可浪费。真跑请走主程序，
它开跑前会调一次 `storage.existing_urls()` 把已知集合一次取全。

## 为什么拆成子命令而不是 `--mode backfill` 一个开关

文档 7.4 原本写的是一个 `--mode` 开关，但四个能力的**输入根本不是同一种东西**：
能力一要关键词，能力二要 URL 清单，能力四要问题 ID。"一个开关跑全流程"
实际会变成"每次都把四件事都跑一遍"，而它们各自要跑十几分钟、各自会被限流、
各自值得单独看结果。分成子命令之后，**一次只做一件事、只等一件事**，
出问题也只需要重跑那一件。

## 目录与去重

`runtime/ops/<run_id>/` 是本地运维账本（断点 + URL 清单 + 限流采样），
**不入库也不入 git**（文档 3.8 / 7.8）。`search` 生产 URL 清单，
`content` / `comments` / `answers` 消费它。

去重仍然**只认"已经确定采过"的东西**（决策 51 的"文件 ∪ 库"）：
独立跑时是本次任务的 `contents.jsonl` 里已有的 URL，
主程序跑时再并上它传进来的那一份库里的集合。**没有别的本地台账**——
台账被文档 7.8 明确禁止，因为它的错误方向是**不对称**的：
台账说"已处理"而实际没处理，就是静默漏采，而且永远补不回来。
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from sentinel_q.collector import (
    answers,
    comments,
    content,
    extract,
    ops,
    parse,
    probe,
    search,
    selectors,
)
from sentinel_q.collector.session import BrowserSession, SessionConfig
from sentinel_q.shared.config import Settings, paths
from sentinel_q.shared.urlnorm import normalize

log = logging.getLogger("collector")

DEFAULT_PROFILE = "secrets/browser-profile"

FLUSH_EVERY = 20
"""攒够多少条做一次交叉验证。见 `_Batcher` 里那段说明。"""


def _now() -> str:
    """限流采样用的时间戳（`runtime/ops/runs.jsonl` 的一列）。"""
    return datetime.now(UTC).isoformat(timespec="seconds")


# ── 公共设施 ────────────────────────────────────────────────────────


def _session_config(args: argparse.Namespace) -> SessionConfig:
    return SessionConfig(
        profile_dir=Path(args.profile),
        headless=args.headless,
        suppress_webdriver_flag=not args.no_stealth,
    )


def _open_run(args: argparse.Namespace, mode: ops.TaskMode) -> ops.OpsStore:
    """开一次新任务，或者用 `--run` 接着上次的跑。"""
    ops_dir = paths().ops
    if args.run:
        run_dir = ops_dir / args.run
        if not (run_dir / "task.json").exists():
            raise SystemExit(f"❌ 找不到任务 {run_dir}。用 `status` 看看有哪些。")
        store = ops.OpsStore.resume(run_dir)
        log.info("接着上次跑：%s（状态 %s）", store.state.run_id, store.state.status)
        return store
    store = ops.OpsStore.new_run(ops_dir, mode)
    log.info("新任务：%s（模式 %s）", store.state.run_id, mode)
    return store


def _policy() -> extract.SnapshotPolicy:
    """快照落盘策略。本地暂存目录，之后由数据库模块上传到 Storage。"""
    return extract.SnapshotPolicy(root=paths().runtime / "snapshots")


def _require_calibrated() -> None:
    """未校准的选择器直接拒绝运行。

    ⚠️ **不做"警告一下继续跑"**：未校准的选择器不会报错，它只是**匹配不到东西**，
    于是采集"成功"完成、条数是 0、报告一片安静。这就是静默漏采，
    而它是本项目明确列为不可接受的那一类失败（文档 4.1）。
    """
    pending = selectors.uncalibrated()
    if pending:
        raise SystemExit(selectors.summary())


def _cross_check(
    items: list[parse.ParsedItem],
    *,
    page_html: str | None = None,
    what: str,
) -> int:
    """一批内容做第一档交叉验证，返回**可疑的条数**。

    ⚠️ 这**不是入库**。采到的东西在 `_ContentsDoc.add()` 那一步就已经落盘了
    （见 `_crawl_comments` / `_crawl_question` 的 `on_item`），
    这里纯粹是"页面里的实体 vs 解析出的正文"对一遍账，本地就能做，
    不碰网络也不碰库。

    它跟着 `_Batcher` 攒批走，是因为要比对**当前页面的 HTML**，
    而序列化一次整页要一两秒——逐条做的话，一个 500 条回答的问题
    要序列化 500 次整页，采集本身都没这么贵。
    """
    if page_html is None:
        # 评论没有对应的 entities（`_crawl_comments` 的说明），无从比起。
        return 0
    suspects = extract.cross_check_all(items, page_html).report()
    if suspects:
        log.error(
            "❌ %s 里有 %d 条正文**可能不是全文**（见上面的逐条说明）。"
            "内容照常落盘（截断的正文仍然是有价值的证据），"
            "但**别把它当全文用**。",
            what,
            suspects,
        )
    return suspects


class _Batcher:
    """攒够 `size` 条（或者收尾时）就做一次交叉验证。

    ## 为什么不是"采一条验一条"

    交叉验证要比对**当前页面的 HTML**，而序列化一次整页要一两秒。
    逐条做的话，一个 500 条回答的问题要序列化 500 次整页——
    采集本身都没这么贵。

    ## 为什么不是"全采完再验"

    `answers.extract_answers` 和 `comments.extract_comments` 的文档里
    都专门说了这件事：一个大问题要滚十几分钟，中途崩了（或者被验证码卡住、
    被限流）就全没了。攒 20 条是一个折中——最坏情况下漏验 20 条，
    而整页序列化的次数降到 1/20。

    ⚠️ 攒的同时**顺序不能乱**：评论落盘时父级要先于子级，
    否则核对的顺序对不上（见 `extract.to_document` 的说明）。
    这里只用 一个 list 追加、按序 flush，不会打乱顺序。
    """

    def __init__(
        self,
        flush: Callable[[list[parse.ParsedItem]], None],
        *,
        size: int = FLUSH_EVERY,
    ) -> None:
        self._flush = flush
        self._size = size
        self._buffer: list[parse.ParsedItem] = []
        self.total = 0

    def add(self, item: parse.ParsedItem) -> None:
        self._buffer.append(item)
        self.total += 1
        if len(self._buffer) >= self._size:
            self.flush()

    def flush(self) -> None:
        if not self._buffer:
            return
        batch, self._buffer = self._buffer, []
        self._flush(batch)


def _entries_from(entries: Iterable[ops.UrlEntry]) -> list[ops.UrlEntry]:
    """从 URL 清单里取**能认出来的内容页**，顺便报出认不出来的那些。

    认不出来的一条都不能丢得无声无息：`搜索` 采到的链接里混着话题页、
    用户页、站外链接是正常的，但如果**整批**都认不出来，那就是选择器的事。

    ⚠️ 返回的是**整条 `UrlEntry`**（不是光 URL），而且 URL 已经换成规范化形式：
    `keyword` 要一路带到落盘文档里，`content_type` 留着给文档去重当键用。
    """
    kept: list[ops.UrlEntry] = []
    bad: list[str] = []
    for entry in entries:
        normalized = normalize(entry.url)
        if normalized is None:
            bad.append(entry.url)
            continue
        kept.append(replace(entry, url=normalized.url))
    if bad:
        log.warning(
            "清单里有 %d 条 URL 认不出来（共 %d 条），跳过。前几条：%s",
            len(bad),
            len(bad) + len(kept),
            ", ".join(bad[:3]),
        )
    return kept


def _dedup(known: Collection[str], urls: Sequence[str]) -> list[str]:
    """⭐ 成本闸门：**开浏览器之前**把已经采过的挡掉（文档 3.9 第 3 条）。

    放在这里而不是采完之后，是因为一次采集要几十秒，而查重是内存里的集合运算。
    先挡掉，能省下的是几十分钟。

    ⚠️ `known` 是**调用方给的普通集合**，不是仓储（决策 52）。采集模块连不上
    数据库，所以"库里已经有什么"这件事只能由**主程序开跑前一次性取全**、
    当参数递进来——也就是 3.3 的【初始列表】/【初始问题】。它和本次采集
    自己产的文件（`_ContentsDoc.urls`）合起来，才是决策 51 那句"文件 ∪ 库"。

    传空集合 = 一条都不挡。那是安全的失败方向：白采一遍，而不是静默漏采。
    """
    if not urls:
        return []
    todo = [url for url in urls if url not in known]
    if len(todo) < len(urls):
        log.info("已经采过 %d 条，跳过；这次要采 %d 条", len(urls) - len(todo), len(todo))
    return todo


# ── 子命令 ──────────────────────────────────────────────────────────


def cmd_search(args: argparse.Namespace) -> int:
    """能力一：搜关键词 → 点筛选 → 滚到底 → 把内容 URL 写进清单。"""
    _require_calibrated()

    keywords = list(args.keyword or [])
    if args.from_keywords:
        keywords += ops.OpsStore.load_keyword_file(paths().ops)
    if not keywords:
        raise SystemExit(
            "❌ 没有关键词。用 --keyword 给一个（可以给多个），或者在 "
            f"{paths().ops / 'keywords.jsonl'} 里写一行 {{\"keyword\": \"...\"}}。\n"
            "   ⚠️ 这里**没有默认关键词**，是刻意的：默认值会让一次忘了传参的调用\n"
            "   静默地拿着某个词去采，而采回来的是别的东西。\n"
            "   只想试跑流程的话，用一个中性词：--keyword 测试"
        )
    if args.limit:
        keywords = keywords[: args.limit]

    store = _open_run(args, args.mode)
    store.set_status("running")

    def on_batch(entries: list[ops.UrlEntry]) -> None:
        # ⚠️ 每轮滚动就追加进 urls.jsonl，不等全部搜完：一轮滚动可能跑十几分钟，
        #    中途崩了（或者被验证码卡住）就全没了。
        #    全量路径下这个回调**已经被跨档去重过**（见 `search.backfill`），
        #    所以同一个 URL 不会因为十二档都命中而写十二遍。
        store.append_urls(entries)

    #: 每个关键词跑完的总条数，用来打 RunSample（3.7 静默漏采检测的基线）。
    counts: list[tuple[str, int]] = []
    try:
        with BrowserSession(_session_config(args)) as session:
            session.on_status = store.set_status
            session.ensure_logged_in()

            for keyword in keywords:
                keyword = keyword.strip()
                if not keyword:
                    continue
                counts.append(
                    (
                        keyword,
                        _collect_keyword(
                            session, keyword, args.mode, store, on_batch, args
                        ),
                    )
                )
                store.advance(keyword_index=len(counts))
                # ⚠️ 关键词之间**必须留停顿**并串行。单账号并发刷搜索是最容易被
                #    限流的用法，而本系统明确不搭代理池去对抗限流（架构文档边界条款）——
                #    用有规避风控痕迹的方式采来的数据，会削弱证据的正当性。
                #    跑慢一点是设计的一部分，不是可以优化的地方。
                #    （关键词**内部**的停顿由 `search.backfill` 逐档负责。）
                session.pace()
    except BaseException:
        # 崩了也要把状态写对：留在 "running" 的话，下次 `status` 会显示一个
        # 早就不在跑的任务，人就不知道到底该不该重跑。
        store.set_status("failed")
        raise

    store.set_status("done")
    total = sum(count for _, count in counts)
    log.info(
        "搜完 %d 个关键词，共 %d 条，已写入 %s",
        len(counts),
        total,
        store.urls_path,
    )
    if total == 0:
        log.error(
            "❌ 一条都没搜到。要么关键词确实没结果，要么选择器/筛选失效了——"
            "**别当成'这个词没热度'**，先人工打开搜索页看一眼。"
        )
        return 1
    return 0


def _collect_keyword(
    session: BrowserSession,
    keyword: str,
    mode: str,
    store: ops.OpsStore,
    on_batch: Callable[[list[ops.UrlEntry]], None],
    args: argparse.Namespace,
) -> int:
    """采一个关键词，返回**去重后**的条数。

    两条路径的差别就是"跑几档组合"：

        backfill  十二档（2 类型 × 6 时间段，排序固定「综合排序」）
        update    **一档**（最新发布 + 一天内 + 不限类型）

    ⚠️ 全量比更新贵十几倍。这不是"全量更认真"，是搜索页只给**样本**——
    见 `search.py` 模块开头那段实测。更新之所以一档就够，是因为它只要
    "昨天到今天新发的那些"，那一档正好覆盖。

    ⚠️ **这里不传 `max_rounds`**（用 `search` 的默认上限）。
    更新路径尤其不需要设：知乎搜索只生成一百来条，滚到底是很快的，
    设一个小上限反而会把"没采完"伪装成"采完了"。
    """
    if mode == "backfill":
        report = search.backfill(session, keyword, on_batch=on_batch)
        found = report.found
        for combo in report.combos:
            combo.report.warn_if_empty()
    else:
        single = search.search(session, search.update_spec(keyword), on_batch=on_batch)
        single.warn_if_empty()
        log.info(single.describe())
        found = single.found

    # ⚠️ **一个关键词一条采样，不是一档一条。**
    #    3.7 的静默漏采检测拿它和**同 kind 的历史均值**比，所以这两件事都不能做：
    #    · 改成每档一条——历史基线的口径会从"每词总量"变成"每档条数"，
    #      两个数差着十几倍，检测会当场误报一轮；
    #    · 给全量换一个新的 kind 值——那等于没有基线，得从头积累，
    #      而这段时间里真正该报的漏采没人管。
    #    每档的条数照样留痕，在日志和 `check_search.py` 的报告里。
    store.append_sample(
        ops.RunSample(at=_now(), keyword=keyword, kind="search", result_count=found)
    )
    return found


def cmd_content(args: argparse.Namespace) -> int:
    """能力二：按 URL 清单取正文，把产物写进 `contents.jsonl`。
    `--with-comments` 时顺带采评论。

    ## 顺序是硬的：正文 → 正文落盘 → 评论 → 评论落盘

    **正文必须先落盘**，理由不在库里，在文档里：`contents.jsonl` 的
    行顺序就是核对顺序，正文那一行要排在它自己的评论前面。

    **评论采在正文之后**：采评论要开弹窗、页面结构会变，先把它取干净再动页面。

    ## 这里不碰库

    采到的东西只落 `contents.jsonl`，由主程序统一入库（决策 52）。
    所以这里没有"入库成功没有"这种状态——**有行就是采到了，没行就是没采到**。
    """
    _require_calibrated()

    # ⚠️ 先校验命令行，再碰环境（浏览器）。
    #    反过来的话，`content` 忘了给 URL 的人会先看到浏览器起不来，
    #    然后去查浏览器、再跑一遍、才发现是没给 URL——而命令行的问题
    #    本该在它自己那一层就被指出来。
    targets = _targets(args)
    if not targets:
        raise SystemExit("❌ 没给 URL。用 --url <内容页地址>，或者 --run <任务 ID> 读清单。")

    store = _open_run(args, "backfill")
    policy = _policy()
    doc = _ContentsDoc(store, policy)

    by_url = {entry.url: entry for entry in targets}
    todo = _dedup(doc.urls, list(by_url))[: args.limit]
    if not todo:
        log.info("清单里 %d 条都已经采过了，没有要采的", len(targets))
        return 0

    store.set_status("running")
    reports: list[content.ContentReport] = []
    comment_reports: list[comments.CommentsReport] = []
    try:
        with BrowserSession(_session_config(args)) as session:
            session.on_status = store.set_status
            session.ensure_logged_in()

            for url in todo:
                keyword = by_url[url].keyword
                report = content.extract_content(session, url)
                reports.append(report)
                # ⚠️ 空正文**不留行**（`has_body`）——正文容器找不到时
                #    parse 不报错、只给回空字符串，写下去就是一条"采到了"的假象。
                #    评论也一并跳过：它要挂在正文那一行上。
                if report.item is not None and report.item.has_body:
                    # 正文那一条**先落盘**，它的评论随后逐条落 ——
                    # 文档里的顺序就是核对顺序，两头对得上才核得动。
                    doc.add(report.item, keyword=keyword)
                    # 一个 URL 一条记录，所以这里逐条验证正好是"每页序列化一次"。
                    _cross_check(
                        [report.item],
                        page_html=_page_html(session),
                        what=f"{report.item.content_type} {report.item.zhihu_id}",
                    )
                    # ⚠️ 必须在正文落盘**之后**才采评论（理由见函数开头）。
                    #    正文没取到就没有可挂的父级，评论也一并跳过。
                    if args.with_comments:
                        comment_reports.append(
                            _crawl_comments(
                                session,
                                url,
                                policy=policy,
                                doc=doc,
                                keyword=keyword,
                            )
                        )
                session.pace()
    except BaseException:
        # 崩了也要把状态写对，理由同 `cmd_search`。
        store.set_status("failed")
        raise

    store.set_status("done")
    log.info("正文落盘：%s（共 %d 行）", store.contents_path, doc.written)
    log.info(content.summarize(reports))
    verdict = _verdict("正文", reports, lambda r: r.ok)
    if args.with_comments:
        log.info(comments.summarize(comment_reports))
        # ⚠️ 分开判、取**更坏**的那个：评论漏采是实打实的证据缺失，
        #    不该被正文的成功盖过去。
        verdict = max(verdict, _verdict("评论", comment_reports, lambda r: r.ok))
    return verdict


class _ContentsDoc:
    """把采到的东西逐条追加进 `<run>/contents.jsonl`。

    ## 它是"采集 → 分析"的衔接面（决策 51）

    不变量是"AI 判完才入库"，所以库里查不出本轮采了什么。这份文档就是那个
    交接点：AI 判定结果与断点标记写在**同一行**，断点 = "哪一行还没有结果"。
    连带一条：**查重不能只查库，得"文件 ∪ 库"**。

    它仍满足 7.8 推论二（可重建），只是重建代价从"重爬一次"变成
    "重爬一次 + 重问一次 AI"——这也是全项目最贵的一处丢失。

    ## 为什么要在内存里记一份 key

    验收流程本身就是"先 `--limit 20` 冒烟、再全量"，两次跑的是同一批 URL；
    跨进程重跑同理。不去重的话前 20 条会在文档里各留两行，
    "行数 == 清单条数"这条验收判据当场失效。

    ⚠️ **只挡"重复写"，不挡"重复采"**：重跑照样会把 URL 重爬一遍
    （浪费几十分钟，但结果正确）。这是刻意的——这份文档只记结果，
    不参与"要不要爬"的决策。
    """

    def __init__(self, store: ops.OpsStore, policy: extract.SnapshotPolicy) -> None:
        self.store = store
        self.policy = policy
        self.path = store.contents_path
        self.written = 0
        rows = list(store.iter_contents())
        self._seen: set[tuple[str | None, str | None]] = {
            (row.get("content_type"), row.get("zhihu_id")) for row in rows
        }
        self.urls: set[str] = {url for row in rows if (url := row.get("url"))}
        """这份产物里已有的 URL。**喂给 `_dedup` 的那一半"文件"**——
        库里那一半只有主程序给得出来（见 `_dedup`）。"""
        if self._seen:
            log.info(
                "这份产物里已经有 %d 条了（%s），重复的不会再写一遍",
                len(self._seen),
                self.path,
            )

    def add(self, item: parse.ParsedItem, *, keyword: str | None = None) -> None:
        key = (item.content_type, item.zhihu_id)
        if key in self._seen:
            return
        self._seen.add(key)
        self.store.append_contents(
            [extract.to_document(item, policy=self.policy, keyword=keyword)]
        )
        if item.url:
            self.urls.add(item.url)
        self.written += 1


def _crawl_comments(
    session: BrowserSession,
    url: str,
    *,
    policy: extract.SnapshotPolicy,
    doc: _ContentsDoc | None = None,
    keyword: str | None = None,
) -> comments.CommentsReport:
    """采一条内容的评论并落盘。**不导航到自己，页面得已经开着。**

    页面得停在 `url` 这条内容上（能力二刚采完正文），直接点入口开弹窗。

    ⚠️ 落盘是**一条一条**写，不是全采完再写：一条热帖的评论要滚十几分钟，
    中途崩了就全没了。

    ⚠️ 交叉验证**不走 `_Batcher` 攒批**——它比的是"页面里的实体 vs 解析出的
    正文"，而评论页的 `entities` 实测是空的（见 `extract` 里 `find_entity` 的
    说明），没有可比的对象。所以这里压根不传 `page_html`。
    """
    batcher = _Batcher(
        lambda batch: _cross_check(
            batch,
            # 不传 page_html：评论没有对应的实体可比。
            what=f"{url} 的评论",
        )
    )

    def on_item(item: parse.ParsedItem) -> None:
        if doc is not None:
            doc.add(item, keyword=keyword)
        batcher.add(item)

    report = comments.extract_comments(
        session, url, on_item=on_item, reuse_open_page=True
    )
    batcher.flush()
    return report


def cmd_comments(args: argparse.Namespace) -> int:
    """能力三：取一条内容的**一级评论**并落盘。

    ⚠️ 评论的 `parent_id` 要挂到**被评论的那条内容**上，而那一跳
    （知乎 ID → `content_id`）只能靠"同一批里父级先插过"来搭。
    所以主程序入库时，正文那一行必须排在它自己的评论前面——
    这个顺序由 `contents.jsonl` 的行序保证（见 `_ContentsDoc`）。
    **这条路径单独跑时只有评论，父级多半挂不上**，会记进
    `IngestReport.dangling_parents`。它是给"补一条内容的评论"用的。

    ⚠️ 这条路径**不落盘** `contents.jsonl`：它是给"只补评论"用的，
    而那份文档记的是"这一轮采到的正文"（正文由能力二负责）。
    走 `content --with-comments` 才会既采正文又落盘。
    """
    _require_calibrated()

    targets = _targets(args)  # 先校验命令行，再碰环境（理由同 cmd_content）
    if not targets:
        raise SystemExit("❌ 没给 URL。用 --url <内容页地址>，或者 --run <任务 ID> 读清单。")

    reports: list[comments.CommentsReport] = []
    with BrowserSession(_session_config(args)) as session:
        session.ensure_logged_in()

        for entry in targets[: args.limit]:
            url = entry.url
            report = comments.extract_comments(session, url)
            reports.append(report)
            session.pace()

    log.info(comments.summarize(reports))
    return _verdict("评论", reports, lambda r: r.ok)


def _crawl_question(
    session: BrowserSession,
    question_id: str,
    mode: ops.TaskMode,
    *,
    policy: extract.SnapshotPolicy,
    doc: _ContentsDoc,
    known: Collection[str],
) -> answers.AnswersReport:
    """采一个问题下的全部回答并落盘。

    ⚠️ 单独一个函数，**不是为了好看**：`is_known` 和落盘回调都要闭包捕获
    `question_id`，如果它们直接写在 `for question_id in questions:` 里面，
    捕获的就是那个**会被下一次迭代改掉的循环变量**。现在它在一次调用的
    参数里，值在函数存续期内不会变。
    （ruff 的 B023 就是专门抓这个的，这里按它说的改。）
    """
    spec = answers.AnswerSpec(question_id, mode)
    resolved: dict[str, bool] = {}

    def is_known(url: str) -> bool:
        """这条回答已经采过了吗。早停判据（只有更新模式会用它）。

        ⚠️ 这里**不查库**（决策 52），只查传进来的那个集合。
        它要么是本次任务的 `contents.jsonl`，要么是主程序一次取全的
        `storage.existing_urls()`——见 `_dedup` 的说明。
        缓存只防同一条被问两次。
        """
        if url not in resolved:
            resolved[url] = url in known
        return resolved[url]

    batcher = _Batcher(
        lambda batch: _cross_check(
            batch,
            page_html=_page_html(session),
            what=f"问题 {question_id} 的回答",
        )
    )

    def on_item(item: parse.ParsedItem) -> None:
        """采到一条回答：落盘（文档那份）+ 攒进交叉验证那一批。

        ⚠️ 落盘是 2026-09-27 补的，对齐能力二。以前只有入库那条路，
        于是冒烟跑完**什么都不剩**——采了几十分钟、界面上一片正常、产物是空的。
        库那条路没了之后它就更是唯一产物了。
        """
        doc.add(item)
        batcher.add(item)

    report = answers.extract_answers(
        session,
        spec,
        is_known=is_known if spec.early_stop else None,
        on_item=on_item,
    )
    batcher.flush()
    return report


def cmd_answers(args: argparse.Namespace) -> int:
    """能力四：取一个问题下的全部回答并落盘。

    ⚠️ 全量 = **默认排序**（相关热度，先拿到最热门的）；
    更新 = 按时间排序 + 追上已采内容就早停。这条口径见 `answers.py` 开头。

    ## 评论**不在这里采**（2026-09-27 项目所有者定的）

    问题页上点「N 条评论」是在**回答卡片内部就地把评论区摊开**，评论多的时候
    才多出一个「点击查看全部评论」开出弹窗；单独回答页上点同一个按钮是**直接
    开弹窗**。两条路的分岔判据（`_comment_area`）在实跑里不够稳，而评论是
    这个系统里最贵的动作——不值得为了顺手把它挂在回答采集后面。

    要评论就走**能力二**（`content --with-comments`，或者 `comments` 单独补）。
    那是一条走通了的路，而且按 URL 一条条来，坏了也只坏一条。
    `comments.extract_answer_comments` 这个接口**留着**，以后要重做再说。
    """
    _require_calibrated()

    questions = list(args.question or [])  # 先校验命令行，再碰环境（理由同 cmd_content）
    if not questions:
        raise SystemExit("❌ 没给问题 ID。用 --question <知乎问题ID>，可以给多个。")

    store = _open_run(args, args.mode)
    policy = _policy()
    doc = _ContentsDoc(store, policy)

    store.set_status("running")
    reports: list[answers.AnswersReport] = []
    try:
        with BrowserSession(_session_config(args)) as session:
            session.on_status = store.set_status
            session.ensure_logged_in()

            for question_id in questions:
                report = _crawl_question(
                    session,
                    question_id,
                    args.mode,
                    policy=policy,
                    doc=doc,
                    known=doc.urls,
                )
                reports.append(report)
                log.info(report.describe())
                session.pace()
    except BaseException:
        # 崩了也要把状态写对，理由同 `cmd_search`。
        store.set_status("failed")
        raise

    store.set_status("done")
    log.info("回答落盘：%s（共 %d 行）", store.contents_path, doc.written)
    return _verdict("回答", reports, lambda r: r.ok)


def _is_probe(argv: list[str]) -> int | None:
    """`probe` 是不是这次的子命令？是就返回它在 argv 里的位置。

    ⚠️ **为什么要在 argparse 之前拦一道，而不是用 `nargs=REMAINDER`**：

    REMAINDER 碰到**以 `-` 开头**的 token 就停止捕获，而 `probe` 的
    每一个参数都是 `-` 开头的（`--check` / `--only` / `--force`…）。
    实测 `probe --list` 会被顶层解析器报成 `unrecognized arguments: --list`。
    `parse_known_args` 也不行——那会让**顶层**的拼写错误
    （比如 `content --urll`）被静默吞掉，然后拿着空清单去跑。

    所以这里只认"第一个非 `-v` 的位置参数是不是 probe"，其余原样转发给
    `probe.py` 自己的解析器。全局参数只有 `-v`，所以这个判断不会误伤。
    """
    head = [token for token in argv if token not in ("-v", "--verbose")][:1]
    if head != ["probe"]:
        return None
    return argv.index("probe")


def cmd_status(args: argparse.Namespace) -> int:
    """看一眼现在能不能跑：选择器校准状态、历次任务、库配没配。"""
    print(selectors.summary())

    ops_dir = paths().ops
    runs = ops.OpsStore.list_runs(ops_dir)
    if not runs:
        print(f"\n还没有跑过任务（{ops_dir} 是空的）。")
    else:
        print(f"\n最近的任务（{ops_dir}）：")
        for state in runs[:10]:
            print(
                f"  {state.run_id}  {state.mode:<9} {state.status:<18}"
                f" {state.cursor or ''}"
            )
        latest = runs[0]
        urls_path = ops_dir / latest.run_id / "urls.jsonl"
        if urls_path.exists():
            count = sum(1 for _ in urls_path.open(encoding="utf-8"))
            print(f"\n最近一次任务的 URL 清单：{count} 条（{urls_path}）")

    settings = Settings.from_env()
    print(
        "\n数据库："
        + ("已配置 SUPABASE_DSN" if settings.supabase_dsn else "⚠️ 没配 SUPABASE_DSN")
    )
    keywords = ops.OpsStore(ops_dir, ops.TaskState(run_id="_", mode="backfill")).load_keywords()
    print(f"关键词清单：{len(keywords)} 个" + ("" if keywords else "（还没有，见 keywords.jsonl）"))
    return 0 if not selectors.uncalibrated() else 1


# ── 内部 ────────────────────────────────────────────────────────────


def _page_html(session: BrowserSession) -> str:
    """当前页面 HTML，用来做第一档交叉验证。失败返回空串（不中断采集）。"""
    from sentinel_q.collector import drive

    try:
        return drive.page_html(session.page)
    except Exception:  # noqa: BLE001 - 拿不到页面不该中断已经采到的内容
        return ""


def _targets(args: argparse.Namespace) -> list[ops.UrlEntry]:
    """这次要对哪些 URL 动手：`--url` 直接给，或者从任务清单里读。

    返回 `UrlEntry` 而不是裸 URL，是为了让清单里的 `keyword` 一路跟到
    `contents.jsonl` 里去（人工核对时"这条是哪次搜索捞出来的"很要紧）。
    `--url` 手给的那些没有关键词，元数据全空。
    """
    targets = [ops.UrlEntry(url=u) for u in (args.url or [])]
    if args.run:
        store = ops.OpsStore.resume(paths().ops / args.run)
        targets += _entries_from(store.iter_urls())
    return targets


def _chunks(words: list[str], size: int | None) -> Iterable[list[str]]:
    """把关键词分批。分批只是为了让断点更细，不改任何采集口径。"""
    if not size:
        yield words
        return
    for index in range(0, len(words), size):
        yield words[index : index + size]


def _verdict(what: str, reports: Sequence, ok: Callable[[object], bool]) -> int:
    """按"取失败的比例"给退出码。

    ⚠️ 单条失败不让整批停：一批几百条，坏一条就全停的话那一条会永远卡在那里。
    但**失败率高到不正常时就是该停下来看的事**——那时候退出码非 0，
    外面接 CI/定时任务的人会看到。
    """
    failed = [r for r in reports if not ok(r)]
    if not reports:
        log.error("❌ %s：一条都没跑（清单是空的？）", what)
        return 1
    if failed:
        log.warning(
            "%s：%d/%d 条没采全（失败的见上方日志）。"
            "零星失败正常，**成片失败就是页面结构变了或者被限流了**。",
            what,
            len(failed),
            len(reports),
        )
    return 0 if len(failed) * 2 <= len(reports) else 1


# ── 命令行 ──────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentinel_q.collector",
        description="采集模块：知乎内容采集（四个能力各自一个子命令）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "典型顺序：\n"
            "  search  →  content  →  comments\n"
            "  问题关键词命中的，再走 answers --question <id>\n"
            "\n"
            "跑之前先看一眼状态：python -m sentinel_q.collector status\n"
        ),
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="打印调试日志（默认只打 info 及以上）",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<子命令>")

    # ── 所有要开浏览器的子命令共有 ──
    def add_browser_options(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--profile",
            default=DEFAULT_PROFILE,
            help=f"浏览器 profile 目录，登录态存在这里（默认 {DEFAULT_PROFILE}）",
        )
        target.add_argument(
            "--headless",
            action="store_true",
            help="⚠️ 别用。人工登录和处理验证码都需要看到窗口",
        )
        target.add_argument(
            "--no-stealth",
            action="store_true",
            help="关掉 navigator.webdriver 抑制。只有在排查'验证码加载不出来'时才用",
        )

    p_search = sub.add_parser(
        "search",
        help="能力一：搜关键词，滚到底，把内容 URL 写进清单",
        description=(
            "搜一个/一批关键词，按模式点筛选，滚到'没有更多了'，"
            "把每张卡片的内容 URL 追加进 runtime/ops/<run>/urls.jsonl。"
        ),
    )
    add_browser_options(p_search)
    p_search.add_argument(
        "--keyword",
        action="append",
        default=None,
        metavar="词",
        help="要搜的关键词，可以给多个（**没有默认值**，不给就报错——"
        "默认值会让忘了传参的调用静默地拿着某个词去采）",
    )
    p_search.add_argument(
        "--from-keywords",
        action="store_true",
        help="从 runtime/ops/keywords.jsonl 读关键词清单",
    )
    p_search.add_argument(
        "--mode",
        choices=["backfill", "update"],
        default="backfill",
        help=(
            "backfill=十二档组合全跑一遍（只看文章/只看回答 × 六个时间段，"
            "排序固定综合排序），慢但覆盖面广，用于首次建库；"
            "update=**只一档**（最新发布+一天内+不限类型），每天增量跑这个"
        ),
    )
    p_search.add_argument(
        "--limit",
        type=int,
        default=None,
        help="最多搜几个关键词（关键词之间要停顿，一个词可能要跑几分钟）",
    )
    p_search.add_argument("--run", default=None, help="接着这个任务跑（默认新建）")
    p_search.set_defaults(func=cmd_search)

    p_content = sub.add_parser(
        "content",
        help="能力二：按 URL 取正文（回答/文章/想法/问题）并落盘",
        description=(
            "打开每条 URL，取正文、作者、赞同数、发布时间，落快照，"
            "并把采到的东西写进这个任务的 contents.jsonl（人工核对 / 验收用）。"
            "⚠️ 这里**不入库**（决策 52）——采到的东西由主程序统一入库。"
            "已经采过的（本任务的 contents.jsonl 里有的）会在**打开浏览器之前**被挡掉。"
            "加 --with-comments 就顺带把一级评论也采下来（会点开评论弹窗）。"
        ),
    )
    add_browser_options(p_content)
    p_content.add_argument("--url", action="append", default=None, help="要采的 URL，可给多个")
    p_content.add_argument("--run", default=None, help="从这个任务的 urls.jsonl 读清单")
    p_content.add_argument(
        "--limit", type=int, default=None, help="最多采几条（一条要几十秒，先试几条）"
    )
    p_content.add_argument(
        "--with-comments",
        action="store_true",
        help=(
            "顺带采这条内容的一级评论。**不加就完全不点评论按钮**——"
            "评论是整个系统里最贵的动作（开弹窗、滚几百下），"
            "所以它默认关着，由调用方按条件决定开不开。"
            "⚠️ 二级回复不采是**定下来的取舍**，不是还没做完，见 comments.py 开头"
        ),
    )
    p_content.set_defaults(func=cmd_content)

    p_comments = sub.add_parser(
        "comments",
        help="能力三：取一条内容的一级评论并落盘",
        description=(
            "点开评论弹窗，滚到底，把一级评论取下来。"
            "⚠️ 二级回复**不采**（关不掉面板、每点一次要重开页面等很久，"
            "见 comments.py 模块开头）。声明总数只作参考，不作通过标准。"
        ),
    )
    add_browser_options(p_comments)
    p_comments.add_argument("--url", action="append", default=None, help="内容页 URL，可给多个")
    p_comments.add_argument("--run", default=None, help="从这个任务的 urls.jsonl 读清单")
    p_comments.add_argument(
        "--limit", type=int, default=None, help="最多采几条内容的评论（一条可能要十几分钟）"
    )
    p_comments.set_defaults(func=cmd_comments)

    p_answers = sub.add_parser(
        "answers",
        help="能力四：取一个问题下的全部回答并落盘",
        description=(
            "打开问题页，按模式选排序（**必须点，知乎默认不是我们要的**），"
            "滚到采不动为止。全量=默认排序；更新=按时间+追上旧内容早停。"
            "采到的东西写进这个任务的 contents.jsonl（人工核对 / 验收用），"
            "**这里不入库**（决策 52）。"
            "⚠️ **评论不采**：要走能力二（content --with-comments / comments）。"
        ),
    )
    add_browser_options(p_answers)
    p_answers.add_argument(
        "--question",
        action="append",
        default=None,
        metavar="ID",
        help="知乎问题 ID（URL 里 /question/ 后面那串），可给多个",
    )
    p_answers.add_argument(
        "--mode",
        choices=["backfill", "update"],
        default="backfill",
        help="backfill=默认排序全量采；update=按时间排序 + 追上已采内容就早停",
    )
    p_answers.add_argument(
        "--run",
        default=None,
        help="接着这个任务跑（默认新建）。contents.jsonl 会追加到那个任务里",
    )
    p_answers.set_defaults(func=cmd_answers)

    # ⚠️ 这个子命令**永远不会被 argparse 解析到**——`main()` 在解析之前
    #    就把 probe 之后的参数原样转发给 `probe.py` 了（见 `_is_probe`）。
    #    注册它只是为了让它出现在 `--help` 的子命令列表里。
    #    参数不在这里重复声明：probe 有十几个参数，抄一份到这里
    #    就是两份会各自漂移的真相。
    sub.add_parser(
        "probe",
        help="校准辅助：采一批真实页面快照（参数见 probe --help）",
        add_help=False,
    )

    p_status = sub.add_parser(
        "status",
        help="看一眼现在能不能跑：选择器校准状态、历次任务、库配没配",
    )
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    index = _is_probe(argv)
    if index is not None:
        rest = argv[index + 1 :]
        # 一个参数都不给就打印 probe 自己的帮助，**不要直接开浏览器采全套**：
        # 那是十几分钟的事，不该由一个手滑的命令触发。
        return probe.main(rest or ["--help"])

    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    paths().ensure()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
