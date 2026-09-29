-- 0005 —— dim_question 补三个热度指标（2026-09-29 定，能力五落地时加）。
--
-- 3.5 原文把这三列的时机写死了："表结构要等实现时再加三列……本次建库不带"。
-- 现在能力五（`collector/question.py`）能取到它们了，所以补上。
--
-- ⚠️ 为什么是"现在才加"而不是建库时就带上：**这三个数只在那一次页面抓取里存在**。
--    取它们的代价是开一次浏览器、打一次知乎，所以它们的可用性完全取决于
--    采集侧什么时候能取——列先建好而采集侧取不到，只会得到三列恒为 null 的
--    数据，然后被人当成"这些数就是 0"。
--
-- ⚠️ 三列都可空，**刻意不给 default 0**：0 个关注和"没采到"是两回事。
--    同一条规矩在 `storage/ingest.py` 里写着（"取不到"和"本来就没有"要分开）。
--
-- ⚠️ 类型选 bigint 而不是 int：`follower_count` / `answer_count` 用 int4
--    （上限 21 亿）绰绰有余，但**被浏览没有上界**——实测单个热门问题
--    920 万，而 view_count 只增不减、知乎开站十几年。三列统一 bigint
--    比"两列 int4 一列 int8"更容易记住，代价是每行多 12 字节。

alter table dim_question add column follower_count bigint;
alter table dim_question add column view_count     bigint;
alter table dim_question add column answer_count   bigint;

comment on column dim_question.follower_count is
  '关注者数。★ 非回溯的热度快照：**第一次抓到的那一刻的值**，之后不更新。

   ⚠️ 为什么冻结：`claim_question` 是 `on conflict do nothing returning` 的
   抢占语义，返回 None（已被别人抢占）时调用方不会再写第二次——和
   description 完全同一条规矩（见 0004 里那一列的注释）。要做"回访更新热度"
   得先定"多久回访一次"的策略，而那个策略不存在；凭空加一个
   update_question_metrics 只会在下一次实现里被误用成"每次采集都刷一遍"。

   ⚠️ **空 = 没采到，不是 0。** 0 个关注是可能的，别把两者混起来做统计。

   ⚠️ 它和 first_seen_at 的差，是"这个问题在我们发现它之前攒了多少热度"——
   但两个数**不是同一时刻**的（first_seen_at 是抢占那一刻，这一列是采描述
   那一刻，同一次抓取里）。做时间线时别把它当成严格的同刻快照。';

comment on column dim_question.view_count is
  '被浏览数。★ 与 follower_count 完全同一套规矩：抢占那一刻冻结、可空、
   空 ≠ 0、非回溯。理由见那一列的注释。

   ⚠️ 它来自 `entities.questions[qid].visitCount`——**页面上那行小字是
   「被浏览」，不是「浏览量」**，两者在知乎上指同一个数（实测 2026-09-29）。';

comment on column dim_question.answer_count is
  '回答数。★ 同样在抢占那一刻冻结。

   ⚠️ 与 `fact_content` 里实际采到的回答条数是**两回事**，别互相校验：
   这个是知乎声明的总数，那个是我们采下来的条数。两者不等是**正常的**
   （采集会早停、会失败、会被限流），而"声明 500 条、采到 3 条"
   恰恰是能力四最该报警的那种状态。';
