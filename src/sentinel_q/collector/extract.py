"""记录装配：衍生字段、长短内容分流、快照落盘、三层取数（架构文档 3.9 / 3.10 / 3.11）。

`parse.py` 把一段 HTML 变成 `ParsedItem`——**知乎给的原始数据**。
这个模块把它变成两样东西，中间要补三样东西：

    1. 衍生字段    content_length（字数）、raw_content_hash（原文 sha256）
    2. 长短分流    超阈值的正文落文件、`storage_path` 指过去，`content_text` 留空
    3. 快照落盘    正文片段的 HTML（gzip）——**取证的核心资产**

那两样东西是：

    to_document(item)  →  contents.jsonl 的一行（**顺带把上面三样落到磁盘**）
    from_document(row) →  ContentRecord（主程序入库时用，纯函数、不碰磁盘）

采集那一步只走第一条（决策 52：采集只产文件）。第二条走的是同一个衔接面——
AI 判定结果也写在**同一行**上，所以 `main ingest` 读的就是这一行。

## ⚠️ 关于快照：这是本项目唯一"过期不候"的数据

"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from bs4 import BeautifulSoup

from sentinel_q.collector import parse, selectors
from sentinel_q.shared import urlnorm
from sentinel_q.shared.models import ContentRecord

log = logging.getLogger(__name__)

INLINE_LIMIT = 500
"""短内容的分界线（字数）。架构文档 3.9 第 5 条。

超过它的正文落成文件，`content_text` 留空只存 `storage_path`——
避免大文本拖慢数据库、占满 Supabase 免费额度。评论大多在这条线以下
（实测最长的一条 200 多字），所以**绝大多数评论是内联存的**，没有文件开销。
"""

FACT_CONTENT_TYPES: frozenset[str] = frozenset(
    {"answer", "article", "thought", "comment"}
)
"""**能进 `fact_content` 的类型**（`ContentRecord.content_type` 的一个子集）。
与库上那条 check 约束必须一字不差。改这里之前先改迁移脚本，
否则代码会写进一个库拒收的值。

⚠️ 2026-09-28（迁移 0004）起**没有 `question`**：问题只进 `dim_question`，
不进 `fact_content`（决策 29 已重写）。所以这里是 **4 个，而下面的
`JSON_ENTITY_KEYS` 是 5 个——两个集合故意不一样，别顺手对齐**：
那个管的是"认识怎么解析"，问题页仍然要解析（能力五要读它页面里的实体）。
"""

JSON_ENTITY_KEYS: dict[str, str] = {
    "question": "questions",
    "answer": "answers",
    "article": "articles",
    "thought": "pins",
    "comment": "comments",
}
"""`ContentRecord.content_type` → `js-initialData` 里对应的实体名。实测的键名。

⚠️ 这是"**认识怎么解析**"那一侧：它比 `FACT_CONTENT_TYPES` 多一个 `question`。
所以 `CONTENT_TYPES` 那个旧名字**不能当判据**——拿它当"能不能入库"用，
问题页就会被放行到 `insert_content`，然后撞在 check 约束上。
"""

TIER1_VERIFIED: frozenset[str] = frozenset({"question", "answer", "article"})
"""第一档**实测有效**的页面类型。其余类型不要拿它下结论。

实测（数字见 `selectors.INITIAL_STATE_SCRIPT`）：

    正文页（回答/文章）   entities.answers/articles = 1     ← 有效
    问题页               entities.answers = 5            ← 只是首屏，不是全部
    搜索页 / 评论         entities 全是 0                  ← 完全无效
    想法详情页            没有快照，**未知**

⚠️ `thought` 不在集合里不是因为它无效，是因为**没实测过**。
`thought.html` 是搜索列表页（pins 是 0，符合预期），想法详情页的快照还没有。
没验证过的页面类型上，"JSON 里没有这条"和"第一档在这页上不работа"分不清，
所以那种页面**不下结论**，只在报告里标一句"未验证"，而不是报一个假警。
"""

_TRUNCATED_FLAG = "contentNeedTruncated"


# ── 快照与分流 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class SnapshotPolicy:
    """快照往哪写、正文多长算长。

    `root` 是**本地暂存目录**：文件写在这里，再由 `storage` 上传到
    Supabase Storage。而记进库的 `snapshot_path` / `storage_path` 是
    **Storage 里的对象键**（文档里的例子 `content/{id}.txt` 就是这个意思），
    两者按 `root / key` 对应，所以本地文件任何时候都能重建、删了也不心疼。
    """

    root: Path
    inline_limit: int = INLINE_LIMIT
    compress: bool = True
    """gzip。实测能压到原始体积的 1/4 左右（文档 3.10 的方案表按这个算的账）。"""

    def local(self, key: str) -> Path:
        """对象键 → 本地暂存路径。两者按这个固定关系对应，**没有第二套命名**。"""
        return self.root / key

    def name(self, stem: str) -> str:
        """`text.txt` + compress → `text.txt.gz`。后缀如实反映存的是什么。"""
        return f"{stem}.gz" if self.compress else stem


@dataclass(frozen=True)
class Stored:
    """一条内容的正文与快照落盘结果，就是 `ContentRecord` 里那几列。

    ⚠️ **只有 `snapshot_path`（正文片段），没有下面这两个**：

        html_snapshot_path  完整网页 HTML   ← 架构文档 3.10：**仅高风险内容**存
        screenshot_path     整页截图        ← 同上

    那两样都是"高风险"才存的，而**是不是高风险要等AI 模块判完才知道**，
    采集那一刻无从判断。所以采集侧一律留空，由后续模块或人工回填。
    这不是漏掉的字段，是**刻意不填**——每条内容都截图的话，
    单张 200KB~1MB，必然超支（文档 3.10 算过这笔账）。
    """

    content_text: str | None
    storage_path: str | None
    content_length: int
    raw_content_hash: str
    snapshot_path: str | None = None

    @property
    def is_inline(self) -> bool:
        return self.storage_path is None


def content_hash(item: parse.ParsedItem) -> str:
    """`raw_content_hash`：这条东西**原文**的 sha256。

    ⚠️ 哈希的原料要分两种，不能一律哈希 `text`：
    纯图片评论的 `text` 是空串，**所有纯图片评论都会得到同一个哈希**——
    那正好废掉了这个字段唯一的用途（重抓时比对"对方改没改过"）。
    没有文字就拿它那段 HTML 片段当原料，那才是这条评论的实际内容。

    抽成函数是因为 `store()` 和 `to_document()` 都要用它：两边各算一份的话，
    将来改了一处，库里的哈希和落盘文档里的就对不上了，而且没人会立刻发现。
    """
    material = (item.text or "") if (item.text or "").strip() else (item.html or "")
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def store(item: parse.ParsedItem, policy: SnapshotPolicy) -> Stored:
    """把一条 `ParsedItem` 变成能入库的那几列，顺便落盘。**不抛异常。**

    ## ⚠️ 纯图片评论的正文是空串，这是刻意的

    实测有三条二级回复只有一张图（表情包/截图），`get_text()` 是空的。
    而库上有一条 check 约束：

        check (content_text is not null or storage_path is not null)

    两条路都留空的话**这条记录根本插不进去**——一条纯图片评论会让
    整批插入报错。存空串能过约束（约束说的是 `is not null`，不是 `<> ''`），
    而且它是一句真话："这条评论没有文字"。

    它是不是"内容"由下游判断（AI 模块会看到空正文），**采集侧不替它下结论**。
    """
    text = item.text or ""
    length = len(text)
    digest = content_hash(item)

    storage_key: str | None = None
    inline_text: str | None = text
    if length > policy.inline_limit:
        key = _key(item, policy.name("text.txt"))
        if _write(policy.local(key), text.encode("utf-8"), policy):
            storage_key, inline_text = key, None
        # ⚠️ 写不成功就**退回内联**，而不是留两个空字段：
        #    库上有 `check (content_text is not null or storage_path is not null)`，
        #    两边都空这条记录**根本插不进去**——一条正文会让整批入库报错。
        #    一坨大文本挤在库里是浪费，但比丢内容好。

    return Stored(
        content_text=inline_text,
        storage_path=storage_key,
        content_length=length,
        raw_content_hash=digest,
        snapshot_path=_store_snapshot(item, policy),
    )


def _store_snapshot(item: parse.ParsedItem, policy: SnapshotPolicy) -> str | None:
    """存**正文片段的 HTML**（不是整页）。返回 Storage 对象键，失败返回 None。

    只存片段是算过账的（文档 3.10）：整页 300KB~1.5MB，5 万条就是 15~75GB，
    远超 Supabase 免费 1GB；正文片段 gzip 后 2~8KB，5 万条约 100~400MB，放得下。

    `item.html` 是解析时就把正文那一块留下来的，所以这里不需要页面。
    """
    if not item.html:
        # 没拿到 HTML 片段（解析层没给）。不报错——正文本身还在，
        # 而且 `snapshot_path` 为空这件事在库里是看得见的。
        return None
    key = _key(item, policy.name("page.html"))
    if not _write(policy.local(key), item.html.encode("utf-8"), policy):
        return None
    return key


def _key(item: parse.ParsedItem, filename: str) -> str:
    """Storage 对象键。**按类型分目录**，不是文档例子里那种扁平写法。

    文档写的是 `content/{id}.txt`。这里加上类型一层是因为知乎的
    回答 ID 和文章 ID 是**两套独立编号**，理论上会撞号——撞了就是
    `runtime/snapshots/content/123.html.gz` 被两个不同东西互相覆盖，
    而且**两边的日志都正常**。多一层目录比事后查这种事便宜。
    """
    return f"content/{item.content_type or 'unknown'}/{item.zhihu_id}/{filename}"


def _write(path: Path, data: bytes, policy: SnapshotPolicy) -> bool:
    """写一个文件（按需 gzip）。**失败只记日志，不抛。**

    一条内容的快照写不出来，不该让整批 500 条停下来。但**也不该安静地过去**：
    `snapshot_path=None` 会跟着记录一起入库，复查时看得见哪些条缺快照。
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if policy.compress:
            # mtime=0：gzip 头里默认带当前时间，两次写同样的内容会得到不同的字节。
            # 定死它，快照文件才是"内容相同 ⟺ 字节相同"，将来做校验才有意义。
            data = gzip.compress(data, mtime=0)
        path.write_bytes(data)
        return True
    except OSError as exc:
        log.error(
            "快照写失败：%s（%s）。\n"
            "   ⚠️ **这条内容没有快照**，将来对方删帖就取不回来了。"
            "记录会照常入库，`snapshot_path` 留空——复查时能看出哪些条缺。",
            path,
            exc,
        )
        return False


# ── 第一档：内嵌 JSON，当交叉验证用 ─────────────────────────────────


@dataclass(frozen=True)
class CrossCheck:
    """第一档对"我们取到的正文是全的吗"给出的判断。"""

    checked: bool
    """这次到底有没有下结论。没下结论的原因写在 `note` 里。"""

    agree: bool = True
    truncated_flag: bool | None = None
    """页面自己说的 `contentNeedTruncated`。`None` = JSON 里没有这个字段。"""

    json_length: int | None = None
    dom_length: int | None = None

    note: str = ""

    @property
    def suspect(self) -> bool:
        return self.checked and not self.agree

    def describe(self, item: parse.ParsedItem) -> str:
        if not self.checked:
            return f"{item.content_type} {item.zhihu_id} 没做第一档交叉验证：{self.note}"
        if self.agree:
            return (
                f"{item.content_type} {item.zhihu_id} 正文长度对得上"
                f"（DOM {self.dom_length} 字 / 页面数据 {self.json_length} 字）"
            )
        why = "页面自己标了 contentNeedTruncated" if self.truncated_flag else "两边长度差得多"
        return (
            f"❌ {item.content_type} {item.zhihu_id} 的正文**可能不全**（{why}）："
            f"DOM 取到 {self.dom_length} 字，页面数据里有 {self.json_length} 字。"
            "**别当成全文入库**，人工看一眼再决定"
        )


def extract_json_state(html: str) -> dict | None:
    """读出 `script#js-initialData` 里的 JSON。没有/读不动返回 None。

    这是**服务端渲染那一刻**的快照，点筛选、滚动、开弹窗之后加载出来的
    东西不会写回它（实测，见 `selectors.INITIAL_STATE_SCRIPT`）。
    """
    soup = BeautifulSoup(html, "html.parser")
    script = soup.select_one(selectors.INITIAL_STATE_SCRIPT)
    if script is None or not script.string:
        return None
    try:
        data = json.loads(script.string)
    except (ValueError, TypeError):
        # 知乎换掉这个 script 的形态了。不是错误——第一档本来就是加分项，
        # 缺了它 DOM 路径照样跑，只是少一层交叉验证。
        return None
    # ⚠️ 先确认它是个对象再 `.get()`：JSON 合法但顶层是字符串/数组/数字时，
    #    以前会当场 `AttributeError` 抛出去。这跟本函数自己那句"读不动返回
    #    None"是矛盾的，而且调用方（`question.detail_from_html`、
    #    `cross_check_json`）都按"不抛"写的——一次形态变化会从
    #    "少一层交叉验证"变成"整批中断"。
    if not isinstance(data, dict):
        return None
    state = data.get("initialState")
    return state if isinstance(state, dict) else None


def find_entity(state: Mapping, content_type: str | None, zhihu_id: str | None) -> dict | None:
    """在 `initialState.entities` 里找这条内容对应的实体。找不到返回 None。"""
    if not content_type or not zhihu_id:
        return None
    bucket = JSON_ENTITY_KEYS.get(content_type)
    if bucket is None:
        return None
    entities = state.get("entities")
    if not isinstance(entities, Mapping):
        return None
    group = entities.get(bucket)
    if not isinstance(group, Mapping):
        return None
    entity = group.get(str(zhihu_id))
    return entity if isinstance(entity, dict) else None


def cross_check_json(item: parse.ParsedItem, page_html: str) -> CrossCheck:
    """拿第一档验证**正文是不是全文**。不改变任何数据，只下判断。

    这条判据补的是一个真实的盲区：正文页上**没有**「阅读全文」按钮
    （实测 0 个），所以 `content.py` 的截断检查在正文页上是空操作。
    没有它的话，"正文被截断了"在采集侧**不可见**——而那是这个项目
    最不能接受的一类失败（采到的东西看着完全正常，只是少了一截）。

    ⚠️ 长度比较留了 5% + 200 字的余量：DOM 文本经过空白归一化，
        和 JSON 里的 HTML 转文本天然会有几个字符的出入（实测回答页差 11 字）。
        把这种正常抖动报成截断，就变成了天天误报，等于没有判据。
    """
    if item.content_type not in TIER1_VERIFIED:
        return CrossCheck(
            checked=False,
            note=f"{item.content_type} 这一档没实测过（见 TIER1_VERIFIED）",
        )

    state = extract_json_state(page_html)
    if state is None:
        return CrossCheck(checked=False, note="页面里没有可读的 js-initialData")

    entity = find_entity(state, item.content_type, item.zhihu_id)
    if entity is None:
        return CrossCheck(
            checked=False,
            note=(
                f"entities.{JSON_ENTITY_KEYS[item.content_type]} 里没有这条"
                "（搜索页/评论页的 entities 实测就是空的）"
            ),
        )

    dom_length = len(item.text or "")
    raw = entity.get("content")
    json_length = len(html_to_text(raw)) if raw else None
    flag = entity.get(_TRUNCATED_FLAG)
    truncated = flag if isinstance(flag, bool) else None

    if truncated:
        return CrossCheck(
            checked=True,
            agree=False,
            truncated_flag=True,
            json_length=json_length,
            dom_length=dom_length,
            note="页面数据自己标了截断",
        )

    if json_length is None:
        return CrossCheck(
            checked=True,
            json_length=None,
            dom_length=dom_length,
            note="实体里没有正文可比",
        )

    agree = dom_length + 200 >= json_length * 0.95
    return CrossCheck(
        checked=True,
        agree=agree,
        truncated_flag=truncated,
        json_length=json_length,
        dom_length=dom_length,
    )


def html_to_text(raw: object) -> str:
    """JSON 里的正文是 HTML，转成文本。

    ⚠️ **公开的**（原 `_text_of`）：`question.py` 要用它把问题描述从
    `detail` 那段 HTML 转成纯文本。不叫 `text_of` 是因为
    `drive.text_of(target, selector)` 已经占了这个名字，含义还不一样
    （那个是"按选择器取元素文案"）。

    传进来的不是字符串就返回空串——**不抛**。调用方据此判断"这条没有正文"。
    """
    if not isinstance(raw, str):
        return ""
    return BeautifulSoup(raw, "html.parser").get_text("\n", strip=True)


# ── 落盘文档 ────────────────────────────────────────────────────────


def to_document(
    item: parse.ParsedItem,
    *,
    policy: SnapshotPolicy,
    keyword: str | None = None,
    collected_at: str | None = None,
) -> dict:
    """`ParsedItem` → 写进 `contents.jsonl` 的那一行。**顺带把文件落了。**

    ## 这份文档是"采集 → 分析 → 入库"的衔接面（决策 51）

    不变量是"AI 判完才入库"，所以采集期间库里看不到本轮内容——AI 判定结果与
    断点标记都落在这份文档的**同一行**上，"哪一行还没有结果"就是断点。
    它仍满足架构文档 7.8 推论二（重爬一次 + 重问一次 AI 即可重建）。

    ## ⚠️ 落盘这一步就在这里，而且只在这里

    `store()` 在这一层调：长正文的正文文件、正文片段的 HTML 快照，都是这一步
    写出去的。以前它在 `to_record()` 里，而采集侧现在**不装配 `ContentRecord`**
    了（那是 `main ingest` 的事，决策 52）——写文件这件事必须跟着搬过来。
    不搬的后果很难看：`snapshot_path` 指向一个**从来没被写出来的文件**，
    字段是满的、日志是正常的，只有将来删帖要取证时才发现快照根本不存在。

    ## 两个名字很像的正文，别混

      - `text`          —— **完整正文**，给人看、给 AI 判。一律是原文。
      - `content_text`  —— 真正要写进 `fact_content.content_text` 的那一列。
                           长正文会分流到 `storage_path` 那个文件，这里留空。
                           两者分流规则不同，所以**两个都得写进文档**——
                           让 `from_document()` 现算一遍的话，就得把
                           `store()` 的分流规则（含"写失败退回内联"那条支路）
                           抄第二份，抄漏了就是长正文悄悄挤进库里。

    ⚠️ `parent_id` 是**知乎那边的 ID**，不是库里的 uuid——它跟 `fact_content`
    join 不上。要 join 得自己按 `(content_type, zhihu_id)` 去对上那一行。
    """
    stored = store(item, policy)
    return {
        "url": item.url,
        "content_type": item.content_type,
        "zhihu_id": item.zhihu_id,
        "parent_id": item.parent_id,
        "question_id": item.question_id,
        "title": item.title,
        "author_name": item.author_name,
        "author_url": item.author_url,
        "text": item.text,
        "text_length": stored.content_length,
        "content_text": stored.content_text,
        "storage_path": stored.storage_path,
        "voteup_count": item.voteup_count,
        "comment_count": item.comment_count,
        "published_at": item.published_at.isoformat() if item.published_at else None,
        "published_text": item.published_text,
        "raw_content_hash": stored.raw_content_hash,
        # ⚠️ 只写下**对象键**，不写路径：它和库里那一列是同一个键
        #    （同一个 `_key`），所以抽查时可以直接 `policy.local(key)` 打开对账。
        #    文件本身已经在上面那个 `store()` 里写过了，别写第二份。
        "snapshot_path": stored.snapshot_path,
        "keyword": keyword,
        "collected_at": collected_at or datetime.now(UTC).isoformat(timespec="seconds"),
    }


def from_document(row: Mapping) -> ContentRecord | None:
    """`contents.jsonl` 的一行 → `ContentRecord`。**取不到必填字段就返回 None。**

    ⚠️ **纯函数**（决策 52）：不查库、不调仓储、**不碰文件系统**——文件早在
    采集那一刻由 `to_document()` 写好了，这里只是把文档里已经有的东西搬进
    记录。所以它不需要 `SnapshotPolicy`：`storage_path` / `snapshot_path`
    都是现成的对象键。

    ## 它是 `to_document()` 的逆

    改一边就要改另一边。两边的字段名**故意不一样**（文档里叫 `text_length` /
    `parent_id` / `question_id`，记录里叫 `content_length` / `parent_zhihu_id` /
    `question_zhihu_id`），所以没法用 `asdict()` 一把梭——`test_extract.py`
    里有一条往返测试钉着这张对照关系。

    ## 为什么在这一层拒绝，而不是留给 insert

    库上写着 `check (content_type in (...))`、`zhihu_id not null`、`url` 唯一。
    编一个枚举值出来、或者拿空 ID 去插，错误都会以"违反约束"的形式在
    **插入那一刻**炸掉，而且炸的是一整批，报错信息还指向 SQL 而不是
    "这行的类型没认出来"。就地拒绝，主程序那边把它计进"装不成记录"报出来。
    """
    content_type = row.get("content_type")
    zhihu_id = row.get("zhihu_id")
    url = row.get("url")

    if not zhihu_id or not url:
        log.warning(
            "文档里有一行没有 %s，装不成记录，跳过（%s）",
            "知乎 ID" if not zhihu_id else "URL",
            url or content_type,
        )
        return None
    if content_type not in FACT_CONTENT_TYPES:
        log.warning(
            "%s 的内容类型不进 fact_content（%r），跳过——库上的枚举约束只认 %s。"
            "⚠️ 问题的正路是 claim_question 进 dim_question（决策 29），"
            "不是从 contents.jsonl 进来",
            url,
            content_type,
            "/".join(sorted(FACT_CONTENT_TYPES)),
        )
        return None

    # 作者维度：`dim_author.zhihu_user_id` 取自**主页链接的最后一段**，
    # 所以链接不是主页（匿名回答、`/people/x/followers` 这类页签）时取不出来，
    # 留 None，由 `storage.ingest` 计数报出来（决策 52）。
    author_url = row.get("author_url")

    return ContentRecord(
        content_type=content_type,
        zhihu_id=str(zhihu_id),
        url=url,
        author_zhihu_id=urlnorm.profile_user_id(author_url) if author_url else None,
        author_name=row.get("author_name"),
        author_url=author_url,
        # ⚠️ 这两个都是**知乎那边的 ID**，落库时要分别翻译成
        #    `dim_question.question_id`（bigint）和 `fact_content.parent_id`（uuid）。
        #    翻译在 storage 里做，这里只是原样搬运。
        question_zhihu_id=row.get("question_id"),
        parent_zhihu_id=row.get("parent_id"),
        title=row.get("title"),
        content_text=row.get("content_text"),
        storage_path=row.get("storage_path"),
        voteup_count=row.get("voteup_count") or 0,
        comment_count=row.get("comment_count") or 0,
        content_length=row.get("text_length"),
        raw_content_hash=row.get("raw_content_hash"),
        snapshot_path=row.get("snapshot_path"),
        published_at=_published_at(row.get("published_at")),
    )


def _published_at(raw: object) -> datetime | None:
    """ISO8601 字符串 → `datetime`。**读不出来就返回 None，不抛。**

    文件是机器写的（`to_document` 用 `isoformat()`），所以正常路径上不会失败。
    真失败了说明这一行被人手改坏了——那时**丢掉一个时间戳，比让整批入库
    停在一行脏数据上划算**：那一行还在文件里、警告也打出来了，人工看得见。
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        log.warning("文档里的时间戳 %r 读不出来，这一条的时间留空", raw)
        return None


@dataclass
class CrossCheckRun:
    """一整批的第一档交叉验证结果。

    刻意把 `items` 和 `checks` **绑在一个对象里**：拆成两个平行列表传参的话，
    顺序错位不会报错，只会让 A 内容的判断被安到 B 头上——而两个列表
    恰好等长时谁也看不出来。这里在构造时就钉死对应关系。
    """

    pairs: list[tuple[parse.ParsedItem, CrossCheck]] = field(default_factory=list)

    @property
    def suspects(self) -> list[tuple[parse.ParsedItem, CrossCheck]]:
        return [(i, c) for i, c in self.pairs if c.suspect]

    def report(self) -> int:
        """打印结果，返回**可疑的条数**。

        ⚠️ 可疑**不等于**要丢弃。截断的正文仍然是有价值的证据，
        只是不能当成全文——所以这里是"记下来 + 大声说"，不是"扔了"。
        """
        for item, check in self.pairs:
            if check.suspect:
                log.error(check.describe(item))
            elif not check.checked:
                log.debug(check.describe(item))
        return len(self.suspects)


def cross_check_all(items: list[parse.ParsedItem], page_html: str) -> CrossCheckRun:
    """对一整批做第一档交叉验证。"""
    return CrossCheckRun(pairs=[(item, cross_check_json(item, page_html)) for item in items])
