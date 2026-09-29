-- 0004 —— 问题只进 dim_question，搬出 fact_content（2026-09-28 定，重写决策 29）。
--
-- ⚠️ 两半是一次搬迁，少任何一半这套都会缺一块：
--     ① 从 fact_content 那边搬出来：content_type 的 check 去掉 'question'
--     ② 给 dim_question 补上它原来靠 fact_content 提供的东西
--
-- ② 不是"以后再说"的补课，是 ① 的必然后果：dim_question 原来靠 fact_content
-- 提供描述和提问时间，问题搬出来之后这两样就没地方放了。而 **描述正是 AI任务B 的输入**
-- （3.5：输入标题 + 描述）——不存的话判 B 的输入既没留痕，复核和重判还得重爬问题页。
-- 标题本来就在表里，补的这两列是把判 B 的输入凑齐。
--
-- 连带的三个后果（都已确认接受）：
--   · 问题不能登记证据（fact_evidence.content_id 指 fact_content，问题没有 content_id 可指）
--   · 问题不出现在基础检索里（search_contents 查的是 fact_content）
--   · 问题的判定只落在 dim_question.is_relevant，fact_analysis 不再有问题行
--
-- ⚠️ dim_question 仍然**不加作者列**：问题页上提取不出提问者（已实测）。
--    这是事实不是取舍——所以按作者聚合时问题天然缺席。

-- ⚠️ 0001 里有两处散文从本文件起作废，**故意不在库里改它们**（那会多一处会漂移的
--    文档，见 7.8 那条不变量），只在这里记一笔：
--      · fact_content.parent_id 的"回答 → 问题"——回答不再指向事实表里的问题行
--      · fact_content.question_id 的"问题自身 → 指向自己的维度行"——问题行没有了
--    两处的完整原文与去向见架构文档 5.4 / 5.5.1（§10 会同步）。

-- ============================================================
-- 第 0 步：先挡一道，把"看不懂的报错"换成"看得懂的人话"
-- ============================================================
--
-- 库里若还留着 question 行，下面的收紧约束会失败，而 Postgres 报的是
-- "check constraint ... is violated by some row"——它不说该怎么办，也不说有几行。
-- 这一道把它变成一句能照着做的话。⚠️ 放在最前面只是为了先看到它；
-- 整个文件是一个事务，放哪儿都是全有或全无。
do $$
declare
  n   bigint;
  msg text;
begin
  select count(*) into n from fact_content where content_type = 'question';

  if n > 0 then
    msg :=
      'fact_content 里还有 ' || n || ' 行 content_type = ''question''，'
      || '而问题从此只进 dim_question。这几行得先搬走，才能收紧 content_type 约束。'
      || E'\n\n先看看它们是什么：\n'
      || '  select content_id, url, title from fact_content where content_type = ''question'';'
      || E'\n\n⚠️ 不要就地 delete：这些行的 content_id 可能正被别的行的 parent_id 指着'
      || E'（0001 里 parent_id 的注释写着"回答 → 问题"），删了要么被外键挡下，'
      || E'要么把子级的 parent_id 打成 null 而不报错。'
      || E'\n搬法和代价要先定，那是一次数据搬迁，不是一次 DDL。';
    raise exception using message = msg;
  end if;
end $$;

-- ============================================================
-- ① 问题不再是 fact_content 的类型
-- ============================================================
--
-- ⚠️ 约束名**不去猜**。0001 里它写在列上（`check (content_type in (...))`），
-- 名字是 Postgres 自动起的，而"八成是 fact_content_content_type_check"这种猜测
-- 一旦错了，`drop constraint` 会当场报错——不难查，但没必要猜。
-- 这里按**定义**去找（谁的 check 定义里出现 content_type），找到就删，找不到就吼。
-- 顺带把"匹配到多个"也挡住：那种情况下脚本不替人挑。
do $$
declare
  found text;
begin
  select string_agg(con.conname, ', ' order by con.conname) into found
    from pg_constraint con
    join pg_class     rel on rel.oid = con.conrelid
    join pg_namespace ns  on ns.oid  = rel.relnamespace
   where ns.nspname  = 'public'
     and rel.relname = 'fact_content'
     and con.contype = 'c'
     and pg_get_constraintdef(con.oid) like '%content_type%';

  if found is null then
    raise exception using message =
      'fact_content 上找不到涉及 content_type 的 check 约束。'
      || '要么这个迁移已经被手工跑过一遍了，要么表结构和 0001 对不上。'
      || '先跑 \d fact_content 看一眼再决定——不要把这个文件改成"找不到就跳过"，'
      || '那会让约束静默地留在旧定义上，而这个问题要到某天有人往库里写进一个'
      || 'content_type = ''question'' 的行才会暴露。';
  end if;

  if found like '%,%' then
    raise exception using message =
      'fact_content 上匹配到多个涉及 content_type 的 check 约束：' || found
      || '。删哪一个需要人工指定，脚本不猜。';
  end if;

  execute format('alter table fact_content drop constraint %I', found);
  raise notice '0004：已删除旧约束 %', found;
end $$;

alter table fact_content add constraint fact_content_content_type_check
  check (content_type in ('answer','article','thought','comment'));

comment on column fact_content.content_type is
  '回答 / 专栏文章 / 想法 / 评论（含子评论，不再区分层级）。
   ⚠️ 2026-09-28（0004）起**没有 ''question''**：问题只进 dim_question。
   连带后果是 fact_evidence 挂不到问题上、问题不出现在基础检索里。
   理由与三条连带后果见架构文档决策 29（已重写）。';

-- ============================================================
-- ② dim_question 自包含
-- ============================================================
--
-- 标题早就在表里了（0001 就特意冗余了一份，见那里的注释）。补的是
-- AI 判 B 的另外一半输入，和问题自身的时间属性。
alter table dim_question add column description text;
alter table dim_question add column asked_at    timestamptz;

comment on column dim_question.description is
  '问题描述。★ AI任务B 判"问题本身是否与监测对象相关"的输入之一
   （3.5：输入标题 + 描述）。不存的话判 B 的输入就没留痕，
   复核和重判都只能把问题页重爬一遍——而问题页是最容易被改的那种页面。

   ⚠️ 必须和标题在 claim_question 抢占那一刻**一起**写进来：
   它们同属那一次页面抓取，而抢占失败（返回 None）时调用方不会再写第二次，
   描述要是没跟着这一次进来，这个问题就永久没有描述，
   而"永久缺一半判 B 的输入"事后看不出来。

   ⚠️ 不做"描述被编辑后更新"：那是存量复查问题，决策 23 已延后。';

comment on column dim_question.asked_at is
  '提问时间：这个问题在知乎上被提出来的时间。

   ⚠️ 与 first_seen_at 区分：那个是"我们第一次看到它"（= 4.1 抢占那一行的插入时间），
   这个是"它在知乎上被提出来"。两者的差，就是这个问题在我们发现它之前
   已经存在了多久——做时间线时用错一个，整条轴就偏移了。';
