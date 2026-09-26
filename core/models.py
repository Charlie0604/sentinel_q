"""跨模块流转的数据结构。

模块之间只允许通过两种方式传数据：**落库之后传 ID，或者传这里定义的结构**。
禁止"我 import 你的函数顺手用一下"——那正是让模块重新粘成一坨的路径（架构文档 8.3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# 提示词模块的固定拼装顺序（架构文档 4.4）。
# 调优时只改对应模块，不影响其他部分。
PROMPT_MODULE_ORDER: tuple[str, ...] = (
    "a_role",  # 角色设定：基本不用改
    "b_subject",  # 监测主体说明：讲清楚"谁是要保护的对象"
    "c_tasks",  # 任务清单：这次要输出哪几项判断
    "d_rubric",  # 判断标准细则：最核心、最常改
    "e_schema",  # 输出格式规范：字段名对应数据库列名
    "f_examples",  # 少样本示例：人工标注的真实案例
    "g_guardrails",  # 边界规则：兜底 + 防提示词注入
)


@dataclass(frozen=True)
class ContentRecord:
    """一条待入库的内容，对应 `fact_content` 的一行。"""

    content_type: str  # question / answer / article / thought / comment
    zhihu_id: str
    url: str  # 规范化后的 URL（见 core.urlnorm）
    author_id: int | None = None
    question_id: int | None = None  # 所属问题，平铺自 5.5.1
    parent_id: str | None = None
    title: str | None = None
    content_text: str | None = None
    storage_path: str | None = None
    voteup_count: int = 0
    comment_count: int = 0
    content_length: int | None = None
    raw_content_hash: str | None = None
    snapshot_path: str | None = None
    html_snapshot_path: str | None = None
    screenshot_path: str | None = None
    published_at: datetime | None = None


@dataclass(frozen=True)
class CrawlQueueItem:
    """`crawl_queue` 里被抢到的一条待采集 URL（架构文档 8.8）。"""

    url: str
    task_id: int | None = None
    question_id: int | None = None
    content_type: str | None = None
    priority: int = 0
    attempts: int = 0


@dataclass(frozen=True)
class PromptBundle:
    """一次任务内冻结的一版提示词（架构文档 8.9）。

    任务开始时从线上库拉取一次，暂存本地；下一次任务重新拉——
    也就是说**提示词在一次任务内不变**，这正是 `prompt_version` 能作为
    `fact_analysis` 单一追溯依据的前提。
    """

    version: int
    content_hash: str
    modules: dict[str, str] = field(default_factory=dict)
    note: str | None = None

    def assemble(self) -> str:
        """按 4.4 的固定顺序拼装成最终 system prompt。"""
        parts = []
        for name in PROMPT_MODULE_ORDER:
            body = self.modules.get(name, "").strip()
            if body:
                parts.append(body)
        return "\n\n".join(parts)

    def missing_modules(self) -> list[str]:
        """哪些模块是空的——加载器用它决定要不要打降级警告。"""
        return [name for name in PROMPT_MODULE_ORDER if not self.modules.get(name, "").strip()]


@dataclass(frozen=True)
class AnalysisResult:
    """AI 对一条内容的判断，对应 `fact_analysis` 的一行（架构文档 4.4 模块 E）。"""

    content_id: str
    ai_summary: str | None = None
    platform_stance: str | None = None  # 有利 / 抹黑 / 中立 / 不相关
    stance_confidence: float | None = None
    risk_level: str | None = None  # 低风险 / 中风险 / 高风险
    risk_reasoning: str | None = None
    analyzed_by: str = "ai"  # ai / human
    model_version: str | None = None
    prompt_version: str | None = None  # PromptBundle.content_hash，见 8.9
