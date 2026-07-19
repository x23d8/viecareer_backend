-- Replace legacy synthetic interview company labels with recognizable technology employers.
-- This migration is intentionally limited to exact placeholder values and is idempotent.

update interview_sessions as session
set
  company = job.company_name,
  updated_at = now()
from jobs as job
where job.id = session.job_id
  and nullif(trim(coalesce(session.company, '')), '') is null
  and nullif(trim(job.company_name), '') is not null;

update interview_sessions
set
  company = case company
    when 'AI Startup' then 'FPT Software'
    when 'SaaS Platform' then 'Viettel Digital'
    when 'E-commerce Lab' then 'Grab'
    when 'Fintech Growth' then 'MoMo'
    when 'VieCareer Demo' then 'NAB Innovation Centre Vietnam'
    when 'VieCareer Synthetic QA' then 'VNG Corporation'
    else company
  end,
  updated_at = now()
where company in (
  'AI Startup',
  'SaaS Platform',
  'E-commerce Lab',
  'Fintech Growth',
  'VieCareer Demo',
  'VieCareer Synthetic QA'
);

update job_applications
set
  company = case company
    when 'AI Startup' then 'FPT Software'
    when 'SaaS Platform' then 'Viettel Digital'
    when 'E-commerce Lab' then 'Grab'
    when 'Fintech Growth' then 'MoMo'
    when 'VieCareer Demo' then 'NAB Innovation Centre Vietnam'
    when 'VieCareer Synthetic QA' then 'VNG Corporation'
    else company
  end,
  updated_at = now()
where company in (
  'AI Startup',
  'SaaS Platform',
  'E-commerce Lab',
  'Fintech Growth',
  'VieCareer Demo',
  'VieCareer Synthetic QA'
);
