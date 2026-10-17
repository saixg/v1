-- Model usage/cost ledger (build-plan.md Part 6 "Platform": llm_calls; Part 3
-- "usage/cost tracking per call"; Part 11 "model cost and failures observable").
--
-- One row per provider attempt made by app.llm.ModelService: a structured call
-- that fails validation and retries writes two rows. Failed attempts (refusal,
-- truncation, transport error) are recorded too.
--
-- Usage only, never content: no prompt or completion text is stored, because
-- repository text is untrusted and may carry secrets (Part 0.3).
--
-- Attribution columns are nullable: nothing calls ModelService yet (Phase 3+),
-- and some calls (e.g. eval harness runs) will never belong to a project.
-- `on delete set null` keeps the cost history when a project/repo/job is
-- deleted; the row holds no repository content, so nothing leaks by keeping it.

create table codeaxis.llm_calls (
  id                           bigserial primary key,
  created_at                   timestamptz not null default now(),
  role                         text not null
                                 check (role in ('bulk','synth','design','vision','embed','rerank')),
  provider                     text not null,
  model                        text not null,          -- requested by the routing table
  served_model                 text,                   -- reported by the provider
  operation                    text not null
                                 check (operation in ('structured','text','embed','rerank')),
  attempt                      smallint not null default 1 check (attempt >= 1),
  status                       text not null
                                 check (status in ('succeeded','validation_failed','refused','truncated','error')),
  input_tokens                 integer not null default 0 check (input_tokens >= 0),
  output_tokens                integer not null default 0 check (output_tokens >= 0),
  cache_read_input_tokens      integer not null default 0 check (cache_read_input_tokens >= 0),
  cache_creation_input_tokens  integer not null default 0 check (cache_creation_input_tokens >= 0),
  cost_usd                     numeric(12, 6) check (cost_usd >= 0),  -- null = model not priced
  latency_ms                   integer not null check (latency_ms >= 0),
  stop_reason                  text,
  request_id                   text,
  schema_name                  text,
  error                        text,
  project_id                   uuid references codeaxis.projects(id) on delete set null,
  repo_id                      uuid references codeaxis.repos(id) on delete set null,
  job_id                       uuid references codeaxis.jobs(id) on delete set null
);

-- Cost/failure dashboards and per-project budget caps read by time window.
create index llm_calls_created_at_idx on codeaxis.llm_calls (created_at);
create index llm_calls_project_id_created_at_idx
  on codeaxis.llm_calls (project_id, created_at) where project_id is not null;
-- FK lookups for on-delete-set-null.
create index llm_calls_repo_id_idx on codeaxis.llm_calls (repo_id) where repo_id is not null;
create index llm_calls_job_id_idx on codeaxis.llm_calls (job_id) where job_id is not null;

-- RLS mirrors codeaxis.jobs: members may read their project's usage directly;
-- the api role reads; the worker role appends. Append-only: no update/delete
-- policy or grant for the worker, so a usage row cannot be rewritten.
alter table codeaxis.llm_calls enable row level security;

create policy llm_calls_select_member on codeaxis.llm_calls
  for select to authenticated
  using (project_id is not null and (select codeaxis.is_project_member(project_id)));

create policy llm_calls_api_read on codeaxis.llm_calls
  for select to codeaxis_api using (true);
create policy llm_calls_worker_read on codeaxis.llm_calls
  for select to codeaxis_worker using (true);
create policy llm_calls_worker_insert on codeaxis.llm_calls
  for insert to codeaxis_worker with check (true);

grant select on codeaxis.llm_calls to authenticated;
grant select on codeaxis.llm_calls to codeaxis_api;
grant select, insert on codeaxis.llm_calls to codeaxis_worker;
grant usage, select on sequence codeaxis.llm_calls_id_seq to codeaxis_worker;
