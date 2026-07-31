-- Give every account that existed before this release a fresh three-use trial
-- for each Free feature. The data-migration marker makes the grant one-time,
-- including when the timestamp column was created by runtime schema setup
-- before this migration was run.

alter table users
  add column if not exists free_interview_attempts_granted_at timestamptz not null default now();

comment on column users.free_interview_attempts_granted_at is
  'One-time lower bound for the account lifetime Free feature quotas; never reset by client requests.';

create table if not exists app_data_migrations (
  key varchar(120) primary key,
  applied_at timestamptz not null default now()
);

with grant_run as (
  insert into app_data_migrations (key)
  values ('20260731_free_usage_trial_v1')
  on conflict (key) do nothing
  returning applied_at
)
update users u
set free_interview_attempts_granted_at = grant_run.applied_at
from grant_run;
