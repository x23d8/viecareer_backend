create table if not exists session_feedback (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null references interview_sessions(id) on delete cascade,
  user_id uuid not null references users(id) on delete cascade,
  source varchar(20) not null check (source in ('interview', 'coding', 'report')),
  emotion varchar(20) not null check (emotion in ('very_bad', 'bad', 'neutral', 'good', 'great')),
  rating smallint not null check (rating between 1 and 5),
  comment text null,
  tags_json jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (session_id, user_id, source)
);

create index if not exists idx_session_feedback_negative
  on session_feedback (rating, emotion, updated_at desc);

create index if not exists idx_session_feedback_session
  on session_feedback (session_id, updated_at desc);

create index if not exists idx_session_feedback_tags
  on session_feedback using gin (tags_json);
