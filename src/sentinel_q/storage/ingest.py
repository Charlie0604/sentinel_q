"""批量入库：把一批 `ContentRecord` 按顺序写进 `fact_content`。

## 为什么在数据库模块里，而不是采集模块

采集模块不连数据库（决策 52），也不该知道 `content_id` 是个 uuid。
它只负责"把页面变成一批 `ContentRecord`"，**写进去是主程序的事**——
这条边界就是本模块存在的理由。

## 顺序是有约束的，不能随便并行

`fact_content.parent_id` 是 **uuid，指向另一行的 `content_id`**，
而 `ContentRecord.parent_zhihu_id` 装的是**知乎那边的 ID**（父评论的 data-id，
或者正文的 ID）。中间这一跳只能靠"插一条、记一条"来搭。

好在评论的解析顺序天然是先父后子（`parse` 的走树顺序），
**打乱这个顺序就会让子级全部挂空**——而且不报错，只是 `parent_id` 变 None。
所以这里严格按传入顺序单趟插入，不并行、不排序。

## 两个入口，一份父级映射

`insert_contents` 和 `insert_judged` 都走同一个 `_insert_batch`。
这不是"顺手复用"：父级映射那段逻辑抄第二份，抄漏一处就是**评论静默挂空**，
而那个错在任何报告里都看不出来。所以它只能有一份。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from collections.abc import Set as AbstractSet  # 别名是为了不遮住内置的 set
from dataclasses import dataclass, field

from sentinel_q.shared.models import ContentJudgment, ContentRecord

log = logging.getLogger(__name__)

_IRRELEVANT_STANCE = "不相关"
"""§5.5.2 情况四里"照实填"的那个值。

自身不相关、但靠所属问题留下来的回答，它的 `platform_stance` 该填 `'不相关'`。
填成别的（模型有时会给个真实立场）不算错到要改数据，但**必须计一笔**——
改数据等于替 AI 改口径，而口径是它判出来的。
"""


class IdIndex:
    """知乎 ID → 库里的 `content_id`。

    ⚠️ 不复用 `FakeRepo.content_id_for()` 那种"查库反推"：
    那要为一堆记录做 N 次往返，而且 process 之间的竞争窗口更大。

    **跨轮**才有可能是缺的——上一次运行插的父级，这一次不在映射里。
    那种情况记进 `dangling`，不猜、不静默丢。
    """

    def __init__(self) -> None:
        self._ids: dict[tuple[str | None, str], str] = {}
        self.dangling: int = 0
        """父级解析不出来的条数。**必须报出来**——它意味着对话结构断了。"""

    def add(self, record: ContentRecord, content_id: str) -> None:
        if record.zhihu_id:
            self._ids[(record.content_type, record.zhihu_id)] = content_id

    def resolve(self, record: ContentRecord) -> str | None:
        """把父级的知乎 ID 换成 `content_id`。换不出来就记一笔并返回 None。"""
        parent = record.parent_zhihu_id
        if not parent:
            return None
        found = self._ids.get((record.content_type, parent))
        if found is None:
            # 父级多半是**另一种类型**（评论的父级是回答/文章，不是评论）。
            # 所以按类型查不到时再全表找一遍——同一批里类型是已知的，
            # 这一次兜底能把"评论挂回答"这类跨类型父级接上。
            found = next(
                (cid for (_type, zid), cid in self._ids.items() if zid == parent),
                None,
            )
        if found is None:
            self.dangling += 1
        return found


@dataclass
class IngestReport:
    """一批内容的入库结果。

    这几个计数器不是日志装饰。**每一项都对应一种"跑完了但其实缺东西"的状态**——
    而这个项目里最危险的失败恰恰是那种：命令正常退出、报告写着"入库 500 条"、
    实际却缺了一批快照或者断了一截对话结构。
    """

    inserted: int = 0
    duplicates: int = 0
    """库里已经有（`on conflict do nothing` 挡掉的）。正常，不是错误。"""

    dangling_parents: int = 0
    """父级挂不上的条数。意味着对话结构断了一截。"""

    no_snapshot: int = 0
    """快照没写成的条数。**这批内容将来删帖就取不回来了。**"""

    unresolved_authors: int = 0
    """`author_id` 没能落上的条数：主页链接不是个人主页，取不出知乎用户 ID。"""

    unresolved_questions: int = 0
    """`question_id` 没能落上的条数：那个知乎问题还没进 `dim_question`。"""

    def describe(self) -> str:
        text = f"入库 {self.inserted} 条，重复 {self.duplicates} 条"
        if self.dangling_parents:
            text += f"；⚠️ {self.dangling_parents} 条的父级挂不上（对话结构有断裂）"
        if self.no_snapshot:
            text += f"；⚠️ {self.no_snapshot} 条没有快照（将来删帖取不回来）"
        return text

    def describe_known_gaps(self) -> str | None:
        """两个**已知**的字段缺口。合并成一句报，不逐条刷屏。"""
        gaps = []
        if self.unresolved_authors:
            gaps.append(
                f"author_id 空着（{self.unresolved_authors} 条）："
                "作者链接取不出知乎用户 ID（不是个人主页链接），挂不上 dim_author"
            )
        if self.unresolved_questions:
            gaps.append(
                f"question_id 空着（{self.unresolved_questions} 条）："
                "那个知乎问题还没有对应的 dim_question 行"
            )
        if not gaps:
            return None
        return "；".join(gaps)


def _insert_batch(
    records: Iterable[ContentRecord],
    *,
    repo,
    report: IngestReport,
    on_inserted: Callable[[ContentRecord, str], None] | None = None,
) -> list[tuple[ContentRecord, str]]:
    """单趟插入，返回**真插进去的**那些 `(记录, content_id)`，按传入顺序。

    这是 `insert_contents` 和 `insert_judged` 共用的那份父级映射逻辑。
    两个入口唯一的差别是插成功之后要不要顺手写点别的（`on_inserted`）。

    ⚠️ **`describe_known_gaps()` 那句 warning 不在这里**，由两个入口各自打：
    它们要报的是**不同的分母**（一个只有入库，一个还要报内容闸门丢了多少），
    挤在这里会让"这批有几个字段填不上"变成一个没有上下文的数字。

    ⚠️ 返回的是列表，**不是 `IdIndex`**。别改成"插完整批、再从 index 反查"：
    `IdIndex.add` 对 `zhihu_id` 为空的记录直接跳过（那种记录在库里照样有
    `content_id`），反查会**静默少写**那些行的下游数据——决策 51 禁止的
    "库里有内容、没有分析"正是这么来的，而且 `inserted` 还是对的。
    """
    index = IdIndex()
    inserted: list[tuple[ContentRecord, str]] = []

    for record in records:
        if record.snapshot_path is None:
            report.no_snapshot += 1
        # 内容**本来有**这个信息、记录里却是空的 —— 那才是缺口。
        # 匿名回答天生没有作者，不该算进去（"取不到"和"本来就没有"要分开）。
        if (record.author_url or record.author_name) and not record.author_zhihu_id:
            report.unresolved_authors += 1
        if record.question_zhihu_id and repo.question_id_for(record.question_zhihu_id) is None:
            report.unresolved_questions += 1

        content_id = repo.insert_content(record, parent_id=index.resolve(record))
        if content_id is None:
            # URL 已存在。**不能把它当成"入库成功"往下走**——但我们也没拿到
            # uuid，所以它的子级这一批挂不上父级。记在 dangling 里。
            report.duplicates += 1
            continue
        report.inserted += 1
        index.add(record, content_id)
        inserted.append((record, content_id))
        if on_inserted is not None:
            on_inserted(record, content_id)

    report.dangling_parents = index.dangling
    return inserted


def insert_contents(records: Iterable[ContentRecord], *, repo) -> IngestReport:
    """把一批采集产物按顺序写进库。**父级必须先于子级出现。**

    ⚠️ **这里不做查重**：查重在调用方做，而且要尽量早（架构文档 3.9 第 3 条）。
    这里只负责 `on conflict do nothing` 那层兜底（并发写入时挡重复）。

    ⚠️ **维表同步不在这里**：`author_id` / `question_id` 由
    `repo.insert_content()` 内部翻译（决策 52 的单点收口）。
    这里只负责 `parent_id`——因为只有批次视角看得见那个 uuid 映射。

    ⚠️ **它落下的行是"有内容、没分析"的**（决策 51 禁止的那个状态）。
    留着它是因为采集和入库拆成了两趟（决策 52），而 AI 判定要写回
    `contents.jsonl` **同一行**的那层文件逻辑还没接上。接上之后
    主程序应当改调 `insert_judged`，这个函数退回去只服务"先落内容、
    稍后补判"的恢复场景。
    """
    report = IngestReport()
    _insert_batch(records, repo=repo, report=report)

    if gaps := report.describe_known_gaps():
        log.warning("这批有几个字段填不上：%s", gaps)
    return report


@dataclass
class JudgedReport:
    """一批**带 AI 判定**的内容的入库结果。

    `ingest` 那个字段就是 `insert_contents` 的那套计数器，一个不少——
    换句话说，`insert_judged` 报的东西是 `insert_contents` 的超集，
    换用它不会丢掉任何一个已有的缺口指标。
    """

    ingest: IngestReport = field(default_factory=IngestReport)

    kept_by_parent: int = 0
    """自身判为不相关，但**所属问题判为相关**因而留下来的（§5.5.2 情况四）。"""

    dropped_irrelevant: int = 0
    """自身不相关，而且没有能救它的所属问题。**这是内容闸门正常工作的样子。**"""

    dropped_unknown_parent: int = 0
    """自身不相关，所属问题也不在 `relevant_questions` 里，**但它压根不在
    `dim_question` 里**——异常：说明第②步（问题登记）漏了，
    或者调用方拿了一份过时的【更新问题列表】。"""

    unjudged: int = 0
    """有记录、没有判定 → **不插**。决策 51 不许库里有没分析的行。"""

    rejected_questions: int = 0
    """`content_type == 'question'` 被挡下的条数。问题不进 `fact_content`
    （迁移 0004），由这里挡是为了报人话，而不是让库上的 check 约束抛英文异常。"""

    stance_conflict: int = 0
    """情况四里 `platform_stance` 不是 `'不相关'` 的条数。
    **照插、不改**，只记一笔（§5.5.2「照实填」）。"""

    content_ids: dict[str, str] = field(default_factory=dict)
    """`url` → `content_id`，**只含真插进去的那些**。

    这是给调用方接着写 `fact_content_event` 用的：那张表要 `content_id`，
    而 `Repo` 上**没有**按 url 反查的接口（决策 52 的单点收口，反查是
    `FakeRepo` 的测试专用方法）。重跑时这里会是空的（全被 url 唯一约束挡掉），
    而那正是对的——没有新内容，就没有新的事件判定可挂。"""

    def describe(self) -> str:
        text = self.ingest.describe()
        kept = []
        if self.kept_by_parent:
            kept.append(f"{self.kept_by_parent} 条靠所属问题留下")
        if kept:
            text += "；" + "，".join(kept)
        if self.dropped_irrelevant:
            text += f"；内容闸门挡掉 {self.dropped_irrelevant} 条"
        return text


def insert_judged(
    records: Iterable[ContentRecord],
    judgments: Mapping[str, ContentJudgment],
    *,
    repo,
    relevant_questions: AbstractSet[str],
) -> JudgedReport:
    """把一批**已经判过的**内容连同判定一起写进库（决策 51）。

    这是"AI 判完才入库"那条不变量的落地处：`fact_content` 和 `fact_analysis`
    在同一批里写，**库里不会出现"有内容、没分析"的行**。

    ## 闸门是"自身相关 **或** 所属问题相关"（决策 28 / §5.5.2）

    四种情况：① 都相关 → 留；② 都不相关 → 丢；
    ③ 问题不相关、内容相关 → 留；④ 问题相关、内容不相关 → **留**（记账）。

    ## `relevant_questions` 是【本轮更新问题列表】

    也就是 §3.3 第⑥步开场重新查出来的那一份：**本轮新登记、且判为相关**的问题。
    ⚠️ **不是**"库里所有相关的问题"——那份包含上一轮的问题，拿它当闸门会让
    这一轮的判断被上一轮的结论左右，而且**结果完全看不出来**（只是多留了几条）。

    ⚠️ 它**故意没有默认值**。默认空集看起来很方便，实际是静默失效：
    情况四的每一条都会被记成 `dropped_irrelevant` 丢掉，而报告上的数字
    和"模型判错了"长得一模一样。宁可让调用方漏传时当场 `TypeError`。

    ## 判定按 `url` 键

    不用 `zhihu_id`：`fact_content.zhihu_id` 上**没有唯一约束**
    （`IdIndex` 按 `(content_type, zhihu_id)` 键正是因为裸 id 不唯一），
    同一个 id 在不同类型下是两条内容。`url` 才是那张表上的唯一键。

    ## 顺序

    **严格按传入顺序单趟处理**，父级必须先于子级——理由和 `insert_contents`
    一样，见模块开头。所以问题记录和正文记录要**合成一次调用**，
    分成两次会各建一个索引，跨文件的父级就挂不上了。

    ⚠️ `save_analysis` 是**紧挨着** `insert_content` 写的，不是"整批插完再整批
    判"。两种写法在正常情况下结果一样，区别只在进程崩在中间时：紧挨着写
    最多留下 1 行没分析，两趟写法会留下 N 行。同一个不变量，更好的失败方式。
    """
    report = JudgedReport()
    kept: list[ContentRecord] = []
    by_url: dict[str, ContentJudgment] = {}

    for record in records:
        if record.content_type == "question":
            # 问题只进 dim_question（迁移 0004 的 check 约束）。挡在这里是为了
            # 报一句人话，而不是让 psycopg 抛原文的英文约束异常。
            report.rejected_questions += 1
            log.warning(
                "内容类型是 question（%s），它该进 dim_question 而不是 fact_content，已跳过",
                record.url,
            )
            continue

        judgment = judgments.get(record.url)
        if judgment is None:
            # 决策 51：库里不许有没分析的行。没有判定就**不插**，
            # 而不是插了留个空——那正是这条不变量要挡的状态。
            report.unjudged += 1
            log.warning("这条没有判定，不插（决策 51）：%s", record.url)
            continue

        if judgment.is_relevant:
            kept.append(record)
            by_url[record.url] = judgment
            continue

        parent_qid = record.question_zhihu_id
        if not parent_qid:
            report.dropped_irrelevant += 1
        elif parent_qid in relevant_questions:
            report.kept_by_parent += 1
            if judgment.platform_stance != _IRRELEVANT_STANCE:
                report.stance_conflict += 1
                log.warning(
                    "自身判为不相关、靠所属问题留下，但 platform_stance 是 %r 而不是 %r：%s",
                    judgment.platform_stance,
                    _IRRELEVANT_STANCE,
                    record.url,
                )
            kept.append(record)
            by_url[record.url] = judgment
        elif repo.question_id_for(parent_qid) is None:
            report.dropped_unknown_parent += 1
            log.warning(
                "所属问题 %s 不在 dim_question 里，这条只能丢：%s",
                parent_qid,
                record.url,
            )
        else:
            report.dropped_irrelevant += 1

    def write_analysis(record: ContentRecord, content_id: str) -> None:
        repo.save_analysis(by_url[record.url].to_analysis_result(content_id))
        report.content_ids[record.url] = content_id

    _insert_batch(kept, repo=repo, report=report.ingest, on_inserted=write_analysis)

    if gaps := report.ingest.describe_known_gaps():
        log.warning("这批有几个字段填不上：%s", gaps)
    return report
