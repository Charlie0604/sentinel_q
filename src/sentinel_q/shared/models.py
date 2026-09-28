"""跨模块流转的数据结构。

模块之间只允许通过这里定义的结构传数据，由主程序转手。
禁止"我 import 你的函数顺手用一下"——那正是让模块重新粘成一坨的路径（架构文档 7.3）。
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
    """一条**采集到的**内容，对应 `fact_content` 的一行。

    ⚠️ **只带知乎侧的原始标识，不含库里任何主键**（决策 52）。

    采集模块不连数据库，所以它不可能知道 `author_id`（bigint）、
    `question_id`（bigint）、`parent_id`（uuid）——那三个都在落库那一刻
    由 `storage.insert_content()` 翻译出来：

    | 这里的字段 | 落库时变成 |
    |---|---|
    | `author_zhihu_id` / `author_name` / `author_url` | `ensure_author(...)` → `author_id` |
    | `question_zhihu_id`（知乎的字符串问题 ID） | 查 `dim_question` → `question_id` |
    | `parent_zhihu_id`（父评论的 data-id） | 查本批已插的映射 → `parent_id` |

    **翻译不在采集侧做**，否则"采集模块不询问数据库"这条边界当场就破了。
    """

    content_type: str  # question / answer / article / thought / comment
    zhihu_id: str
    url: str  # 规范化后的 URL（见 shared.urlnorm）
    author_zhihu_id: str | None = None  # 主页链接最后一段，取不到就是 None
    author_name: str | None = None
    author_url: str | None = None
    question_zhihu_id: str | None = None  # 所属问题，**知乎那边的 ID**
    parent_zhihu_id: str | None = None  # 父级内容的知乎 ID
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
class PromptBundle:
    """一次任务内冻结的一版提示词（架构文档 7.9）。

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
    prompt_version: str | None = None  # PromptBundle.content_hash，见 7.9
