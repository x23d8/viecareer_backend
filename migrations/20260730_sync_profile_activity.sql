-- Persist parsed CV/JD uploads so My Profile can hydrate them across clients.
alter table uploaded_files
  add column if not exists file_purpose varchar(20) not null default 'cv';

alter table uploaded_files
  add column if not exists content_sha256 varchar(64) null;

update uploaded_files f
set file_purpose = 'jd'
where exists (
  select 1
  from cv_jd_analyses a
  where a.jd_file_id = f.id
);

create unique index if not exists idx_uploaded_files_user_purpose_hash
  on uploaded_files (user_id, file_purpose, content_sha256)
  where content_sha256 is not null;

create index if not exists idx_uploaded_files_user_purpose_created
  on uploaded_files (user_id, file_purpose, created_at desc);
