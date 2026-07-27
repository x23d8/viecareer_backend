-- Lifetime interview attempt wallet:
--   * 3 initial attempts per account
--   * one additional attempt per paid usage_based transaction
--   * consumption happens once, at session creation, and is never refunded

alter table interview_sessions
  add column if not exists attempt_consumed boolean not null default false;

alter table interview_sessions
  add column if not exists attempt_source varchar(20) null
  check (attempt_source in ('included', 'purchased'));

alter table interview_sessions
  add column if not exists requested_question_count int null;

alter table interview_sessions
  add column if not exists requested_duration_seconds int null;

update interview_sessions
set attempt_consumed = true,
    attempt_source = coalesce(attempt_source, 'included')
where context_json is not null
  and started_at is not null;

create index if not exists idx_interview_sessions_user_attempt_consumed
  on interview_sessions (user_id, attempt_consumed, attempt_source, created_at desc);

delete from plan_feature_limits l
using plans p
where l.plan_id = p.id
  and p.code = 'usage_based'
  and not (
    l.feature_code = 'realtime_interview'
    and l.metric in ('session_count', 'session_minutes')
  );

insert into plan_feature_limits (plan_id, feature_code, metric, period, limit_value)
select p.id, 'realtime_interview', 'session_count', 'month', limits.limit_value
from plans p
join (
  values
    ('free', 3),
    ('standard', 10),
    ('pro', 20),
    ('team', 20)
) as limits(plan_code, limit_value) on limits.plan_code = p.code
on conflict (plan_id, feature_code, metric, period) do update
set limit_value = excluded.limit_value,
    updated_at = now();

insert into plan_feature_limits (plan_id, feature_code, metric, period, limit_value)
select p.id, 'realtime_interview', 'session_count', 'purchase', 1
from plans p
where p.code = 'usage_based'
on conflict (plan_id, feature_code, metric, period) do update
set limit_value = excluded.limit_value,
    updated_at = now();

insert into plan_feature_limits (plan_id, feature_code, metric, period, limit_value)
select p.id, 'ai_code_feedback', 'attempt_count', 'month', limits.limit_value
from plans p
join (
  values
    ('free', 2),
    ('standard', 10),
    ('pro', 15),
    ('team', 15)
) as limits(plan_code, limit_value) on limits.plan_code = p.code
on conflict (plan_id, feature_code, metric, period) do update
set limit_value = excluded.limit_value,
    updated_at = now();
