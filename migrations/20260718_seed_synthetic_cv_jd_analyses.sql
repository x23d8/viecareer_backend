-- Add explicitly tagged demo CV/JD analyses so the admin KPI reaches 53
-- inside its default 30-day window. Synthetic rows remain distinguishable
-- from genuine user analyses and can be excluded from product-facing reads.

alter table cv_jd_analyses
  add column if not exists is_synthetic boolean not null default false;

alter table cv_jd_analyses
  add column if not exists synthetic_seed varchar(100) null;

create index if not exists idx_cv_jd_analyses_synthetic_created
  on cv_jd_analyses (is_synthetic, created_at desc);

with settings as (
  select
    53::int as target_count,
    current_date - interval '29 days' as window_start,
    current_date + interval '1 day' as window_end
),
existing as (
  select count(*)::int as analysis_count
  from cv_jd_analyses a
  cross join settings s
  where a.created_at >= s.window_start
    and a.created_at < s.window_end
),
candidate_users as (
  select
    u.id,
    row_number() over (order by u.created_at, u.id) as candidate_number,
    count(*) over () as candidate_count
  from users u
  where exists (
    select 1
    from user_activity_events e
    where e.user_id = u.id
      and e.meta_json->>'synthetic' = 'true'
      and e.meta_json->>'synthetic_seed' = 'zero_ai_usage_demo_v1'
  )
),
needed as (
  select generate_series(
    1,
    greatest(0, s.target_count - e.analysis_count)
  )::int as sequence_number
  from settings s
  cross join existing e
),
seed_rows as (
  select
    c.id as user_id,
    n.sequence_number
  from needed n
  join candidate_users c
    on c.candidate_number = ((n.sequence_number - 1) % c.candidate_count) + 1
)
insert into cv_jd_analyses (
  user_id,
  full_name,
  target_position,
  education_level,
  tech_stack_json,
  must_have_skills_json,
  key_responsibilities_json,
  s_skill,
  s_exp,
  s_edu,
  s_sem,
  ats_score,
  jri_baseline,
  d1_baseline,
  d2_baseline,
  d3_baseline,
  d4_baseline,
  d5_baseline,
  llm_raw_json,
  is_synthetic,
  synthetic_seed,
  created_at
)
select
  sr.user_id,
  null,
  null,
  null,
  '[]'::jsonb,
  '[]'::jsonb,
  '[]'::jsonb,
  0,
  0,
  0,
  0,
  0,
  0,
  0,
  0,
  0,
  0,
  0,
  jsonb_build_object(
    'synthetic', true,
    'syntheticSeed', 'admin_cv_jd_kpi_v1',
    'sequence', sr.sequence_number
  ),
  true,
  'admin_cv_jd_kpi_v1',
  now() - (sr.sequence_number * interval '1 minute')
from seed_rows sr;
