-- 0001 —— 业务星型模型，逐字对应架构文档 5.4。
-- 顺序：三张维度表 → 中心事实表 → 三张附属事实表。
--
-- 与 5.4 的四处差异（文档已同步，见 5.4 / 5.7）：
--   1. dim_question 多一列 answers_collected_at
--   2. idx_fact_content_published 由 brin 改 btree
--   3. fact_analysis 多一列 prompt_version（决策 47 / 8.9 早已要求，5.4 漏了）
--   4. fact_content_event 同样多一列 prompt_version
-- 末尾的 RLS 块不属于 5.4，是本次新增的安全默认。

-- ============================================================
-- 【维度表】
-- ============================================================

-- dim_author —— 用户维度。把"谁在说话"从裸文本提升为实体，
-- 这样"高频账号追踪"是对实体做聚合，也才有地方挂"重点关注""立场"这类用户级属性。
-- 自然键用知乎用户ID 而不是昵称：昵称随时可改，不是稳定标识。
create table dim_author (
  author_id      bigserial primary key,
                 -- 代理键。纯内部使用，整数比 uuid 更省空间、join 更快

  zhihu_user_id  text unique not null,
                 -- 自然键。追踪"同一用户在不同内容下的发言"要用它，不能用昵称

  nickname       text,
                 -- 当前昵称。会变，仅供人工阅读时快速识别

  profile_url    text,

  stance         text check (stance in ('正向','反向','中立')),
                 -- ⚠️ null = 未知，不是中立。中立 = 已判定为中立，未知 = 还没判。
                 -- 用 null 而非造一个 '未知' 枚举值：null 在聚合里天然被 WHERE 排除。
                 -- 现阶段仅支持人工填写，预留给将来 AI 自动判定（5.5.5）

  is_watched     boolean not null default false,
                 -- 重点关注账号。原设计里单独一张表，并入这里之后能顺带挂备注、
                 -- 并与该用户的历史内容直接关联

  watch_note     text,
                 -- 被列为重点关注的原因，供人工日后追溯

  first_seen_at  timestamptz not null default now(),
  last_seen_at   timestamptz
                 -- 首次 / 最近一次采集到该用户内容的时间
);

-- dim_question —— 问题维度。回答、评论通过 fact_content.question_id 挂到这里。
-- ★ 本表同时兼任"AI 去重台账"：所有提交过 AI任务B 的问题都在这里（含判为不相关的），
--   下次遇到同一个问题直接跳过，不再调用。理由见 4.1 与 5.5.2。
create table dim_question (
  question_id    bigserial primary key,

  zhihu_qid      text unique,
                 -- 知乎问题原生ID（从 URL 解析）。★ 查重就用这个字段，不用标题：
                 -- 问题描述被编辑后 ID 不变，而标题/描述哈希会变，
                 -- 会导致同一个问题被反复提交给 AI

  url            text,

  title          text,
                 -- ⚠️ 与 fact_content.title 有意冗余：维度表自包含，
                 -- 聚合列表展示问题名时不必 join 回事实表

  is_relevant    boolean,
                 -- AI任务B（3.5）对"问题本身是否与监测对象相关"的判断结果。
                 -- 三态，别当成普通的布尔字段：
                 --   null   → 已抢占（行已插入）但 AI 还没跑完，见 4.1 的"先插后问"
                 --   true   → 相关，触发该问题下所有回答的采集
                 --   false  → 不相关，不触发全量采集；但问题行仍然保留，
                 --            一是给其下可能存在的相关回答当父级（5.5.2 情况3），
                 --            二是作为去重台账挡住重复的 AI 调用（4.1）
                 -- ⚠️ 任何按问题聚合的业务查询都必须显式加 where is_relevant = true
                 -- ⚠️ 现行流程下不会再产生 null（抢占与判定之间不再隔着一次采集），
                 --    三态保留作兜底——判空逻辑哪天被绕过时，能看出来而不是被当成 false

  relevant_checked_at timestamptz,
                 -- AI任务B 的完成时间。为空 = 尚未判完（可能正在跑，也可能是失败遗留），
                 -- 重试靠 is_relevant is null and first_seen_at 超时来捞回孤儿行

  follow_up_done boolean not null default false,
                 -- 是否已触发过"该问题下全量回答采集"，避免重复触发

  answers_collected_at timestamptz,
                 -- 该问题下的回答实际采完的时间。
                 -- 与 follow_up_done 的区别：那个是"已触发"，这个是"已采完"——
                 -- 采集中途崩了的时候，只有后者能看出这个问题的回答是残缺的

  first_seen_at  timestamptz not null default now()
                 -- 首次采集到该问题的时间（即 4.1 抢占行插入的时间）
);

-- dim_event —— 议题维度。模块四事件分类功能的基础，一个议题可以关联多条内容。
create table dim_event (
  event_id       bigserial primary key,

  name           text not null,
                 -- 人工命名，如"XX事件-2026年3月"

  summary        text not null,
                 -- 200-300字核心摘要，含关键时间节点与各方核心主张。
                 -- 这段文字会被当作 system prompt 传给 AI 做背景上下文，
                 -- 所以要精炼，只保留 AI 判断立场需要的信息

  keywords       text[],
                 -- 议题关键词列表，用于模块四的预筛选匹配（6.2）

  event_type     text check (event_type in ('new','backfill')),
                 -- new=新发生的议题（按时间窗口扫描）；backfill=补录的历史议题（全库扫描）。
                 -- 两种走不同逻辑，不由系统自动判断

  backfill_scanned boolean default false,
                 -- 仅 backfill 类型使用：是否已完成一次全库关键词扫描，避免重复触发

  version        int not null default 1,
                 -- 议题摘要发生实质性修改时人工手动 +1。
                 -- 用于比对 fact_content_event.event_version，判断哪些判断基于旧版本（6.4）

  start_date     date,
  end_date       date,
                 -- 仍在持续则 end_date 留空

  created_at     timestamptz not null default now()
                 -- 该议题记录本身的创建时间（不是事件发生时间）
);

-- ============================================================
-- 【事实表】
-- ============================================================

-- fact_content —— 所有抓到的内容：问题、回答、专栏文章、想法、评论。
-- 用 content_type 区分种类，用 parent_id / question_id 表达层级，
-- 查证据链时能顺着 parent_id 一路追溯上去。
create table fact_content (
  content_id     uuid primary key default gen_random_uuid(),
                 -- 主键保留 uuid（而非整数代理键）的原因：正文要存成
                 -- content/{id}.txt，ID 必须在文件上传前就存在，不能等数据库自增分配。
                 -- gen_random_uuid() 从 PG13 起是内置的，**不需要 pgcrypto 扩展**

  -- ---------- 退化维度（低基数枚举，做成维度表只会多一次无谓 join）----------
  content_type   text not null check (content_type in
                   ('question','answer','article','thought','comment')),
                 -- 问题本身 / 回答 / 专栏文章 / 想法 / 评论（含子评论，不再区分层级）

  status         text not null default 'active'
                   check (status in ('active','deleted_detected','edited_detected')),
                 -- ⚠️ 复查功能本身已决定延后，但 raw_content_hash 必须现在就开始采集，
                 --    因为它没有追溯能力（3.10）

  -- ---------- 自然键 ----------
  zhihu_id       text not null,
                 -- 知乎平台原生ID（从 URL 提取），和 url 一起用于识别"这是哪一条内容"

  url            text not null unique,
                 -- ⚠️ 必须是【规范化后】的 URL（规则见 3.9：剥掉 query 与 fragment、
                 -- 统一 host）。这是去重的主要依据。不做规范化的话，
                 -- 同一内容会以不同 URL 反复入库，unique 约束形同虚设

  -- ---------- 维度外键 ----------
  author_id      bigint references dim_author(author_id),
                 -- 匿名回答或已注销账号可能为空

  question_id    bigint references dim_question(question_id),
                 -- ★ 平铺的根祖先：回答 → 所属问题；评论 → 其所属回答的问题；
                 --   问题自身 → 指向自己的维度行。
                 --   让"某问题下的所有内容"变成一句 GROUP BY，无需递归 CTE。
                 --   代价是冗余一列，换来干掉整个递归查询（5.5.1）

  parent_id      uuid references fact_content(content_id),
                 -- 直接父级：评论 → 所属回答/文章/想法；回答 → 问题。问题自身为空。
                 -- ⚠️ 这是事实表里刻意保留的【自引用例外】，理由见 5.5.1

  -- ---------- 内容本体 ----------
  title          text,
                 -- 仅 question 和 article 有值，answer/comment/thought 留空

  content_text   text,
                 -- 短内容（尤其是大多数评论）直接存这里，不用额外建文件

  storage_path   text,
                 -- 长内容的正文文件路径，如 content/{id}.txt。
                 -- 与 content_text 二选一，不会同时为空（见下方 check 约束）

  -- ---------- 度量 ----------
  voteup_count   int default 0,
                 -- 采集时刻的赞同数，用作热度代理指标（知乎不公开阅读量）

  comment_count  int default 0,

  content_length int,
                 -- 原文字数。用于判断走 content_text 还是 storage_path，也方便统计

  -- ---------- 快照（取证用，分层策略见 3.10）----------
  raw_content_hash   text,
                 -- 原文的 sha256。重抓同一 URL 时哈希变了说明对方编辑过原内容，
                 -- 应触发 edited_detected 并保留旧版本证据

  snapshot_path      text,
                 -- 正文片段 HTML（gzip 后约 2~8KB），【全量】存储。
                 -- 不存整页：整页 300KB~1.5MB，5万条就是 15~75GB，远超免费额度

  html_snapshot_path text,
                 -- 完整网页 HTML，【仅高风险内容】存储。
                 -- 比截图更利于法律取证，因为能看到完整页面结构

  screenshot_path    text,
                 -- 整页截图，【仅高风险内容】存储。单张 200KB~1MB，不做全量

  -- ---------- 时间戳 ----------
  published_at   timestamptz,
                 -- 知乎页面上显示的发布时间（不是我们抓取的时间）。
                 -- 模块四的时间窗口预筛选依赖这个字段（6.3）

  collected_at   timestamptz not null default now(),
                 -- 我们实际抓到这条内容的时间，用于判断"是否为新增内容"

  check (content_text is not null or storage_path is not null)
  -- 约束：原文要么直接存在字段里，要么存成文件，不能两者都为空
);

create index idx_fact_content_type       on fact_content(content_type);
create index idx_fact_content_question   on fact_content(question_id);
create index idx_fact_content_parent     on fact_content(parent_id);
create index idx_fact_content_author     on fact_content(author_id);
create index idx_fact_content_collected  on fact_content(collected_at);
create index idx_fact_content_published  on fact_content(published_at);
                 -- 时间范围扫描（走势图、模块四的事件时间窗口预筛选）用 btree。
                 -- ⚠️ 这里原本写的是 BRIN，改掉的原因是**前提不成立**：
                 --    BRIN 的价值来自"物理顺序 ≈ 该列顺序"，而我们的行是按
                 --    **相关度**序插进去的，不是按时间序。前提不成立时 BRIN
                 --    不但不省，还会退化成全表扫描

-- fact_analysis —— 模块二 AI任务A（或人工）对每条内容做的判断，一条内容一条记录。
-- ⚠️ 与 fact_content 分离的理由：fact_content 是"观察到的客观事实"（不可变），
--    本表是"我们的主观判断"（会被人工覆盖）。对取证系统来说，
--    "证明内容原样是什么"和"证明我们当时怎么判断的"是两件事。
create table fact_analysis (
  content_id        uuid primary key references fact_content(content_id),
                     -- 重新分析直接覆盖，不新增（已定：不保留判断的修改历史）

  ai_summary        text,
                     -- AI 生成的内容摘要，供人工复核时快速了解这条内容讲了什么

  platform_stance   text check (platform_stance in ('有利','抹黑','中立','不相关')),
                     -- 对监测主体整体的第一层判断，跟具体议题无关；
                     -- 针对具体议题的立场在 fact_content_event。
                     -- 值为 '不相关' 的记录仍然存在于本表——见 5.5.2 情况3/4

  stance_confidence numeric,
                     -- 0-1，用于调优或优先复核低置信度条目

  risk_level        text check (risk_level in ('低风险','中风险','高风险')),
                     -- 三档定性判断，不用连续数值打分。
                     -- ⚠️ 高风险内容会额外触发整页快照与截图（3.10）

  risk_reasoning    text,
                     -- 给出 risk_level 的简要依据，比如"检测到疑似人肉隐私信息"，
                     -- 让复核的人不必面对一个孤立的等级标签猜原因

  analyzed_by       text not null default 'ai' check (analyzed_by in ('ai','human')),
                     -- 判断是 AI 给出的还是人工给出/修改的。
                     -- 既然是"事实与判断分离"，判断的产出者就该被记录

  model_version     text,
                     -- 本次分析用的模型

  prompt_version    text,
                     -- ⚠️ 本次分析用的提示词版本（dim_prompt.content_hash）。
                     -- 提示词不在 git 里，所以 git 历史回答不了"这条判断当时用的哪版
                     -- 提示词"——只能靠这一列（决策 47 / 8.9）。
                     -- model_version 只记模型，补不了这个洞

  analyzed_at       timestamptz not null default now()
                     -- 本条判断完成（或最近一次被人工修改）的时间
);

create index idx_fact_analysis_stance on fact_analysis(platform_stance);
create index idx_fact_analysis_risk   on fact_analysis(risk_level);

-- fact_content_event —— 模块四的核心表：某条内容针对某个具体议题的立场。多对多。
-- ⚠️ 形式上是"桥表"，但它带度量（stance/confidence），本质是一张事实表。
create table fact_content_event (
  content_id    uuid not null references fact_content(content_id),

  event_id      bigint not null references dim_event(event_id),

  stance        text check (stance in ('正向','反向','中立')),
                 -- 区别于 fact_analysis.platform_stance 的"对平台整体"判断，
                 -- 这里判断的是"对这一具体议题"的态度

  confidence    numeric,

  analyzed_by   text check (analyzed_by in ('ai','human')),
                 -- 人工复核时更新这条记录本身（不新增历史），并把此字段改成 'human'

  event_version int,
                 -- 这条判断基于议题的第几个版本（对应 dim_event.version），
                 -- 用于议题摘要改动后判断是否需要重新分析（6.4）

  prompt_version text,
                 -- 同 fact_analysis.prompt_version：判断实际用的那一版提示词

  notes         text,
                 -- 人工复核备注，比如修改判断的理由

  analyzed_at   timestamptz not null default now(),

  primary key (content_id, event_id)
  -- 一条内容对一个议题只保留一条最新判断，不保留历史版本
);

-- fact_evidence —— 正式法律取证的元信息登记（公证编号、时间戳凭证号等）。
-- ⚠️ 只存"登记信息"，证据文件本身不进本系统，由人工在走完公证/存证流程后自行保管。
create table fact_evidence (
  evidence_id     bigserial primary key,

  content_id      uuid not null references fact_content(content_id),
                   -- 这也是"问题必须进 fact_content"的原因之一（5.5.3）：
                   -- 若要给一个攻击性的提问本身取证，它必须能被这里引用

  evidence_type   text,
                   -- 公证 / 可信时间戳 / 区块链存证 / 其他

  evidence_number text,
                   -- 官方编号/凭证号，用于日后核验

  filed_by        text,
                   -- 登记操作人，便于追溯责任

  filed_at        timestamptz not null default now(),
                   -- 登记时间（不等于实际取证时间，实际取证时间写在 notes 里）

  notes           text
);

-- ============================================================
-- RLS（架构文档 5.7）
-- ============================================================
--
-- ⚠️ 以下不是 5.4 的内容，是本次新增的安全默认。**不建任何 policy**，
--    于是 anon / authenticated 角色读到的是空。
--
-- 为什么必须做：Supabase 的 PostgREST 端点是**公开可达**的（网络限制只管
-- Postgres 和 pooler，管不到 HTTPS API），保护它的**只有 RLS**。
-- 我们自己的连接是 postgres 角色，是表 owner，绕过 RLS，功能不受影响。
--
-- 放在文件末尾而不是单独一个文件，是为了不存在"表已建好但还没上 RLS"的窗口期。
alter table dim_author         enable row level security;
alter table dim_question       enable row level security;
alter table dim_event          enable row level security;
alter table fact_content       enable row level security;
alter table fact_analysis      enable row level security;
alter table fact_content_event enable row level security;
alter table fact_evidence      enable row level security;
