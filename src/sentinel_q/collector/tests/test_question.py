"""能力五（问题详情）的测试。

`question.extract_question()` 要浏览器，跑不了。但这一层有几块能测死，
而且**每一块错了都会静默存下错数据**：

  1. ⭐ 「不用点开显示全部」—— 这条不是设计偏好，是**实测事实**，
     所以它拿点开前/点开后两份真实快照对着断言。错了的表现是
     "描述少了一截，其它字段全对"，没有任何报错。
  2. ⭐ URL 那一关不能删 —— 实测 `answer.html` 的 `entities.questions` 里
     也有整整 1 条（它所属的父问题）。删掉这关，一份回答页会顺利产出
     一行**别人的**问题详情，标题描述热度全是那个父问题的值。
  3. 描述空串 ≠ 失败 —— 三份真实快照的 `detail` 就是 `""`。
     判错的表现是"这三条永远采不到"，而它们本来是完全正常的。
  4. 计数取不到时是 `None` 而不是 `0` —— 库上那三列可空正是为了
     区分"没采到"和"就是 0"（迁移 0005）。写成 `or 0` 就全毁了。

第 1、2、3 条是**实测事实**，用例读真实快照并把对照表钉住；
快照不在（别人的机器 / CI）就 skip。第 4 条和其余边界只用合成 HTML。

⚠️ 快照含用户本人的昵称与主页链接，**永远不许拷到别处**（见 .gitignore）。
   这里只读不写。
"""

from __future__ import annotations

import functools
import json
import pathlib

import pytest

from sentinel_q.collector import question

CALIB_DIR = pathlib.Path("runtime/calib")

# 题目固定用这两个 qid，省得每条用例里重复拼 URL。
QID_LONG = "62450652"
"""`question.html` / `question2.html` 的问题——描述 79 字，是**第③种形态**
（有「显示全部」按钮、DOM 里被截断）。能力五"不用点开"这个结论就是拿它证出来的。"""

QID_ANSWER_PARENT = "22230085"
"""`answer.html` 里那条 `entities.questions` 的 qid——它是**那篇回答的父问题**。

⭐ 这个数字是好几条用例的支点：同一份 HTML，喂它自己的 qid 要拒，
喂这个 qid 就会产出一行完完整整、**没有任何异常**的详情。
"""


@functools.cache
def _snapshot(name: str) -> str:
    path = CALIB_DIR / name
    if not path.exists():
        pytest.skip(f"缺快照 {path}（不在仓库里，得由项目所有者本机采集）")
    return path.read_text(errors="ignore")


def _url(qid: str) -> str:
    return f"https://www.zhihu.com/question/{qid}"


# ── 合成 HTML（不依赖快照，别的机器上也跑）──────────────────────────


def _page(entities: object) -> str:
    """把一段 `entities` 包成一张最小的知乎页面。

    ⚠️ `initialState` 那一层不能省：`extract_json_state` 读的是
    `data["initialState"]`，而真页面的 script 里就是这个形状
    （外层还有十几个别的键，这里只留用得上的那个）。
    """
    payload = json.dumps({"initialState": {"entities": entities}}, ensure_ascii=False)
    return (
        "<!doctype html><html><head>"
        f'<script id="js-initialData" type="text/json">{payload}</script>'
        "</head><body></body></html>"
    )


def _question_html(qid: str, **fields: object) -> str:
    """一页只装一个问题的页面。`fields` 直接就是实体里的字段。"""
    entity = {"title": "一条问题的标题", **fields}
    return _page({"questions": {qid: entity}})


# ── 一、⭐ 「不用点开」的可执行证明 ──────────────────────────────────


def test_the_clicked_and_unclicked_snapshots_yield_the_same_row() -> None:
    """⭐ 点开前（`question.html`）与点开后（`question2.html`）必须**逐字相同**。

    这两个文件是项目所有者 2026-09-29 为同一道题采的：先采一份没点开的，
    再点「显示全部」采一份点开的。实测结论是 `js-initialData` 里
    `entities.questions[<qid>].detail` **本来就是全文**，那个按钮纯粹是
    CSS 层面的展开——所以能力五**不点击、不读 DOM、不新增任何 CSS 选择器**。

    ⚠️ 这条测试是全项目**唯一**能挡住"哪天有人加了个点击步骤"的东西。
    加了点击不会让结果变错（那份 JSON 反正不变），只会让采集慢一拍、
    多一处会失败的地方、多一个要校准的选择器——而那是这个模块开头
    花了一整节论证过要避免的。所以这里比的是**整行**，不是某个字段。
    """
    before = question.detail_from_html(_snapshot("question.html"), _url(QID_LONG))
    after = question.detail_from_html(_snapshot("question2.html"), _url(QID_LONG))

    assert before is not None
    assert after is not None
    assert before == after, "点开前后必须一模一样——数据本来就在页面里"

    # 顺带把这份快照钉住：79 字是**点开之后**的长度（点开前 DOM 只有 76 字）。
    # 哪天它变了，要么是知乎改了页面，要么是取数口从 JSON 换成了 DOM。
    assert len(before.description) == 79
    assert before.title == "《魁拔》系列为何知名度不高？如果说它失败，那它失败在哪里呢？"


def test_the_row_carries_the_full_description_not_the_truncated_one() -> None:
    """描述取的是**全文**，而且是从 JSON 来的——不是 DOM 里那段被截断的。

    实测 `question.html` 的 DOM 文本是 76 字（句尾停在「这究竟是为…」），
    JSON 里是 79 字（「…这究竟是为什么呢？」）。两者的差就是「显示全部」
    藏起来的那几个字。

    ⚠️ 这条和上一条测的不是一回事：上一条管"点开不点开一个样"，
    这条管"取到的到底是不是全文"。只断言长度不为 0 的话，
    截断版和全文版**都过得去**。
    """
    detail = question.detail_from_html(_snapshot("question.html"), _url(QID_LONG))

    assert detail is not None
    assert detail.description.endswith("呢？"), "被截断的那版结尾停在「为…」"
    assert "显示全部" not in detail.description, "按钮的文案不该混进正文"


# ── 二、⭐ URL 那一关（删了就静默存错数据）────────────────────────


def test_an_answer_url_is_refused_even_though_the_json_carries_a_question() -> None:
    """⭐ 拿真快照 `answer.html` + **它自己那条回答的 URL** → 必须拒。

    这份回答页的 `entities.questions` 里有整整 1 条，就是它所属的父问题
    （`22230085`）。这正说明「JSON 里有问题实体」**完全不能**证明
    "这一页是那个问题的页面"：只查 JSON 那一关的话，一份回答页会顺利
    产出一行**别人的**问题详情——标题、描述、关注数全是父问题的值，
    而 `url` 那一列写的是那条回答。字段全满、日志正常、零报错。
    """
    html = _snapshot("answer.html")
    answer_url = f"https://www.zhihu.com/question/{QID_ANSWER_PARENT}/answer/1594809785"

    assert question.detail_from_html(html, answer_url) is None


def test_the_same_answer_snapshot_would_happily_produce_the_parent_question() -> None:
    """⭐ 紧接着上一条：同一份 HTML 喂**父问题**的 URL 就成功了。

    这条是把上一条的"拒"变成有意义的那种断言——如果那份 JSON 里压根
    没有可解析的问题实体，上一条的 None 就只是碰巧，证明不了 URL 那一关
    挡掉了任何东西。这里要求它**确实能**产出父问题的详情。

    ⚠️ 顺带钉住一个更细的点：这份回答页里的父问题实体是**另一个时刻**的
    快照——`visitCount` 是 1366449，而 `question_default.html`（真的问题页）
    里是 1366461。差 12 次浏览。所以它不只是"另一个问题的数据"，
    是"另一个问题**在另一时刻**的数据"。拿它顶上去，数字看着完全正常。
    """
    html = _snapshot("answer.html")

    detail = question.detail_from_html(html, _url(QID_ANSWER_PARENT))

    assert detail is not None, "这份 JSON 里确实有可解析的问题实体"
    assert detail.title == "新人如何入门和学习软件测试？"
    assert detail.view_count == 1_366_449


def test_a_page_whose_json_has_no_such_question_is_refused() -> None:
    """第二关：URL 是问题页，但 JSON 里没有这个 qid → 拒。

    挡的是**残留快照**：`js-initialData` 是服务端渲染那一刻的快照，
    SPA 内部跳转不会重写它（`selectors.INITIAL_STATE_SCRIPT` 已实测记录）。
    不校验的话，上一页的 JSON 会被当成这一页的详情写下去。

    合成 HTML 就够——真快照里"URL 和 JSON 对不上"这种情况本来就造不出来
    （能造出来的话那才是真出事了）。
    """
    html = _question_html("111", detail="<p>问题的描述</p>")

    assert question.detail_from_html(html, _url("111")) is not None
    assert question.detail_from_html(html, _url("999")) is None


def test_a_recognisable_but_non_question_url_is_refused() -> None:
    """不是问题页的 URL 一律拒——哪怕页面里恰好有个问题实体。

    混进清单里的是回答、文章、想法（能力一搜出来的是**内容页**，
    不只有问题）。`urlnorm` 认得出它们，只是类型不是 question。
    """
    html = _question_html("111", detail="<p>问题的描述</p>")

    for url in (
        "https://www.zhihu.com/question/111/answer/222",
        "https://zhuanlan.zhihu.com/p/123456",
        "https://www.zhihu.com/people/someone",
    ):
        assert question.detail_from_html(html, url) is None, url


def test_an_unrecognisable_url_is_refused_without_looking_at_the_page() -> None:
    """URL 认不出来就拒，连 JSON 都不用看。

    ⚠️ 顺序是有意的：`detail_from_html` 先过 URL 关再解析。反过来的话，
    一份完全的垃圾 HTML + 一个合法 URL 的组合会走到解析那一步，
    然后在"没有 JSON"上失败——**报出来的原因是错的**，
    而失败原因正是调用方决定"重试还是换 URL"的依据。
    """
    for bad in ("", "不是 url", "https://www.example.com/question/111"):
        assert question.detail_from_html("", bad) is None, bad


# ── 三、空描述 = 正常的，不是失败 ──────────────────────────────────


@pytest.mark.parametrize(
    ("snapshot", "qid", "expected"),
    [
        # 三份**真的没有描述**的快照，页面上也确实没有描述那行小字。
        ("question_default.html", "22230085", (1264, 1366461, 252)),
        ("question_newest.html", "648988282", (2219, 9200883, 1735)),
        ("question_sort_open.html", "648988282", (2219, 9200883, 1735)),
    ],
)
def test_a_question_without_a_description_is_not_a_failure(
    snapshot: str, qid: str, expected: tuple[int, int, int]
) -> None:
    """⭐ 描述是**空串**、`ok is True`——「本来就没有」不是「没采到」。

    这条对着的是最容易写错的那行代码：把空描述当失败（`if not description`），
    结果是这三条**永远采不到**，而它们是完全正常的问题页。
    同一条规矩在 `storage/ingest.py` 里写着（"取不到"和"本来就没有"要分开）。

    ⚠️ 但**标题空是失败**——那是真的没取到，而标题是 AI 判 B 的主输入（3.5）。
    两种"空"在这里必须分开，所以这条同时断言 `ok` 和 `description == ""`。
    """
    detail = question.detail_from_html(_snapshot(snapshot), _url(qid))

    assert detail is not None
    assert detail.description == "", "没有描述是空串，不是 None，也不是失败"
    assert detail.title, "标题必须有——没有标题才是失败"
    assert (
        detail.follower_count,
        detail.view_count,
        detail.answer_count,
    ) == expected
    # 描述空了，热度指标照样是全的：两件事互不相干。
    assert question.QuestionReport(url=_url(qid), detail=detail).ok is True


def test_the_two_sorts_of_empty_are_told_apart() -> None:
    """标题空 → 失败；描述空 → 不失败。**只差一个字段，结论相反。**

    合成 HTML 写的，因为真快照里没有"标题空"这种页面（有的话就是知乎挂了）。
    """
    no_description = _question_html("111", detail="")
    no_title = _question_html("111", title="", detail="<p>有描述没标题</p>")

    quiet = question.detail_from_html(no_description, _url("111"))
    broken = question.detail_from_html(no_title, _url("111"))

    assert quiet is not None
    assert question.QuestionReport(url=_url("111"), detail=quiet).ok is True
    assert broken is None, "没有标题 = 真的没取到，不能入库"


def test_a_whitespace_only_title_counts_as_missing() -> None:
    """只有空白的标题等于没有标题。

    ⚠️ 不 `strip()` 的话，一个 `" "` 会让 `ok` 变成 True、标题那一列存一个
    空格——库里看着"有值"，而 AI 判 B 拿到的是一个空格。
    """
    html = _question_html("111", title="   \n  ")

    assert question.detail_from_html(html, _url("111")) is None


# ── 四、计数：取不到是 None，不是 0 ────────────────────────────────


@pytest.mark.parametrize("field", ["followerCount", "visitCount", "answerCount"])
@pytest.mark.parametrize(
    "value",
    [
        None,  # 字段没了
        "71",  # 知乎把它换成字符串了
        71.5,  # 浮点
        True,  # ⚠️ bool 是 int 的子类，`True` 当计数没有意义
        -1,  # JSON 里常拿 -1 当"未知"的哨兵值
    ],
    ids=["missing", "string", "float", "bool", "negative"],
)
def test_an_unusable_count_becomes_none_not_zero(field: str, value: object) -> None:
    """三种脏值一律 `None`。**绝不能写成 `int(x or 0)`。**

    库上那三列**可空**、且刻意不给 `default 0`（迁移 0005），就是为了区分
    "没采到"和"就是 0"。写成 `or 0` 的话，这两件事在库里长得一模一样，
    而"0 个关注"本身是个**合法观测值**——统计时会把脏数据算成真实的 0。

    ⚠️ `-1` 单独说：知乎用负数当"未知"。库上那三列没有 check 约束，
    一个 -1 会安安静静躺在那儿像个观测值，所以它在写库之前就得挡掉。
    """
    html = _question_html("111", **{field: value})

    detail = question.detail_from_html(html, _url("111"))

    assert detail is not None
    assert getattr(detail, _ATTR[field]) is None


_ATTR = {
    "followerCount": "follower_count",
    "visitCount": "view_count",
    "answerCount": "answer_count",
}
"""JSON 字段名 → dataclass 属性名。⚠️ `visitCount` 对应 `view_count` 不是笔误：
页面上那行小字是「被浏览」，知乎内部叫 `visitCount`。"""


def test_a_real_zero_stays_zero() -> None:
    """但**真的 0 要留住**——0 个关注是可能的，而且是个观测值。

    这条和上面那条是一对：一起才说明白"None 和 0 是两回事"。
    只测一边的话，把 `_int_of` 改成永远返回 None 也能过。
    """
    html = _question_html("111", followerCount=0, visitCount=0, answerCount=0)

    detail = question.detail_from_html(html, _url("111"))

    assert detail is not None
    assert (detail.follower_count, detail.view_count, detail.answer_count) == (0, 0, 0)


def test_a_missing_count_does_not_take_the_whole_detail_down() -> None:
    """计数缺了只是那一列空，**其它字段照样要采到**。

    它们是三个独立的可空列，不是"要么全有要么全无"。让一个缺失的
    `followerCount` 把整条详情判成失败，等于白跑一趟浏览器。
    """
    html = _question_html("111", title="标题", detail="<p>描述</p>")

    detail = question.detail_from_html(html, _url("111"))

    assert detail is not None
    assert detail.title == "标题"
    assert detail.description == "描述"
    assert detail.follower_count is None


def test_the_broken_json_paths_are_told_apart_from_each_other() -> None:
    """四种失败各自报得出名字来。

    合成 HTML 全覆盖：没有 `js-initialData`、JSON 读不动、`entities.questions`
    是空的、`entities` 整个没有。它们对应四种处置（重试 / 看页面 / 换 URL），
    混成一句"取不到"就没法决定下一步做什么。
    """
    cases = [
        ("<html><body>没有那个 script</body></html>", question.REASON_NO_JSON),
        (
            '<script id="js-initialData" type="text/json">{不是合法 JSON</script>',
            question.REASON_NO_JSON,
        ),
        (
            '<script id="js-initialData" type="text/json">"是个字符串不是对象"</script>',
            question.REASON_NO_JSON,
        ),
        (_page({"questions": {}}), question.REASON_NOT_IN_JSON),
        (_page({}), question.REASON_NOT_IN_JSON),
    ]

    for html, expected in cases:
        _, reason = question._read_detail(html, _url("111"))
        assert reason == expected, html


def test_the_report_names_the_url_it_was_given_not_the_normalised_one() -> None:
    """失败报告里记的是**调用方给的那个 URL**，不是规范化后的。

    ⚠️ 这不是细节：失败报告是给人按图索骥用的，而人要回头去看的是
    **自己当初贴进清单的那个地址**。换成规范化形式的话，日志里的 URL
    和清单里的 URL 对不上，找起来得先在脑子里过一遍 `urlnorm` 的规则。
    """
    raw = f"{_url('111')}?sort=created#comments"

    report = question.QuestionReport(url=raw)

    assert report.url == raw
    assert report.describe().startswith(f"❌ {raw}：")


# ── 五、to_row：进得了 jsonl 才行 ──────────────────────────────────


def test_datetime_is_serialised_in_to_row() -> None:
    """`asked_at` 在 `to_row` 里就转成字符串。

    ⚠️ `ops.append_contents` 是逐行 `json.dumps`，塞不进 `datetime`
    （同 `extract.to_document`）。在 `to_row` 里转而不是在调用方转，
    是因为那样每个调用方都得记得转一次。
    """
    html = _question_html("111", created=1_500_000_000)

    detail = question.detail_from_html(html, _url("111"))
    assert detail is not None
    row = question.to_row(detail)

    assert row["asked_at"] == "2017-07-14T02:40:00+00:00"
    json.dumps(row, ensure_ascii=False), "整行必须能直接 dumps"


def test_a_broken_timestamp_does_not_take_the_whole_detail_down() -> None:
    """`created` 读不动就 `None`，别的字段照采。

    数值太大时 `datetime.fromtimestamp` 会抛 `OverflowError` / `OSError` /
    `ValueError`（各平台不一样）。抛出去的话，一个坏的时间戳会让整条详情
    丢失——而提问时间恰恰是最不重要的那一列（热度才是非回溯的）。
    """
    for bad in (None, "1500000000", 10**20, -1):
        html = _question_html("111", title="标题", created=bad)

        detail = question.detail_from_html(html, _url("111"))

        assert detail is not None, bad
        assert detail.title == "标题"
        assert detail.asked_at is None, bad


def test_to_row_matches_the_fixture_field_for_field() -> None:
    """行里的字段名对齐夹具 `runtime/ops/20260929-090000-fixture/questions.jsonl`。

    ⚠️ 夹具是**这个能力的验收基准**，字段名对不上就等于验收基准作废。
    夹具里没有 `answer_count` / `asked_at`（它写在那次讨论之前），
    所以断言的是"夹具的字段这里都有"，不是"两边的集合相等"。
    """
    fixture_keys = {
        "zhihu_qid",
        "url",
        "title",
        "description",
        "follower_count",
        "view_count",
        "keyword",
        "collected_at",
    }

    row = question.to_row(question.QuestionDetail(zhihu_qid="1", url=_url("1"), title="t", description="d"))

    assert fixture_keys <= set(row), f"夹具里有、这里没有：{fixture_keys - set(row)}"


def test_to_row_leaves_the_keyword_to_the_caller() -> None:
    """`keyword` 由调用方用 `replace()` 带上，`to_row` 只负责搬。

    能力五自己不知道"这条是哪次搜索捞出来的"——那是 `urls.jsonl` 的事
    （同 `UrlEntry.keyword`）。`--url` 临时补采的那种没有来源，是 `None`。
    """
    from dataclasses import replace

    detail = question.QuestionDetail(zhihu_qid="1", url=_url("1"), title="t", description="")

    assert question.to_row(detail)["keyword"] is None
    assert question.to_row(replace(detail, keyword="某公司"))["keyword"] == "某公司"


def test_the_normalised_url_is_what_gets_recorded() -> None:
    """行里记的是**规范化后**的 URL，和 `urls.jsonl` / `fact_content.url` 同一种形态。

    ⚠️ 断点判断靠的就是这个：`_QuestionsDoc` 按 qid 挡重、`cmd_question` 按
    `normalize(url).zhihu_id` 过滤待采——两边必须是同一次规范化的结果，
    否则"采过的"和"要采的"会对不上，同一道题每跑一次采一次。
    """
    detail = question.detail_from_html(
        _question_html("111"), "https://www.zhihu.com/question/111?sort=created"
    )

    assert detail is not None
    assert detail.url == _url("111"), "query 要去掉"


def test_summarize_names_every_kind_of_failure() -> None:
    """汇总必须**按失败原因分组报数**，不能只说"成功 N 失败 M"。

    分组报数才看得出这一趟是"全都不是问题页"（清单给错了）还是
    "全都没加载出来"（被挡了）——两者的处置完全相反。
    """
    reports = [
        question.QuestionReport(url=_url("1"), detail=question.QuestionDetail("1", _url("1"), "t", "")),
        question.QuestionReport(url=_url("2"), reason=question.REASON_NOT_A_QUESTION),
        question.QuestionReport(url=_url("3"), reason=question.REASON_NOT_A_QUESTION),
        question.QuestionReport(url=_url("4"), reason=question.REASON_NO_JSON),
    ]

    text = question.summarize(reports)

    assert "共 4 个" in text
    assert "成功 1 个" in text
    assert "失败 3 个" in text
    assert f"{question.REASON_NOT_A_QUESTION} 2 个" in text
    assert f"{question.REASON_NO_JSON} 1 个" in text
