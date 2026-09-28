"""记录装配测试：衍生字段、长短分流、快照落盘、文档往返、三层取数。

分四部分：

**第一部分：分流与快照**（合成数据，不碰快照文件）。钉住的是几条
**"错了也不报错"**的规则——纯图片评论的哈希、写失败退回内联、
gzip 的可复现性。这几条都属于"静默错误"，只有测试能拦住。

**第二部分：文档往返**（`to_document` ⇄ `from_document`）。采集侧现在
只产文件（决策 52），记录是主程序读文件时装配的——所以"哪些字段进库"
这件事全压在 `from_document` 上，它和 `to_document` 是逆运算，
字段名还故意不一样，只能靠测试钉住。

**第三部分：第一档交叉验证**（真实快照）。这里测的是**判断本身对不对**：
`answer.html` 上 DOM 和 JSON 应该判"一致"（实测差 11 字，是空白归一化的
正常抖动，不能被报成截断）；`search_bottom.html` 上应该判"没验证过"
而不是"没问题"——**把"没查"说成"没问题"是这段代码最可能的错法**。

（`IdIndex` / 批量入库的测试跟着代码搬去 `storage/tests/test_ingest.py` 了。）
"""

from __future__ import annotations

import functools
import gzip
import pathlib
from datetime import UTC, datetime

import pytest

from sentinel_q.collector import extract, parse

CALIB_DIR = pathlib.Path("runtime/calib")

ANSWER_URL = "https://www.zhihu.com/question/22230085/answer/1594809785"
"""`answer.html` 里那条回答的 URL。**必须是带 question 的完整形态。**

`https://www.zhihu.com/answer/1594809785`（裸形态）`normalize()` 认不出来，
返回 None——不是缺陷，是刻意的：知乎的搜索页发的全是
`/question/{qid}/answer/{aid}`（实测 99 个链接，**0 个裸形态**），
而裸形态既要去重又要丢掉 `question_id`（库里那列是平铺的根祖先，
见架构文档 5.5.1）。所以窄一点是**对的**，是这条测试一开始用错了 URL。
"""


# ── 固件 ────────────────────────────────────────────────────────────


@functools.cache
def snapshot(name: str) -> str:
    path = CALIB_DIR / name
    if not path.exists():
        pytest.skip(f"缺快照 {path}（不在仓库里，得由项目所有者本机采集）")
    return path.read_text(encoding="utf-8")


_AUTO = object()
"""`None` 是有意义的值（"没有"），所以"没传"得用一个单独的哨兵。

不这么做的话 `item(html=None)` 会被读成"没传"，于是**永远造不出
"没有 HTML 片段"的样本**——而那条路径（快照留空）恰恰是要测的。
"""


def item(
    *,
    zhihu_id: str | None = "123",
    content_type: str | None = "answer",
    text: str | None = "正文",
    html: object = _AUTO,
    url: object = _AUTO,
    question_id: str | None = None,
    parent_id: str | None = None,
    author_name: str | None = "某人",
    author_url: str | None = "https://www.zhihu.com/people/someone",
) -> parse.ParsedItem:
    if html is _AUTO:
        html = f"<p>{text}</p>" if text else None
    if url is _AUTO:
        url = f"https://www.zhihu.com/{content_type}/{zhihu_id}"
    return parse.ParsedItem(
        url=url,
        content_type=content_type,
        zhihu_id=zhihu_id,
        question_id=question_id,
        text=text,
        html=html,
        author_name=author_name,
        author_url=author_url,
        published_at=datetime(2020, 11, 25, 10, 17, 52, tzinfo=UTC),
        parent_id=parent_id,
    )


NOT_A_PROFILE = "https://www.zhihu.com/people/someone/followers"
"""主页里的一个子页签。最后一段是 `followers`，**不是用户 ID**。"""


# ── 一、长短分流与快照 ──────────────────────────────────────────────


class TestStore:
    def test_short_content_stays_inline_and_writes_no_file(self, tmp_path: pathlib.Path) -> None:
        """短内容（大多数评论）直接存字段，**一个文件都不该产生**。

        这是文档 3.9 第 5 条的原话意图："短内容直接存这里，不用额外建文件，
        减少文件管理负担"。评论动辄几千条，每条一个文件的话，
        文件管理本身就变成一个问题了。
        """
        policy = extract.SnapshotPolicy(root=tmp_path / "snap", inline_limit=500)
        stored = extract.store(item(text="短"), policy)

        assert stored.is_inline
        assert stored.storage_path is None
        assert stored.content_text == "短"
        assert stored.content_length == 1
        # 快照还是要写的——**短内容也要快照**，删帖取不回来的是正文本身
        assert stored.snapshot_path is not None
        assert not list((tmp_path / "snap").glob("content/*/*/*.txt.gz"))

    def test_long_content_goes_to_file_and_leaves_text_null(self, tmp_path: pathlib.Path) -> None:
        policy = extract.SnapshotPolicy(root=tmp_path / "snap")
        stored = extract.store(item(text="字" * 501), policy)

        assert not stored.is_inline
        assert stored.content_text is None
        assert stored.content_length == 501
        # ⚠️ 长正文文件也是 **gzip 过的**（`compress=True`），所以读回来要解压。
        #    键名 `.txt.gz` 如实说了这件事——不然下一个人打开这个"txt"会看到二进制。
        path = policy.local(stored.storage_path)
        assert path.read_bytes()[:2] == b"\x1f\x8b", "gzip 魔数"
        assert gzip.decompress(path.read_bytes()).decode() == "字" * 501

    def test_boundary_is_strictly_greater_than_the_limit(self, tmp_path: pathlib.Path) -> None:
        """恰好等于阈值算**短**。界限两边的行为必须钉死，不然改阈值时容易差一。"""
        policy = extract.SnapshotPolicy(root=tmp_path / "snap", inline_limit=500)
        assert extract.store(item(text="字" * 500), policy).is_inline
        assert not extract.store(item(text="字" * 501), policy).is_inline

    def test_content_text_is_never_null_and_storage_never_set(
        self, tmp_path: pathlib.Path
    ) -> None:
        """长内容落盘时**两个字段不能同时空**——库上的 check 约束会拒收。

        `check (content_text is not null or storage_path is not null)`

        写失败（这里让 root 指向一个**文件**而不是目录，mkdir 必定失败）
        必须退回内联，而不是留两个空字段让整批入库炸掉。
        """
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("我是个文件，不是目录")
        policy = extract.SnapshotPolicy(root=blocker)

        stored = extract.store(item(text="字" * 501), policy)

        assert stored.storage_path is None
        assert stored.content_text == "字" * 501, "写失败必须退回内联，不能丢正文"
        # 退回内联之后 `is_inline` 是 **True** —— 名字说的是"正文存在哪"，
        # 不是"本来打算存在哪"。将来的读取方只看这个字段，不看意图。
        assert stored.is_inline

    def test_image_only_comment_gets_empty_text_not_none(self, tmp_path: pathlib.Path) -> None:
        """纯图片评论：`content_text` 是**空串**，不是 None。

        实测有三条二级回复只有一张图（`comments_second_level.html` 里的
        11541195635 / 11542249778 / 11573411523）。空串能过 check 约束
        （约束说的是 `is not null`），None 不行——一条表情包评论
        会让整批插入报错。
        """
        policy = extract.SnapshotPolicy(root=tmp_path / "snap")
        stored = extract.store(item(text="", html='<img src="x.jpg">'), policy)

        assert stored.content_text == ""
        assert stored.content_text is not None
        assert stored.content_length == 0

    def test_image_only_comments_get_different_hashes(self, tmp_path: pathlib.Path) -> None:
        """⭐ 两个不同的纯图片评论，哈希**必须不同**。

        这是"一律哈希 text"这个写法会踩的坑：它们的 text 都是空串，
        于是所有图片评论拿到同一个哈希——**恰好废掉了这个字段唯一的用途**
        （重抓时比对"对方改没改过"）。所以没有文字时改哈希 HTML 片段。

        这条测试是专门为这个失败模式写的，不是凑数的。
        """
        policy = extract.SnapshotPolicy(root=tmp_path / "snap")
        a = extract.store(item(text="", html='<img src="a.jpg">'), policy)
        b = extract.store(item(text="", html='<img src="b.jpg">'), policy)

        assert a.raw_content_hash != b.raw_content_hash

    def test_hash_is_stable_for_the_same_text(self, tmp_path: pathlib.Path) -> None:
        """同样的正文 → 同样的哈希。**这是 `raw_content_hash` 存在的全部意义**：
        重抓时哈希变了才说明对方改过内容（网暴场景里的"删改小作文"）。
        """
        policy = extract.SnapshotPolicy(root=tmp_path / "snap")
        a = extract.store(item(text="原文"), policy)
        b = extract.store(item(text="原文"), policy)
        c = extract.store(item(text="原文。"), policy)

        assert a.raw_content_hash == b.raw_content_hash
        assert a.raw_content_hash != c.raw_content_hash

    def test_hash_covers_the_text_only_not_the_id(self, tmp_path: pathlib.Path) -> None:
        """同一条内容换个 ID 重抓，哈希应当不变——哈希的是**内容**，不是身份。"""
        policy = extract.SnapshotPolicy(root=tmp_path / "snap")
        a = extract.store(item(zhihu_id="1", text="原文"), policy)
        b = extract.store(item(zhihu_id="2", text="原文"), policy)
        assert a.raw_content_hash == b.raw_content_hash

    def test_gzip_output_is_byte_identical_across_writes(self, tmp_path: pathlib.Path) -> None:
        """⭐ gzip 头里默认带**当前时间**，同一个快照间隔一秒写两次字节就不同。

        定死 mtime 之后，"内容相同 ⟺ 字节相同"才成立——
        将来想用文件本身做校验、或者比对两轮采集的快照有没有变，
        靠的就是这个。不做这一步的话，每次比对都会报"变了"。
        """
        policy = extract.SnapshotPolicy(root=tmp_path / "snap")
        first = extract.store(item(text="正文", html="<p>正文</p>"), policy)
        raw_a = policy.local(first.snapshot_path).read_bytes()
        second = extract.store(item(text="正文", html="<p>正文</p>"), policy)
        raw_b = policy.local(second.snapshot_path).read_bytes()

        assert raw_a == raw_b
        assert gzip.decompress(raw_a).decode() == "<p>正文</p>"

    def test_snapshot_key_is_namespaced_by_type(self, tmp_path: pathlib.Path) -> None:
        """⚠️ 键里带类型。知乎的回答 ID 和文章 ID 是**两套独立编号**，会撞号。

        文档写的是扁平的 `content/{id}.txt`。真撞号的表现是
        `content/123.html.gz` 被两个不同东西互相覆盖，**而且两边的日志都正常**。
        """
        policy = extract.SnapshotPolicy(root=tmp_path / "snap")
        ans = extract.store(item(content_type="answer", zhihu_id="123"), policy)
        art = extract.store(item(content_type="article", zhihu_id="123"), policy)

        assert ans.snapshot_path != art.snapshot_path
        assert "answer" in ans.snapshot_path
        assert "article" in art.snapshot_path

    def test_missing_html_fragment_leaves_snapshot_path_null(self, tmp_path: pathlib.Path) -> None:
        """解析层没给 HTML 片段时，`snapshot_path` 留空，**不报错**。

        正文还在，而且"这条没快照"在库里看得见。硬造一个空文件出来
        反而更糟：它看起来像有快照。
        """
        policy = extract.SnapshotPolicy(root=tmp_path / "snap")
        stored = extract.store(item(html=None), policy)
        assert stored.snapshot_path is None


# ── 二、记录装配 ──────────────────────────────────────────


class TestFromDocument:
    """⭐ `contents.jsonl` 的一行 → `ContentRecord`。**`main ingest` 走的就是这条路。**

    采集侧不装配记录了（决策 52），所以"哪些字段进库、哪些不进"这件事
    现在全部发生在这一层——它取代了原来那个收 `ParsedItem` 的 `to_record()`。

    ⚠️ 它是 `to_document()` 的**逆**，所以这里的样本一律走真的 `to_document()`
    造出来，不手写 dict：手写的话测的是"我以为文档长什么样"，而不是
    "文档真的长什么样"，字段一改名两边一起错，测试还是绿的。
    """

    def _row(self, tmp_path: pathlib.Path, **kwargs: object) -> dict:
        policy = extract.SnapshotPolicy(root=tmp_path / "snap")
        return extract.to_document(item(**kwargs), policy=policy)

    def test_unrecognised_content_type_is_refused(self, tmp_path: pathlib.Path) -> None:
        """⭐ 类型认不出来就**拒绝入库**，绝不兜底成 "unknown"。

        库上写着 `check (content_type in (...))` 五个值。
        编一个枚举值出来的后果是：**插入那一刻**违反约束，
        炸掉的是一整批，而且报错信息指向 SQL 而不是"类型没认出来"。
        """
        row = self._row(tmp_path)
        assert extract.from_document({**row, "content_type": None}) is None
        assert extract.from_document({**row, "content_type": "pin"}) is None
        for kind in extract.CONTENT_TYPES:
            assert extract.from_document({**row, "content_type": kind}) is not None

    def test_missing_id_or_url_is_refused(self, tmp_path: pathlib.Path) -> None:
        """两个必填项：`zhihu_id` 是 `not null`，`url` 是唯一约束和去重的依据。"""
        row = self._row(tmp_path)
        assert extract.from_document({**row, "zhihu_id": None}) is None
        assert extract.from_document({**row, "url": ""}) is None

    def test_the_question_id_stays_a_zhihu_string(self, tmp_path: pathlib.Path) -> None:
        """⭐ 记录里装的是**知乎的问题 ID**，不是库里那个 bigint（决策 52）。

        换 ID 是数据库模块的事，采集侧换不了，也**不该顺手拿
        `claim_question()` 去换**（那会把"先插后问"的抢占领掉，
        那个问题就永远不会被送去判相关性，而且全程不报错）。
        """
        record = extract.from_document(self._row(tmp_path, question_id="19581646"))
        assert record is not None
        assert record.question_zhihu_id == "19581646"
        assert not hasattr(record, "question_id"), "库里那个 bigint 不是采集侧的字段"

    def test_the_parent_stays_a_zhihu_id(self, tmp_path: pathlib.Path) -> None:
        """父级同理：这里是知乎的 ID，落库时由 `storage.ingest.IdIndex` 换成 uuid。"""
        record = extract.from_document(
            self._row(tmp_path, content_type="comment", parent_id="100")
        )
        assert record is not None
        assert record.parent_zhihu_id == "100"
        assert not hasattr(record, "parent_id"), "库里那个 uuid 不是采集侧的字段"

    def test_author_comes_from_the_last_segment_of_the_profile_link(
        self, tmp_path: pathlib.Path
    ) -> None:
        """`author_zhihu_id` 是 `dim_author` 的自然键，而它取自链接最后一段。

        文档里**没有**这个字段——它是从 `author_url` 现推出来的。所以这条
        测的是"推得对"，而不是"搬得对"。

        昵称和链接也一并带上——`ensure_author` 是个 upsert，
        那两列每次都要用最新值覆盖（见 `Repo.ensure_author` 的说明）。
        """
        record = extract.from_document(self._row(tmp_path))

        assert record is not None
        assert record.author_zhihu_id == "someone"
        assert record.author_name == "某人"
        assert record.author_url == "https://www.zhihu.com/people/someone"

    def test_a_link_that_is_not_a_profile_leaves_the_author_null(
        self, tmp_path: pathlib.Path
    ) -> None:
        """⭐ 子页签链接**不能**取最后一段当用户 ID。

        `/people/someone/followers` 的最后一段是 `followers`。照取的话，
        每个用户的关注者页都会变成一个"新用户"，脏维度行安静地累积，
        而且和真实用户混在同一列里分不开——比空着危险得多。

        ⚠️ 昵称和链接**照样带着**：那是"本来就有、只是挂不上作者"，
        `storage.ingest` 靠这个区别把它计进 `unresolved_authors`
        （和匿名回答的"本来就没有"分开）。
        """
        record = extract.from_document(self._row(tmp_path, author_url=NOT_A_PROFILE))

        assert record is not None
        assert record.author_zhihu_id is None
        assert record.author_url == NOT_A_PROFILE, "链接要留着，缺口才有得报"

    def test_published_at_survives(self, tmp_path: pathlib.Path) -> None:
        """绝对时间戳必须传下去——取证时"什么时候发的"经常比正文还重要。

        它在文档里是 ISO8601 字符串（JSONL 装不下 `datetime`），
        到这里要变回 `datetime` 才进得了库。
        """
        record = extract.from_document(self._row(tmp_path))
        assert record is not None
        assert record.published_at == datetime(2020, 11, 25, 10, 17, 52, tzinfo=UTC)

    def test_a_broken_timestamp_loses_the_time_but_not_the_row(
        self, tmp_path: pathlib.Path
    ) -> None:
        """⚠️ 时间戳读不出来时**只丢时间，不丢这条内容**。

        文件是机器写的，所以正常路径上不会坏；坏了说明有人手改过。
        那时让整批入库停在一行脏数据上，比少一个时间戳贵得多——
        行还在文件里、警告也打出来了。
        """
        row = {**self._row(tmp_path), "published_at": "2020年11月25日"}
        record = extract.from_document(row)
        assert record is not None
        assert record.published_at is None
        assert record.zhihu_id == "123", "其余字段照旧"

    def test_a_round_trip_keeps_every_field_that_matters(
        self, tmp_path: pathlib.Path
    ) -> None:
        """⭐ 这条是防漂移的：`to_document` / `from_document` 互为逆。

        两边的字段名故意不一样（文档里叫 `text_length` / `parent_id` /
        `question_id`，记录里叫 `content_length` / `parent_zhihu_id` /
        `question_zhihu_id`），所以没法用 `asdict()` 一把梭。改了文档的字段名
        却忘了改这里，表现是**那一列静默变空**——没有报错，只是库里少东西。
        """
        policy = extract.SnapshotPolicy(root=tmp_path / "snap")
        original = item(
            content_type="comment",
            zhihu_id="c1",
            text="很长的评论" * 200,  # 过阈值（500 字），走 storage_path 那条路
            question_id="19581646",
            parent_id="456",
        )
        row = extract.to_document(original, policy=policy, keyword="某公司")
        record = extract.from_document(row)

        assert record is not None
        assert record.url == original.url
        assert record.content_type == original.content_type
        assert record.zhihu_id == original.zhihu_id
        assert record.question_zhihu_id == original.question_id
        assert record.parent_zhihu_id == original.parent_id
        assert record.author_name == original.author_name
        assert record.author_url == original.author_url
        assert record.content_length == len(original.text or "")
        assert record.raw_content_hash == extract.content_hash(original)
        assert record.published_at == original.published_at

        # ⚠️ 长正文这一对是**分开**的，正是它俩最容易接错
        assert record.storage_path is not None, "长正文该落文件"
        assert record.content_text is None, "落了文件，库里那一列就留空"
        assert row["text"] == original.text, "给人看的那份仍是完整正文"


# ── 三、第一档：内嵌 JSON 交叉验证 ──────────────────────────────────


class TestJsonState:
    def test_reads_initial_state(self) -> None:
        state = extract.extract_json_state(snapshot("answer.html"))
        assert state is not None
        assert "entities" in state

    def test_returns_none_instead_of_raising_on_junk(self) -> None:
        """知乎换掉这个 script 的形态时应当**安静地降级**。

        第一档本来就是加分项：缺了它 DOM 路径照样跑，只是少一层交叉验证。
        为一个"没有这层验证"的页面抛异常，等于把可选功能变成了单点故障。
        """
        assert extract.extract_json_state("<html><body>啥也没有</body></html>") is None
        assert (
            extract.extract_json_state(
                '<html><script id="js-initialData">这不是 JSON</script></html>'
            )
            is None
        )
        assert (
            extract.extract_json_state(
                '<html><script id="js-initialData">{"a":1}</script></html>'
            )
            is None
        ), "没有 initialState 也算读不动"

    def test_find_entity_maps_type_to_the_right_bucket(self) -> None:
        """类型 → 实体名的映射错了会**永远返回 None**，于是所有内容都
        被判成"没验证过"——看起来像"第一档对我这套页面都不适用"，
        实际是我把 `answers` 写成了 `answer`。"""
        state = {"entities": {"answers": {"1": {"content": "x"}}}}
        assert extract.find_entity(state, "answer", "1") == {"content": "x"}
        assert extract.find_entity(state, "article", "1") is None
        assert extract.find_entity(state, "comment", "1") is None
        assert extract.find_entity(state, None, "1") is None
        assert extract.find_entity(state, "answer", None) is None

    def test_every_content_type_has_a_bucket(self) -> None:
        for kind in extract.CONTENT_TYPES:
            assert kind in extract.JSON_ENTITY_KEYS, f"{kind} 没有对应的实体名"


class TestCrossCheckAgainstRealSnapshots:
    """⭐ 这一组测的是**判断本身对不对**，用真实页面做判据。"""

    def test_answer_page_agrees(self) -> None:
        """`answer.html`：DOM 与 JSON 的正文**应当判为一致**。

        实测 DOM 18,869 字 / JSON 转文本 18,858 字。差的 11 个字是
        DOM 文本经过空白归一化的正常抖动。**这条测试真正防的是误报**：
        如果容差写得太死，每一条都会报"可能被截断"，报告变成噪声，
        人就再也不看它了——判据等于没有。
        """
        html = snapshot("answer.html")
        parsed = parse.parse_content_page(html, ANSWER_URL)
        assert parsed is not None, "answer.html 里取不到那条回答——校准可能过期了"

        check = extract.cross_check_json(parsed, html)

        assert check.checked, f"应当能验证，实际：{check.note}"
        assert check.truncated_flag is False, "实测这条没有被截断"
        assert check.agree, check.describe(parsed)

    def test_answer_page_lengths_are_close(self) -> None:
        """两个来源的字数量级必须一致。

        差一个数量级就是取错了元素（比如取到了外层容器）；差几个字是
        空白归一化。**这条测试的实际价值是给"容差"定一个实测的锚点**——
        实测差 11 字，`cross_check_json` 留的余量比它宽得多。
        """
        html = snapshot("answer.html")
        parsed = parse.parse_content_page(html, ANSWER_URL)
        assert parsed is not None, "校准可能过期了"

        check = extract.cross_check_json(parsed, html)
        assert check.json_length and check.dom_length
        diff = abs(check.json_length - check.dom_length)
        assert diff < 100, f"两个来源差了 {diff} 字，不是空白归一化能解释的"

    def test_truncated_flag_true_forces_disagreement(self) -> None:
        """⭐ 页面自己标了 `contentNeedTruncated` 就必须判可疑，**不管长度像不像**。

        长度比较是启发式，`contentNeedTruncated` 是页面自己说的。
        启发式说"看着没问题"不能推翻页面的自述——这正是正文页上
        **没有**「阅读全文」按钮（实测 0 个）时唯一的截断信号。
        """
        import json

        html = (
            '<html><script id="js-initialData">'
            + json.dumps(
                {
                    "initialState": {
                        "entities": {
                            "answers": {
                                "1": {
                                    "content": "<p>短</p>",
                                    "contentNeedTruncated": True,
                                }
                            }
                        }
                    }
                }
            )
            + "</script></html>"
        )
        parsed = item(zhihu_id="1", content_type="answer", text="短")

        check = extract.cross_check_json(parsed, html)

        assert check.checked
        assert check.truncated_flag is True
        assert not check.agree and check.suspect
        assert "contentNeedTruncated" in check.describe(parsed)

    def test_unchecked_says_why_and_is_not_a_pass(self) -> None:
        """⭐ **"没查"和"没问题"必须能分开。**

        这是这段代码最可能的错法：把没验证过的当成验证通过的，
        于是一整页没查过的内容在报告里都是"✅ 一致"。
        `checked=False` 时 `suspect` 必须是 False（不是错误）但
        `describe()` 必须说清楚为什么没查。
        """
        parsed = item(zhihu_id="1", content_type="thought")
        check = extract.cross_check_json(parsed, snapshot("search_bottom.html"))

        assert not check.checked
        assert not check.suspect, "没查不等于有问题"
        assert "没实测过" in check.describe(parsed)

    def test_search_page_entities_is_empty_so_nothing_is_claimed(self) -> None:
        """搜索页的 entities 实测是空的。在那种页面上不该下任何结论。"""
        html = snapshot("search_bottom.html")
        state = extract.extract_json_state(html)
        if state is None:
            pytest.skip("这张快照里没有可读的 initialState")

        parsed = item(zhihu_id="1594809785", content_type="answer")
        check = extract.cross_check_json(parsed, html)

        assert not check.checked
        assert "entities" in check.note

    def test_article_page_agrees(self) -> None:
        """文章页也要判一致——**走 DOM 路径取正文，不是从 JSON 里拿**。

        ⚠️ 这很重要：如果正文是从 JSON 里拿的、再拿同一份 JSON 去验证，
        那就是自己跟自己比，`agree` 恒为真，**这条测试等于什么都没测**。
        走的必须是真实的采集路径（`parse_content_page`），
        JSON 只用来当裁判。
        """
        html = snapshot("article.html")
        state = extract.extract_json_state(html)
        if state is None:
            pytest.skip("这张快照里没有可读的 initialState")
        entities = state.get("entities", {}).get("articles", {})
        if not entities:
            pytest.skip("article.html 的 entities 里没有文章")

        zhihu_id = next(iter(entities))
        parsed = parse.parse_content_page(html, f"https://zhuanlan.zhihu.com/p/{zhihu_id}")
        assert parsed is not None, "校准可能过期了"

        check = extract.cross_check_json(parsed, html)

        assert check.checked
        assert check.agree, check.describe(parsed)
        assert check.json_length and check.json_length > 1000, "文章正文不该这么短"


class TestCrossCheckRun:
    def test_pairs_stay_aligned(self) -> None:
        """⭐ `CrossCheckRun` 把 item 和 check 绑在一起，就是为了防**错位**。

        两个平行列表传参的话，顺序错了不会报错，只会把 A 内容的判断
        安到 B 头上——而且两个列表恰好等长时谁也看不出来。
        """
        run = extract.CrossCheckRun(
            pairs=[
                (item(zhihu_id="1"), extract.CrossCheck(checked=True, agree=True)),
                (
                    item(zhihu_id="2"),
                    extract.CrossCheck(checked=True, agree=False, dom_length=1),
                ),
            ]
        )
        assert [i.zhihu_id for i, _ in run.suspects] == ["2"]
        assert run.report() == 1

    def test_report_counts_suspects(self) -> None:
        run = extract.CrossCheckRun(
            pairs=[
                (item(zhihu_id="1"), extract.CrossCheck(checked=False, note="没查")),
                (item(zhihu_id="2"), extract.CrossCheck(checked=True, agree=False)),
            ]
        )
        assert run.report() == 1, "没查的不算可疑，但也不该被算成通过"
