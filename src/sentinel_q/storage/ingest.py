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
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass

from sentinel_q.shared.models import ContentRecord

log = logging.getLogger(__name__)


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


def insert_contents(records: Iterable[ContentRecord], *, repo) -> IngestReport:
    """把一批采集产物按顺序写进库。**父级必须先于子级出现。**

    ⚠️ **这里不做查重**：查重在调用方做，而且要尽量早（架构文档 3.9 第 3 条）。
    这里只负责 `on conflict do nothing` 那层兜底（并发写入时挡重复）。

    ⚠️ **维表同步不在这里**：`author_id` / `question_id` 由
    `repo.insert_content()` 内部翻译（决策 52 的单点收口）。
    这里只负责 `parent_id`——因为只有批次视角看得见那个 uuid 映射。
    """
    report = IngestReport()
    index = IdIndex()

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

    report.dangling_parents = index.dangling

    if gaps := report.describe_known_gaps():
        log.warning("这批有几个字段填不上：%s", gaps)
    return report
