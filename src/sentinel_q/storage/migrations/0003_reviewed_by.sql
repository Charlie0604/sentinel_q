-- 0003 —— fact_analysis.reviewed_by：这条判断是谁复核的（架构文档 6.1 第 2 项）。
--
-- 决策 5 定的是"不保留判断的修改历史，人工直接覆盖"，于是"这条判断被人动过"
-- 这件事只剩两处痕迹：analyzed_by = 'human'，以及这一列。
-- 前者说"被改过"，后者说"谁改的"——只有前者的话，两个人先后改过同一条内容，
-- 事后分不出是谁最后拍的板。
--
-- 与 fact_evidence.filed_by 同形：这套系统没有用户身份（不做角色分级，
-- 密码是唯一那道门，见决策"分发形态"），所以"谁"只能是自由文本，不能是外键。
alter table fact_analysis add column reviewed_by text;

comment on column fact_analysis.reviewed_by is
  '复核人。自由文本——本系统没有用户身份，密码是唯一那道门，所以这里存不了外键。
   null = 没人改过。
   ⚠️ 写入路径只有 apply_human_analysis（storage/supabase.py），它在写这一列的同时
   强制 analyzed_by = ''human''。反过来不成立：analyzed_by = ''human'' 而这一列为 null
   是可能的（复核的人没填名字），别拿这一列当"是否被人工改过"的判据——
   那个判据是 analyzed_by。';
