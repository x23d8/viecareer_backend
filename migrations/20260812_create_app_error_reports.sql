create table if not exists app_error_reports (
  id uuid primary key default gen_random_uuid(),
  user_id uuid null references users(id) on delete set null,
  external_auth_id varchar(255) not null,
  email varchar(320) null,
  category varchar(30) not null check (category in ('authentication', 'network', 'api', 'validation', 'database', 'realtime', 'media', 'ui', 'unexpected')),
  severity varchar(20) not null check (severity in ('warning', 'error', 'fatal')),
  platform varchar(20) not null check (platform in ('android', 'web')),
  location varchar(200) not null,
  route varchar(500) null,
  error_type varchar(160) not null,
  message text not null,
  technical_detail text null,
  stack_trace text null,
  http_status smallint null check (http_status between 100 and 599),
  request_id varchar(160) null,
  app_version varchar(80) null,
  occurred_at_epoch_ms bigint null,
  context_json jsonb not null default '{}'::jsonb,
  fingerprint varchar(64) not null,
  occurrence_count integer not null default 1,
  status varchar(20) not null default 'open' check (status in ('open', 'reviewing', 'resolved')),
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (external_auth_id, fingerprint)
);

create index if not exists idx_app_error_reports_last_seen
  on app_error_reports (last_seen_at desc);

create index if not exists idx_app_error_reports_status
  on app_error_reports (status, severity, last_seen_at desc);

create index if not exists idx_app_error_reports_user
  on app_error_reports (external_auth_id, last_seen_at desc);

create index if not exists idx_app_error_reports_category
  on app_error_reports (category, platform, last_seen_at desc);
