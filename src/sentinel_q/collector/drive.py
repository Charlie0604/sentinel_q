"""浏览器侧通用动作：四个能力模块共用的一小层。

## 为什么要单独一层

`scrolling.py` 刻意不 import playwright（那样才能用假对象测），只接收
`scroll` / `at_end` / `collect` 三个回调。**这一层就是写那三个回调的地方**，
顺便把"按文案点按钮""把元素 outerHTML 收下来"这类四个能力都要用的动作收拢。

不这么分的话，`search.py` / `answers.py` / `comments.py` 会各写一份
几乎一样的滚动循环，然后各修各的 bug。

## 一个反复出现的坑：滚动目标不一定是窗口

搜索页、问题页滚的是窗口；**评论弹窗滚的是弹窗元素本身**——
`window.scrollBy` 对它完全无效，页面看起来"滚不动"，然后滚动循环会
一路空转到 max_rounds，报一个看起来像"知乎改版了"的错误。
所以下面所有接收 `target` 的函数都同时接受 Page 和 ElementHandle。
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterable
from typing import Any

from sentinel_q.collector import selectors

# Playwright 的 ElementHandle.evaluate 要求一个函数表达式。
#
# ⚠️⚠️ **下面几个滚动脚本都写成"双形态"的，这不是风格问题，是修 bug。**
# Playwright 在 Page 上把我们的参数当第一个参数，在 ElementHandle 上把
# **元素本身**当第一个参数、我们的参数当第二个。写死成 `el => …` 的那种：
#   * 传给 Page     → `el` 是 undefined，`el.scrollTop` 抛异常（至少不静默）；
#   * 写成 `(px) => window.scrollBy(0, px)` 传给元素 →
#     `px` 绑到了元素上，`scrollBy(0, <元素>)` **不报错、也不滚**。
# 后者最要命：**评论弹窗滚的就是元素**，而它会安静地一次都不滚，
# 然后滚动循环空转到 max_rounds，报一个看起来像"知乎改版了"的错。
_SCROLL_TO_BOTTOM = """
(a) => {
  const isEl = a && a.nodeType === 1;
  if (isEl) { a.scrollTop = a.scrollHeight; return; }
  window.scrollTo(0, document.body.scrollHeight);
}
"""
_SCROLL_TOP = """
(a) => {
  const isEl = a && a.nodeType === 1;
  const el = isEl ? a : document.scrollingElement;
  if (el) el.scrollTop = 0;
}
"""
# 一次调用收完整批，顺便按需滤掉"有同类祖先"的嵌套节点。见 Harvester.roots_only。
_HARVEST = """
(a, b) => {
  // 双形态，同 _FIND_BY_TEXT
  const isEl = a && a.nodeType === 1;
  const [sel, rootsOnly] = isEl ? b : a;
  const root = isEl ? a : document;
  const all = Array.from(root.querySelectorAll(sel));
  const picked = !rootsOnly ? all : all.filter((el) => {
    // 「没有匹配同一选择器的祖先」= 树里的根。评论的二级回复是**嵌套**在
    // 父评论里的，不用这条判据就会把两级拍平（见 parse._root_comments）。
    let p = el.parentElement;
    while (p) {
      if (p.matches && p.matches(sel)) return false;
      p = p.parentElement;
    }
    return true;
  });
  return picked.map((el) => el.outerHTML);
}
"""
# 数一个字数的当前长度，给"等渲染完"的轮询用。见 text_length()。
_TEXT_LENGTH = """
(a, b) => {
  // 双形态，同 _HARVEST
  const isEl = a && a.nodeType === 1;
  const [cands, scope] = isEl ? b : a;
  // scope（"哪张卡片"）只可能从文档根上找
  const root = scope ? document.querySelector(scope) : (isEl ? a : document);
  if (!root) return -1;
  for (const c of cands) {
    const el = root.querySelector(c);
    if (el) return (el.textContent || "").length;
  }
  return 0;
}
"""
# 窗口滚动没有 el 参数


# ── 查询 ────────────────────────────────────────────────────────────


def first(target: Any, selector: str) -> Any | None:
    """取第一个匹配的元素；选择器为空/非法时返回 None 而不是抛异常。

    校准期间选择器写错是常态，这里让它退化成"没找到"，由调用方统计并报出来，
    不要在半路抛异常打断整轮采集。
    """
    try:
        return target.query_selector(selectors.css(selector))
    except Exception:
        return None


def pick(target: Any, selector: str) -> Any | None:
    """按**候选书写顺序**逐个试（= 3.11 的稳定性优先级），返回第一个命中的。

    浏览器侧要用这个，不要用 `first`——`first` 把整组交给 Playwright，
    命中的是**文档里靠前**的元素而不是我们字面上靠前的候选。
    """
    for candidate in selectors.candidates(selector):
        el = first(target, candidate)
        if el is not None:
            return el
    return None


def all_of(target: Any, selector: str) -> list[Any]:
    try:
        return target.query_selector_all(selectors.css(selector))
    except Exception:
        return []


def element_text(el: Any) -> str | None:
    """取一个**已经在手上的元素**的可见文案，取不到返回 None。

    和 `text_of(target, selector)` 的分工：那个是"按选择器找元素再取文案"，
    这个是"元素已经拿到了，只要文案"。按组读筛选状态时需要后者——
    组是 `all_of` 拿到的，组内的激活项得在**那个组**里找。
    """
    try:
        text = (el.inner_text() or "").strip()
    except Exception:
        return None
    return text or None


def text_of(target: Any, selector: str) -> str | None:
    el = pick(target, selector)
    if el is None:
        return None
    return element_text(el)


def text_length(target: Any, selector: str, *, scope: str | None = None) -> int:
    """正文容器**此刻有多少个字**。一次 JS 往返，专给"等渲染完"的轮询用。

    返回值三态，调用方必须分清：

      * `-1` —— 给了 `scope` 但那张卡片还不存在（页面还没渲染到目标内容）
      * `0`  —— 卡片在，候选一个都没命中（正文容器还没挂上）
      * `>0` —— 命中了，这是它的字数

    ⚠️ **按候选书写顺序挨个试、命中即返回，和 `parse._pick()` 逐字对齐**，
    包括"第一个候选命中了但是空的"也照样返回。两边不同源的话，
    会出现"这里说渲染好了、解析层却取到空的"这种最难查的错。

    ⚠️ 读 `textContent` 而不是 `innerText`：解析层用的是 BeautifulSoup 的
    `get_text()`，那个不看可见性，两边才对得上。

    ⚠️ 选择器非法时返回 `0`（同 `first()`）——校准期写错选择器是常态，
    不该在轮询里抛异常打断整轮采集。代价是那种情况会一路等到超时，
    然后由解析层的报错收尾。
    """
    try:
        cands = [selectors.css(c) for c in selectors.candidates(selector)]
        return int(target.evaluate(_TEXT_LENGTH, [cands, scope]))
    except Exception:
        return 0


def active_text(target: Any, selector: str) -> str | None:
    """取"当前激活的"标签文案，用来验证筛选/排序点击真的生效了。

    知乎用 `--active` 类或 `aria-selected` 标记激活项，两种都试。
    """
    # 用 candidates() 而不是 css().split()：要保持书写顺序（= 稳定性优先级）
    for candidate in selectors.candidates(selector):
        el = first(target, candidate)
        if el is not None:
            text = element_text(el)
            if text:
                return text
    return None


# ── 文案匹配 ────────────────────────────────────────────────────────


def has_text(target: Any, texts: Iterable[str]) -> bool:
    """页面上（或某个容器里）有没有出现这些文案中的任意一个。

    这是滚动循环的**显式终止条件**——"没有更多了"出现了就停。
    只认文案是不够的（文案变了会空转到 max_rounds），所以 scrolling.py
    另外有"连续 N 轮无新增"兜底，两层都要。
    """
    for text in texts:
        try:
            if target.query_selector(f"text={text}") is not None:
                return True
        except Exception:
            continue
    return False


def click_text(
    target: Any,
    texts: Iterable[str],
    *,
    timeout: int = 5000,
    scope: str | None = None,
) -> str | None:
    """按文案点按钮，返回点中的那个文案；都没点着返回 None。

    ⚠️ 返回 None 是**必须被调用方处理**的信号，不是可以忽略的返回值。
    能力四靠它决定"排序没切换成功，要么重试要么报错停下"——
    静默地继续跑会得到一份按相关热度排的全量数据，看起来完全正常。

    两条路，先精确后兜底：

        1. `button:has-text(…)` —— 真的按钮，最干净
        2. `find_by_text(…)`   —— 文案落在 `<div>` / `<span>` 上时兜底

    ## ⚠️ 为什么第 2 条不能写成 `:has-text(…)`

    旧版第二路是 `target.query_selector(":has-text('…')")`，**它会点错元素。**
    Playwright 里裸的 `:has-text()` 等价于 `*:has-text(…)`，匹配的是
    "**任意**包含这段文字的元素"，按文档顺序返回第一个——那是
    `<html>` 或 `<body>`。`element.click()` 对它们会点**视口正中**，
    等于在页面中间随机点一下。

    这个坑实际踩到过：搜索页的筛选入口是个 `<div>`（见
    `selectors.SEARCH_FILTER_ENTRY`），于是 probe 报「点了最新=False」——
    不是页面上没有，是点在了 `<body>` 上，而且**不报错**。

    `scope` 传一个 CSS 选择器可以把两条路都限制在某个容器里，
    解决"页面上有 4 个「N 条评论」按钮，该点哪一个"这类问题。
    """
    for text in texts:
        prefix = f"{scope} " if scope else ""
        el = _first_safe(target, f"{prefix}button:has-text('{text}')")
        if el is not None and _click_quietly(el, timeout):
            return text
        found = find_by_text(target, text, scope=scope)
        if found is not None and _click_quietly(found, timeout):
            return text
    return None


_FIND_BY_TEXT = """
(a, b) => {
  // ⚠️ **双形态**：Playwright 在 Page 上只传一个参数，在 ElementHandle 上
  //    把元素本身当第一个参数、我们的参数当第二个。写死成 `(args)` 的话，
  //    传元素进来时 `args` 就是那个元素，`args[0]` 是 undefined，
  //    整个查找静默失效；而更早的版本里它还会退化成**搜整个 document**——
  //    于是"在某个容器里找文案"变成了"在整页找文案"，点到别的元素上，
  //    而且不报错。所以这里显式区分两种调用。
  const isEl = a && a.nodeType === 1;
  const [text, exact, scopeSel] = isEl ? b : a;
  // 归一化：零宽字符 + 空白塌缩。
  // ⚠️ 知乎的按钮文案前面挂着一个 U+200B（零宽空格）——实测
  //    「9 条评论」那个按钮的 innerText 是 "\\u200b 9 条评论"。
  //    不剥掉的话，任何精确比对都会失败，而失败在这里表现为"没找到按钮"，
  //    不是报错。见 selectors.COMMENT_ENTRY_BUTTON 那段实测记录。
  const norm = (s) => (s || '')
    .replace(/[\\u200b-\\u200d\\ufeff]/g, '')
    .replace(/\\s+/g, ' ')
    .trim();
  const want = norm(text);
  if (!want) return null;

  const root = isEl ? a
    : (scopeSel ? document.querySelector(scopeSel) : document.body);
  if (!root) return null;

  // 走文本节点而不是 document.querySelectorAll('*')：大页面（large_question.html
  // 有 89 张卡片、两万多个元素）上后者要把每个元素的 textContent 全拼一遍。
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const exactHits = [];
  const looseHits = [];
  let node;
  while ((node = walker.nextNode())) {
    const t = norm(node.nodeValue);
    if (!t) continue;
    const el = node.parentElement;
    if (!el) continue;
    if (t === want) exactHits.push(el);
    else if (!exact && t.includes(want)) looseHits.push(el);
  }
  // 精确命中永远优先于包含命中：「默认排序」和「默认排序（推荐）」同时在页面上时，
  // 选前者。
  const pool = exactHits.length ? exactHits : looseHits;
  if (!pool.length) return null;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  return pool.find(visible) || pool[0];
}
"""


def find_by_text(
    target: Any, text: str, *, exact: bool = True, scope: str | None = None
) -> Any | None:
    """找到**文案所在的那个元素**——最内层的、可见的那个。找不到返回 None。

    文本节点 → `parentElement`，天然就是"最内层"，不会像 `:has-text()`
    那样退回到 `<body>`。同文案有多处时优先返回**可见**的那个：
    知乎的下拉选项一直留在 DOM 里（关着的时候也在），不可见的不该被点到。

    `exact=True` 是精确比对（归一化空白与零宽字符之后）；`exact=False`
    是包含比对，给「条评论」这类**片段**文案用。

    `target` 可以是 **Page，也可以是 ElementHandle**：传元素时查找被限制在
    那个元素的子树里。搜索页的筛选面板必须这么用——三个组里都有
    「一天内」「不限时间」这种**跨组重名**的选项（组0 有「不限类型」、
    组2 有「不限时间」），整页找会串台点到别的组上。
    """
    try:
        handle = target.evaluate_handle(_FIND_BY_TEXT, [text, exact, scope])
    except Exception:
        return None
    try:
        return handle.as_element()
    except Exception:
        return None


_FIND_TEXT = """
(a, b) => {
  // 双形态，同 _FIND_BY_TEXT
  const isEl = a && a.nodeType === 1;
  const [pattern, excludeSel] = isEl ? b : a;
  const root = isEl ? a : document;
  let re;
  try {
    re = new RegExp(pattern);
  } catch (e) {
    return null;
  }
  const norm = (s) => (s || '')
    .replace(/[\\u200b-\\u200d\\ufeff]/g, '')
    .replace(/\\s+/g, ' ')
    .trim();
  // ⚠️ **命中即返回**，不是 `find_by_text` 那种"扫完整棵子树再挑"：
  //    评论面板可能有两万个元素，而要找的标题就在最顶上。
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  let node;
  while ((node = walker.nextNode())) {
    const t = norm(node.nodeValue);
    if (!t || !re.test(t)) continue;
    const el = node.parentElement;
    if (!el) continue;
    if (excludeSel && el.closest(excludeSel)) continue;
    return el;
  }
  return null;
}
"""


def find_text(target: Any, pattern: str, *, exclude: str = "") -> Any | None:
    """找**文本节点整串匹配** `pattern`（JS 正则）的那个元素，文档顺序第一个。

    和 `find_by_text` 的分工：那个是"按文案找元素"（精确/包含，扫完整棵树，
    用来点按钮）；这个是"按**形态**找元素"（`\\d+ 条评论` 这种带数字的文案），
    而且命中即返回，大页面上便宜得多。

    `exclude` 是"匹配到的文字不该待在哪种节点里"：评论采集传 `[data-id]`，
    防的是"某条评论的正文正好写成这几个字"被当成标题。
    """
    try:
        handle = target.evaluate_handle(_FIND_TEXT, [pattern, exclude])
    except Exception:
        return None
    try:
        return handle.as_element()
    except Exception:
        return None


def click_option(
    target: Any,
    *,
    texts: Iterable[str],
    option_selector: str,
    open_button: str | None = None,
    timeout: int = 5000,
) -> str | None:
    """⭐ 点下拉菜单里的一个选项。**先开下拉，再在选项列表里点。**

    ## 为什么不能直接按文案在整页找

    实测 `anwser_sort_by_time.html`：combobox 选中「按时间排序」时，
    **它的 `<span>` 里也是「按时间排序」这四个字**，
    而选项 `<button role="option">` 里同样是这四个字。整页按文案找会先命中
    combobox——点它只会把已经开着的下拉收起来，看起来"点过了"，
    排序却纹丝不动。这正是能力四最怕的那种静默失败。

    所以这里把查找**限定在 `option_selector` 里**（实测是
    `.Select-list button[role='option']`，共 2 个，DOM 顺序
    `默认排序` → `按时间排序`）。

    Returns:
        点中的文案；没点着返回 None。**调用方必须处理 None**——
        排序没切成功时采集照样会"成功"完成，只是拿到的是一份排序错误的数据。
    """
    if open_button:
        opener = pick(target, open_button)
        if opener is not None:
            _click_quietly(opener, timeout)
            _wait_for_options(target, option_selector)

    for text in texts:
        for el in all_of(target, option_selector):
            if element_text(el) != text:
                continue
            if _click_quietly(el, timeout):
                return text
        # 选项元素上挂着零宽字符 / 换行时，退回到归一化比对
        found = find_by_text(target, text, scope=option_selector)
        if found is not None and _click_quietly(found, timeout):
            return text
    return None


def _wait_for_options(target: Any, option_selector: str, *, timeout: int = 3000) -> None:
    """等下拉渲染出来。等不到就算了，后面点不到会返回 None。"""
    try:
        target.wait_for_selector(selectors.css(option_selector), timeout=timeout)
    except Exception:
        pass


def _click_quietly(el: Any, timeout: int) -> bool:
    """滚进视口再点，成功返回 True。点不动不是异常，是"没点着"。"""
    try:
        el.scroll_into_view_if_needed(timeout=timeout)
    except Exception:
        pass  # 有些元素本来就在视口里，这一步失败不影响点击
    try:
        el.click(timeout=timeout)
        return True
    except Exception:
        return False


def click_element(el: Any, *, timeout: int = 5000) -> bool:
    """点一个**已经在手上的元素**，成功返回 True。

    点不动不是异常，是"没点着"——调用方必须处理 False（见 `click_text` 的说明）。
    评论采集尤其依赖这个返回值：评论入口按钮点不动时，弹窗根本不会开，
    而"没开"和"这条内容 0 条评论"在数字上长得一模一样。
    """
    return _click_quietly(el, timeout)


_SCROLL_CONTAINER = """
(a, b) => {
  // 双形态，同 _FIND_BY_TEXT
  const isEl = a && a.nodeType === 1;
  const [scopeSel, tolerance, climb] = isEl ? b : a;
  const root = isEl ? a : document;
  const scope = scopeSel ? root.querySelector(scopeSel) : root;
  if (!scope) return null;

  // ⚠️ **「内容溢出」不等于「能滚」。** 一个 `overflow: visible` 的普通 div，
  //    内容照样把 scrollHeight 撑得比 clientHeight 大，但 scrollTop 写不进去、
  //    永远读回 0。问答页那个评论弹窗上就是这么翻车的：BFS 外层优先先撞上
  //    这么一个祖先，于是日志里"找到了滚动容器"，实际全程空转，直到
  //    "连续 N 轮无新增"收工——**安静地只采了首屏**。
  //
  //    判据取**计算样式的 overflow-y**，不是"写 scrollTop 再读回来"：
  //    后者会被 `scroll-behavior: smooth` 骗（赋值后立刻读，动画还没起步，
  //    看起来"没动"），反而把**真能滚的**当成滚不动。
  //    `visible` / `clip` 是仅有的两个"写不进去"的值；`hidden` 不占滚动条，
  //    但程序写 scrollTop 是**生效**的，所以它要算。
  const scrollable = (el) => {
    if (el.clientHeight <= 0) return false;
    if (el.scrollHeight <= el.clientHeight + tolerance) return false;
    const oy = getComputedStyle(el).overflowY;
    return oy !== 'visible' && oy !== 'clip';
  };

  // 广度优先：**外层优先**。弹窗那种"外层定高、内层滚动"的结构里，
  // 真正吃滚动条的是外层；反过来取最内层会拿到一个只是内容溢出的普通 div。
  const queue = [scope];
  while (queue.length) {
    const el = queue.shift();
    if (scrollable(el)) return el;
    for (const child of el.children) queue.push(child);
  }

  // ⚠️⚠️ **只往下找是不够的：滚动条可能挂在面板外面。**
  //    问答页那个评论弹窗就是这个形状——面板（`.Modal-content > div`）里面
  //    一个能滚的都没有，真正吃滚动条的是它**祖先**那一层。只往下找会返回
  //    null，调用方退回"滚面板本身"：`scrollTop` 写不进去、**不报错也不抛异常**，
  //    于是滚动循环一路空转到 STABLE，日志读起来像"评论本来就这么点"。
  //    用户看到的现象就是「评论本身不滚动」。
  //
  //    **必须停在 `body` 之前**：再往上就是 `body` / `html`，滚它们等于滚整个
  //    页面——那正是"评论没滚、背后的回答页面在滚"。宁可返回 null 让调用方报错。
  if (climb) {
    for (let el = root.parentElement; el && el !== document.body; el = el.parentElement) {
      if (scrollable(el)) return el;
    }
  }
  return null;
}
"""


# 问一次"我在滚谁、它滚到哪了"。见 `probe_scroller`。
_SCROLL_PROBE = """
(el) => {
  const cls = el.className ? String(el.className) : '';
  return {
    label: el.tagName.toLowerCase()
         + (cls.trim() ? '.' + cls.trim().split(/\\s+/).join('.') : ''),
    viewport: el.clientHeight,
    content: el.scrollHeight,
    offset: Math.round(el.scrollTop),
    overflowY: getComputedStyle(el).overflowY,
  };
}
"""


def probe_scroller(el: Any) -> dict | None:
    """读一次滚动目标的**身份和当前位置**。

    ⚠️ 这不是调试残留。**"找到了滚动容器"和"容器真的滚动了"是两件事**，
    而两者的失败长得一模一样：都是"连续 N 轮无新增"。只有滚动前后各读一次
    `offset` 才分得出来——分不出来的话，失败就是**安静地只采首屏**，
    正是本项目最怕的那种错。

    读不出来返回 None（句柄失效等），调用方按"没证据"处理，不要当成"没动"。
    """
    if el is None:
        return None
    with contextlib.suppress(Exception):
        data = el.evaluate(_SCROLL_PROBE)
        if isinstance(data, dict):
            return data
    return None


def describe_scroller(info: dict | None) -> str:
    """把 `probe_scroller` 的结果写成一行，给日志和报告用。"""
    if not info:
        return "（读不出来）"
    return (
        f"{info.get('label')} 视口={info.get('viewport')} "
        f"内容={info.get('content')} overflowY={info.get('overflowY')} "
        f"位置={info.get('offset')}"
    )


# 找不到滚动容器时用来**说清楚为什么**：把"内容比可视区高"的元素全列出来，
# 连它们的 `overflow-y` 一起。光说"没找到"没法定位——是容器在弹窗外面？
# 还是找到了但 `overflow: visible` 写不进去？这两个要靠 `overflowY` 分。
_SCROLL_CENSUS = """
(a, b) => {
  const isEl = a && a.nodeType === 1;
  const root = isEl ? a : document;
  const args = (isEl ? b : a) || [];
  const limit = args[0] || 12;
  const label = (el) => {
    const cls = el.className ? String(el.className) : '';
    return (cls.slice(0, 48) || el.tagName.toLowerCase());
  };
  const describe = (el) => {
    const oy = getComputedStyle(el).overflowY;
    const dead = (oy === 'visible' || oy === 'clip') ? ' ← 写不进去，不算滚动容器' : '';
    return `${label(el)} 视口=${el.clientHeight} 内容=${el.scrollHeight} overflowY=${oy}${dead}`;
  };
  const overflow = [];
  const walk = (el, depth) => {
    if (!el || el.nodeType !== 1 || depth > 8 || overflow.length >= limit) return;
    if (el.clientHeight > 0 && el.scrollHeight > el.clientHeight + 2) {
      overflow.push(`${'  '.repeat(depth)}${describe(el)}`);
    }
    for (const c of el.children) walk(c, depth + 1);
  };
  walk(root, 0);

  // 祖先链也要看：关闭叉是 `.Modal-content` 的**兄弟**，说明滚动条也可能
  // 挂在弹窗外面——那样从面板往下找永远找不到。
  const ancestors = [];
  for (let el = root.parentElement; el && el !== document.documentElement; el = el.parentElement) {
    if (ancestors.length >= limit) break;
    ancestors.push(describe(el));
  }
  return {overflow, ancestors};
}
"""


def find_scroll_container(
    target: Any, *, scope: str = "", tolerance: int = 4, climb: bool = True
) -> Any | None:
    """找**真正会滚的那个元素**：`scrollHeight > clientHeight` 的那个。

    ## 为什么不能在 selectors.py 里写死

    评论弹窗里的滚动容器类名是内容哈希（`css-xxxxxx`），知乎每次发版都换。
    而"内容比可视区高"这个判据**和类名无关**——它问的是布局，不是标记。
    架构文档把这条写在 `COMMENT_MODAL` 的说明里，这里是它的实现。

    ⚠️ `tolerance` 是给"差几个像素"的舍入留的余量：`scrollHeight` 和
    `clientHeight` 都是**取整后的**，内容只溢出半像素的元素会看起来"刚好相等"。

    ⚠️ 找不到时返回 None，**调用方要当成"滚不动"处理并报出来**：
    返回弹窗本身去滚的话，滚动循环会安静地空转到 max_rounds。

    ⚠️ **判据是"真的滚得动"，不是"内容比可视区高"**——两者不等价，
    差别见 `_SCROLL_CONTAINER` 里的说明。只测溢出的旧版会在
    `overflow: visible` 的容器上给出一个**点了没反应**的元素。

    ⚠️ `climb=True` 时，目标子树里找不到就**往祖先找**（见 `_SCROLL_CONTAINER`）。
    弹窗那种"面板在里面、滚动条在外面"的结构必须靠它，否则调用方会退回去滚
    一个写不进 `scrollTop` 的元素——**不报错，只是评论永远不动**。
    往上会**停在 `body` 之前**，所以永远不会返回页面本身的滚动容器。
    """
    try:
        handle = target.evaluate_handle(
            _SCROLL_CONTAINER, [scope, tolerance, climb]
        )
    except Exception:
        return None
    try:
        return handle.as_element()
    except Exception:
        return None


def describe_scroll_candidates(target: Any, *, limit: int = 12) -> list[str]:
    """`find_scroll_container` 说"找不到"时，**把现场列出来**。

    只报"没找到"是没法定位的：容器在弹窗外面？找到了但 `overflow: visible`
    写不进去？两种情况的修法完全不同，而它们的日志长得一模一样。
    这份清单把每个"内容比可视区高"的元素的 `overflowY` 和祖先链一起吐出来，
    一眼就能分开——所以它是**报错文案的一部分**，不是调试用的临时东西。
    """
    try:
        data = target.evaluate(_SCROLL_CENSUS, [limit])
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    lines: list[str] = []
    overflow = data.get("overflow") or []
    ancestors = data.get("ancestors") or []
    if overflow:
        lines.append("  弹窗里内容超出视口的元素：")
        lines.extend(f"    {line}" for line in overflow)
    else:
        lines.append("  弹窗里**没有任何元素的内容超出视口**（弹窗根本没渲染完？）")
    if ancestors:
        lines.append("  往上到 body 的祖先链：")
        lines.extend(f"    {line}" for line in ancestors)
    return lines


def click_all_text(target: Any, texts: Iterable[str]) -> int:
    """把所有这类按钮都点一遍（「阅读全文」可能有很多个），返回点掉的个数。"""
    clicked = 0
    for text in texts:
        for el in _all_safe(target, f"button:has-text('{text}')"):
            try:
                el.click(timeout=3000)
                clicked += 1
            except Exception:
                continue
    return clicked


# ── 收集 ────────────────────────────────────────────────────────────


class Harvester:
    """按元素身份去重地收 HTML，每次调用返回**本轮新增的条数**。

    正好对上 `scroll_until_exhausted` 的 `collect` 回调签名（返回新增条数）。

    ⚠️ 已知代价：**两段一模一样的 HTML 会被并成一条**。对搜索结果和回答
    无所谓（URL 不同，HTML 必然不同）；对评论则与已有设计一致——
    评论的伪 URL 本来就是 `作者+内容哈希`，同一作者的两条相同评论在数据库
    唯一约束那里同样会并成一条（见 `parse._comment_pseudo_url` 的注释）。
    这不是新引入的问题，是同一个取舍。

    刻意**不用"第几个元素"做身份**：知乎的列表会虚拟化（滚出屏幕的节点被
    移除），下标会错位，去重就乱了。

    ## `roots_only`：评论必须开

    `[data-id]` 是**嵌套**的（二级回复在父评论的子树里），
    `querySelectorAll` 会把两级一起吐出来。而收下来的是一段段**脱离 DOM 的
    HTML 字符串**，到 Python 那边已经**看不出谁在谁里面**了——
    `find_parent(attrs=...)` 对游离片段永远返回 None。

    所以"只要根节点"这件事必须在**浏览器里**判：开 `roots_only`，
    过滤条件是"没有匹配同一选择器的祖先"，和 `parse._root_comments` 是**同一条
    规则**，只是执行的地点不同。不开的话，二级回复会被当成独立的一级评论，
    `parent_id` 全指错，而且不报错。
    """

    def __init__(self, target: Any, selector: str, *, roots_only: bool = False) -> None:
        self.target = target
        self.selector = selector
        self.roots_only = roots_only
        self.items: list[str] = []
        self._seen: set[str] = set()

    def __call__(self) -> int:
        try:
            htmls = self.target.evaluate(
                _HARVEST, [selectors.css(self.selector), self.roots_only]
            )
        except Exception:
            return 0
        fresh = 0
        for html in htmls or []:
            if not html or html in self._seen:
                continue
            self._seen.add(html)
            self.items.append(html)
            fresh += 1
        return fresh


# ── 滚动 ────────────────────────────────────────────────────────────


def scroll_bottom(target: Any) -> None:
    """把滚动目标**一步拉到底**。Page 和元素都支持。

    ⚠️⚠️ **拉到底是不对的，别用它做滚动循环。用 `scroll_step`。**

    用户 2026-09-26 实测：「如果问题很长快速的滚动到底部它会**不加载**，
    如果慢一点滚动好像没有这个问题。」

    原因不难猜：知乎的懒加载挂在滚动事件上，一步跳到 `scrollHeight` 只产生
    一次滚动事件，触发一次加载；而内容插进来之后页面又变高了，当前位置
    已经不在底部——于是看起来就是"滚不动了、不加载了"。

    留着它是给**一次性场景**用的（比如"先把页面拉到最下面再截图"），
    不是给滚动循环用的。
    """
    try:
        target.evaluate(_SCROLL_TO_BOTTOM)
    except Exception:
        pass


_STEP = """
(a, b) => {
  // 双形态：Page 传一个参数（我们的是数组），元素传"元素 + 数组"。
  // ⚠️ 参数**一律包成数组**再传，这样两种形态下都能靠 `nodeType` 分清，
  //    不会出现"数字被当成元素"或"元素被当成数字"的错位。
  const isEl = a && a.nodeType === 1;
  const [px] = isEl ? b : a;
  if (isEl) {
    a.scrollTop += px;
  } else {
    window.scrollBy(0, px);
  }
}
"""


def scroll_step(target: Any, *, step: int = 400) -> None:
    """**小步**往下滚一屏的几分之一。

    这才是滚动循环该用的。每调用一次产生一次真实的滚动事件，懒加载才有机会
    触发；`scroll_bottom` 那种一步到底会跳过中间的触发点。

    `step=400` 大约是普通屏高的 1/2。**别调太大**——调大到一定程度就退化成
    `scroll_bottom` 的行为了，而且不会报错，只会安静地少采。
    """
    _scroll_by(target, step)


# 拨一次的间隔。见 `scroll_flick`。
FLICK_GAP = 0.07
FLICK_STEPS = 5
FLICK_STEP = 400


def scroll_flick(
    target: Any,
    *,
    steps: int = FLICK_STEPS,
    step: int = FLICK_STEP,
    gap: float = FLICK_GAP,
) -> None:
    """⭐ **像人拨滚轮那样：连着快滚几下，再停。** 滚动循环该用的是这个。

    和 `scroll_step` 的分工：那个是"滚一下、等一轮、再滚一下"——
    **那不像人。** 真人拨滚轮是一个连续手势：几十毫秒内连着好几下，
    然后停下来看内容。用户 2026-09-26 的原话是「我自己手操的时候是
    疯狂的滚动滚轮这样每次滚动输出的间隔就是非常小，也不会触发报警」。

    所以这里把"一个手势"打包成一次调用：`steps` 次滚动，每次之间只隔
    `gap`（默认 70ms）。默认 5×400 = 2000px，用时约 0.35 秒——
    比 `scroll_step` 那种"400px + 冻 3 秒"**既快得多，也更像人**。

    ⚠️ **但这不是纯粹的提速，别把它当成免费的。** 每一轮跨过的距离变大了，
    一次没触发懒加载就浪费得更多。判断标准是**卡住的轮次数**，不是总耗时：
    如果日志里「连续 N 轮无新增」变多了，把 `steps` 调小（调到 3 试），
    而不是把 `gap` 调大——变慢解决不了"跨过头"，只有变短能。

    ⚠️ `gap` 里那几次 `sleep` **不是节流、也不是反爬**，是手势本身的形状：
    连着两下之间总要有几十毫秒。整个项目里唯一有反爬含义的停顿是
    `session.pace`，别把两者搞混。

    最后一下之后也会 `sleep(gap)`——那一下是留给 DOM 渲染的，
    好让紧随其后的 `collect()` 拿到的是稳定内容。

    Raises:
        ValueError: `steps < 1`。**这一条必须抛，不能静默返回**——
            滚 0 次等于一次都没滚，而滚动循环会一路空转到 `max_rounds`
            然后报一个看起来像"知乎改版了"的错。
    """
    if steps < 1:
        raise ValueError(f"拨滚轮至少要滚一下，收到 steps={steps}")
    for _ in range(steps):
        _scroll_by(target, step)
        time.sleep(gap)


def scroll_up_a_bit(target: Any, *, step: int = 600) -> None:
    """往上滚一小段。配合 `scroll_step` 组成 `nudge`。"""
    _scroll_by(target, -step)


def nudge(target: Any, *, up: int = 600, down: int = 900) -> None:
    """⭐ **卡住时的补救动作：上滚一段再滚回来，重新触发懒加载。**

    用户实测原话：「如果不加载了**往上滚一下再继续往下**又可以继续触发加载。」

    为什么这样有效：知乎在"滚动到接近底部"时才发下一页请求，而且请求过一次
    就记了状态。往上滚会让"距底部距离"重新变大，再滚回来就构成了一次全新的
    "接近底部"事件，于是重新触发。

    这个动作**不是万能药**——试了还不行就该放弃并上报，不要一直重试。
    退避与放弃由 `scrolling.scroll_until_exhausted(max_nudges=…)` 管。
    """
    scroll_up_a_bit(target, step=up)
    scroll_step(target, step=down)


def _scroll_by(target: Any, px: int) -> None:
    """按 `px` 滚动：窗口就滚窗口，元素就滚元素。**一次调用，不来回试。**

    ⚠️ 旧版是"先按窗口试，抛异常再按元素试"。那个写法在**元素**上是
    **静默错误**：`(px) => window.scrollBy(0, px)` 传给元素时 `px` 绑到了元素上，
    `scrollBy(0, <元素>)` 不报错也不滚——于是它"成功"了，元素永远没动。
    现在由脚本自己判断形态（见 `_STEP`），不存在试错。
    """
    try:
        target.evaluate(_STEP, [px])
    except Exception:
        pass  # 滚不动不是异常：循环那边有"连续 N 轮无新增"兜底


def scroll_top(target: Any) -> None:
    try:
        target.evaluate(_SCROLL_TOP)
    except Exception:
        pass


def page_html(page: Any) -> str:
    """整页 outerHTML——**这正是用户存快照要的那份东西**。

    `page.content()` 拿到的和 DevTools 里 `copy(document.documentElement.outerHTML)`
    基本一致（都是渲染后的 DOM），所以本地存下来的快照可以直接当校准固件。
    """
    try:
        return page.content()
    except Exception:
        return ""


# ── 内部 ────────────────────────────────────────────────────────────


def _first_safe(target: Any, selector: str) -> Any | None:
    try:
        return target.query_selector(selector)
    except Exception:
        return None


def _all_safe(target: Any, selector: str) -> list[Any]:
    try:
        return target.query_selector_all(selector)
    except Exception:
        return []
