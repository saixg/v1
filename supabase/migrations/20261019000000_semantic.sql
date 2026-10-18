-- Phase 3, semantic layer (build-plan.md Part 4.D / 4.E, Part 6).
--
-- Adds the five tables the semantic layer needs and the `origin` column that
-- Part 5 requires on every derived entity. Nothing here is populated by a
-- model without validation: `origin = 'inferred'` is how an LLM-written row
-- is marked, and the UI must never present it as parsed fact.

-- --- origin tags (parsed | configured | inferred), Part 4.C / Part 5 --------
-- Default 'parsed': everything written before this migration came from a
-- deterministic analyzer reading source, which is exactly what 'parsed' means.
alter table codeaxis.nodes add column origin text not null default 'parsed'
  check (origin in ('parsed','configured','inferred'));
alter table codeaxis.edges add column origin text not null default 'parsed'
  check (origin in ('parsed','configured','inferred'));

-- --- chunks: AST-aware retrieval units, one per symbol (Part 4.D) ----------
create table codeaxis.chunks (
  id               bigserial primary key,
  repo_id          uuid not null references codeaxis.repos(id) on delete cascade,
  commit_sha       text not null,
  node_id          bigint references codeaxis.nodes(id) on delete cascade,
  path             text not null,
  symbol           text,
  kind             text not null,
  line_start       int not null,
  line_end         int not null,
  content          text not null,
  content_hash     text not null,          -- incremental re-index key, Part 4.D
  token_count      int not null,
  part_index       int not null default 0, -- >0 when one symbol split by size
  part_count       int not null default 1,
  embedding        extensions.vector(1024),       -- voyage-code-3 (Part 13 #2, decided 2026-10-17)
  embedding_model  text,
  embedded_at      timestamptz,
  tsv              tsvector generated always as (
                     to_tsvector('english', coalesce(symbol,'') || ' ' || coalesce(path,'') || ' ' || content)
                   ) stored,
  analyzer         text not null,
  analyzer_version text not null,
  created_at       timestamptz not null default now(),
  unique (repo_id, commit_sha, path, line_start, line_end, part_index)
);

create index chunks_repo_commit_path_idx on codeaxis.chunks (repo_id, commit_sha, path);
create index chunks_node_idx on codeaxis.chunks (node_id) where node_id is not null;
create index chunks_content_hash_idx on codeaxis.chunks (repo_id, content_hash);
create index chunks_tsv_idx on codeaxis.chunks using gin (tsv);
create index chunks_embedding_idx on codeaxis.chunks
  using hnsw (embedding extensions.vector_cosine_ops);

-- --- summaries: hierarchical, symbol -> file -> directory -> component -> repo
-- Always `origin='inferred'`: a summary is model prose. `evidence` holds the
-- chunk/node ids and file+line spans it was written from, so every sentence
-- stays clickable back to source (Part 5).
create table codeaxis.summaries (
  id               bigserial primary key,
  repo_id          uuid not null references codeaxis.repos(id) on delete cascade,
  commit_sha       text not null,
  level            text not null check (level in ('symbol','file','directory','component','repo')),
  node_id          bigint references codeaxis.nodes(id) on delete cascade,
  scope_key        text not null,          -- path, directory, component key, or '' for repo
  summary          text not null,
  origin           text not null default 'inferred' check (origin in ('parsed','configured','inferred')),
  evidence         jsonb not null default '{}',
  model            text,
  analyzer         text not null,
  analyzer_version text not null,
  created_at       timestamptz not null default now(),
  unique (repo_id, commit_sha, level, scope_key)
);

create index summaries_repo_commit_level_idx on codeaxis.summaries (repo_id, commit_sha, level);
create index summaries_node_idx on codeaxis.summaries (node_id) where node_id is not null;

-- --- conventions: naming / structure / test style / lint+CI rules (Part 4.D)
-- `configured` when read from a real config file (eslintrc, ruff.toml, CI
-- workflow); `parsed` when counted from code; `inferred` when the model
-- generalized a pattern.
create table codeaxis.conventions (
  id               bigserial primary key,
  repo_id          uuid not null references codeaxis.repos(id) on delete cascade,
  commit_sha       text not null,
  category         text not null check (category in ('naming','structure','testing','lint','ci','other')),
  rule             text not null,
  origin           text not null check (origin in ('parsed','configured','inferred')),
  confidence       real,
  evidence         jsonb not null default '{}',
  analyzer         text not null,
  analyzer_version text not null,
  created_at       timestamptz not null default now(),
  unique (repo_id, commit_sha, category, rule)
);

create index conventions_repo_commit_idx on codeaxis.conventions (repo_id, commit_sha);

-- --- grounded chat (Part 6, Part 8) ---------------------------------------
create table codeaxis.chat_threads (
  id          uuid primary key default gen_random_uuid(),
  repo_id     uuid not null references codeaxis.repos(id) on delete cascade,
  commit_sha  text not null,
  user_id     text not null,
  title       text,
  created_at  timestamptz not null default now()
);

create index chat_threads_repo_user_idx on codeaxis.chat_threads (repo_id, user_id, created_at desc);

create table codeaxis.chat_messages (
  id           bigserial primary key,
  thread_id    uuid not null references codeaxis.chat_threads(id) on delete cascade,
  role         text not null check (role in ('user','assistant')),
  content      text not null,
  -- [{path, line_start, line_end, node_id?, chunk_id?}] — validated against the
  -- snapshot before the message is persisted; a failed citation check fails the
  -- answer visibly rather than storing an uncited claim.
  citations    jsonb not null default '[]',
  model        text,
  llm_call_id  bigint references codeaxis.llm_calls(id),
  created_at   timestamptz not null default now()
);

create index chat_messages_thread_idx on codeaxis.chat_messages (thread_id, created_at);
