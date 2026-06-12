-- VieCareer schema for Supabase (PostgreSQL)
-- Generated from DB_TABLES_PROPOSAL.md

create extension if not exists pgcrypto;

create table if not exists roles (
  id bigserial primary key,
  code varchar(50) not null unique,
  name varchar(100) not null,
  created_at timestamptz not null default now()
);

create table if not exists plans (
  id bigserial primary key,
  code varchar(50) not null unique,
  name varchar(100) not null,
  monthly_price numeric(12,2) not null,
  quota_total int not null,
  rate_limit_per_minute int not null,
  created_at timestamptz not null default now()
);

create table if not exists users (
  id uuid primary key default gen_random_uuid(),
  external_auth_id varchar(255) null,
  auth_provider varchar(50) null default 'clerk',
  email varchar(255) not null unique,
  password_hash text null,
  full_name varchar(255) not null,
  role_id bigint not null references roles(id),
  status varchar(20) not null check (status in ('active', 'pending', 'suspended')),
  avatar_url text null,
  last_active_at timestamptz null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table users add column if not exists external_auth_id varchar(255);
alter table users add column if not exists auth_provider varchar(50) null default 'clerk';

create unique index if not exists idx_users_external_auth_id
  on users (external_auth_id)
  where external_auth_id is not null;

create table if not exists user_profiles (
  id bigserial primary key,
  user_id uuid not null unique references users(id) on delete cascade,
  birth_year int null,
  university varchar(255) null,
  target_position varchar(255) null,
  bio text null,
  location varchar(255) null,
  github_url text null,
  linkedin_url text null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists user_subscriptions (
  id bigserial primary key,
  user_id uuid not null references users(id) on delete cascade,
  plan_id bigint not null references plans(id),
  status varchar(20) not null check (status in ('active', 'expired', 'cancelled')),
  start_at timestamptz not null,
  end_at timestamptz null,
  auto_renew boolean not null default false,
  created_at timestamptz not null default now()
);

create table if not exists auth_sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  refresh_token_hash text not null unique,
  user_agent text null,
  ip_address varchar(64) null,
  expires_at timestamptz not null,
  revoked_at timestamptz null,
  created_at timestamptz not null default now()
);

create table if not exists uploaded_files (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  file_name varchar(255) not null,
  file_type varchar(20) not null check (file_type in ('pdf', 'docx', 'txt')),
  mime_type varchar(100) not null,
  file_size_bytes bigint not null,
  storage_url text not null,
  extracted_text text null,
  created_at timestamptz not null default now()
);

create table if not exists cv_jd_analyses (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  cv_file_id uuid null references uploaded_files(id) on delete set null,
  jd_file_id uuid null references uploaded_files(id) on delete set null,
  full_name varchar(255) null,
  target_position varchar(255) null,
  education_level varchar(255) null,
  tech_stack_json jsonb not null default '[]'::jsonb,
  must_have_skills_json jsonb not null default '[]'::jsonb,
  key_responsibilities_json jsonb not null default '[]'::jsonb,
  s_skill int not null check (s_skill between 0 and 100),
  s_exp int not null check (s_exp between 0 and 100),
  s_edu int not null check (s_edu between 0 and 100),
  s_sem int not null check (s_sem between 0 and 100),
  ats_score int null check (ats_score between 0 and 100),
  jri_baseline int null check (jri_baseline between 0 and 100),
  d1_baseline int null check (d1_baseline between 0 and 100),
  d2_baseline int null check (d2_baseline between 0 and 100),
  d3_baseline int null check (d3_baseline between 0 and 100),
  d4_baseline int null check (d4_baseline between 0 and 100),
  d5_baseline int null check (d5_baseline between 0 and 100),
  llm_raw_json jsonb null,
  created_at timestamptz not null default now()
);

alter table cv_jd_analyses add column if not exists ats_score int null;
alter table cv_jd_analyses add column if not exists jri_baseline int null;
alter table cv_jd_analyses add column if not exists d1_baseline int null;
alter table cv_jd_analyses add column if not exists d2_baseline int null;
alter table cv_jd_analyses add column if not exists d3_baseline int null;
alter table cv_jd_analyses add column if not exists d4_baseline int null;
alter table cv_jd_analyses add column if not exists d5_baseline int null;

create table if not exists cv_jd_gap_items (
  id bigserial primary key,
  analysis_id uuid not null references cv_jd_analyses(id) on delete cascade,
  category varchar(100) not null,
  skill varchar(255) not null,
  status varchar(20) not null check (status in ('matched', 'partial', 'missing')),
  cv_evidence text null,
  jd_requirement text null,
  score int null check (score between 0 and 100)
);

create table if not exists cv_jd_readiness_signals (
  id bigserial primary key,
  analysis_id uuid not null references cv_jd_analyses(id) on delete cascade,
  source_phase varchar(20) not null default 'cv_jd' check (source_phase in ('cv_jd', 'interview', 'final')),
  signal_key varchar(100) not null,
  signal_label varchar(150) not null,
  dimension varchar(100) not null,
  status varchar(20) null check (status in ('strong', 'moderate', 'weak', 'missing')),
  score int null check (score between 0 and 100),
  cv_evidence text null,
  jd_requirement text null,
  created_at timestamptz not null default now()
);

create table if not exists interview_sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  cv_jd_analysis_id uuid null references cv_jd_analyses(id) on delete set null,
  position varchar(255) not null,
  company varchar(255) null,
  session_type varchar(20) not null check (session_type in ('technical', 'behavioral', 'mixed', 'coding')),
  status varchar(20) not null check (status in ('scheduled', 'in_progress', 'completed', 'cancelled')),
  scheduled_at timestamptz null,
  started_at timestamptz null,
  ended_at timestamptz null,
  duration_seconds int null,
  overall_score int null check (overall_score between 0 and 100),
  candidate_json jsonb null,
  job_description text null,
  language varchar(10) null,
  voice varchar(30) null,
  blueprint_json jsonb null,
  context_json jsonb null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table interview_sessions add column if not exists cv_jd_analysis_id uuid null references cv_jd_analyses(id) on delete set null;
alter table interview_sessions add column if not exists candidate_json jsonb null;
alter table interview_sessions add column if not exists job_description text null;
alter table interview_sessions add column if not exists language varchar(10) null;
alter table interview_sessions add column if not exists voice varchar(30) null;
alter table interview_sessions add column if not exists blueprint_json jsonb null;
alter table interview_sessions add column if not exists context_json jsonb null;

create table if not exists interview_qa_pairs (
  id bigserial primary key,
  session_id uuid not null references interview_sessions(id) on delete cascade,
  question_order int not null,
  question text not null,
  answer text null,
  answer_timestamp_sec int null,
  score int null check (score between 0 and 100),
  strengths_json jsonb null,
  weaknesses_json jsonb null,
  optimal_answer text null,
  transcript_events_json jsonb null,
  created_at timestamptz not null default now(),
  unique (session_id, question_order)
);

alter table interview_qa_pairs add column if not exists transcript_events_json jsonb null;

create table if not exists interview_scores (
  id bigserial primary key,
  session_id uuid not null unique references interview_sessions(id) on delete cascade,
  d1_technical int not null check (d1_technical between 0 and 100),
  d2_communication int not null check (d2_communication between 0 and 100),
  d3_problem_solving int not null check (d3_problem_solving between 0 and 100),
  d4_attitude int not null check (d4_attitude between 0 and 100),
  d5_cultural_fit int not null check (d5_cultural_fit between 0 and 100),
  phase2_signals_json jsonb null,
  final_dimensions_json jsonb null,
  created_at timestamptz not null default now()
);

alter table interview_scores add column if not exists phase2_signals_json jsonb null;
alter table interview_scores add column if not exists final_dimensions_json jsonb null;

create table if not exists interview_transcript_events (
  id bigserial primary key,
  session_id uuid null references interview_sessions(id) on delete cascade,
  event_order int not null,
  role varchar(20) not null check (role in ('user', 'assistant', 'system')),
  text text not null,
  source varchar(50) not null default 'gpt-realtime',
  timestamp_sec int null,
  question_order int null,
  created_at timestamptz not null default now()
);

create table if not exists interview_recordings (
  id uuid primary key default gen_random_uuid(),
  session_id uuid not null unique references interview_sessions(id) on delete cascade,
  storage_url text not null,
  file_size_bytes bigint null,
  duration_seconds int null,
  transcript_text text null,
  created_at timestamptz not null default now()
);

create table if not exists chat_conversations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  channel varchar(30) not null check (channel in ('generic', 'viebot', 'interview')),
  title varchar(255) null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists chat_messages (
  id bigserial primary key,
  conversation_id uuid not null references chat_conversations(id) on delete cascade,
  role varchar(20) not null check (role in ('user', 'assistant', 'system')),
  content text not null,
  token_count int null,
  created_at timestamptz not null default now()
);

create table if not exists user_skills (
  id bigserial primary key,
  user_id uuid not null references users(id) on delete cascade,
  skill_name varchar(100) not null,
  progress_percent int null check (progress_percent between 0 and 100),
  stage varchar(20) null check (stage in ('beginner', 'intermediate', 'advanced')),
  trend varchar(20) null check (trend in ('up', 'stable', 'down')),
  updated_at timestamptz not null default now(),
  unique (user_id, skill_name)
);

create table if not exists job_applications (
  id bigserial primary key,
  user_id uuid not null references users(id) on delete cascade,
  job_title varchar(255) not null,
  company varchar(255) not null,
  status varchar(30) not null check (status in ('applied', 'interviewing', 'rejected', 'offered')),
  applied_at timestamptz null,
  updated_at timestamptz not null default now()
);

create table if not exists learning_roadmaps (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references users(id) on delete cascade,
  target_role varchar(255) not null,
  match_percent int null check (match_percent between 0 and 100),
  status varchar(20) not null check (status in ('active', 'archived')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists roadmap_tasks (
  id bigserial primary key,
  roadmap_id uuid not null references learning_roadmaps(id) on delete cascade,
  phase_name varchar(255) null,
  task_title varchar(255) not null,
  task_type varchar(50) null check (task_type in ('technical', 'performance', 'behavioral')),
  duration_minutes int null,
  status varchar(20) not null check (status in ('planned', 'in_progress', 'completed', 'locked')),
  display_order int not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists activity_logs (
  id bigserial primary key,
  actor_user_id uuid null references users(id) on delete set null,
  target_user_id uuid null references users(id) on delete set null,
  log_type varchar(20) not null check (log_type in ('auth', 'session', 'payment', 'account', 'system')),
  action varchar(255) not null,
  detail text null,
  ip_address varchar(64) null,
  meta_json jsonb null,
  created_at timestamptz not null default now()
);

-- Recommended indexes
create index if not exists idx_interview_sessions_user_created
  on interview_sessions (user_id, created_at desc);

create index if not exists idx_interview_sessions_status_scheduled
  on interview_sessions (status, scheduled_at);

create index if not exists idx_chat_messages_conversation_created
  on chat_messages (conversation_id, created_at);

create index if not exists idx_activity_logs_type_created
  on activity_logs (log_type, created_at desc);

create index if not exists idx_cv_jd_analyses_user_created
  on cv_jd_analyses (user_id, created_at desc);

create index if not exists idx_uploaded_files_user_created
  on uploaded_files (user_id, created_at desc);

-- Optional seed data
insert into roles (code, name)
values ('admin', 'Administrator'), ('user', 'User')
on conflict (code) do nothing;

insert into plans (code, name, monthly_price, quota_total, rate_limit_per_minute)
values
  ('free', 'Free', 0, 10, 20),
  ('pro', 'Pro', 299000, 50, 50),
  ('team', 'Team', 799000, 200, 100)
on conflict (code) do nothing;
