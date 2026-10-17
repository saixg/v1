-- Repo Model: schema, roles, tables. See docs/04-graph-schema.md.

create extension if not exists vector with schema extensions;

create schema if not exists codeaxis;

create table codeaxis.repos (
  id             uuid primary key default gen_random_uuid(),
  remote_url     text,
  local_path     text,
  default_branch text,
  created_at     timestamptz not null default now()
);

create table codeaxis.pass_runs (
  id               bigserial primary key,
  repo_id          uuid not null references codeaxis.repos(id),
  commit_sha       text not null,
  analyzer         text not null,
  analyzer_version text not null,
  status           text not null check (status in ('running','succeeded','failed')),
  started_at       timestamptz not null default now(),
  finished_at      timestamptz,
  error            text,
  unique (repo_id, commit_sha, analyzer, analyzer_version)
);

create table codeaxis.nodes (
  id               bigserial primary key,
  repo_id          uuid not null references codeaxis.repos(id),
  commit_sha       text not null,
  kind             text not null,
  name             text not null,
  path             text,
  line_start       int,
  line_end         int,
  col_start        int,
  col_end          int,
  attrs            jsonb not null default '{}',
  embedding        extensions.vector(768),
  analyzer         text not null,
  analyzer_version text not null,
  status           text not null check (status in ('verified','inferred','unknown','unanalyzed')),
  source_file      text,
  confidence       real,
  reason           text,
  check (status <> 'unknown' or reason is not null),
  unique nulls not distinct (repo_id, commit_sha, kind, path, name, line_start)
);

create table codeaxis.edges (
  id               bigserial primary key,
  repo_id          uuid not null references codeaxis.repos(id),
  commit_sha       text not null,
  kind             text not null,
  src_id           bigint not null references codeaxis.nodes(id),
  dst_id           bigint references codeaxis.nodes(id),
  analyzer         text not null,
  analyzer_version text not null,
  status           text not null check (status in ('verified','inferred','unknown','unanalyzed')),
  source_file      text,
  line_start       int,
  line_end         int,
  col_start        int,
  col_end          int,
  confidence       real,
  reason           text,
  check (status <> 'unknown' or reason is not null)
);

create index on codeaxis.edges (repo_id, commit_sha, src_id, kind, status);
create index on codeaxis.edges (repo_id, commit_sha, dst_id, kind, status);
create index on codeaxis.nodes using ivfflat (embedding vector_cosine_ops);

create table codeaxis.coverage (
  repo_id       uuid not null references codeaxis.repos(id),
  commit_sha    text not null,
  area          text not null,
  metric        text not null,
  resolved      int not null,
  total         int not null,
  blockers      jsonb not null default '{}',
  primary key (repo_id, commit_sha, area, metric)
);

-- codeaxis_worker / codeaxis_api roles are created and granted by
-- scripts/setup_db_roles.py, not here — role creation needs a per-environment
-- password and this file is a versioned migration, not a secret.
