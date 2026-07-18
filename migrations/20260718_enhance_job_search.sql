create extension if not exists unaccent;
create extension if not exists pg_trgm;

alter table jobs add column if not exists search_text text not null default '';
alter table jobs add column if not exists experience_years_min smallint null;
alter table jobs add column if not exists experience_years_max smallint null;

alter table jobs drop constraint if exists jobs_experience_years_min_check;
alter table jobs add constraint jobs_experience_years_min_check
  check (experience_years_min is null or experience_years_min between 0 and 50);

alter table jobs drop constraint if exists jobs_experience_years_max_check;
alter table jobs add constraint jobs_experience_years_max_check
  check (experience_years_max is null or experience_years_max between 0 and 50);

alter table jobs drop constraint if exists jobs_experience_years_range_check;
alter table jobs add constraint jobs_experience_years_range_check
  check (
    experience_years_min is null
    or experience_years_max is null
    or experience_years_min <= experience_years_max
  );

update jobs
set search_text = trim(
  regexp_replace(
    lower(
      unaccent(
        concat_ws(
          ' ',
          title,
          normalized_title,
          company_name,
          location,
          description,
          skills_json::text
        )
      )
    ),
    '[^a-z0-9+#.]+',
    ' ',
    'g'
  )
)
where search_text = '';

update jobs
set experience_years_min = case experience_level
  when 'internship' then 0
  when 'fresher' then 0
  when 'junior' then 1
  when 'middle' then 2
  when 'senior' then 5
  when 'lead' then 5
  when 'manager' then 5
  else null
end
where experience_years_min is null;

create index if not exists idx_jobs_search_fts
  on jobs using gin (to_tsvector('simple', search_text));

create index if not exists idx_jobs_title_trgm
  on jobs using gin (normalized_title gin_trgm_ops);

create index if not exists idx_jobs_company_trgm
  on jobs using gin (lower(company_name) gin_trgm_ops);

create index if not exists idx_jobs_filter_experience
  on jobs (experience_level, experience_years_min, workplace_type, employment_type);

create index if not exists idx_jobs_location_trgm
  on jobs using gin (lower(coalesce(location, '')) gin_trgm_ops);
