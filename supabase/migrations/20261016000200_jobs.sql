-- Platform job tracking (build-plan.md Part 6 "Platform", Part 8 GET /jobs/{id}).
-- Generic and user-facing: one row per long-running request a user started.
-- Distinct from codeaxis.pass_runs, which records one analyzer's run against
-- one commit; a scan job drives exactly one scan pass_run.

create table codeaxis.jobs (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references codeaxis.projects(id) on delete cascade,
  -- The repo the job acts on, when it acts on one (a scan always does).
  repo_id     uuid references codeaxis.repos(id) on delete cascade,
  kind        text not null check (kind in ('scan')),
  status      text not null check (status in ('queued', 'running', 'succeeded', 'failed')),
  error       text,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now(),
  check (status = 'failed' or error is null)
);

create index jobs_project_id_idx on codeaxis.jobs (project_id);

-- One scan per repo, enforced atomically: two concurrent confirms cannot both
-- start a scan (matches pass_runs' one-run-per-(repo, commit, analyzer)).
create unique index jobs_one_scan_per_repo on codeaxis.jobs (repo_id) where kind = 'scan';

alter table codeaxis.jobs enable row level security;

create policy jobs_select_member on codeaxis.jobs
  for select to authenticated
  using ((select codeaxis.is_project_member(project_id)));

create policy jobs_api_read on codeaxis.jobs
  for select to codeaxis_api using (true);
create policy jobs_worker_all on codeaxis.jobs
  for all to codeaxis_worker using (true) with check (true);

grant select on codeaxis.jobs to authenticated;
grant select, insert, update, delete on codeaxis.jobs to codeaxis_worker;
grant select on codeaxis.jobs to codeaxis_api;
