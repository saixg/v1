# 04 — Repo Model Schema

The graph is two tables plus provenance. Everything in the product is a query over them.

## Storage: Supabase Postgres

All Repo Model tables live in a dedicated `codeaxis` schema, not `public`. `public` is left for Supabase's own auth/storage tables and is never used by this product. `codeaxis` is **not** in the Data API's exposed schema list, so PostgREST never serves graph rows directly — `services/api` is the only HTTP surface, enforcing the one-way rule from `03-architecture.md` at the infrastructure level, not just by convention.

Two Postgres roles read and write this schema:

| Role | Grants | Used by |
| --- | --- | --- |
| `codeaxis_worker` | `usage` on `codeaxis`, full read/write on its tables | `services/worker` — the only writer, per the one-way rule |
| `codeaxis_api` | `usage` on `codeaxis`, `select` only | `services/api` — every read query |

`DATABASE_URL` (worker) and `API_DATABASE_URL` (api) in `.env` hold each role's connection string over the session pooler.

## Node types

| Type | Key attributes |
| --- | --- |
| `repository` | remote URL, commit SHA, default branch, analyzed_at, language tiers present |
| `directory` | path, parent |
| `file` | path, language, loc, content_hash, tier, analyzed flag |
| `symbol` | kind (function, class, method, variable, type), name, file, line range, signature, exported flag, SCIP symbol string |
| `route` | method, path pattern, handler symbol, middleware chain, framework |
| `model` | table or collection name, fields, relations, ORM |
| `test` | symbol, framework, targets |
| `external_service` | kind (http, llm, queue, storage, db), endpoint or SDK, call sites |
| `config_value` | key, source file, default, read sites |
| `cluster` | purpose label, member symbols, commit SHA computed at |

## Edge types

| Edge | From → To | Typical analyzer |
| --- | --- | --- |
| `imports` | file → file | tree-sitter + resolver |
| `defines` | file \| symbol → symbol | SCIP |
| `calls` | symbol → symbol | SCIP; tree-sitter fallback marked `inferred` |
| `implements` / `extends` | symbol → symbol | SCIP |
| `routes_to` | route → symbol | framework extractor |
| `reads` / `writes` | symbol → model \| config_value | framework extractor, CodeQL |
| `flows_to` | symbol → symbol | CodeQL taint |
| `tests` | test → symbol | naming + import analysis |
| `calls_external` | symbol → external_service | framework extractor |
| `belongs_to` | symbol → cluster | embeddings |
| `changed_with` | file → file | git co-change |
| `contains` | directory → file, file → symbol | tree-sitter |

`contains` makes containment a queryable, cited edge instead of something inferred by matching `path` prefixes — the Explorer's file tree is a graph walk, not a string operation.

## Tables

```sql
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
  embedding        vector(768),
  -- provenance, see 01-evidence-model.md — a node is a claim too, not just an edge
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
  -- provenance, see 01-evidence-model.md
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
  area          text not null,     -- module path or cluster label
  metric        text not null,     -- symbols_resolved | call_edges_resolved | ...
  resolved      int not null,
  total         int not null,
  blockers      jsonb not null default '{}',  -- { "dynamic_import": 12, ... }
  primary key (repo_id, commit_sha, area, metric)
);
```

`unique nulls not distinct` on `nodes` matters because plain `unique` treats every `null` as distinct — a `repository` or `cluster` node with no `line_start` would never collide-check against a duplicate insert of itself.

`col_start`/`col_end` exist alongside the line range on both tables because a line is not a precise enough citation target on a long line (e.g. two calls on one line) — the Component Inspector's "show me the code" needs to highlight the exact span.

An `unknown` edge has `dst_id = null` and carries its `reason`. This is what makes "the story stops here, because of a dynamic import" renderable.

### Blob store

```sql
create table codeaxis.blobs (
  repo_id       uuid not null references codeaxis.repos(id),
  commit_sha    text not null,
  path          text not null,     -- same repo-relative path as the file node
  content       text not null,
  content_hash  text not null,     -- sha256, equals the file node's attrs.content_hash
  primary key (repo_id, commit_sha, path)
);
```

This is the "blob store for pinned source" from `docs/03-architecture.md`: the raw text of each scanned file at the analyzed commit, so a node's source span can be shown without touching the checkout (which is deleted after a GitHub/ZIP scan) and always matches the commit the graph was built from. The scan pass writes a row only for text files under the size cap that it actually read; oversized, unreadable and binary files get none. A missing row means "source not available" and is shown as such — never as an empty file. Like every other table, the worker role writes it and the API role only reads it.

## Provenance example

```csv
edge_id,analyzer,analyzer_version,status,source_file,line_start,line_end,commit_sha,confidence,reason
8841,scip-typescript,0.9.3,verified,src/chat/ChatService.ts,118,118,a3f91c2,,
8842,framework:fastapi,1.2.0,verified,app/routes/auth.py,42,49,a3f91c2,,
8843,llm,claude-opus-5,inferred,app/services/doc.py,,,a3f91c2,0.61,
8844,scip-python,0.6.1,unknown,app/jobs/registry.py,88,88,a3f91c2,,string_dispatch
```

An `inferred` edge with an empty line range is renderable but **never counts toward coverage** and is excluded whenever the user filters to verified-only.

## Two design calls worth defending

**One graph per commit, not a mutable graph.** Analysis is keyed by `(repo_id, commit_sha, analyzer_version)`. This makes caching trivial, results reproducible, and re-analysis a diff rather than a migration.

**Clusters are nodes, not labels.** Making the Purpose Atlas a first-class node type lets a cluster be cited, inspected, and compared across commits, instead of being a transient UI grouping.

## Query patterns the schema must serve fast

| Pattern | Used by |
| --- | --- |
| Neighbourhood of a node, depth N, filtered by edge kind and status | System Map, Inspector |
| Reverse reachability from a symbol, bounded depth | Impact Analysis |
| Forward walk from a route handler, stopping at unknown or external | Execution Stories |
| Paths where `kind = 'flows_to'` between two node sets | Data Journeys |
| Vector + BM25 hybrid over symbol names, docstrings, bodies | Ask, Purpose Atlas |
| Aggregate edge counts between module subtrees | System Map at system zoom |
| Coverage rollup by area | Understanding Gap |
