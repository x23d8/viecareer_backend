create table if not exists user_reports (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  kind varchar(20) not null check (kind in ('feedback', 'bug')),
  feature varchar(80) not null,
  path varchar(500) null,
  comment text not null,
  status varchar(20) not null default 'open' check (status in ('open', 'reviewing', 'resolved')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_user_reports_kind_created
  on user_reports (kind, created_at desc);

create index if not exists idx_user_reports_user_created
  on user_reports (user_id, created_at desc);
