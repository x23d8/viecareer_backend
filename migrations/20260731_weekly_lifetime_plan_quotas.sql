-- Replace ambiguous monthly allowances with lifetime trials and weekly paid quotas.
-- Free usage is intentionally non-resetting. Paid weekly quotas reset Monday
-- 00:00 Asia/Ho_Chi_Minh (the application stores the boundary as UTC).

alter table plan_feature_limits
  drop constraint if exists plan_feature_limits_period_check;

alter table plan_feature_limits
  add constraint plan_feature_limits_period_check
  check (period in ('lifetime', 'day', 'week', 'month', 'session', 'purchase'));

alter table interview_sessions
  add column if not exists attempt_plan_code varchar(50) null;

-- Preserve the plan bucket that originally paid for an included interview.
-- This prevents paid usage from consuming the account's three lifetime Free trials
-- after a subscription expires.
update interview_sessions s
set attempt_plan_code = coalesce(
  case when s.attempt_source = 'purchased' then 'usage_based' end,
  (
    select p.code
    from user_subscriptions us
    join plans p on p.id = us.plan_id
    where us.user_id = s.user_id
      and p.code in ('standard', 'pro', 'team')
      and us.start_at <= s.created_at
      and (us.end_at is null or us.end_at > s.created_at)
    order by
      case p.code when 'team' then 3 when 'pro' then 2 when 'standard' then 1 else 0 end desc,
      us.start_at desc,
      us.id desc
    limit 1
  ),
  'free'
)
where s.attempt_consumed = true
  and s.attempt_plan_code is null;

delete from plan_feature_limits l
using plans p
where p.id = l.plan_id
  and p.code in ('free', 'standard', 'pro', 'team', 'usage_based')
  and l.feature_code in (
    'cv_jd_analysis',
    'realtime_interview',
    'chatbot',
    'coding_plan',
    'ai_code_feedback'
  );

insert into plan_feature_limits (plan_id, feature_code, metric, period, limit_value)
select p.id, limits.feature_code, limits.metric, limits.period, limits.limit_value
from plans p
join (
  values
    ('free', 'cv_jd_analysis', 'count', 'lifetime', 3),
    ('free', 'realtime_interview', 'session_count', 'lifetime', 3),
    ('free', 'realtime_interview', 'session_minutes', 'session', 10),
    ('free', 'chatbot', 'message_count', 'lifetime', 3),
    ('free', 'coding_plan', 'generation_count', 'lifetime', 3),
    ('free', 'ai_code_feedback', 'attempt_count', 'lifetime', 3),
    ('standard', 'cv_jd_analysis', 'count', 'week', 15),
    ('standard', 'realtime_interview', 'session_count', 'week', 10),
    ('standard', 'realtime_interview', 'session_minutes', 'session', 25),
    ('standard', 'chatbot', 'message_count', 'day', 50),
    ('standard', 'coding_plan', 'generation_count', 'week', 10),
    ('standard', 'ai_code_feedback', 'attempt_count', 'week', 10),
    ('pro', 'cv_jd_analysis', 'count', 'week', 30),
    ('pro', 'realtime_interview', 'session_count', 'week', 20),
    ('pro', 'realtime_interview', 'session_minutes', 'session', 45),
    ('pro', 'chatbot', 'message_count', 'day', 150),
    ('pro', 'coding_plan', 'generation_count', 'week', 15),
    ('pro', 'ai_code_feedback', 'attempt_count', 'week', 15),
    ('team', 'cv_jd_analysis', 'count', 'week', 30),
    ('team', 'realtime_interview', 'session_count', 'week', 20),
    ('team', 'realtime_interview', 'session_minutes', 'session', 45),
    ('team', 'chatbot', 'message_count', 'day', 150),
    ('team', 'coding_plan', 'generation_count', 'week', 15),
    ('team', 'ai_code_feedback', 'attempt_count', 'week', 15),
    ('usage_based', 'realtime_interview', 'session_count', 'purchase', 1),
    ('usage_based', 'realtime_interview', 'session_minutes', 'session', 15)
) as limits(plan_code, feature_code, metric, period, limit_value)
  on p.code = limits.plan_code;

create index if not exists idx_interview_sessions_user_attempt_plan
  on interview_sessions (user_id, attempt_plan_code, attempt_consumed, created_at desc);
