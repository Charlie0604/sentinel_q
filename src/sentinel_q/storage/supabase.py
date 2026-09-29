"""Supabase 上的真实实现。

⚠️ **用 psycopg 直连 Postgres，不走 supabase-py / PostgREST**。理由：
批量查重的 `= any(%s)`、`claim_question` 的 `on conflict do nothing returning`、
`mark_follow_up_done` 的条件更新，都是原生 SQL 表达起来更直接的东西；
PostgREST 要绕一圈还不一定等价。

⚠️ **这个文件里不该出现爬虫的运维状态**（任务断点、URL 清单、限流采样）——
那些是本地文件（架构文档 3.8 / 7.8），爬虫本地运行，为它建表等于每条 URL
一次网络往返。这里只放业务数据和提示词。

⚠️ 免费版必须用 Supabase 的连接池端口（6543），不要用直连端口（5432）：
直连端口在免费版**只有 IPv6**（IPv4 是付费附加项），普通网络下根本连不上。
另一个后果是事务模式不支持预编译语句，所以 `psycopg.connect` 要带
`prepare_threshold=None`（见 `__init__`）。
每个进程一个长连接即可，不要每查询一次新建。

⚠️ 本文件尚未实测——Supabase 项目建好后，用 tests/ 下带
`@pytest.mark.integration` 的用例跑一遍再算数。

## 这个文件的两条纪律

**① 所有 SQL 都是模块级常量，方法体里不拼字符串。**
唯一的例外是基础检索，它的 join 和 where 取决于传进来的筛选项，
所以拼装被单独抽成一个**纯函数** `_search_from_where()`——它拼的只有片段
（模块级常量），**值一律走参数**。抽成函数不只是为了好看：离线测试要能
直接调它，断言"片段序列 + 参数序列"，而不必真连库（见
`storage/tests/test_queries.py`）。`search_contents` 和 `count_contents`
共用它——两者必须口径一致，否则会出现"第 3 页是空的、总数却说还有 50 条"。

**② 行 → dataclass 一律用 `_row()`，不手写字段映射。**
SQL 里 select 的列名与 dataclass 的字段名**逐字相同**，所以 `_row` 直接
`cls(**dict(row))`。手写映射是"加了一列忘了加一句"这类错的老家，而这类错
只在真库上炸。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from typing import Any, TypeVar

from sentinel_q.shared.models import (
    AnalysisResult,
    ContentRecord,
    PromptBundle,
)
from sentinel_q.storage.repo import (
    AnalysisPatch,
    AuthorRow,
    ContentFilter,
    ContentRow,
    EventRow,
    EventStancePatch,
    EventStanceRow,
    EvidenceRow,
    PrescreenRow,
    QuestionRow,
    SearchRow,
    SortKey,
)

# ============================================================
# 行结构：列名与 repo.py 的 dataclass 字段逐字对应
# ============================================================

# dim_question 的全部列。三处查询共用，免得漏一列。
_QUESTION_COLUMNS = """
    question_id, zhihu_qid, url, title, description, asked_at,
    follower_count, view_count, answer_count,
    is_relevant, relevant_checked_at, follow_up_done,
    answers_collected_at, first_seen_at
"""

_QUESTION_BY_ID = f"select {_QUESTION_COLUMNS} from dim_question where question_id = %s"

_ALL_QUESTIONS = f"""
select {_QUESTION_COLUMNS} from dim_question
"""

# ⚠️ 纯查询，**不是 claim_question**。后者的语义是"先插后问"的原子抢占，
#    有返回值才表示"该去问 AI 相关性了"。采集侧拿它换 ID 等于把抢占吃掉，
#    那个问题就永远不会被送去判相关性，而且全程不报错。
_QUESTION_ID = "select question_id from dim_question where zhihu_qid = %s"

# ★【更新问题列表】（架构文档 3.3 第 ③ 步）。
# ⚠️ 不要顺手加 `and follow_up_done = false`：这一份列表**同时**是第 ⑥ 步
#    入库判据的输入（3.3 第 ⑥ 步"所属问题在更新问题列表里"）。第 ③ 步会把
#    命中的问题全标成 follow_up_done = true，加了那个条件之后这份列表会变成
#    空集，第 ⑥ 步于是把**所有**内容都挡在门外——而且是静默的。
_FOLLOW_UP_QUESTIONS = f"""
select {_QUESTION_COLUMNS} from dim_question
where is_relevant = true and first_seen_at >= %s
"""

# 「先插后问」的原子抢占（4.1）：有返回行才是第一个到达者。
# ⚠️ description / asked_at / 三个热度指标都必须跟标题在**同一次 insert** 里
#    写进去：返回 None（已被别人抢占）时调用方不会再写第二次，那这个问题就
#    永久没有描述——而描述是 AI 判 B 的输入之一（3.5），"永久缺一半输入"
#    事后看不出来。热度指标还多一层：它是**非回溯**的，错过就是永远没有。
_CLAIM_QUESTION = """
insert into dim_question (
    zhihu_qid, url, title, description, asked_at,
    follower_count, view_count, answer_count,
    is_relevant
)
values (%s, %s, %s, %s, %s, %s, %s, %s, null)
on conflict (zhihu_qid) do nothing
returning question_id
"""

# 条件更新（4.2）：有返回行才触发全量回答采集
_MARK_FOLLOW_UP = """
update dim_question set follow_up_done = true
where question_id = %s and follow_up_done = false
returning question_id
"""

# ⚠️ 与 _MARK_FOLLOW_UP 是两件事：那个是"已触发"，这个是"已采完"。
#    采集中途崩了的时候，只有这一列看得出回答是残缺的
#    （follow_up_done = true 而 answers_collected_at is null）。
_MARK_ANSWERS_COLLECTED = """
update dim_question set answers_collected_at = now()
where question_id = %s
"""

# ============================================================
# 内容
# ============================================================

# 键与 ContentRecord 的字段一一对应，外加上调用方翻译好的三个库里主键
# （author_id / question_id / parent_id）——见 insert_content。
_INSERT_CONTENT = """
insert into fact_content (
    content_type, zhihu_id, url, author_id, question_id, parent_id,
    title, content_text, storage_path, voteup_count, comment_count,
    content_length, raw_content_hash, snapshot_path, html_snapshot_path,
    screenshot_path, published_at
) values (
    %(content_type)s, %(zhihu_id)s, %(url)s, %(author_id)s, %(question_id)s, %(parent_id)s,
    %(title)s, %(content_text)s, %(storage_path)s, %(voteup_count)s, %(comment_count)s,
    %(content_length)s, %(raw_content_hash)s, %(snapshot_path)s, %(html_snapshot_path)s,
    %(screenshot_path)s, %(published_at)s
)
on conflict (url) do nothing
returning content_id
"""

_CONTENT_BY_ID = """
select content_id, content_type, status, zhihu_id, url, author_id, question_id,
       parent_id, title, content_text, storage_path, voteup_count, comment_count,
       content_length, raw_content_hash, snapshot_path, html_snapshot_path,
       screenshot_path, published_at, collected_at
from fact_content
where content_id = %s
"""

# ⚠️ 只更新度量，**不碰 content_text，也不碰 raw_content_hash**。
#    刷正文会破坏决策 51：fact_analysis 是针对当时那段正文做的判断，
#    刷完之后这一行就成了"正文没判过、判断对着旧正文"。
#    raw_content_hash 是"它被改过"的现成证据，schema 里没有第二个地方放旧值。
_REFRESH_METRICS = """
update fact_content set voteup_count = %s, comment_count = %s
where content_id = %s
"""

_MARK_CONTENT_STATUS = "update fact_content set status = %s where content_id = %s"

_ALL_URLS = "select url from fact_content"

# ============================================================
# 判断
# ============================================================

# ⚠️ 这里 select 的列刻意**不含 reviewed_by**：返回类型是 shared/models.py 的
#    AnalysisResult，而那个类这一轮一个字都不改（7.6：冻结的跨模块契约）。
#    reviewed_by 由 analysis_reviewed_by() 单独取——就一列，多一次往返换
#    "不去改一个三个模块共用的类型"，值。
_ANALYSIS_FOR = """
select content_id, ai_summary, platform_stance, stance_confidence,
       risk_level, risk_reasoning, analyzed_by, model_version, prompt_version
from fact_analysis
where content_id = %s
"""

_ANALYSIS_REVIEWED_BY = "select reviewed_by from fact_analysis where content_id = %s"

_SAVE_ANALYSIS = """
insert into fact_analysis (
    content_id, ai_summary, platform_stance, stance_confidence,
    risk_level, risk_reasoning, analyzed_by, model_version, prompt_version
) values (
    %(content_id)s, %(ai_summary)s, %(platform_stance)s, %(stance_confidence)s,
    %(risk_level)s, %(risk_reasoning)s, %(analyzed_by)s, %(model_version)s, %(prompt_version)s
)
on conflict (content_id) do update set
    ai_summary        = excluded.ai_summary,
    platform_stance   = excluded.platform_stance,
    stance_confidence = excluded.stance_confidence,
    risk_level        = excluded.risk_level,
    risk_reasoning    = excluded.risk_reasoning,
    analyzed_by       = excluded.analyzed_by,
    model_version     = excluded.model_version,
    prompt_version    = excluded.prompt_version,
    analyzed_at       = now()
"""

# ⭐ 人工复核（决策 5 / 6.1 第 2 项）。**故意不复用 _SAVE_ANALYSIS**，尽管两者
#    长得很像——理由写在 repo.py:apply_human_analysis 的 docstring 里，一句话：
#    复用会让"AI 写的"和"人改的"走同一条代码路径，而 analyzed_by 是唯一还能
#    看出"这条判断被人动过"的地方。**这个重复是故意的，不要顺手合并回去。**
#
# ⚠️ coalesce 是"没给的字段不动"：人工只想把中风险改成低风险时，
#    不该把 AI 写的 ai_summary 和 risk_reasoning 一起抹掉。
#    代价是**没法把某一列改回 NULL**——要清空只能写空串。这是有意收窄的，
#    因为"误清掉 AI 的分析结论"比"清不掉"难发现得多。
# ⚠️ model_version / prompt_version **刻意不出现在 set 里**（决策 47）：
#    整行覆盖会把它们抹成 NULL，而"这条判断当时用的哪版提示词"只有这两列
#    答得出来；5.4 明说不保留修改历史，抹了就永远查不回来。
_APPLY_HUMAN_ANALYSIS = """
insert into fact_analysis (
    content_id, ai_summary, platform_stance, stance_confidence,
    risk_level, risk_reasoning, analyzed_by, reviewed_by
) values (
    %(content_id)s, %(ai_summary)s, %(platform_stance)s, %(stance_confidence)s,
    %(risk_level)s, %(risk_reasoning)s, 'human', %(reviewed_by)s
)
on conflict (content_id) do update set
    ai_summary        = coalesce(excluded.ai_summary, fact_analysis.ai_summary),
    platform_stance   = coalesce(excluded.platform_stance, fact_analysis.platform_stance),
    stance_confidence = coalesce(excluded.stance_confidence, fact_analysis.stance_confidence),
    risk_level        = coalesce(excluded.risk_level, fact_analysis.risk_level),
    risk_reasoning    = coalesce(excluded.risk_reasoning, fact_analysis.risk_reasoning),
    analyzed_by       = 'human',
    reviewed_by       = coalesce(excluded.reviewed_by, fact_analysis.reviewed_by),
    analyzed_at       = now()
"""

# ============================================================
# 议题关联
# ============================================================

_EVENT_STANCES_FOR = """
select ce.event_id, e.name as event_name, ce.stance, ce.confidence,
       ce.analyzed_by, ce.event_version, ce.prompt_version, ce.notes, ce.analyzed_at
from fact_content_event ce
left join dim_event e on e.event_id = ce.event_id
where ce.content_id = %s
order by ce.event_id
"""

_SAVE_CONTENT_EVENT = """
insert into fact_content_event (
    content_id, event_id, stance, confidence, analyzed_by,
    event_version, prompt_version, notes
) values (
    %(content_id)s, %(event_id)s, %(stance)s, %(confidence)s, %(analyzed_by)s,
    %(event_version)s, %(prompt_version)s, %(notes)s
)
on conflict (content_id, event_id) do update set
    stance         = excluded.stance,
    confidence     = excluded.confidence,
    analyzed_by    = excluded.analyzed_by,
    event_version  = excluded.event_version,
    prompt_version = excluded.prompt_version,
    notes          = excluded.notes,
    analyzed_at    = now()
"""

# ⭐ 人工改写议题立场。**upsert 不是 update**：人工可以给一条内容新挂一个
#    AI 原本没关联到的议题（复核的人比 AI 更清楚这条内容在说哪件事），
#    写成 update 的话那部分永远补不上——而那恰好是人工复核的主要价值之一。
# ⚠️ event_version 取 dim_event 的当前版本（子查询），不能让调用方自己查：
#    漏填的话新挂的这条一出生就是"基于旧版本"（4.5.4），界面上凭空多出一条
#    莫名其妙的待重跑标记。
_OVERWRITE_EVENT_STANCE = """
insert into fact_content_event (
    content_id, event_id, stance, confidence, analyzed_by, event_version, notes
) values (
    %(content_id)s, %(event_id)s, %(stance)s, %(confidence)s, 'human',
    (select version from dim_event where event_id = %(event_id)s),
    %(notes)s
)
on conflict (content_id, event_id) do update set
    stance        = coalesce(excluded.stance, fact_content_event.stance),
    confidence    = coalesce(excluded.confidence, fact_content_event.confidence),
    notes         = coalesce(excluded.notes, fact_content_event.notes),
    analyzed_by   = 'human',
    event_version = excluded.event_version,
    analyzed_at   = now()
"""

# ============================================================
# 议题
# ============================================================

_EVENT_COLUMNS = """
    event_id, name, summary, keywords, event_type,
    backfill_scanned, version, start_date, end_date, created_at
"""

_CREATE_EVENT = """
insert into dim_event (name, summary, keywords, event_type, start_date, end_date)
values (%(name)s, %(summary)s, %(keywords)s, %(event_type)s,
        %(start_date)s, %(end_date)s)
returning event_id
"""

_EVENT_BY_ID = f"select {_EVENT_COLUMNS} from dim_event where event_id = %s"

# 局部更新：`coalesce(新值, 旧值)` = "没给就不动"。
# ⚠️ bump 做成参数而不是 `version = version + 1` 写在 SQL 里：
#    "这次改动算不算实质修改"是人的判断（4.5.4），无脑 +1 会把整个议题下的
#    判断全标成"基于旧版本"，逼人去重跑一批没变化的东西。
_UPDATE_EVENT = """
update dim_event set
    name             = coalesce(%(name)s, name),
    summary          = coalesce(%(summary)s, summary),
    keywords         = coalesce(%(keywords)s, keywords),
    event_type       = coalesce(%(event_type)s, event_type),
    start_date       = coalesce(%(start_date)s, start_date),
    end_date         = coalesce(%(end_date)s, end_date),
    version          = version + %(bump)s,
    backfill_scanned = backfill_scanned or %(mark_backfill_scanned)s
where event_id = %(event_id)s
returning event_id
"""

# ⚠️ 计数只算 `stance is not null` 的关联——那是"AI 真的判过这个议题"，
#    光有关联不算（4.5 的关联是 AI 分类的产物，stance 才是结论）。
# ⚠️ **这里刻意不 join dim_question、不加 is_relevant = true。** 5.5.2 那条
#    查询纪律管的是"按问题聚合"（dim_question 里混着判过不相关的问题），
#    而这条查询根本碰不到 dim_question。硬加会**少算**：一条内容若是凭
#    "自身相关"进来的（规则二：自身相关 **或** 所属问题相关），它的所属问题
#    完全可能被判为不相关，加了这个条件它就被误排除。
_LIST_EVENTS = f"""
select {_EVENT_COLUMNS},
       count(ce.content_id) filter (where ce.stance is not null) as content_count
from dim_event e
left join fact_content_event ce on ce.event_id = e.event_id
group by e.event_id
having %(include_empty)s
    or count(ce.content_id) filter (where ce.stance is not null) > 0
order by e.event_id desc
"""

_COUNT_STALE_JUDGMENTS = """
select count(*) as n
from fact_content_event ce
join dim_event e on e.event_id = ce.event_id
where ce.event_id = %s and ce.event_version is distinct from e.version
"""

# ============================================================
# 作者 / 证据
# ============================================================

_AUTHOR_BY_ID = """
select author_id, zhihu_user_id, nickname, profile_url, stance,
       is_watched, watch_note, first_seen_at, last_seen_at
from dim_author where author_id = %s
"""

# 作者维度 upsert。**必须 returning**：do nothing 在昵称变更时也不返回行，
# 那就拿不到 author_id 了。
_ENSURE_AUTHOR = """
insert into dim_author (zhihu_user_id, nickname, profile_url)
values (%s, %s, %s)
on conflict (zhihu_user_id) do update set
    nickname     = excluded.nickname,
    profile_url  = excluded.profile_url,
    last_seen_at = now()
returning author_id
"""

# ⚠️ 三个 coalesce = "没给就不动"，不是"没给就清空"。这三个字段的语义是
#    null = 未知（5.5.5：未知 ≠ 中立），"把标好的重点关注账号误清掉"是那种
#    事后看不出来的错。真要清空就传空串（空串不是 null，null 的语义得以保留）。
_SET_AUTHOR_WATCH = """
update dim_author set
    is_watched = coalesce(%(is_watched)s, is_watched),
    watch_note = coalesce(%(watch_note)s, watch_note),
    stance     = coalesce(%(stance)s, stance)
where zhihu_user_id = %(zhihu_user_id)s
"""

_EVIDENCE_COLUMNS = """
    evidence_id, content_id, evidence_type, evidence_number,
    filed_by, filed_at, notes
"""

_INSERT_EVIDENCE = """
insert into fact_evidence (content_id, evidence_type, evidence_number, filed_by, notes)
values (%(content_id)s, %(evidence_type)s, %(evidence_number)s, %(filed_by)s, %(notes)s)
returning evidence_id
"""

_EVIDENCE_FOR_CONTENT = f"""
select {_EVIDENCE_COLUMNS} from fact_evidence
where content_id = %s
order by filed_at desc, evidence_id desc
limit %s
"""

_EVIDENCE_ALL = f"""
select {_EVIDENCE_COLUMNS} from fact_evidence
order by filed_at desc, evidence_id desc
limit %s
"""

# ============================================================
# 查询：基础检索的片段
# ============================================================
#
# ⚠️ 拼的只有下面这些**片段**（模块级常量），**值一律走 %(name)s 参数**。
#    任何一处把值插进字符串，这个接口就是注入点——它将来直接对前端暴露。
#    离线测试 test_queries.py 断言的正是这件事。

_SEARCH_FROM = "from fact_content c"

# 作者与判断都是"一条内容至多一行"，所以条件 join 不会产生重复行。
_SEARCH_LEFT_JOIN_AUTHOR = " left join dim_author a on a.author_id = c.author_id"
_SEARCH_JOIN_AUTHOR = " join dim_author a on a.author_id = c.author_id"
_SEARCH_LEFT_JOIN_ANALYSIS = " left join fact_analysis an on an.content_id = c.content_id"
_SEARCH_JOIN_ANALYSIS = " join fact_analysis an on an.content_id = c.content_id"

# 列名与 SearchRow 的字段逐字相同。search_contents / alert_rows 共用。
_SEARCH_COLUMNS = """
    c.content_id, c.content_type, c.zhihu_id, c.url, c.title,
    c.author_id, a.nickname as author_name, a.zhihu_user_id as author_zhihu_id,
    c.published_at, c.collected_at, c.voteup_count, c.comment_count,
    an.ai_summary, an.platform_stance, an.stance_confidence,
    an.risk_level, an.risk_reasoning, an.analyzed_by
"""

_SEARCH_SORT_COLUMNS = {
    "published_at": "c.published_at",
    "collected_at": "c.collected_at",
    "voteup_count": "c.voteup_count",
    "comment_count": "c.comment_count",
}

_WHERE_AUTHOR = "a.zhihu_user_id = %(author_zhihu_id)s"
_WHERE_PLATFORM_STANCE = "an.platform_stance = %(platform_stance)s"
_WHERE_RISK = "an.risk_level = %(risk_level)s"
_WHERE_CONTENT_TYPE = "c.content_type = %(content_type)s"
_WHERE_PUBLISHED_FROM = "c.published_at >= %(published_from)s"
_WHERE_PUBLISHED_TO = "c.published_at <= %(published_to)s"
_WHERE_COLLECTED_FROM = "c.collected_at >= %(collected_from)s"

# ⚠️ 议题筛选用 **exists 子查询而不是 join**，这不是风格问题：
#    `fact_content_event` 的主键是 (content_id, event_id)，一条内容可以关联
#    多个议题。用 join 的话，只给 event_stance 不给 event_id 时同一条内容会
#    出现**多次**（每个匹配的议题一行），而 count_contents 也得跟着去重，
#    两处口径一旦不一致就是"翻页翻出重复行"这种只有用户看得见的错。
#    exists 天然不可能重复，两个条件又都落在**同一行** ce 上，
#    所以语义与 join 完全一致。
#    代价是用不上 event_id 上的索引（`is null or` 挡住了），
#    议题过滤因此是全表扫 fact_content_event——量级不大，先正确再说。
_WHERE_EVENT_EXISTS = """
exists (
    select 1 from fact_content_event ce
    where ce.content_id = c.content_id
      and (%(event_id)s is null or ce.event_id = %(event_id)s)
      and (%(event_stance)s is null or ce.stance = %(event_stance)s)
)
"""

_ALERT_ROWS = f"""
select {_SEARCH_COLUMNS}
from fact_content c
left join dim_author a on a.author_id = c.author_id
join fact_analysis an on an.content_id = c.content_id
where an.risk_level = any(%(risk_levels)s)
  and c.collected_at >= %(since)s
order by c.collected_at desc
limit %(limit)s
"""

# ⚠️ 没有 fact_analysis 的内容**不计入任何一档**，也不单列一项：
#    它们不是"零风险"，是"还没判"，混进来会让看板上的数字失去意义。
#    （决策 51 之后理论上不该有这种行，所以真出现了也是这条查询该吼出来的事。）
_COUNT_BY_RISK = """
select an.risk_level, count(*) as n
from fact_content c
join fact_analysis an on an.content_id = c.content_id
where c.collected_at >= %(since)s
  and an.risk_level is not null
group by an.risk_level
"""

# ── 事件预筛（4.5.2）──────────────────────────────────────────────
#
# ⚠️ **不要照抄架构文档 4.5.2 那段示例 SQL。** 它用的是 `%`（similarity），
#    那是**整串相似度**不是"包含"：拿一个 2~5 字的关键词去比一段 2000 字的
#    文章，分母是长文长度，结果趋近于 0，set_limit() 调到多低都救不回来
#    （低到能命中长文时，短文本那边已经全是噪声）。文档自己举的例子
#    （"长文章恰恰最可能详述事件"）正是那个写法唯一做不到的事。
#
# ⚠️ 用的是 **word_similarity 函数形式**（"这个关键词是否作为一个近似词出现在
#    这段文字里"，语义上正好是需求），而且**故意用函数而不是 %> 运算符**：
#    ① 运算符的参数方向（哪个是被搜的词、哪个是被搜的文本）必须拿真库实测，
#       写反了的表现是"查询能跑、结果为空"，属于最难查的一类——函数形式的
#       签名是可查的，运算符方向不是；
#    ② 函数形式**绕开了 set_limit()**。set_limit 是会话级的，必须与那条 SELECT
#       在同一个事务里，否则"本地跑对了、连接池上不对"（6543 是事务模式）。
#       这里直接把阈值写成 `>= %(threshold)s` 参数，没有会话状态可漏。
#    代价：函数形式用不上 GIN trgm 索引。索引本来就要等阈值标定完再建
#    （0000_extensions.sql 的注释），所以这不是新欠的债。
#
# ⚠️ **匹配三处缺一不可**（title / content_text / ai_summary）。只查 content_text
#    会漏掉**全部长内容**：长正文走 storage_path，那一列是空的，而长文章恰恰
#    最可能详述事件。这一条能成立，靠的是决策 51——所有入库内容都经过 AI 分析，
#    所以 ai_summary 必定存在（这也是下面用 inner join 而不是 left join 的原因）。
#
# ⚠️ **阈值与运算符方向都还没标定**（架构文档 8 的待办）。标定之前这个查询的
#    返回值不能进任何生产判断。下面这个默认值是个**占位**，不是"调好的值"——
#    它的唯一作用是让"忘了传 threshold"这件事不至于静默地用一种匹配口径跑起来。
_PRESCREEN_DEFAULT_THRESHOLD = 0.5

_PRESCREEN_INNER = """
    select c.content_id, c.content_type, c.url, c.title, c.published_at,
           greatest(
               word_similarity(k.kw, coalesce(c.title, '')),
               word_similarity(k.kw, coalesce(c.content_text, '')),
               word_similarity(k.kw, coalesce(an.ai_summary, ''))
           ) as score
    from fact_content c
    join fact_analysis an on an.content_id = c.content_id
    cross join unnest(%(keywords)s::text[]) as k(kw)
"""

# backfill：全库扫（偶发的一次性补救，不是常规开销）
_PRESCREEN_ALL = f"""
select * from ({_PRESCREEN_INNER}) hits
where hits.score >= %(threshold)s
order by hits.score desc
limit %(limit)s
"""

# new：限定时间窗口。新事件的讨论不可能早于它发生（4.5.3）。
_PRESCREEN_WINDOWED = f"""
select * from (
    {_PRESCREEN_INNER}
    where c.collected_at >= %(since)s
) hits
where hits.score >= %(threshold)s
order by hits.score desc
limit %(limit)s
"""

# ============================================================
# 行 → dataclass
# ============================================================

_T = TypeVar("_T")

# psycopg 把 numeric 读成 Decimal，而 `Decimal('0.8') == 0.8` 是 **False**。
# 不转的话，围绕置信度的断言会以"值看着一样却不相等"的方式挂掉，很难查。
# stance_confidence 是库里唯一的 numeric 列。
_NUMERIC_FIELDS = ("stance_confidence",)


def _row(cls: type[_T], row: dict[str, Any]) -> _T:
    """dict_row → dataclass。

    SQL 里 select 的列名与 dataclass 的字段名逐字相同，所以直接 `cls(**row)`。
    **不要手写字段映射**——那是"加了一列忘了加一句"这类错的老家，
    而这类错只在真库上炸（见本文件开头第二条纪律）。
    """
    data = dict(row)
    for name in _NUMERIC_FIELDS:
        if data.get(name) is not None:
            data[name] = float(data[name])
    return cls(**data)


def _search_from_where(flt: ContentFilter) -> tuple[str, str, dict[str, Any]]:
    """把 `ContentFilter` 拼成 (from 片段, where 片段, 参数)。

    **纯函数，不碰数据库**——离线测试直接调它，断言两件事：
    ① 哪些片段被选中（比如"按风险筛"必须带上 fact_analysis 的 inner join）；
    ② 参数是从 `%(name)s` 来的，没有任何值被插进 SQL 字符串。

    ⚠️ **join 是条件性的，而且一旦 join 就是 inner join——这是对的，但要知道
    后果**：按议题筛时没关联到该议题的内容全部消失；按风险/平台立场筛时
    没有 `fact_analysis` 的行消失。想"筛了但保留没判断的"，那是不存在的语义：
    一条没判断的内容谈不上"风险等级是高"。

    ⚠️ 两处 filter 共用一个 join，所以 join 只加一次；`event_*` 那组走 exists
    子查询（理由见 `_WHERE_EVENT_EXISTS`），**不产生 join**。
    """
    from_parts = [_SEARCH_FROM]
    where: list[str] = []
    params: dict[str, Any] = {}

    # 作者要筛就必须 inner join：left join 之后 `a.zhihu_user_id = ...`
    # 筛出来的是"作者为空"的行，正好筛反。
    if flt.author_zhihu_id is not None:
        from_parts.append(_SEARCH_JOIN_AUTHOR)
        where.append(_WHERE_AUTHOR)
        params["author_zhihu_id"] = flt.author_zhihu_id
    else:
        from_parts.append(_SEARCH_LEFT_JOIN_AUTHOR)

    needs_analysis = flt.platform_stance is not None or flt.risk_level is not None
    from_parts.append(_SEARCH_JOIN_ANALYSIS if needs_analysis else _SEARCH_LEFT_JOIN_ANALYSIS)

    if flt.platform_stance is not None:
        where.append(_WHERE_PLATFORM_STANCE)
        params["platform_stance"] = flt.platform_stance
    if flt.risk_level is not None:
        where.append(_WHERE_RISK)
        params["risk_level"] = flt.risk_level

    # 议题：两个参数一起给（没给的那个是 None），因为片段里两条 `is null or`
    # 都写着，少传一个就是 KeyError。
    if flt.event_id is not None or flt.event_stance is not None:
        where.append(_WHERE_EVENT_EXISTS)
        params["event_id"] = flt.event_id
        params["event_stance"] = flt.event_stance

    if flt.content_type is not None:
        where.append(_WHERE_CONTENT_TYPE)
        params["content_type"] = flt.content_type
    if flt.published_from is not None:
        where.append(_WHERE_PUBLISHED_FROM)
        params["published_from"] = flt.published_from
    if flt.published_to is not None:
        where.append(_WHERE_PUBLISHED_TO)
        params["published_to"] = flt.published_to
    if flt.collected_from is not None:
        where.append(_WHERE_COLLECTED_FROM)
        params["collected_from"] = flt.collected_from

    where_sql = (" where " + " and ".join(where)) if where else ""
    return "".join(from_parts), where_sql, params


class SupabaseRepo:
    """实现 `storage.repo.Repo` 协议。"""

    def __init__(self, dsn: str) -> None:
        import psycopg  # 延迟导入：单元测试不需要装驱动
        from psycopg.rows import dict_row

        # prepare_threshold=None 是 6543 端口（连接池事务模式）的硬要求：
        # pgbouncer 在事务模式下不保证同一连接的语句落在同一后端，
        # 预编译语句会失效。表现是跑到第 5 次同一条语句时才炸，很难查。
        #
        # ⚠️⚠️ `autocommit=True` **不是优化，是这一层能不能存住东西的前提**。
        #    psycopg 默认不开 autocommit，于是**任何一条语句**都会隐式开一个事务；
        #    而那之后所有 `with self._conn.transaction():` 都不再是"开事务"，
        #    而是**开 SAVEPOINT**——退出时 release，**不提交外层的那个事务**。
        #    最后 `close()` 一关，整个事务回滚，**写进去的东西一条都不剩**。
        #
        #    触发条件低得可怕：**只要在读之后写就行**。而这一层每个读方法
        #    （`question_id_for` / `all_urls` / `active_prompt_bundle`…）用的都是
        #    裸 cursor，读一次连接就停在 INTRANS。`insert_content` 内部
        #    就先调了 `ensure_author`/`question_id_for`，所以**它自己也中招**。
        #
        #    `dim_prompt` 曾经是唯一能存住的表，因为 `prompts push` 一个读都不做，
        #    第一条语句就是那个 `update`——它恰好是唯一一条"先写后读"的路径。
        #    换句话说：**这个 bug 在此之前没被发现，只是因为还没有别的路径跑过。**
        self._conn = psycopg.connect(
            dsn,
            row_factory=dict_row,
            prepare_threshold=None,
            # 单条语句各自提交；要原子性就用 `with self._conn.transaction():`
            # ——显式 BEGIN/COMMIT，在 autocommit 下依然成立（psycopg 文档的做法）。
            autocommit=True,
        )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> SupabaseRepo:
        return self

    def __exit__(self, *exc: object) -> bool:
        """连着的那个长连接**必须显式关**。

        `psycopg.connect` 不是 `with` 就能自动收的（`self._conn` 是实例属性，
        不是 with 里的那个对象），所以这里手动关。少了它，命令行跑完
        连接会挂到进程退出才断——而连接池端口 6543 的连接数是有限的。
        """
        self.close()
        return False

    # ── 查重 ────────────────────────────────────────────────────────

    def existing_urls(self, urls: Sequence[str]) -> set[str]:
        with self._conn.cursor() as cur:
            cur.execute("select url from fact_content where url = any(%s)", (list(urls),))
            return {row["url"] for row in cur.fetchall()}

    def existing_question_ids(self, zhihu_qids: Sequence[str]) -> set[str]:
        with self._conn.cursor() as cur:
            cur.execute(
                "select zhihu_qid from dim_question where zhihu_qid = any(%s)",
                (list(zhihu_qids),),
            )
            return {row["zhihu_qid"] for row in cur.fetchall()}

    def all_urls(self) -> set[str]:
        with self._conn.cursor() as cur:
            cur.execute(_ALL_URLS)
            return {row["url"] for row in cur.fetchall()}

    # ── 问题 ────────────────────────────────────────────────────────

    def all_questions(self) -> list[QuestionRow]:
        with self._conn.cursor() as cur:
            cur.execute(_ALL_QUESTIONS)
            return [_row(QuestionRow, row) for row in cur.fetchall()]

    def follow_up_questions(self, since: datetime) -> list[QuestionRow]:
        with self._conn.cursor() as cur:
            cur.execute(_FOLLOW_UP_QUESTIONS, (since,))
            return [_row(QuestionRow, row) for row in cur.fetchall()]

    def question_by_id(self, question_id: int) -> QuestionRow | None:
        with self._conn.cursor() as cur:
            cur.execute(_QUESTION_BY_ID, (question_id,))
            row = cur.fetchone()
            return _row(QuestionRow, row) if row else None

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
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                _CLAIM_QUESTION,
                (
                    zhihu_qid,
                    url,
                    title,
                    description,
                    asked_at,
                    follower_count,
                    view_count,
                    answer_count,
                ),
            )
            row = cur.fetchone()
            return row["question_id"] if row else None

    def set_question_relevance(self, question_id: int, is_relevant: bool) -> None:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(
                """
                update dim_question
                set is_relevant = %s, relevant_checked_at = now()
                where question_id = %s
                """,
                (is_relevant, question_id),
            )

    def mark_follow_up_done(self, question_id: int) -> bool:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_MARK_FOLLOW_UP, (question_id,))
            return cur.fetchone() is not None

    def mark_answers_collected(self, question_id: int) -> None:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_MARK_ANSWERS_COLLECTED, (question_id,))

    # ── 作者维度 ────────────────────────────────────────────────────

    def ensure_author(
        self, zhihu_user_id: str, nickname: str | None, profile_url: str | None
    ) -> int:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_ENSURE_AUTHOR, (zhihu_user_id, nickname, profile_url))
            return cur.fetchone()["author_id"]

    def question_id_for(self, zhihu_qid: str) -> int | None:
        with self._conn.cursor() as cur:
            cur.execute(_QUESTION_ID, (zhihu_qid,))
            row = cur.fetchone()
            return row["question_id"] if row else None

    def author_by_id(self, author_id: int) -> AuthorRow | None:
        with self._conn.cursor() as cur:
            cur.execute(_AUTHOR_BY_ID, (author_id,))
            row = cur.fetchone()
            return _row(AuthorRow, row) if row else None

    def set_author_watch(
        self,
        zhihu_user_id: str,
        *,
        is_watched: bool | None = None,
        watch_note: str | None = None,
        stance: str | None = None,
    ) -> None:
        params = {
            "zhihu_user_id": zhihu_user_id,
            "is_watched": is_watched,
            "watch_note": watch_note,
            "stance": stance,
        }
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_SET_AUTHOR_WATCH, params)

    # ── 内容入库 ────────────────────────────────────────────────────

    def insert_content(
        self, record: ContentRecord, *, parent_id: str | None = None
    ) -> str | None:
        """维度表同步在这里收口：记录只带知乎侧标识，主键在这一层翻译。

        ⚠️ `record.content_type == 'question'` 不在这里挡——库上的 check 约束
        会拒（迁移 0004 之后），但报出来的是英文的约束错误。挡住它并报一句
        人话，是 `ingest.py` 的 `insert_judged` 的活。
        """
        params = dict(vars(record))
        params["parent_id"] = parent_id

        params["author_id"] = (
            self.ensure_author(record.author_zhihu_id, record.author_name, record.author_url)
            if record.author_zhihu_id
            else None
        )
        params["question_id"] = (
            self.question_id_for(record.question_zhihu_id) if record.question_zhihu_id else None
        )

        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_INSERT_CONTENT, params)
            row = cur.fetchone()
            return str(row["content_id"]) if row else None

    def save_analysis(self, result: AnalysisResult) -> None:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_SAVE_ANALYSIS, vars(result))

    # ── 内容维护 ────────────────────────────────────────────────────

    def refresh_content_metrics(
        self, content_id: str, *, voteup_count: int, comment_count: int
    ) -> None:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_REFRESH_METRICS, (voteup_count, comment_count, content_id))

    def mark_content_status(self, content_id: str, status: str) -> None:
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_MARK_CONTENT_STATUS, (status, content_id))

    # ── 复核：读 ────────────────────────────────────────────────────

    def content_by_id(self, content_id: str) -> ContentRow | None:
        with self._conn.cursor() as cur:
            cur.execute(_CONTENT_BY_ID, (content_id,))
            row = cur.fetchone()
            return _row(ContentRow, row) if row else None

    def analysis_for(self, content_id: str) -> AnalysisResult | None:
        with self._conn.cursor() as cur:
            cur.execute(_ANALYSIS_FOR, (content_id,))
            row = cur.fetchone()
            return _row(AnalysisResult, row) if row else None

    def analysis_reviewed_by(self, content_id: str) -> str | None:
        with self._conn.cursor() as cur:
            cur.execute(_ANALYSIS_REVIEWED_BY, (content_id,))
            row = cur.fetchone()
            return row["reviewed_by"] if row else None

    def event_stances_for(self, content_id: str) -> list[EventStanceRow]:
        with self._conn.cursor() as cur:
            cur.execute(_EVENT_STANCES_FOR, (content_id,))
            return [_row(EventStanceRow, row) for row in cur.fetchall()]

    # ── 复核：写 ────────────────────────────────────────────────────

    def apply_human_analysis(
        self, content_id: str, patch: AnalysisPatch, *, reviewed_by: str | None = None
    ) -> None:
        params: dict[str, Any] = {"content_id": content_id, "reviewed_by": reviewed_by}
        params.update(vars(patch))
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_APPLY_HUMAN_ANALYSIS, params)

    def overwrite_event_stance(
        self, content_id: str, event_id: int, patch: EventStancePatch
    ) -> None:
        params: dict[str, Any] = {"content_id": content_id, "event_id": event_id}
        params.update(vars(patch))
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_OVERWRITE_EVENT_STANCE, params)

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
        params = {
            "content_id": content_id,
            "event_id": event_id,
            "stance": stance,
            "analyzed_by": analyzed_by,
            "event_version": event_version,
            "confidence": confidence,
            "prompt_version": prompt_version,
            "notes": notes,
        }
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_SAVE_CONTENT_EVENT, params)

    # ── 议题 ────────────────────────────────────────────────────────

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
        params = {
            "name": name,
            "summary": summary,
            "keywords": list(keywords),
            "event_type": event_type,
            "start_date": start_date,
            "end_date": end_date,
        }
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_CREATE_EVENT, params)
            return cur.fetchone()["event_id"]

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
        params: dict[str, Any] = {
            "event_id": event_id,
            "name": name,
            "summary": summary,
            "keywords": list(keywords) if keywords is not None else None,
            "event_type": event_type,
            "start_date": start_date,
            "end_date": end_date,
            "bump": 1 if bump_version else 0,
            "mark_backfill_scanned": mark_backfill_scanned,
        }
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_UPDATE_EVENT, params)
            return cur.fetchone() is not None

    def event_by_id(self, event_id: int) -> EventRow | None:
        with self._conn.cursor() as cur:
            cur.execute(_EVENT_BY_ID, (event_id,))
            row = cur.fetchone()
            return _row(EventRow, row) if row else None

    def count_stale_judgments(self, event_id: int) -> int:
        with self._conn.cursor() as cur:
            cur.execute(_COUNT_STALE_JUDGMENTS, (event_id,))
            return cur.fetchone()["n"]

    def list_events(self, *, include_empty: bool = True) -> list[EventRow]:
        with self._conn.cursor() as cur:
            cur.execute(_LIST_EVENTS, {"include_empty": include_empty})
            return [_row(EventRow, row) for row in cur.fetchall()]

    # ── 证据 ────────────────────────────────────────────────────────

    def insert_evidence(
        self,
        content_id: str,
        *,
        evidence_type: str | None,
        evidence_number: str | None,
        filed_by: str | None,
        notes: str | None = None,
    ) -> int:
        params = {
            "content_id": content_id,
            "evidence_type": evidence_type,
            "evidence_number": evidence_number,
            "filed_by": filed_by,
            "notes": notes,
        }
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute(_INSERT_EVIDENCE, params)
            return cur.fetchone()["evidence_id"]

    def evidence_for(
        self, *, content_id: str | None = None, limit: int = 50
    ) -> list[EvidenceRow]:
        # 两条分支分开写，**不写成 `sql = A if ... else B` 再配一个条件元组**：
        # 那样参数个数取决于运行时，`test_sql_params.py` 的静态检查就看不见它，
        # 而"占位符和参数对不上"正是那个文件要挡的错。
        with self._conn.cursor() as cur:
            if content_id is not None:
                cur.execute(_EVIDENCE_FOR_CONTENT, (content_id, limit))
            else:
                cur.execute(_EVIDENCE_ALL, (limit,))
            return [_row(EvidenceRow, row) for row in cur.fetchall()]

    # ── 分析型查询 ──────────────────────────────────────────────────

    def search_contents(
        self,
        flt: ContentFilter,
        *,
        sort: SortKey,
        limit: int,
        offset: int,
    ) -> list[SearchRow]:
        try:
            column = _SEARCH_SORT_COLUMNS[sort.field]
        except KeyError:
            raise ValueError(
                f"不认识的排序字段 {sort.field!r}；"
                f"只能是 {sorted(_SEARCH_SORT_COLUMNS)} 之一"
            ) from None

        from_sql, where_sql, params = _search_from_where(flt)
        params["limit"] = limit
        params["offset"] = offset
        direction = "desc" if sort.desc else "asc"
        sql = (
            f"select{_SEARCH_COLUMNS}{from_sql}{where_sql}"
            f" order by {column} {direction}, c.content_id"
            f" limit %(limit)s offset %(offset)s"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return [_row(SearchRow, row) for row in cur.fetchall()]

    def count_contents(self, flt: ContentFilter) -> int:
        from_sql, where_sql, params = _search_from_where(flt)
        with self._conn.cursor() as cur:
            cur.execute(f"select count(*) as n {from_sql}{where_sql}", params)
            return cur.fetchone()["n"]

    def alert_rows(
        self, *, risk_levels: Sequence[str], since: datetime, limit: int
    ) -> list[SearchRow]:
        params = {"risk_levels": list(risk_levels), "since": since, "limit": limit}
        with self._conn.cursor() as cur:
            cur.execute(_ALERT_ROWS, params)
            return [_row(SearchRow, row) for row in cur.fetchall()]

    def count_by_risk(self, *, since: datetime) -> dict[str, int]:
        with self._conn.cursor() as cur:
            cur.execute(_COUNT_BY_RISK, {"since": since})
            return {row["risk_level"]: row["n"] for row in cur.fetchall()}

    def prescreen_rows(
        self,
        *,
        keywords: Sequence[str],
        since: datetime | None,
        limit: int,
        threshold: float | None = None,
    ) -> list[PrescreenRow]:
        # 阈值是**参数不是常量**：它还没标定（架构文档 8），标定之前不该被
        # 任何代码当成既定事实。默认值只是一个明显不靠谱的占位。
        if threshold is None:
            threshold = _PRESCREEN_DEFAULT_THRESHOLD
        # ⚠️ 两条分支的参数字典**分开写**，虽然只差一个 since。两个理由：
        #    ① `sql = A if ... else B` 再传 `sql`，静态检查就看不见用的是哪个
        #       常量了（`test_sql_params.py` 会因此漏检）；
        #    ② `_PRESCREEN_ALL` 的 SQL 里没有 `%(since)s`，多塞一个键进去
        #       会让"参数字典就是这条查询的完整描述"这件事不再成立。
        #    这份重复是刻意换来的精确，别为了少写三个键把它合回去。
        with self._conn.cursor() as cur:
            if since is None:
                # backfill：全库扫，偶发的一次性补救
                cur.execute(
                    _PRESCREEN_ALL,
                    {
                        "keywords": list(keywords),
                        "threshold": threshold,
                        "limit": limit,
                    },
                )
            else:
                # new：限时间窗口，走得上 idx_fact_content_collected
                cur.execute(
                    _PRESCREEN_WINDOWED,
                    {
                        "keywords": list(keywords),
                        "threshold": threshold,
                        "limit": limit,
                        "since": since,
                    },
                )
            return [_row(PrescreenRow, row) for row in cur.fetchall()]

    # ── 提示词 ──────────────────────────────────────────────────────

    def active_prompt_bundle(self) -> PromptBundle | None:
        with self._conn.cursor() as cur:
            cur.execute(
                "select version, content_hash, content, note from dim_prompt where is_active limit 1"
            )
            row = cur.fetchone()
            if not row:
                return None
            return PromptBundle(
                version=row["version"],
                content_hash=row["content_hash"],
                modules=row["content"],
                note=row["note"],
            )

    def push_prompt_bundle(self, bundle: PromptBundle) -> None:
        from psycopg.types.json import Jsonb

        # 同一个事务里"先下线旧的、再上线新的"——避免出现两个 active
        with self._conn.transaction(), self._conn.cursor() as cur:
            cur.execute("update dim_prompt set is_active = false where is_active")
            cur.execute(
                """
                insert into dim_prompt (version, content_hash, content, note, is_active)
                values (%s, %s, %s, %s, true)
                """,
                # ⚠️ 必须包 Jsonb：psycopg 不会自动把 dict 适配到 jsonb 列，
                #    直接传会抛 cannot adapt type 'dict'
                (
                    bundle.version,
                    bundle.content_hash,
                    Jsonb(bundle.modules),
                    bundle.note,
                ),
            )
