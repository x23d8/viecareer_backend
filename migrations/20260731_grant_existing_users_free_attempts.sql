-- Give every account that existed before this release a fresh three-attempt
-- interview trial. PostgreSQL fills existing rows with the timestamp captured
-- when this column is first added. IF NOT EXISTS makes the grant one-time and
-- prevents deploys, restarts, or API requests from granting it again.

alter table users
  add column if not exists free_interview_attempts_granted_at timestamptz not null default now();

comment on column users.free_interview_attempts_granted_at is
  'One-time lower bound for the account lifetime three-attempt interview trial; never reset by client requests.';
