"""能力五：打开一个问题页，取描述正文 + 热度指标（架构文档 3.5，决策 53 第②步）。

输入是问题页 URL，输出是**能直接进 `dim_question` 的那几列**：
标题、描述、关注者、被浏览、回答数、提问时间。落点是 `<run>/questions.jsonl`。

## ⭐ 不用点「显示全部」——数据早就在页面里了

问题页的描述有三种形态：① 没有描述（底下没有小字）② 描述很短（有小字，
没有「显示全部」按钮）③ 描述很长（有「显示全部」按钮，DOM 里被截断）。

**第三种不需要点开。** `script#js-initialData` 里
`initialState.entities.questions[<qid>].detail` 就是**全文**，
那条「显示全部」纯粹是 CSS 层面的展开。实测（2026-09-29，
`runtime/calib/question.html` 点开前 vs `question2.html` 点开后）：

    点开前 DOM   76 字  「…这究竟是为…」        ← 被截断
    点开后 DOM   79 字  「…这究竟是为什么呢？」  ← 全文
    JSON detail  79 字  「…这究竟是为什么呢？」  ← 与点开后逐字相同
    点开前后 initialData 一字未变

所以本模块的取数口**只有一个**：`js-initialData`。不点击、不读 DOM、
**不新增任何 CSS 选择器**。`test_question.py` 里有一条测试拿这两份快照
钉死这件事——它也是唯一能挡住"哪天有人加了个点击步骤"的东西。

## ⚠️ 两道 URL 关 + 一道 JSON 关，缺一不可

    第一关  urlnorm.normalize(url) 必须给出 content_type == "question"
    第二关  URL 里的 qid 必须是 entities.questions 的键

**第一关不是多余的。** 实测 `answer.html` 里 `entities.questions` 也有整整 1 条
——那是它所属的那个问题（22230085）。所以「JSON 里有问题实体」**完全不能**
证明"这一页是那个问题的页面"：只查第二关的话，一份回答页会顺利产出一行
**别人的**问题的详情，标题、描述、关注数全是那个父问题的值，
而 URL 那一列写的是别的东西。

第二关挡的是另一种：`js-initialData` 是**服务端渲染那一刻**的快照，
SPA 内部跳转不会重写它（`selectors.INITIAL_STATE_SCRIPT` 已实测记录）。
所以「JSON 里有数据」和「JSON 里是**这一页**的数据」是两件事——
不校验的话，一份上一页残留的 JSON 会被当成这一页的详情写下去，
**字段全满、日志正常、没有任何报错**。

## `description` 空串 ≠ 失败

`question_default.html` / `question_newest.html` / `question_sort_open.html`
三份真实快照的 `detail` 就是 `""`，页面上也确实没有描述。这跟 `storage/ingest.py`
里那条「"取不到"和"本来就没有"要分开」是同一条规矩。但**标题空是失败**——
那是真的没取到，而标题是 AI 判 B 的主输入（3.5）。

## 为什么解析函数在这里，而不在 `parse.py`

读 JSON 的三件工具（`extract_json_state` / `find_entity` / `html_to_text`）
都在 `extract.py`，而 `extract.py` 已经 `from sentinel_q.collector import parse`。
解析函数放进去就是循环 import。**这不是理想分层，是 import 方向逼出来的**，
而架构文档 1704 行本来就把 `collector/question.py` 这个文件名留好了。

## 这里不落快照

`dim_question` 没有 `snapshot_path` 列，而"文件写了、库里没指针"正是
`extract.to_document` 的 docstring 警告过的那种错（反过来也一样）。
描述只留纯文本（项目所有者 2026-09-29 定的形态）；要连 `detail` 的 HTML
一起留，那是加一列的事。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from sentinel_q.collector import drive, extract
from sentinel_q.collector.session import BrowserSession
from sentinel_q.shared.urlnorm import normalize

log = logging.getLogger(__name__)

# ── 四条失败原因 ────────────────────────────────────────────────────
#
# 抽成常量是**为了测试能按名字断言**，而不是去比对一句人话里的子串——
# 那些句子随时会被改得更好读，而"哪一种失败"是这个模块的对外语义。

REASON_NOT_A_QUESTION = "URL 不是问题页"
"""第一关没过：`urlnorm` 认不出这个 URL，或者它的类型不是 question。"""

REASON_NO_JSON = "页面里没有可读的 js-initialData"
"""没加载完 / 被挡 / 知乎换掉了这个 script 的形态。"""

REASON_NOT_IN_JSON = "页面数据里没有这个问题"
"""第二关没过：残留快照，或者这压根不是**这个问题**的页面。"""

REASON_NO_TITLE = "问题页里没有标题"
"""实体在、但标题是空的。标题是 AI 判 B 的主输入，缺了这条不能用。"""


@dataclass(frozen=True)
class QuestionDetail:
    """一个问题页的详情，就是 `dim_question` 要的那几列。

    ⚠️ `description` **没有描述时是空串，不是 `None`**：`None` 留给"整条详情
    都没取到"，两者混在一起的话，"这个问题本来就没描述"和"这一页没采成"
    在文档里长得一模一样。
    """

    zhihu_qid: str
    url: str
    """**规范化后**的 URL（`shared.urlnorm`），和 `urls.jsonl` / `fact_content.url`
    同一种形态，这样"这条问题采过没有"在两边说法一致。"""

    title: str
    description: str
    """纯文本。空串 = 这个问题没有描述，**不是失败**（见模块开头）。"""

    follower_count: int | None = None
    """关注者数。`None` = 没取到。**不是 0**——0 个关注和"没采到"是两回事。"""

    view_count: int | None = None
    """被浏览数。同上。"""

    answer_count: int | None = None
    """回答数。同上。"""

    asked_at: datetime | None = None
    """提问时间，来自实体里的 `created`（unix 秒）。

    ⚠️ 与 `dim_question.first_seen_at` 区分：那个是"我们第一次看到它"，
    这个是"它在知乎上被提出来"。两者的差就是这个问题在我们发现它之前
    已经存在了多久——做时间线时用错一个，整条轴就偏移了。
    """

    keyword: str | None = None
    """哪次搜索捞出来的。人工核对时最常问的就是这个（同 `UrlEntry.keyword`）。"""


def to_row(detail: QuestionDetail, *, collected_at: str | None = None) -> dict:
    """`QuestionDetail` → `questions.jsonl` 的一行。

    ⚠️ **`datetime` 在这里就转成字符串**：`ops.append_contents` 是逐行
    `json.dumps`，塞不进 `datetime`（同 `extract.to_document`）。

    字段与夹具 `runtime/ops/20260929-090000-fixture/questions.jsonl` 对齐，
    多出来的是 `answer_count` / `asked_at`（那份夹具写在这次讨论之前）。
    """
    return {
        "zhihu_qid": detail.zhihu_qid,
        "url": detail.url,
        "title": detail.title,
        "description": detail.description,
        "follower_count": detail.follower_count,
        "view_count": detail.view_count,
        "answer_count": detail.answer_count,
        "keyword": detail.keyword,
        "collected_at": collected_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "asked_at": detail.asked_at.isoformat() if detail.asked_at else None,
    }


def detail_from_html(html: str, url: str) -> QuestionDetail | None:
    """问题页 HTML → `QuestionDetail`。**认不出来返回 None，不抛。**

    这是纯函数，所以测试拿 `runtime/calib/question*.html` 直接喂它就行，
    不必开浏览器。想知道**是哪一种失败**的调用方用 `QuestionReport.reason`
    （由 `extract_question` 填），这里只回答"成没成"。
    """
    return _read_detail(html, url)[0]


@dataclass
class QuestionReport:
    """一次问题详情采集的结果。

    `ok` 是**采集是否可信**，不是"有没有拿到东西"——拿到了但标题是空的
    同样不算：标题是 AI 判 B 的主输入，缺了它这一行进库也只是占了个位。

    ⚠️ 描述是空串**不算失败**（见模块开头）。
    """

    url: str
    detail: QuestionDetail | None = None
    reason: str = ""
    """失败原因，取值是上面那四个 `REASON_*` 常量之一。成功时是空串。"""

    @property
    def ok(self) -> bool:
        return self.detail is not None and bool(self.detail.title)

    def describe(self) -> str:
        if self.detail is None:
            return f"❌ {self.url}：{self.reason}"
        detail = self.detail
        text = (
            f"问题 {detail.zhihu_qid}：描述 {len(detail.description)} 字"
            f"，关注者 {_shown(detail.follower_count)}"
            f"，被浏览 {_shown(detail.view_count)}"
            f"，回答数 {_shown(detail.answer_count)}"
        )
        if not detail.description:
            # 不是错误，但值得记一笔：核对时看到 0 字要知道那是"本来就没有"。
            text += "；这个问题没有描述（正常，不是失败）"
        return text


def extract_question(session: BrowserSession, url: str) -> QuestionReport:
    """打开一个问题页，取详情。**不抛异常**——失败记在报告里。

    同 `content.extract_content`：单个 URL 失败不该中断整批。

    ⚠️ **不给 `open()` 传 `ready`。** 能力二要轮询等正文渲染完
    （`content._body_ready`），因为正文是 JS 填进 DOM 的；能力五不用——
    `script#js-initialData` 就在文档本身里，`wait_until="domcontentloaded"`
    一返回它就已经在了。少一处会等错地方的地方。
    """
    report = QuestionReport(url=url)
    session.open(url)

    report.detail, report.reason = _read_detail(drive.page_html(session.page), url)
    if report.detail is None:
        log.error(
            "%s 取不到问题详情（%s）。**这条不入库**——宁可缺一条，"
            "也不要拿别的问题的数据顶上。",
            url,
            report.reason,
        )
        return report

    log.info(report.describe())
    return report


def summarize(reports: list[QuestionReport]) -> str:
    """一批问题采集的汇总。**失败必须显式报出来，不能只看"跑完了"。**"""
    from collections import Counter

    failed = [r for r in reports if not r.ok]
    text = f"共 {len(reports)} 个问题：成功 {len(reports) - len(failed)} 个，失败 {len(failed)} 个"
    if failed:
        kinds = Counter(r.reason or "未知原因" for r in failed)
        text += "（" + "，".join(f"{k} {v} 个" for k, v in kinds.most_common()) + "）"
    return text


# ── 内部 ────────────────────────────────────────────────────────────


def _read_detail(html: str, url: str) -> tuple[QuestionDetail | None, str]:
    """两关 + 解析。返回 `(详情, 失败原因)`，成功时原因是空串。

    `(值, 原因)` 而不是"返回 None 再让调用方猜"：四种失败对应四种处置
    （换 URL / 重试 / 人工看页面 / 补标题），分不清就只能笼统报一句
    "取不到"，而那正是本项目最不想要的那种反馈。
    """
    normalized = normalize(url)
    if normalized is None or normalized.content_type != "question":
        # ⚠️ 第一关。别以为 JSON 里有问题实体就能证明这是问题页——
        #    实测 answer.html 的 entities.questions 里也有 1 条（父问题）。
        return None, REASON_NOT_A_QUESTION

    qid = normalized.zhihu_id
    if not qid:
        return None, REASON_NOT_A_QUESTION

    state = extract.extract_json_state(html)
    if state is None:
        return None, REASON_NO_JSON

    entity = extract.find_entity(state, "question", qid)
    if entity is None:
        # ⚠️ 第二关。残留快照、或者这压根不是**这个问题**的页面。
        return None, REASON_NOT_IN_JSON

    title = entity.get("title")
    title = title.strip() if isinstance(title, str) else ""
    if not title:
        return None, REASON_NO_TITLE

    return (
        QuestionDetail(
            zhihu_qid=qid,
            url=normalized.url,
            title=title,
            description=extract.html_to_text(entity.get("detail")),
            follower_count=_int_of(entity, "followerCount"),
            view_count=_int_of(entity, "visitCount"),
            answer_count=_int_of(entity, "answerCount"),
            asked_at=_asked_at(entity),
        ),
        "",
    )


def _int_of(entity: Mapping, key: str) -> int | None:
    """读一个计数。**缺了或形态不对就是 `None`，不是 0。**

    ⚠️ 这里刻意不写 `int(entity.get(key) or 0)`：那个写法会把
    "这个字段没了"和"这个数是 0"变成同一个值，而库上那三列**可空**正是
    为了区分这两件事（迁移 0005）。
    """
    value = entity.get(key)
    # `bool` 是 `int` 的子类，而 `True` 当计数没有意义——挡掉，
    # 免得哪天的脏数据变成"1 个关注者"。
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    # 负数当没采到：JSON 里 `-1` 常被用作"未知"的哨兵值。**不写进库**——
    # 库上那三列没有 check 约束，一个 -1 会安安静静地躺在那儿像个观测值。
    if value < 0:
        return None
    return value


def _asked_at(entity: Mapping) -> datetime | None:
    """`created`（unix 秒）→ 带时区的 `datetime`。读不动就 None。

    ⚠️ **负数当没采到**，和 `_int_of` 同一条规矩。不挡的话 `-1` 会变成
    1969-12-31 23:59:59——一个**看着完全合理**的时间戳，`fromtimestamp`
    对负数一点意见都没有。它会安安静静进 `asked_at` 那一列，然后让
    整条时间线偏掉，而没有任何东西会报错。知乎 2010 年才开站，
    负的 unix 秒在这个数据集里不可能是真的。
    """
    created = entity.get("created")
    if isinstance(created, bool) or not isinstance(created, int):
        return None
    if created < 0:
        return None
    try:
        return datetime.fromtimestamp(created, tz=UTC)
    except (OverflowError, OSError, ValueError):
        # 超出平台能表示的范围（或者是个负数）。不是错误，只是取不到。
        return None


def _shown(value: int | None) -> str:
    return "没取到" if value is None else str(value)
