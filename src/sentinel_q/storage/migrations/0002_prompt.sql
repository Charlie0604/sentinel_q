-- 0002 —— dim_prompt：提示词的权威副本（架构文档 8.9）。
--
-- 提示词不进 git，所以**库是唯一能找到它的地方**。这也是全项目最敏感的一张表。
create table dim_prompt (
  prompt_id    bigserial primary key,

  version      int  not null,
                 -- 人工维护的版本号

  content_hash text not null,
                 -- 拼装后的整体 hash，写进 fact_analysis / fact_content_event
                 -- 的 prompt_version

  content      jsonb not null,
                 -- {"a_role": "...", "b_subject": "...", ...}

  is_active    boolean not null default false,

  note         text,
                 -- 这次改了什么

  created_at   timestamptz not null default now(),

  unique (version)
);

-- 同时只能有一版生效。
-- 推送时在同一个事务里"先下线旧的、再上线新的"，避免出现两个 active
-- （见 storage/supabase.py:push_prompt_bundle）。
create unique index on dim_prompt (is_active) where is_active;

-- RLS（架构文档 5.7）：不建任何 policy，anon 读到的是空。
-- 本表尤其不能漏——它存的就是提示词本身。
alter table dim_prompt enable row level security;
