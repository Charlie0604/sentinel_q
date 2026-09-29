"""仓储接口。

"能独立测试"的关键不在测试代码，而在**数据库访问是否可以替换**。
这个接口就是那道缝：`SupabaseRepo` 真连库，`FakeRepo` 纯内存。
于是绝大多数测试都能在不碰网络、不连数据库的情况下跑完（架构文档 7.5）。

## 原语是方法，组合是函数（2026-09-28）

本文件只放**原语**——一条 SQL 能说完的操作。判据是三条同时成立：

  1. 它落成**一条 SQL 语句**（含 join / 聚合 / order by / limit，一条就是一条）；
  2. 参数是标量或行列表，返回是标量或行列表，**不做跨表推理**；
  3. 职责是"取这些行 / 存这些行"，**不决定业务上该怎么办**。

需要调多个原语、或在 Python 侧对一批行做判断、或做语义翻译的，是**组合函数**，
放在 `pipeline.py` / `alert.py` / `review.py` / `events.py` / `evidence.py` /
`search.py` / `prescreen.py` 里，收一个 `repo` 参数——与既有的
`insert_contents(records, *, repo)`、`fetch_bundle(repo, paths)` 同形。

⚠️ 由此有一条反直觉的推论：**只调一个方法、不做任何语义翻译的"组合函数"是纯噪音，
不要写。** 所以"提取全部已存 URL"是下面那个 `all_urls()` 方法，
而不是 `pipeline.py` 里的一个函数（它没有 `load_baseline` 那种"两份必须同一次取"的组合语义）。

## 两个形状约定

- **本文件里的 dataclass 是数据库那一侧的契约**（行结构、查询参数），形状跟着列走。
- **组合函数的输出是前端那一侧的契约**，形状跟着界面走，放在各自的文件里。
  两者故意不合并：`alert_rows` 的行带 `author_name`（join 来的），
  而看板要的是"两个数字 + 一列行"，不是同一件东西。

## 分析型方法没有内存替身

带 join / 聚合 / `pg_trgm` 的方法（本文件最后两组）**`FakeRepo` 一律不实现**，
它们的测试是 `@pytest.mark.integration`。理由不是"难写"，是**写出来就是第二个
查询引擎**——在 Python 里手写一遍条件 join 和排序，与 SQL 有一百处可以不一致，
而测试会一直绿。分界线的机械化见 `storage/fake.py` 的 `HONEST`。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol

from sentinel_q.shared.models import AnalysisResult, ContentRecord, PromptBundle

# ============================================================
# 行结构：与库里的列一一对应
# ============================================================


@dataclass(frozen=True)
class QuestionRow:
    """`dim_question` 的一行。

    ⚠️ `is_relevant` 是**三态**，别当成普通布尔：
    `None` = 已抢占但 AI 还没判完（见 `claim_question`），`True` / `False` 才是结论。
    把 `None` 当 `False` 会让"正在判"的问题被当成"判过不相关"而永久跳过。
    """

    question_id: int
    zhihu_qid: str
    url: str | None = None
    title: str | None = None
    description: str | None = None
    """问题描述。**AI 判 B 的输入之一**（架构文档 3.5：输入标题+描述）——
    不存的话判 B 的输入没留痕，重判还得重爬一次问题页。"""
    asked_at: datetime | None = None
    """提问时间。⚠️ 与 `first_seen_at` 区分：那个是"我们第一次看到它"，
    这个是"它在知乎上被提出来"。"""
    follower_count: int | None = None
    """关注者数。★ 非回溯的热度快照：**抢占那一刻的值**，之后不更新。

    ⚠️ `None` = 没采到，**不是 0**（迁移 0005 的三列都可空，正是为了分开
    这两件事）。冻结的理由、以及为什么不做 `update_question_metrics`
    写在 `claim_question` 的 docstring 里。"""
    view_count: int | None = None
    """被浏览数。同上：抢占那一刻冻结、可空、空 ≠ 0。"""
    answer_count: int | None = None
    """知乎声明的回答数。同上。

    ⚠️ 和 `fact_content` 里实际采到的回答条数**不是一回事**，别互相校验。"""
    is_relevant: bool | None = None
    relevant_checked_at: datetime | None = None
    follow_up_done: bool = False
    answers_collected_at: datetime | None = None
    first_seen_at: datetime | None = None
    """首次采集到该问题的时间，也就是本轮"是不是新问题"的判据
    （`follow_up_questions` 的 `since` 比的正是它）。

    ⚠️ 表上没有作者列，**而且不打算加**：问题页上提取不出提问者（已实测）。
    所以按作者聚合时问题天然缺席，这不是疏漏。"""


@dataclass(frozen=True)
class ContentRow:
    """`fact_content` 的一整行。复核界面用它，列表用 `SearchRow`。"""

    content_id: str
    content_type: str
    status: str
    zhihu_id: str
    url: str
    author_id: int | None = None
    question_id: int | None = None
    parent_id: str | None = None
    title: str | None = None
    content_text: str | None = None
    """短内容直接在这里。**长内容为 None**——那时看 `storage_path`。
    这两列不会同时为空（库上有 check 约束）。"""
    storage_path: str | None = None
    voteup_count: int = 0
    comment_count: int = 0
    content_length: int | None = None
    raw_content_hash: str | None = None
    snapshot_path: str | None = None
    html_snapshot_path: str | None = None
    screenshot_path: str | None = None
    published_at: datetime | None = None
    collected_at: datetime | None = None


@dataclass(frozen=True)
class SearchRow:
    """列表行：`fact_content` + 它的判断 + 作者昵称（都是 left join，都可空）。

    ⚠️ 判断那几列可空有**两个不同来源**，别混：① 这条内容没有 `fact_analysis` 行；
    ② 有行但那一列本来就没值（比如 `platform_stance = '不相关'` 时的 `risk_level`）。
    """

    content_id: str
    content_type: str
    zhihu_id: str
    url: str
    title: str | None = None
    author_id: int | None = None
    author_name: str | None = None
    author_zhihu_id: str | None = None
    published_at: datetime | None = None
    collected_at: datetime | None = None
    voteup_count: int = 0
    comment_count: int = 0
    ai_summary: str | None = None
    platform_stance: str | None = None
    stance_confidence: float | None = None
    risk_level: str | None = None
    risk_reasoning: str | None = None
    analyzed_by: str | None = None


@dataclass(frozen=True)
class EventStanceRow:
    """`fact_content_event` 的一行 + 议题名。

    ⚠️ 与 `AnalysisResult.platform_stance` 是**两个独立维度**（架构文档 5.2）：
    那个是对平台整体、一对一；这个是针对具体议题、多对多。
    复核界面上是两张卡片，不要合并成一张。
    """

    event_id: int
    event_name: str | None = None
    stance: str | None = None  # 正向 / 反向 / 中立
    confidence: float | None = None
    analyzed_by: str | None = None
    event_version: int | None = None
    prompt_version: str | None = None
    notes: str | None = None
    analyzed_at: datetime | None = None


@dataclass(frozen=True)
class EventRow:
    """`dim_event` 的一行 + 该议题下已判定的内容数。"""

    event_id: int
    name: str
    summary: str
    keywords: list[str] = field(default_factory=list)
    event_type: str | None = None  # new / backfill
    backfill_scanned: bool = False
    version: int = 1
    start_date: date | None = None
    end_date: date | None = None
    created_at: datetime | None = None
    content_count: int = 0
    """该议题下**已判定**的内容数。

    ⚠️ 计数只算 `fact_content_event` 里 `stance is not null` 的行——
    那是"AI 真的判过这个议题"的意思，光有关联不算。
    """


@dataclass(frozen=True)
class AuthorRow:
    """`dim_author` 的一行。"""

    author_id: int
    zhihu_user_id: str
    nickname: str | None = None
    profile_url: str | None = None
    stance: str | None = None
    """⚠️ `None` = 未知，**不是中立**。中立 = 已判定为中立，未知 = 还没判
    （架构文档 5.5.5）。所以这里没有 `'未知'` 这个枚举值，别造。"""
    is_watched: bool = False
    watch_note: str | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None


@dataclass(frozen=True)
class EvidenceRow:
    """`fact_evidence` 的一行。

    ⚠️ **只存登记信息，证据文件本身不在本系统里**（决策 8）：公证书、时间戳凭证、
    截图原件都在人工手里。这一行的用途是"以后要用时按内容搜出来，不用翻纸质档案"。
    ⚠️ `filed_at` 是**登记时间**，不等于实际取证时间；后者在 `notes` 里。
    """

    evidence_id: int
    content_id: str
    evidence_type: str | None = None
    evidence_number: str | None = None
    filed_by: str | None = None
    filed_at: datetime | None = None
    notes: str | None = None


@dataclass(frozen=True)
class PrescreenRow:
    """事件预筛的候选（架构文档 4.5.2）。"""

    content_id: str
    content_type: str
    url: str
    title: str | None = None
    published_at: datetime | None = None
    score: float | None = None
    """命中的最高相似度。**排序用**，不是判据——判据是查询里的那个阈值。"""


# ============================================================
# 查询参数
# ============================================================


@dataclass(frozen=True)
class ContentFilter:
    """基础检索的筛选条件（架构文档 6.1 第 5 项）。全字段可选，全空 = 不加条件。

    ⚠️ 三个"立场/风险"是**三样不同的东西**，不要合并成一个 `stance`：

      - `event_id` + `event_stance` → `fact_content_event`（对某议题的立场，多对多）
      - `platform_stance`          → `fact_analysis`（对平台整体的立场，一对一）
      - `risk_level`               → `fact_analysis`

    合并的话，"XX 事件下被标为抹黑的内容"这句话就没法表达了。
    """

    event_id: int | None = None
    event_stance: str | None = None  # 正向 / 反向 / 中立
    platform_stance: str | None = None  # 有利 / 抹黑 / 中立 / 不相关
    risk_level: str | None = None  # 低风险 / 中风险 / 高风险
    content_type: str | None = None
    author_zhihu_id: str | None = None
    published_from: datetime | None = None
    published_to: datetime | None = None
    collected_from: datetime | None = None


@dataclass(frozen=True)
class SortKey:
    """排序。字段名是白名单，由 `search.py` 校验，不直接拼进 SQL。"""

    field: str = "collected_at"  # published_at / collected_at / voteup_count / comment_count
    desc: bool = True


@dataclass(frozen=True)
class AnalysisPatch:
    """人工复核对判断的改动。**每个字段可选，`None` = 不改这一项。**

    ⚠️ **没有 `analyzed_by` 字段**：它由 `apply_human_analysis` 强制写 `'human'`。
    让它出现在这里，就等于允许调用方写 `analyzed_by='ai'`——那是一条把
    "人工改过"伪装成"AI 判的"的路，必须在类型上就走不通。
    """

    ai_summary: str | None = None
    platform_stance: str | None = None
    stance_confidence: float | None = None
    risk_level: str | None = None
    risk_reasoning: str | None = None


@dataclass(frozen=True)
class EventStancePatch:
    """人工复核对议题立场的改动。同样 `None` = 不改（`analyzed_by` 由实现层强制）。"""

    stance: str | None = None
    confidence: float | None = None
    notes: str | None = None


# ============================================================
# 协议
# ============================================================


class Repo(Protocol):
    """数据读写接口。每个方法都对应架构文档里一条已确定的规则。"""

    # ── 查重：AI 调用前的成本闸门（3.9 第 3 条 / 4.1）──────────────────

    def existing_urls(self, urls: Sequence[str]) -> set[str]:
        """这一批 URL 里，哪些已经入库了。命中即跳过，不进 AI。

        按批查询，一批一次往返——不要为它做本地持久化台账（见 7.8）。
        """
        ...

    def existing_question_ids(self, zhihu_qids: Sequence[str]) -> set[str]:
        """这一批知乎问题 ID 里，哪些已经在 `dim_question` 里了。

        ⚠️ 查问题是按 `zhihu_qid` 而不是标题——问题改了描述 ID 也不变（4.1）。
        """
        ...

    # ── 开跑前一次取全（3.12 / 决策 53 第 ① 步）─────────────────────

    def all_urls(self) -> set[str]:
        """全库已采 URL。**搜索前一次性取全**，当本轮的去重基准（3.9 第 3 条）。

        ⚠️ **这不是 `existing_urls(urls)` 的批量版，两个都要留。**
        `existing_urls` 要求调用方**先有一份 URL 清单**；而第 ① 步是"先搜出清单"，
        清单还不存在——按批查等于搜完才能查，而查重要省掉的正是**搜索之前**的
        浏览器开销。所以这一条只能是全量拉取，返回一整个 `set`。

        ⚠️ 它**没有分页**，是一次全表扫描。这是刻意的：调用方要的是"集合"，
        分页会给调用方留下"拉了一半就开始搜"的机会，那种漏采事后看不出来。
        代价是库大了以后这一步变慢——真慢了再改流式，但要改的是**两边的接口**。
        同理**不要**给它做本地持久化台账（7.8：第二个真相源）。
        """
        ...

    def all_questions(self) -> list[QuestionRow]:
        """`dim_question` 全量，**含 `is_relevant = false` 的行**，当【初始问题】。

        ⚠️ 全量是刻意的，不要顺手加 `where is_relevant = true`。这张表兼任
        "问过 AI 的问题的去重台账"（决策 34）：**判过不相关的问题也必须在去重
        基准里**，否则它的回答下次冒出搜索结果时会重新触发一次 AI 调用——
        正是 4.1 想省掉的那笔钱。判不判断是调用方的事（`QuestionRow.is_relevant`
        是三态，`None` = 已抢占待判，**不许当 False**）。
        """
        ...

    def follow_up_questions(self, since: datetime) -> list[QuestionRow]:
        """★【更新问题列表】：`is_relevant = true` 且 `first_seen_at >= since`。

        这是流水线第 ③ 步（能力四）的任务清单，`since` = 本轮的 `started_at`。

        ⚠️ **必须在第 ③ 步开场重新调一次，不能复用第 ② 步内存里那份。**
        第 ② 步刚判完一批问题、刚入库；拿旧快照的话，这一轮新判为相关的问题
        **它一个都看不见**，而失败是静默的（采到的回答变少，不报错）。
        """
        ...

    # ── 问题：先插后问的原子抢占（4.1）───────────────────────────────

    def claim_question(
        self,
        zhihu_qid: str,
        url: str | None,
        title: str | None,
        *,
        description: str | None = None,
        asked_at: datetime | None = None,
        follower_count: int | None = None,
        view_count: int | None = None,
        answer_count: int | None = None,
    ) -> int | None:
        """`insert ... on conflict do nothing returning`。

        有返回行 = 本进程是第一个到达者，才提交 AI 调用；
        无返回行 = 别人已经问过了（或已经在问），直接跳过。
        `is_relevant` 留 null 表示"已抢占，待判断"。

        ⚠️ **描述必须跟标题在抢占那一刻一起写进去**（2026-09-28 加的两个参数）：
        它们是同一次页面抓取的产物，而本方法返回 None（已被别人抢占）时
        调用方**就不会再写第二次**了——描述要是没跟着这一次进来，
        这个问题就永久没有描述，而"永久缺一半判 B 的输入"事后看不出来。

        ⚠️ **三个热度指标同理**（2026-09-29 加的，迁移 0005）：同一次抓取的
        产物，同样是"抢占失败就不再写第二次"。而它们还多一层——热度是
        **非回溯**的，重爬一次要开一次浏览器打一次知乎，错过了就永远没有
        那一刻的值。

        ⚠️ 不做"描述被编辑后更新"，**也不做热度回访更新**：那是存量复查问题，
        决策 23 已延后。要做"多久回访一次"得先有一个策略，而那个策略不存在；
        凭空加一个 `update_question_metrics` 只会在下一次实现里被误用成
        "每次采集都刷一遍热度"。
        """
        ...

    def set_question_relevance(self, question_id: int, is_relevant: bool) -> None:
        """写入问题判定结果，同时填 `relevant_checked_at`。"""
        ...

    def mark_follow_up_done(self, question_id: int) -> bool:
        """条件更新，返回 True 才触发"该问题下全量回答采集"（4.2）。

        `update ... where question_id = $1 and follow_up_done = false returning`
        ——并发下多个线程可能同时发现"这个问题刚变相关"，
        不加条件会把整个问题的回答**重新爬两遍**。
        """
        ...

    def mark_answers_collected(self, question_id: int) -> None:
        """写下"该问题下的回答**实际采完**"的时间（`answers_collected_at`）。

        ⚠️ 与 `mark_follow_up_done` 是**两件事，不要合并**（5.4 的字段注释）：
        那个是"已触发"，这个是"已采完"。采集中途崩了的时候，只有后者能看出
        这个问题的回答是残缺的——`follow_up_done = true` 而
        `answers_collected_at is null` 就是那个状态。合并之后它就不存在了。
        """
        ...

    # ── 作者维度 ────────────────────────────────────────────────────

    def ensure_author(
        self, zhihu_user_id: str, nickname: str | None, profile_url: str | None
    ) -> int:
        """按知乎用户 ID upsert 一条 `dim_author`，返回 `author_id`。

        `insert ... on conflict (zhihu_user_id) do update set nickname = ...,
        profile_url = ..., last_seen_at = now() returning author_id`

        **必须是 upsert，不能是普通 insert**：同一个人在这批里发 50 条，
        每条都要拿到同一个 `author_id`。普通 insert 撞唯一约束会抛异常，
        `do nothing` 则不返回行、等于拿不到 ID。

        昵称和链接**每次覆盖**：`nickname` 会改，`profile_url` 是人工核实的入口，
        都取最新值。`first_seen_at` 不动——那是"第一次见"的存档。
        """
        ...

    def question_id_for(self, zhihu_qid: str) -> int | None:
        """按知乎问题 ID 查 `dim_question.question_id`（库里那个 bigint）。

        ⚠️ **纯查询，不是 `claim_question`。** 后者的语义是"先插后问"的原子抢占，
        有返回值才表示"该去问 AI 相关性了"。采集侧拿它换 ID 等于把抢占吃掉，
        那个问题就永远不会被送去判相关性，而且全程不报错（见 `from_document` 的说明）。
        """
        ...

    def author_by_id(self, author_id: int) -> AuthorRow | None:
        """按 `author_id` 取作者维度行。

        ⚠️ 返回值可空，而且**内容也可以没有作者**（匿名回答、已注销账号），
        所以调用方要准备好"有内容、没作者"这个组合。
        """
        ...

    def set_author_watch(
        self,
        zhihu_user_id: str,
        *,
        is_watched: bool | None = None,
        watch_note: str | None = None,
        stance: str | None = None,
    ) -> None:
        """人工维护 `is_watched` / `watch_note` / `stance`（3.12：「暂时由人工维护」）。

        ⚠️ **三个参数都是"没给就不动"，不是"没给就清空"。** 这三个字段的语义是
        `null = 未知`（5.5.5：未知 ≠ 中立），而"把已经标好的重点关注账号误清掉"
        是那种事后看不出来的错。真要清空，传 `watch_note=""`——空串不是 null，
        `null` 的语义得以保留。

        ⚠️ `stance` 只允许 `正向 / 反向 / 中立`——**没有"未知"这个值**：
        未知就用 `null`。这是库上的 check 约束，代码里也不要造第六个枚举值。
        """
        ...

    # ── 内容入库（3.9）──────────────────────────────────────────────

    def insert_content(
        self, record: ContentRecord, *, parent_id: str | None = None
    ) -> str | None:
        """写入一条内容，返回 `content_id`；URL 已存在则返回 None。

        **这是维度表同步的单一收口**：`record` 只带知乎侧的原始标识，
        `author_id` 由这里先 `ensure_author` 拿到、`question_id` 由这里查
        `dim_question` 翻译——调用方（主程序）不用管（决策 52）。

        `parent_id` 是唯一的例外，得由调用方给：它是 uuid，
        只能靠"同一批里父级先插完"来搭，而那个映射只有批次视角看得见
        （见 `storage/ingest.py` 的 `IdIndex`）。

        并发写入靠 `url` 唯一约束 + `on conflict do nothing` 静默挡掉，
        不能让重复写入报错中断整个任务。

        ⚠️ `record.content_type == 'question'` 会被库上的 check 约束拒绝：
        **问题不进 `fact_content`**，只进 `dim_question`（2026-09-28 定，
        见 `migrations/0004_question_home.sql`）。调用方（`insert_judged`）
        应该在那之前就把这件事挡住，报一句人话而不是让约束抛英文异常。
        """
        ...

    def save_analysis(self, result: AnalysisResult) -> None:
        """写入 `fact_analysis`。**与 `insert_content` 同批调用**（决策 51）。

        顺序是 `insert_content` → 拿到 `content_id` → `save_analysis`：
        `fact_analysis.content_id` 有外键指向 `fact_content`，反了会炸。

        ⚠️ `insert_content` 返回 `None`（URL 重复）时**不要往下调它**——
        那一行的分析早就有了，硬写会撞外键。

        ⚠️ 这是**整行 upsert**，`model_version` / `prompt_version` 会被覆盖成
        传进来的值。**人工复核不要走这里**——用 `apply_human_analysis`，
        它会保留那两列（决策 47）。理由见那个方法的 docstring。
        """
        ...

    # ── 内容维护（3.10 / 7.2 工具箱）────────────────────────────────

    def refresh_content_metrics(
        self, content_id: str, *, voteup_count: int, comment_count: int
    ) -> None:
        """更新一条已入库内容的**度量**（点赞数、评论数）—— 7.2 的"更新元数据"。

        ⚠️ **刻意只更新度量，不碰正文，也不碰 `raw_content_hash`。** 两条理由：

        ① **更新正文会破坏决策 51。** 那条不变量是"库里的每一行都先经过 AI
           判定"，而 `fact_analysis` 是针对**当时那段正文**做的判断。刷掉正文
           之后这一行就成了"正文没判过、判断对着旧正文"——正是 51 要禁止的状态。
           要更新正文，唯一的正路是**重判**，也就是走"新内容"那条路径。
        ② **`raw_content_hash` 是"它被改过"的证据**，覆盖掉就把证据抹了，
           而 schema 里没有第二个地方放旧值（`fact_content` 一行一 url）。
           3.10 的"功能延后 ≠ 数据延后"——这一列现在就开始采。

        也就是说 7.2 工具箱里"更新元数据（正文、评论数、点赞数）"**只有一半
        现在能做**，另一半要等存量复查定下来。
        """
        ...

    def mark_content_status(self, content_id: str, status: str) -> None:
        """把 `status` 置成 `active` / `deleted_detected` / `edited_detected`（3.10）。

        ⚠️ **判断逻辑不在这一层，这里只是个写入口。** 存量复查功能已决定延后
        （决策 23），"谁来发现、发现之后要不要重判、判过旧版本的判断怎么办"
        这些问题都还没有答案。留这个方法是为了**不让判断逻辑被迫塞进 storage**——
        它一塞进来，规则就变成了"存储模块自己的看法"。
        """
        ...

    # ── 复核：读（6.1 第 2 项）──────────────────────────────────────

    def content_by_id(self, content_id: str) -> ContentRow | None:
        """按 `content_id` 取一行 `fact_content`。

        与 `question_id_for` 那种"按自然键查代理键"反向：这里是从 URL 进来之后
        一路用 `content_id`。复核界面先用 `existing_urls`/`insert` 拿到 id，
        之后所有查询都按 id 走。
        """
        ...

    def analysis_for(self, content_id: str) -> AnalysisResult | None:
        """取一条内容的判断。

        ⚠️ **没有判断是合法情况，不是错误。** 判断不抛异常：预警看板、检索
        都统一按 `content_id` 取，不该给某一种内容开一条特例路径。

        ⚠️ 返回的 `AnalysisResult` 里**没有 `reviewed_by`**——那个类在
        `shared/models.py`，是冻结的跨模块契约，这一轮一个字都不改（7.6）。
        复核界面要显示"谁改过"就再调一次 `analysis_reviewed_by`。
        """
        ...

    def analysis_reviewed_by(self, content_id: str) -> str | None:
        """谁复核过这条判断。`null` = 没被人改过（迁移 0003）。

        ⚠️ 单独一个方法而不是塞进 `AnalysisResult`，是上面那条约束的直接后果：
        为一个字段去改一个三个模块共用的类型不划算，多一次往返更便宜。
        两次调用读的是**同一行**，不是两个真相源。
        """
        ...

    def question_by_id(self, question_id: int) -> QuestionRow | None:
        """按 `question_id` 取一个问题行。

        ⚠️ 复核界面要用它显示"这条回答属于哪个问题"（`ContentDetail.question`）。
        与 `question_id_for(zhihu_qid)` 是两个方向：那个是自然键→代理键，
        这个是代理键→整行。
        """
        ...

    def event_stances_for(self, content_id: str) -> list[EventStanceRow]:
        """这条内容针对各个议题的立场（`fact_content_event` 的多对多那一侧）。

        ⚠️ 与 `analysis_for` 是两个独立维度，见 `EventStanceRow` 的说明。
        """
        ...

    # ── 复核：写（6.1 第 2 项）──────────────────────────────────────

    def apply_human_analysis(
        self,
        content_id: str,
        patch: AnalysisPatch,
        *,
        reviewed_by: str | None = None,
    ) -> None:
        """⭐ 人工改写判断。`analyzed_by` 由这一层**强制**置 `'human'`。

        `reviewed_by` 是自由文本（迁移 0003 那一列），跟 `fact_evidence.filed_by`
        同形：这套系统没有用户身份（不做角色分级，密码是唯一那道门），
        所以"谁"只能是自由文本，不能是外键。

        ⚠️ **故意不复用 `save_analysis`**，尽管两者的 SQL 几乎一样。复用会让
        "AI 写的"和"人改的"走同一条代码路径，而这两条的**审计含义完全不同**：
        决策 5 说"不保留判断的修改历史，人工直接覆盖"，于是 `analyzed_by`
        是**唯一**还能看出"这条判断被人动过"的地方。走同一个方法时，某天 AI
        那条路径多写一个字段，人工那条跟着变，`analyzed_by = 'human'` 就不再
        意味着"这些字段是人填的"。入口那句强制赋值把这个语义钉住，
        代价是两段 SQL 长得像——**这个重复是故意的，不要"顺手"合并回去**。

        ⚠️ **保留 `model_version` / `prompt_version`**（决策 47）。整行 upsert 会
        把它们覆盖成 None，而"这条判断当时用的哪版提示词"只有这一列答得出来；
        抹掉是纯粹的净损失（5.4 明说不保留修改历史，抹了就永远查不回来）。

        ⚠️ `patch` 里**没给的字段不动**。人工只想把"中风险"改成"低风险"时，
        不该把 AI 写的 `ai_summary` 和 `risk_reasoning` 一起抹掉。

        ⚠️ 这是 **upsert**：库里还没有判断行时，人工的那份就是第一份。
        """
        ...

    def overwrite_event_stance(
        self, content_id: str, event_id: int, patch: EventStancePatch
    ) -> None:
        """人工改写某条内容针对某个议题的立场（6.1：「也覆盖 `fact_content_event`」）。

        ⚠️ 主键是 `(content_id, event_id)`，所以这是 **upsert 不是 update**：
        人工可以给一条内容**新挂**一个它原本没被 AI 关联到的议题（复核的人
        比 AI 更清楚这条内容在说哪件事）。写成 update 的话，"AI 没想到要关联"
        的那些内容就永远补不上了——而那恰好是人工复核的主要价值之一。

        ⚠️ 新挂的这条要顺手填 `event_version = dim_event.version`，
        否则它一出生就是"基于旧版本"（4.5.4 的比对逻辑）。这一跳由这一层做，
        不让调用方自己查——调用方漏填的话，界面上会多出一条莫名其妙的待重跑标记。
        """
        ...

    def save_content_event(
        self,
        content_id: str,
        event_id: int,
        *,
        stance: str,
        analyzed_by: str,
        event_version: int,
        confidence: float | None = None,
        prompt_version: str | None = None,
        notes: str | None = None,
    ) -> None:
        """AI 的事件分类结果，upsert on `(content_id, event_id)`（4.5）。

        人工复核走 `overwrite_event_stance`（它自己强制 `analyzed_by='human'`
        并补 `event_version`），不直接调这个。
        """
        ...

    # ── 议题（6.1 第 3 项 / 4.5）────────────────────────────────────

    def create_event(
        self,
        *,
        name: str,
        summary: str,
        keywords: Sequence[str],
        event_type: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> int:
        """新建议题，返回 `event_id`（6.1 事件管理界面）。

        ⚠️ `event_type` **不给默认值**，必须由人明确选（4.5.3：「不由系统自动
        判断」）：`new` 走时间窗口扫描、`backfill` 走全库扫描，选错的后果是
        **成本数量级的差异**，而不是结果差一点。

        ⚠️ `summary` 是 200~300 字的背景摘要，会被当 system prompt 传给 AI
        （5.4 的字段注释）。它不是给人看的介绍，措辞直接影响判断结果。
        """
        ...

    def update_event(
        self,
        event_id: int,
        *,
        name: str | None = None,
        summary: str | None = None,
        keywords: Sequence[str] | None = None,
        event_type: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        bump_version: bool = False,
        mark_backfill_scanned: bool = False,
    ) -> bool:
        """编辑议题，返回是否命中该行。**局部更新，没给的不动。**

        ⚠️ **`bump_version` 由人决定，不自动递增**（4.5.4 / 6.1）。自动递增是错的：
        摘要的措辞微调很多时候不影响立场判断，无脑 +1 会让整个议题下的判断全部
        被标成"基于旧版本"，逼着人去重跑一批没变化的东西。"这次改动算不算实质
        修改"是人的判断，和提示词版本号由人给（`prompts push -v`）是同一条原则。

        ⚠️ `mark_backfill_scanned` 只有 `backfill` 类型的议题用（4.5.3：
        全库扫描是偶发的一次性补救，扫过就置上，避免重复触发）。
        """
        ...

    def event_by_id(self, event_id: int) -> EventRow | None:
        """按 id 取一个议题。`prescreen` 要靠它拿 keywords / event_type / start_date。"""
        ...

    def count_stale_judgments(self, event_id: int) -> int:
        """该议题下**基于旧版本**的判断条数：`event_version <> dim_event.version`（4.5.4）。

        界面上的"该事件下有 N 条判断基于旧版本，是否重新分析"就是它。

        ⚠️ 比的是 `fact_content_event.event_version` 与 `dim_event.version`——
        **版本号是逐个议题比，不是全局的**。拿一个全局版本号去比，
        会让"刚改过 A 议题"顺手把 B 议题下的判断全标成过期。

        ⚠️ 聚合查询，`FakeRepo` 不实现（见 `fake.py` 的 `HONEST`）。
        """
        ...

    # ── 证据（6.1 第 4 项 / 决策 8）─────────────────────────────────

    def insert_evidence(
        self,
        content_id: str,
        *,
        evidence_type: str | None,
        evidence_number: str | None,
        filed_by: str | None,
        notes: str | None = None,
    ) -> int:
        """登记一条取证元信息，返回 `evidence_id`。

        ⚠️ **本系统不存证据文件**（决策 8）：只存"哪条内容、哪种取证、凭证号多少、
        谁经手"。别指望从这里能把证据取回来。
        ⚠️ `filed_at` 是**登记时间**，不等于实际取证时间；后者写进 `notes`。
        """
        ...

    # ============================================================
    # 以下都是分析型查询：join / 聚合 / pg_trgm
    # ⚠️ FakeRepo 一律不实现（见 fake.py 的 HONEST 与它的理由）
    # ============================================================

    def search_contents(
        self,
        flt: ContentFilter,
        *,
        sort: SortKey,
        limit: int,
        offset: int,
    ) -> list[SearchRow]:
        """基础检索（6.1 第 5 项）：按事件 / 时间 / 立场 / 风险筛，可排序可翻页。

        ⚠️ **join 是条件性的，且一旦 join 就是 inner join——这是对的，但要知道
        后果**：按议题筛时，没关联到该议题的内容全部消失；按风险筛时，没有
        `fact_analysis` 的行消失。

        ⚠️ SQL 是**拼出来的**（join 哪几张表取决于 `flt` 里哪几个字段非空）。
        唯一的纪律是：**拼的只有 SQL 片段，值一律走参数**。任何一处把值插进
        字符串，这个接口就变成了注入点——而它将来是直接对前端暴露的。
        片段本身必须是**模块级常量**，不在方法体里现拼，这样离线测试才扫得到。

        ⚠️ `offset` 深翻页会退化（Postgres 要扫掉前 N 行）。现在的数据量下
        无所谓，真慢了再换 keyset 分页，而不是现在为它加一个游标参数。
        """
        ...

    def count_contents(self, flt: ContentFilter) -> int:
        """同上筛选条件下的总数，给分页用。

        ⚠️ 与 `search_contents` **必须共用同一份 WHERE 拼装**，否则会出现
        "第 3 页是空的、总数却说还有 50 条"这种只有用户能发现的错。
        """
        ...

    def alert_rows(
        self, *, risk_levels: Sequence[str], since: datetime, limit: int
    ) -> list[SearchRow]:
        """`collected_at >= since` 且风险等级在 `risk_levels` 里的内容。

        ⚠️「新增」按 `collected_at` 而不是 `published_at`——采集时间才是
        "我们什么时候知道的"；发布时间可能是几年前。走 `idx_fact_content_collected`。
        """
        ...

    def count_by_risk(self, *, since: datetime) -> dict[str, int]:
        """`collected_at >= since` 的内容按 `risk_level` 分组计数。

        看板卡片上那个数字（6.1 的例子："过去24小时新增3条高风险内容"）。
        ⚠️ 没有 `fact_analysis` 的内容**不计入任何一档**，也不单列——
        它们不是"零风险"，是"还没判"，混进来会让数字失去意义。
        """
        ...

    def list_events(self, *, include_empty: bool = True) -> list[EventRow]:
        """议题列表 + 各自的已判定内容数。

        ⚠️ 计数**只算 `stance is not null` 的关联**（"AI 真的判过这个议题"），
        而且**必须**带 `where is_relevant = true`（5.5.2 的查询纪律：
        `dim_question` 里混着判过不相关的问题，不加就会把噪声统进来）。
        """
        ...

    def evidence_for(
        self, *, content_id: str | None = None, limit: int = 50
    ) -> list[EvidenceRow]:
        """取证登记记录。传 `content_id` 就只取那一条内容的。

        ⚠️ **按事件搜证据这条路是断的**：`fact_evidence` 只挂 `content_id`，
        到议题要经过 `fact_content_event`，而一条内容可以关联多个议题，
        所以"按事件搜"是一条 join 且可能返回重复行。这轮先不做——
        去重 / 不去重留给真要做的人定。
        """
        ...

    def prescreen_rows(
        self,
        *,
        keywords: Sequence[str],
        since: datetime | None,
        limit: int,
        threshold: float | None = None,
    ) -> list[PrescreenRow]:
        """事件预筛：关键词命中的候选（4.5.2）。

        ⚠️ **匹配三处，缺一不可**：`title` / `content_text` / `ai_summary`。
        只查 `content_text` 会漏掉**全部长内容**——长正文走 `storage_path`，
        那一列是空的，而长文章恰恰最可能详述事件。这一条能成立，靠的是 5.5.4
        那个不变量：所有入库内容都经过 AI 分析，所以 `ai_summary` 必定存在。

        ⚠️ **`since` 为 None 表示全库扫（`backfill`）**，非 None 表示限时间窗口
        （`new`）。由调用方（`prescreen.py`）根据 `event_type` 决定，这一层不管。

        ⚠️ 用的是 `pg_trgm` 的 **`word_similarity` 家族**，不是 `%`（`similarity`）。
        `%` 比的是整串相似度，拿 2~5 字的关键词去比 2000 字的文章，分母是长文
        长度，结果趋近于 0，阈值调到多低都救不回来。**运算符方向与阈值都还没
        拿真库标定**（架构文档 8 的待办），标定之前这个方法的返回值不能进任何
        生产判断。
        """
        ...

    # ── 提示词（7.9）──────────────────────────────────────────────

    def active_prompt_bundle(self) -> PromptBundle | None:
        """取当前生效的那一版提示词。任务开始时调用一次，整个任务内不变。"""
        ...

    def push_prompt_bundle(self, bundle: PromptBundle) -> None:
        """把本地工作区的提示词推上库（人工改完提示词后执行）。

        线上库是提示词的**权威副本**——它不进 git，所以库是唯一能找到它、
        也是唯一能追溯"这条判断当时用的哪版"的地方。
        """
        ...
