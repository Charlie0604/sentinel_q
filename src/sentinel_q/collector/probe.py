"""校准辅助：把真实页面的 HTML 采下来当测试固件。

## 为什么需要它

`selectors.py` 里现在每一条都带 `TODO-` 前缀，意思是"猜的、没验证过"。
猜的原因是：写这个爬虫的人（我）没有知乎账号，也看不到真实页面。
**校准的判据只能是对着真实页面的 HTML 逐个改。**

手工采很烦，而且烦在关键处：有几个状态必须"点一下、滚一会儿、再点一下"
才能到达（筛选生效后、评论弹窗滚出多页、问题页切成按最新）。手抄很容易
抄成"点之前"的样子，那校准出来的选择器就是错的——而且是**看起来对了**
的那种错。

所以这个小工具替你点、替你滚，每一步 dump 一份 HTML。

## 它刻意不做什么

**完全不引用 `selectors.py`。** 这里所有定位都靠文案（"最新"、"查看全部评论"），
因为用未经校准的选择器来驱动校准工具是循环论证——选不中元素就什么都采不到，
而你还以为是页面结构变了。

目标 URL 从哪来：搜索页那份 HTML 里扒。分类用的是 `shared.urlnorm.normalize`
（这个模块有测试覆盖、是可信的），所以**不依赖任何猜测的选择器**。

## 它跑起来会做什么

打开一个真实浏览器 → 如果是未登录状态就停下来等你登录 → 依次走 11 个状态 →
每个状态往 `runtime/calib/` 写一个 .html → 最后打印一张表告诉你哪些成了、
哪些没成、没成的该补什么参数。

⚠️ 采到的快照里会有你自己的昵称、头像、主页链接。`runtime/` 在 .gitignore 里
（允许列表式，见文件头注释），不会进 git——但别手动拷到别处去。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

from bs4 import BeautifulSoup

from sentinel_q.collector import drive
from sentinel_q.collector.session import (
    BrowserSession,
    SessionConfig,
    is_logged_in,
    login_diagnostics,
)
from sentinel_q.shared.urlnorm import normalize

HOME_URL = "https://www.zhihu.com"
SEARCH_URL = "https://www.zhihu.com/search?type=content&q={query}"

# 搜索"必定没有结果"的串。**写死而不是随机生成**：固件要可复现，
# 而且这个页面是用来对照"选择器失效"和"真的没结果"的，每次都该长得一样。
NO_RESULT_QUERY = "zzqqxx9988776qqzz"

# 点击最多滚多少轮。够加载出几十条评论，又不会真的一直滚下去。
MAX_SCROLL_ROUNDS = 30

END_MARKERS = ("没有更多了", "没有更多内容", "已经到底了")


@dataclass
class Result:
    """一份快照的采集结果。"""

    name: str
    what: str
    ok: bool = False
    size: int = 0
    note: str = ""
    need_flag: str = ""
    """没采成时要补的命令行参数，比如 `--thought-url`。"""


@dataclass
class Context:
    """一路上攒下来的东西：搜索页扒到的链接，以及用户显式给的 URL。"""

    keyword: str
    answer_url: str | None = None
    article_url: str | None = None
    thought_url: str | None = None
    question_url: str | None = None
    derived: dict[str, str] = field(default_factory=dict)
    """从搜索页自动扒到的候选（可能为空——比如搜索页里根本没有想法）。"""

    def target(self, kind: str) -> str | None:
        """取某一类目标的 URL：**显式给的优先**，否则用自动扒到的。"""
        explicit = {
            "answer": self.answer_url,
            "article": self.article_url,
            "thought": self.thought_url,
            "question": self.question_url,
        }[kind]
        return explicit or self.derived.get(kind)


# ── 入口 ────────────────────────────────────────────────────────────

SNAPSHOT_ORDER = (
    "search_initial",
    "search_filtered",
    "search_bottom",
    "answer",
    "article",
    "thought",
    "comments_collapsed",
    "comments_modal",
    "question_default",
    "question_newest",
    "search_noresult",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sentinel_q.collector.probe",
        description="采一批真实页面 HTML 当选择器校准固件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例子：\n"
            "  python -m sentinel_q.collector.probe --keyword 某某公司\n"
            "  python -m sentinel_q.collector.probe --keyword 某某公司 \\\n"
            "      --thought-url https://www.zhihu.com/pin/1234567890\n"
            "  python -m sentinel_q.collector.probe --only comments_modal --force\n"
        ),
    )
    parser.add_argument(
        "--keyword",
        default="测试",
        help=(
            "用来采搜索页的关键词。默认故意用一个中性词——"
            "真实监测关键词的敏感度和提示词同级（文档 7.7），校准用不上真的"
        ),
    )
    parser.add_argument("--out", default="runtime/calib", help="快照输出目录")
    parser.add_argument(
        "--profile",
        default="secrets/browser-profile",
        help="浏览器 profile 目录（登录态就存在这里）",
    )
    parser.add_argument("--answer-url", default=None, help="回答页 URL（不传就试着从搜索页扒）")
    parser.add_argument("--article-url", default=None, help="专栏文章 URL（同上）")
    parser.add_argument("--thought-url", default=None, help="想法（pin）URL——搜索页多半扒不到，建议直接给")
    parser.add_argument("--question-url", default=None, help="问题页 URL（不传就从回答页推）")
    parser.add_argument("--only", default=None, help="只采这几项，逗号分隔")
    parser.add_argument("--force", action="store_true", help="已存在的快照也重采")
    parser.add_argument("--list", action="store_true", help="只列清单，不启动浏览器")
    parser.add_argument(
        "--check",
        action="store_true",
        help="体检：开浏览器访问一次知乎，报告能不能用，不采任何快照",
    )
    return parser


def check(session: BrowserSession) -> int:
    """体检：这个浏览器 + profile 现在到底能不能用。

    为什么单独一个子命令：完整采集要跑十几分钟，而"能不能用"这个判断
    只需要一次访问。调试反爬问题时等的越短越好——不然你会在一个根本
    走不通的配置上反复跑完整流程。

    ⚠️ 它走的是**采集时真正走的那条路**（`guard()` 等验证码、`ensure_logged_in()`
    等登录），所以撞上拦截页或未登录时会**停下来等你**，不会自己跳过。
    这正是要验的东西：那两条人工等待路径到底通不通。

    刻意**不用 `session.open()`**——它内部就会调 `guard()`，那样第一份状态
    报告永远看不到拦截页（早被等掉了）。分步走才能看见每一步的原始样子。
    """
    print("\n[1/3] 访问知乎首页，先看原始状态（不触发人工等待）")
    session.page.goto(HOME_URL, wait_until="domcontentloaded")
    session.settle()
    _print_state(session)

    print("\n[2/3] 走真实的验证码路径（撞上就停下来等你手动过）")
    session.guard()
    _print_state(session)

    print("\n[3/3] 走真实的登录检查路径（没登录就停下来等你登录）")
    print(login_diagnostics(session.page))
    session.ensure_logged_in()
    _print_state(session)

    print("\n体检结束。上面三份状态里：webdriver 是 False、最后一份是「已登录」，就可以跑采集了。")
    return 0


def _print_state(session: BrowserSession) -> None:
    from sentinel_q.collector import selectors

    try:
        webdriver = session.page.evaluate("() => navigator.webdriver")
    except Exception:
        webdriver = "?"

    interstitial = drive.has_text(session.page, selectors.RISK_INTERSTITIAL_TEXTS)
    logged_in = is_logged_in(session.page)
    size_kb = len(drive.page_html(session.page)) // 1024

    print(
        f"    webdriver={webdriver}  "
        f"登录={'✓' if logged_in else '✗'}  "
        f"拦截页={'⚠️ 是' if interstitial else '否'}  "
        f"{size_kb}KB  {session.page.url}"
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    wanted = _wanted(args.only)
    if args.list:
        _print_plan(wanted)
        return 0
    if args.check:
        with BrowserSession(SessionConfig(profile_dir=Path(args.profile))) as session:
            return check(session)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    ctx = Context(
        keyword=args.keyword,
        answer_url=args.answer_url,
        article_url=args.article_url,
        thought_url=args.thought_url,
        question_url=args.question_url,
    )
    results: list[Result] = []

    config = SessionConfig(profile_dir=Path(args.profile))
    print(f"启动浏览器（profile：{args.profile}）……")
    with BrowserSession(config) as session:
        session.ensure_logged_in()

        for name in SNAPSHOT_ORDER:
            if name not in wanted:
                continue
            path = out_dir / f"{name}.html"
            if path.exists() and not args.force:
                results.append(
                    Result(name, _WHAT[name], ok=True, size=path.stat().st_size, note="已存在，跳过")
                )
                continue

            print(f"\n=== 采 {name} —— {_WHAT[name]} ===")
            result = _COLLECTORS[name](session, path, ctx)
            results.append(result)
            print(f"    {'✓' if result.ok else '✗'} {result.note}")

    _report(results, out_dir, ctx)
    return 0 if all(r.ok for r in results) else 1


# ── 各个状态的采法 ──────────────────────────────────────────────────


def _search_initial(session: BrowserSession, path: Path, ctx: Context) -> Result:
    """搜索页，**什么都别点**——这是"筛选前"的对照组。"""
    session.open(SEARCH_URL.format(query=ctx.keyword))
    session.settle()
    _harvest_links(session, ctx)
    return _dump(session, path, "search_initial")


def _search_filtered(session: BrowserSession, path: Path, ctx: Context) -> Result:
    """点「最新」+「不限」之后。**这一份是用来对照激活态的**，别跳过。"""
    session.open(SEARCH_URL.format(query=ctx.keyword))
    session.settle()
    clicked_newest = drive.click_text(session.page, ("最新",))
    session.guard()
    session.settle()
    clicked_time = drive.click_text(session.page, ("不限",))
    session.guard()
    session.settle()
    _harvest_links(session, ctx)

    note = f"点了「最新」={bool(clicked_newest)}、「不限」={bool(clicked_time)}"
    return _dump(session, path, "search_filtered", note=note)


def _search_bottom(session: BrowserSession, path: Path, ctx: Context) -> Result:
    """滚到底，采「没有更多了」出现时的状态。

    滚到文案出现或达到轮数上限为止，两种结局都会 dump——**因为"滚到底了
    但没看见那句文案"本身就是要拿来校准的信息**（说明我们对末尾标记的假设错了）。
    """
    session.open(SEARCH_URL.format(query=ctx.keyword))
    session.settle()
    rounds, hit = _scroll_until_end(session)
    return _dump(
        session,
        path,
        "search_bottom",
        note=f"滚了 {rounds} 轮，{'看到末尾文案' if hit else '⚠️ 没看到末尾文案（可能还在加载，或假设错了）'}",
    )


def _answer(session: BrowserSession, path: Path, ctx: Context) -> Result:
    return _content_page(session, path, ctx, "answer", "回答页")


def _article(session: BrowserSession, path: Path, ctx: Context) -> Result:
    return _content_page(session, path, ctx, "article", "专栏文章页")


def _thought(session: BrowserSession, path: Path, ctx: Context) -> Result:
    """想法（pin）。URL 规则已确认（2026-09-26 对着真实快照实测，见
    `shared/urlnorm.py` 的 `_PIN_RE`），但页面能不能从 `js-initialData` 提取
    还没验过——`thought` 至今不在 `extract.TIER1_VERIFIED` 里。这一份的任务
    就是把它提上去，否则想法只能采到列表页的元数据、采不到正文。"""
    return _content_page(session, path, ctx, "thought", "想法页")


def _comments_collapsed(session: BrowserSession, path: Path, ctx: Context) -> Result:
    """正文页滚到评论区，**先别点开弹窗**——「查看全部评论」按钮的原始样子。"""
    url = ctx.target("answer")
    if not url:
        return _missing("comments_collapsed", "answer", "回答页 URL")
    session.open(url)
    session.settle()
    _scroll_until_end(session, max_rounds=8)  # 滚到评论区露出来即可
    return _dump(session, path, "comments_collapsed")


def _comments_modal(session: BrowserSession, path: Path, ctx: Context) -> Result:
    """点开评论弹窗，在**弹窗里**滚出多页，并把二级回复展开。

    滚动用 `_scroll_every_scrollable`：我不确定弹窗是哪个元素，与其猜，
    不如把页面上所有能滚的东西都滚到底——这份固件要的是"评论加载完了"的样子。
    """
    url = ctx.target("answer")
    if not url:
        return _missing("comments_modal", "answer", "回答页 URL")
    session.open(url)
    session.settle()
    _scroll_until_end(session, max_rounds=8)

    clicked = drive.click_text(session.page, ("查看全部评论", "条评论", "评论"))
    if clicked is None:
        return Result(
            "comments_modal",
            _WHAT["comments_modal"],
            ok=False,
            note="⚠️ 没找到任何评论按钮——页面结构可能和我们想的不一样，值得看一眼",
        )
    session.pace()
    session.settle()

    rounds = _scroll_every_scrollable(session)
    expanded = drive.click_all_text(session.page, ("展开回复", "条回复"))
    session.settle()
    rounds += _scroll_every_scrollable(session)

    return _dump(
        session,
        path,
        "comments_modal",
        note=f"点中「{clicked}」，滚动 {rounds} 轮，展开回复 {expanded} 次",
    )


def _question_default(session: BrowserSession, path: Path, ctx: Context) -> Result:
    """问题页，**保持默认排序**（相关热度）——这是"点最新之前"的对照组。"""
    url = ctx.target("question")
    if not url:
        return _missing("question_default", "question", "问题页 URL")
    session.open(url)
    session.settle()
    return _dump(session, path, "question_default")


def _question_newest(session: BrowserSession, path: Path, ctx: Context) -> Result:
    """问题页点了「最新」。这一份回答"激活态长什么样"——能力四靠它断言点击生效。"""
    url = ctx.target("question")
    if not url:
        return _missing("question_newest", "question", "问题页 URL")
    session.open(url)
    session.settle()
    clicked = drive.click_text(session.page, ("最新",))
    session.guard()
    session.settle()
    return _dump(
        session,
        path,
        "question_newest",
        note=f"点中「最新」={bool(clicked)}（没点中就是页面上的文案不叫这个）",
    )


def _search_noresult(session: BrowserSession, path: Path, ctx: Context) -> Result:
    """搜一个必定没结果的词。

    ⚠️ 这一份别省：它是**校准期唯一能区分"选择器失效"和"真的没结果"的对照**。
    没有它，"采到 0 条"到底是改版了还是本来就没有，永远说不清。
    """
    session.open(SEARCH_URL.format(query=NO_RESULT_QUERY))
    session.settle()
    return _dump(session, path, "search_noresult")


_COLLECTORS = {
    "search_initial": _search_initial,
    "search_filtered": _search_filtered,
    "search_bottom": _search_bottom,
    "answer": _answer,
    "article": _article,
    "thought": _thought,
    "comments_collapsed": _comments_collapsed,
    "comments_modal": _comments_modal,
    "question_default": _question_default,
    "question_newest": _question_newest,
    "search_noresult": _search_noresult,
}

_WHAT = {
    "search_initial": "搜索页·未点筛选（对照组）",
    "search_filtered": "搜索页·点过「最新+不限」（对照激活态）",
    "search_bottom": "搜索页·滚到底（找「没有更多了」）",
    "answer": "回答页",
    "article": "专栏文章页",
    "thought": "想法页（顺便定 pin 的 URL 规则）",
    "comments_collapsed": "评论区·未点开（对照组）",
    "comments_modal": "评论弹窗·已滚多页+展开二级回复",
    "question_default": "问题页·默认排序（对照组）",
    "question_newest": "问题页·点过「最新」（对照激活态）",
    "search_noresult": "搜索页·无结果（对照「选择器失效」）",
}


# ── 内部工具 ────────────────────────────────────────────────────────


def _content_page(
    session: BrowserSession, path: Path, ctx: Context, kind: str, what: str
) -> Result:
    url = ctx.target(kind)
    if not url:
        return _missing(kind, kind, f"{what} URL")
    session.open(url)
    session.settle()
    expanded = drive.click_all_text(session.page, ("阅读全文", "展开阅读全文"))
    session.settle()
    note = f"展开「阅读全文」{expanded} 次" if expanded else ""
    if kind == "thought":
        note = (note + "；" if note else "") + "用来定 pin 的 URL 规则"
    return _dump(session, path, kind, note=note)


def _missing(name: str, kind: str, what: str) -> Result:
    flag = f"--{kind}-url"
    return Result(
        name,
        _WHAT[name],
        ok=False,
        note=f"采不了：不知道{what}。传 {flag} <url> 再跑一次",
        need_flag=flag,
    )


def _dump(session: BrowserSession, path: Path, name: str, note: str = "") -> Result:
    """写文件。**空文件和没采到要分开报**——空 HTML 当固件用会得出错误结论。"""
    html = drive.page_html(session.page)
    if len(html) < 200:
        return Result(name, _WHAT[name], ok=False, note=f"⚠️ 页面几乎是空的（{len(html)} 字节）")
    path.write_text(html, encoding="utf-8")
    size = len(html.encode("utf-8"))
    return Result(name, _WHAT[name], ok=True, size=size, note=f"{note}；{size // 1024} KB".lstrip("；"))


def _scroll_until_end(
    session: BrowserSession, *, max_rounds: int = MAX_SCROLL_ROUNDS
) -> tuple[int, bool]:
    """滚到出现末尾文案，或滚够轮数。返回 `(轮数, 是否看到文案)`。"""
    stable = 0
    prev_height = 0
    for round_no in range(1, max_rounds + 1):
        if drive.has_text(session.page, END_MARKERS):
            return round_no - 1, True
        drive.scroll_bottom(session.page)
        session.pace(0.4)
        try:
            height = session.page.evaluate("() => document.body.scrollHeight")
        except Exception:
            height = prev_height
        stable = stable + 1 if height == prev_height else 0
        prev_height = height
        if stable >= 3:
            return round_no, drive.has_text(session.page, END_MARKERS)
    return max_rounds, drive.has_text(session.page, END_MARKERS)


_SCROLL_ALL = """() => {
  const touched = [];
  document.querySelectorAll('*').forEach(el => {
    const s = getComputedStyle(el);
    const scrollable = s.overflowY === 'auto' || s.overflowY === 'scroll';
    if (scrollable && el.scrollHeight > el.clientHeight + 40) {
      el.scrollTop = el.scrollHeight;
      touched.push(el.tagName + '.' + (el.className || ''));
    }
  });
  window.scrollTo(0, document.body.scrollHeight);
  return touched.length;
}"""


def _scroll_every_scrollable(session: BrowserSession, *, rounds: int = 12) -> int:
    """把所有**能滚的元素**都滚到底，滚几轮。

    为什么这么粗暴：评论弹窗滚的是弹窗元素而不是窗口（`window.scrollBy` 对它
    无效）。与其猜弹窗的选择器（猜错就什么都加载不出来），不如全滚一遍——
    这份固件要的是"评论已经加载完了"的状态，不是"证明弹窗是哪个元素"。
    弹窗到底是哪个，看固件里的 DOM 就知道。
    """
    total = 0
    for _ in range(rounds):
        try:
            session.page.evaluate(_SCROLL_ALL)
        except Exception:
            break
        session.pace(0.3)
        total += 1
    return total


def _harvest_links(session: BrowserSession, ctx: Context) -> None:
    """从当前页面把所有内容链接扒出来，按 urlnorm 分类存进 ctx.derived。

    **用 urlnorm 而不是选择器**：urlnorm 有测试覆盖、是可信的分类权威；
    选择器目前全是猜测。用猜的东西驱动校准工具是循环论证。
    """
    html = drive.page_html(session.page)
    if not html:
        return
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.select("a[href]"):
        normalized = normalize(str(anchor.get("href")))
        if normalized is None or normalized.content_type is None:
            continue
        ctx.derived.setdefault(normalized.content_type, normalized.url)
    # 问题页从回答页推：/question/123/answer/456 → /question/123
    if "question" not in ctx.derived and "answer" in ctx.derived:
        head = ctx.derived["answer"].split("/answer/")[0]
        ctx.derived["question"] = head


def _wanted(only: str | None) -> set[str]:
    if not only:
        return set(SNAPSHOT_ORDER)
    names = {part.strip() for part in only.split(",") if part.strip()}
    unknown = names - set(SNAPSHOT_ORDER)
    if unknown:
        raise SystemExit(f"不认识这几项：{', '.join(sorted(unknown))}\n可选：{', '.join(SNAPSHOT_ORDER)}")
    return names


def _print_plan(wanted: set[str]) -> None:
    print("将要采集：")
    for name in SNAPSHOT_ORDER:
        if name in wanted:
            print(f"  {name:22s} {_WHAT[name]}")
    print("\n需要登录态。未登录时会停下来等你，进程不会退出。")


def _report(results: list[Result], out_dir: Path, ctx: Context) -> None:
    print("\n" + "=" * 72)
    print(f"快照目录：{out_dir}")
    print("=" * 72)

    done = [r for r in results if r.ok]
    failed = [r for r in results if not r.ok]
    for result in results:
        mark = "✓" if result.ok else "✗"
        print(f" {mark} {result.name:22s} {result.note}")

    print(f"\n成功 {len(done)} / {len(results)}")
    if failed:
        print("\n没采到的：")
        for result in failed:
            print(f"  - {result.name}：{result.note}")

    # 自动扒到的链接回显出来——用户能据此判断要不要手动补 --thought-url
    if ctx.derived:
        print("\n从页面里自动扒到的内容链接：")
        for kind, url in sorted(ctx.derived.items()):
            print(f"  {kind:10s} {url}")

    if failed:
        print(
            "\n补采例子（采完的会自动跳过，只补缺的那几个）：\n"
            f"  python -m sentinel_q.collector.probe --keyword {ctx.keyword} "
            "--thought-url https://www.zhihu.com/pin/<id>"
        )


if __name__ == "__main__":
    sys.exit(main())
