"""跨模块流转的数据结构。

模块之间只允许通过这里定义的结构传数据，由主程序转手。
禁止"我 import 你的函数顺手用一下"——那正是让模块重新粘成一坨的路径（架构文档 7.3）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# 三个任务各自的拼装顺序（架构文档 4.4 的七个模块，按任务展开）。
#
# 任务 A 那一行是 4.4 的**逐字转写**——`a_role` / `b_subject` / `c_tasks` … 这七个
# 名字不动，`prompts/` 下现有的七个模板也不用改名。
#
# ## 为什么任务 B 和事件任务不各开一份 bundle
#
# 它们要的 `c_tasks` / `d_rubric` / `e_schema` 内容完全不同，看起来该是两份独立的
# 提示词。但 `dim_prompt` 上有一条 `unique index on dim_prompt (is_active) where is_active`
# ——**同时只有一行生效**，三份 bundle 在库里根本放不下；而且决策 47 要求
# `prompt_version` 那个 hash 覆盖"这条判断实际用的那版提示词"，三份独立 hash
# 挤在同一个列里，追溯就失真了。
#
# 所以三个任务共用**一份 bundle**，靠模块名前缀区分：`question_*` 只进任务 B 的
# system prompt，`event_*` 只进事件任务的。`assemble(task)` 按下面的元组挑。
#
# ⚠️ 代价是这个 dict 的名字容易让人以为"提示词是一整块"。它不是——
# `PromptBundle.assemble("question")` 拼出来的串里**一个 `c_tasks` 都不会有**。
PROMPT_MODULES: dict[str, tuple[str, ...]] = {
    "content": (
        "a_role",  # 角色设定：基本不用改
        "b_subject",  # 监测主体说明：讲清楚"谁是要保护的对象"
        "c_tasks",  # 任务清单：这次要输出哪几项判断
        "d_rubric",  # 判断标准细则：最核心、最常改
        "e_schema",  # 输出格式规范：字段名对应数据库列名
        "f_examples",  # 少样本示例：人工标注的真实案例
        "g_guardrails",  # 边界规则：兜底 + 防提示词注入
    ),
    "question": (
        "a_role",
        "b_subject",
        "question_tasks",  # 任务 B 只有一项判断：这条问题是否与监测对象相关
        "question_rubric",
        "question_schema",  # {"is_relevant": true/false}，没有立场/风险/摘要
        "question_examples",
        "g_guardrails",
    ),
    "event": (
        "a_role",
        "b_subject",
        "event_tasks",  # 是否属于该议题 + 相对该议题的立场
        "event_rubric",
        "event_schema",  # {"is_relevant": …, "stance": …, "confidence": …}
        "event_examples",
        "g_guardrails",
    ),
}

# 全部模块名的**规范顺序**：hash 按它算（同内容必同 hash），
# `prompts/<name>.txt` 这个工作区文件布局也按它。
#
# ⚠️ 它**不再是"一次拼装的顺序"**——那是上面每个任务各自的元组。改这个元组的
# 顺序会改掉所有 `prompt_version`，别为了"看起来整齐"动它。
PROMPT_MODULE_ORDER: tuple[str, ...] = tuple(
    dict.fromkeys(name for names in PROMPT_MODULES.values() for name in names)
)


def modules_for(task: str) -> tuple[str, ...]:
    """某个任务要拼哪几块。不认识的任务名当场报错，不静默退回全部。"""
    try:
        return PROMPT_MODULES[task]
    except KeyError:
        raise ValueError(
            f"没有 {task!r} 这个任务。可选：{'、'.join(PROMPT_MODULES)}"
        ) from None


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

    def assemble(self, task: str = "content") -> str:
        """拼出**某一个任务**的 system prompt。

        ⚠️ 默认值是 `"content"`（任务 A），不是"全部"——一份 bundle 装着三个任务的
        模块，把 15 块拼成一个串喂给模型，等于让任务 B 的模型看见"风险等级怎么判"，
        然后按 `e_schema` 输出立场字段。所以这里必须显式选任务，
        切错任务在 `modules_for` 那层就抛 `ValueError` 了。
        """
        parts = []
        for name in modules_for(task):
            body = self.modules.get(name, "").strip()
            if body:
                parts.append(body)
        return "\n\n".join(parts)

    def missing_modules(self, task: str | None = None) -> list[str]:
        """哪些模块是空的——加载器用它决定要不要打降级警告。

        `task=None`（默认）查**全部** 15 个：`prompts push` 要的就是这个，
        推一份半空的提示词上库必须吵，哪怕空的是别人还没写的那个任务。
        """
        names = PROMPT_MODULE_ORDER if task is None else modules_for(task)
        return [name for name in names if not self.modules.get(name, "").strip()]


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


# ── AI 模块的产物（三个任务各自的判断） ──────────────────────────────
#
# ⚠️ **`AnalysisResult` 和下面三个不是一回事，别合并。** 它有两个地方装不下
# AI 模块的产物，而那个类是冻结的跨模块契约（storage/supabase.py 里写着
# "那一轮一个字都不改"）：
#
#   1. `content_id` 是 `fact_content` 的主键。AI 模块**不连库、拿不到它**
#      （决策 52），它只能按知乎侧标识回指内容，翻译发生在落库那一刻。
#   2. `is_relevant` 不在 `fact_analysis` 里（5.4 的建表语句没有这一列）。
#      它是"这条要不要入库"的裁决输入（决策 28），落点在 `dim_question`。
#
# **放这里的判据**：这个结构跨两个以上模块（analyst → main → storage）。
# 只从 main 流向 analyst 的**输入**类型（`QuestionBrief` / `EventBrief`）
# 不进这里，它们跟着各自的 judge_*.py 走。


@dataclass(frozen=True)
class ContentJudgment:
    """AI 对一条内容的判断 = 任务 A 的产物（架构文档 3.5 / 4.4 模块 E）。

    `to_analysis_result()` 是给落库用的翻译：主程序拿到 `content_id` 之后调一次，
    得到的就是能直接 `repo.save_analysis()` 的东西。
    """

    zhihu_id: str  # 回指内容；落库时由 storage 翻译成 fact_content.content_id
    is_relevant: bool | None = None  # ⚠️ 不落 fact_analysis，是入库裁决的输入
    ai_summary: str | None = None
    platform_stance: str | None = None  # 有利 / 抹黑 / 中立 / 不相关
    stance_confidence: float | None = None
    risk_level: str | None = None
    risk_reasoning: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None  # 决策 47：必须入库

    def to_analysis_result(self, content_id: str) -> AnalysisResult:
        """换成 `fact_analysis` 的那一行。

        `model_version` / `prompt_version` **原样带过去**（决策 47）——它们是
        "这条判断当时用的哪版模型、哪版提示词"的唯一追溯依据，而提示词不进 git，
        库是唯一能回答它的地方。丢掉它们这个洞就补不回来了。

        `analyzed_by` 走 `AnalysisResult` 的默认值 `"ai"`：人工复核是另一条路径
        （决策 5：没有修订历史，人工直接覆盖，`analyzed_by` 是唯一的痕迹）。
        """
        return AnalysisResult(
            content_id=content_id,
            ai_summary=self.ai_summary,
            platform_stance=self.platform_stance,
            stance_confidence=self.stance_confidence,
            risk_level=self.risk_level,
            risk_reasoning=self.risk_reasoning,
            model_version=self.model_version,
            prompt_version=self.prompt_version,
        )


@dataclass(frozen=True)
class QuestionJudgment:
    """AI 对一个问题的判断 = 任务 B 的产物。

    **只有相关性**。问题不判立场、不判风险、不出摘要（3.5 的对照表：
    任务 B 的输出栏里只有"是否相关"）——所以这个类上没有那几个字段，
    模型就算多返回了也落不下来。

    落点是 `dim_question.is_relevant`，一个**三态**字段：`null` = 已抢占但还没判完
    （4.1 的"先插后问"），`true` / `false` 才是判断结果。
    """

    zhihu_qid: str
    is_relevant: bool | None = None
    model_version: str | None = None
    prompt_version: str | None = None


@dataclass(frozen=True)
class EventJudgment:
    """AI 对「一条内容 × 一个议题」的判断 = 事件任务的产物（架构文档 4.5）。

    落点是 `fact_content_event`，主键是 `(content_id, event_id)`——所以两个标识
    都要带回来。

    ⚠️ **`is_relevant` 是一处与文档的有意分歧。** 4.5.2 说"是否属于该议题"由
    `pg_trgm` 预筛决定，AI 只判立场；实际要 AI 也判一次。于是两层的关系是
    **预筛是粗筛、AI 是精筛**：粗筛命中但 AI 判 `false` 的内容，
    **不写 `fact_content_event`**——那张表没有 `is_relevant` 列，
    写了就等于把"其实不相关"记成了"相关且中立"。

    ⚠️ 这里的"相关"是**二级**的：议题由人工确认后入库，"跟监测对象有关"已经定了，
    所以这一列问的是"**说的是不是这个议题所描述的那一桩事**"。典型反例是
    **同一个企业、另一桩事**——预筛按关键词捞，区分不了它们。
    """

    zhihu_id: str
    event_id: str  # 调用方给的标识，AI 模块原样回带，不解释它是什么
    event_version: int  # dim_event.version → fact_content_event.event_version（4.5.4）
    is_relevant: bool | None = None
    stance: str | None = None  # 正向 / 反向 / 中立——相对**该议题**的立场
    confidence: float | None = None
    model_version: str | None = None
    prompt_version: str | None = None
