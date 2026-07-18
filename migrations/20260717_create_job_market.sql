create extension if not exists pgcrypto;

create table if not exists jobs (
  id uuid primary key default gen_random_uuid(),
  fingerprint varchar(64) not null unique,
  title varchar(300) not null,
  normalized_title varchar(300) not null,
  company_name varchar(300) not null,
  company_logo_url text null,
  location varchar(300) null,
  workplace_type varchar(20) null check (workplace_type in ('remote', 'hybrid', 'on-site')),
  employment_type varchar(30) null check (employment_type in ('full-time', 'part-time', 'contract', 'internship', 'temporary', 'freelance', 'other')),
  experience_level varchar(30) null,
  experience_years_min smallint null check (experience_years_min is null or experience_years_min between 0 and 50),
  experience_years_max smallint null check (experience_years_max is null or experience_years_max between 0 and 50),
  search_text text not null default '',
  salary_min bigint null check (salary_min is null or salary_min >= 0),
  salary_max bigint null check (salary_max is null or salary_max >= 0),
  salary_currency varchar(10) null,
  salary_text varchar(200) null,
  description text not null default '',
  skills_json jsonb not null default '[]'::jsonb,
  posted_at timestamptz null,
  expires_at timestamptz null,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (salary_min is null or salary_max is null or salary_min <= salary_max)
  ,check (experience_years_min is null or experience_years_max is null or experience_years_min <= experience_years_max)
);

create table if not exists job_sources (
  id uuid primary key default gen_random_uuid(),
  job_id uuid not null references jobs(id) on delete cascade,
  source varchar(30) not null check (source in ('viecareer', 'linkedin', 'topcv', 'itviec')),
  external_id varchar(255) not null,
  source_url text not null,
  apply_url text null,
  content_hash varchar(64) not null,
  metadata_json jsonb not null default '{}'::jsonb,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now(),
  is_active boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (source, external_id),
  unique (source, source_url)
);

create table if not exists user_job_preferences (
  user_id uuid primary key references users(id) on delete cascade,
  desired_titles_json jsonb not null default '[]'::jsonb,
  locations_json jsonb not null default '[]'::jsonb,
  workplace_types_json jsonb not null default '[]'::jsonb,
  employment_types_json jsonb not null default '[]'::jsonb,
  updated_at timestamptz not null default now()
);

create table if not exists job_interactions (
  id bigserial primary key,
  user_id uuid not null references users(id) on delete cascade,
  job_id uuid not null references jobs(id) on delete cascade,
  event_type varchar(20) not null check (event_type in ('impression', 'view', 'save', 'unsave', 'dismiss', 'apply')),
  context varchar(30) null,
  source varchar(30) null,
  created_at timestamptz not null default now()
);

create table if not exists job_search_history (
  id bigserial primary key,
  user_id uuid not null references users(id) on delete cascade,
  query varchar(300) not null,
  location varchar(300) null,
  filters_json jsonb not null default '{}'::jsonb,
  searched_at timestamptz not null default now()
);

create table if not exists job_ingestion_runs (
  id uuid primary key default gen_random_uuid(),
  source varchar(30) not null,
  status varchar(20) not null check (status in ('running', 'succeeded', 'failed', 'skipped')),
  discovered_count int not null default 0,
  upserted_count int not null default 0,
  error_count int not null default 0,
  message text null,
  started_at timestamptz not null default now(),
  finished_at timestamptz null
);

alter table job_applications add column if not exists job_id uuid null references jobs(id) on delete set null;
alter table job_applications add column if not exists source varchar(30) null;
alter table job_applications add column if not exists apply_url text null;

create unique index if not exists idx_job_applications_user_job
  on job_applications (user_id, job_id)
  where job_id is not null;

create index if not exists idx_jobs_active_posted
  on jobs (is_active, posted_at desc nulls last, updated_at desc);

create index if not exists idx_jobs_title_company
  on jobs (normalized_title, company_name);

create index if not exists idx_jobs_skills
  on jobs using gin (skills_json);

create index if not exists idx_job_sources_job_active
  on job_sources (job_id, is_active, last_seen_at desc);

create index if not exists idx_job_interactions_user_time
  on job_interactions (user_id, created_at desc);

create index if not exists idx_job_interactions_user_job_event
  on job_interactions (user_id, job_id, event_type, created_at desc);

create index if not exists idx_job_search_history_user_time
  on job_search_history (user_id, searched_at desc);

create index if not exists idx_job_ingestion_runs_source_time
  on job_ingestion_runs (source, started_at desc);
