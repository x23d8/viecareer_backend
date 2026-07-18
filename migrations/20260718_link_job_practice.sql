alter table cv_jd_analyses
  add column if not exists job_id uuid null references jobs(id) on delete set null;

alter table interview_sessions
  add column if not exists job_id uuid null references jobs(id) on delete set null;

create index if not exists idx_cv_jd_analyses_job
  on cv_jd_analyses (job_id, created_at desc)
  where job_id is not null;

create index if not exists idx_interview_sessions_job
  on interview_sessions (job_id, started_at desc)
  where job_id is not null;
