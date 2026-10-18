-- Phase 4, Agent Context Engine (build-plan.md Part 4.F, Part 6 "Context
-- engine", Part 5 evidence model).
--
-- This migration is the data-model foundation only: the tables that task
-- intake, requirement/constraint extraction, context-package assembly, the
-- REST routes and the read-only MCP server all write to and read from. No
-- extraction logic, no routes, no exports live here.
--
-- Three rules shape every table below and are worth stating once rather than
-- repeating per column:
--
-- 1. **The snapshot key in this repository is `(repo_id, commit_sha)`, not a
--    `snapshot_id` uuid.** Part 6's key-column list writes
--    `context_items(..., snapshot_id, ...)`, but there is no
--    `repository_snapshots` table in this codebase: every existing derived
--    table (`codeaxis.nodes`, `edges`, `chunks`, `summaries`, `conventions`,
--    `coverage`, `blobs`, `chat_threads`) is keyed by the pair, and the whole
--    read layer filters on `repo_id = %s and commit_sha = %s`. Inventing a
--    surrogate snapshot id here would fork the convention and make Part 4.F's
--    grounding unable to join to the graph it is grounded in. So the pair is
--    carried, denormalized, on every derived row — including rows that also
--    hang off `task_id`, whose task already names a snapshot. The duplication
--    is deliberate: snapshot scoping must be expressible without a join, so
--    that a query for one commit can never return rows analyzed at another.
--
-- 2. **Part 5 provenance on every derived record**: `origin`
--    (`parsed|configured|inferred`), the snapshot pair, `analyzer` +
--    `analyzer_version`, and `evidence jsonb` holding the file+line spans or
--    document refs the record was written from. Model-written rows are
--    `origin = 'inferred'` and must never be presented as parsed fact.
--
-- 3. **`unresolved` / `ambiguous` / `open` are valid stored states** (Part
--    0.3). A requirement the extractor could not pin down is stored as
--    ambiguous with the question attached; it is never guessed into a
--    confident statement, and never silently dropped.
--
-- Untrusted text: `tasks.intent`, `requirements.statement`,
-- `constraints.statement` and `context_items.content` can contain repository
-- text, user prose, or text extracted from an uploaded document. All of it is
-- data. Nothing downstream may treat it as instructions (Part 0.3, Part 9).
--
-- RLS and the missing `authenticated` policies: the three policy shapes used
-- by the platform migrations were `*_select_member` (role `authenticated`),
-- `*_api_read` (role `codeaxis_api`) and `*_worker_all` (role
-- `codeaxis_worker`). Only the last two are created here. The
-- `authenticated` shape is structurally dead since
-- `20261017000000_clerk_auth.sql`: that migration dropped
-- `codeaxis.is_project_member(uuid)` (the function every such policy called),
-- revoked `usage on schema codeaxis` from `authenticated`, and dropped the
-- existing member policies, because Clerk JWTs never populate
-- `request.jwt.claims` and the browser has no path to Postgres in this app.
-- Re-adding that shape would not compile without resurrecting the function,
-- and would be decoration over an access path that cannot fire. The operative
-- boundary is the same as everywhere else: app-layer project-membership checks
-- in FastAPI (`app/auth.py`) plus the role policies below.

-- --------------------------------------------------------------- tasks
-- One developer task: "what someone wants built or changed", grounded against
-- exactly one snapshot of one repo. `project_id` is carried alongside
-- `repo_id` (rather than reached through it) because authorization is decided
-- per project and the read layer must be able to scope a list by project
-- without a join through repos.

create table codeaxis.tasks (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references codeaxis.projects(id) on delete cascade,
  repo_id     uuid not null references codeaxis.repos(id) on delete cascade,
  -- The snapshot this task was grounded against. A new commit does not
  -- rewrite the task: `task_snapshots` records the move and marks the old
  -- grounding stale (Part 4.F.6).
  commit_sha  text not null,
  title       text not null check (length(btrim(title)) between 1 and 300),
  -- Raw task input as the user supplied it, kept verbatim so extraction can
  -- be re-run and audited against the original. Untrusted data.
  intent      text,
  -- draft      — created, nothing extracted yet
  -- extracting — an extraction pass is running
  -- ready      — requirements/constraints extracted and validated
  -- blocked    — a contradiction or missing-information gap must be resolved
  --              by a human before a context package can be honest (Part 4.F.3)
  -- archived   — kept for provenance, not offered for assembly
  status      text not null default 'draft'
                check (status in ('draft','extracting','ready','blocked','archived')),
  -- Clerk user id (codeaxis.profiles.id is text since the Clerk migration).
  -- No cascade: deleting a profile must not silently erase task history that
  -- a project still depends on.
  created_by  text references codeaxis.profiles(id),
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index tasks_project_idx on codeaxis.tasks (project_id, created_at desc);
create index tasks_repo_commit_idx on codeaxis.tasks (repo_id, commit_sha);

-- --------------------------------------------------- context_packages
-- Declared before `context_items` because an item may point at the package it
-- was assembled into. The package row is the *budget decision record*, not a
-- second copy of the content: the content lives in `context_items`, so a
-- package can be re-rendered (API, CLAUDE.md, AGENTS.md) from rows that each
-- still carry their own evidence and snapshot.

create table codeaxis.context_packages (
  id            uuid primary key default gen_random_uuid(),
  task_id       uuid not null references codeaxis.tasks(id) on delete cascade,
  repo_id       uuid not null references codeaxis.repos(id) on delete cascade,
  commit_sha    text not null,
  -- Monotonic per task. Assembly never mutates a package: requirements
  -- change, the repo moves, verification arrives (Part 4.F.6) -> new version,
  -- so an agent run can always be traced to the exact package it was given.
  version       int not null check (version >= 1),
  -- assembled — complete and inside budget
  -- stale     — a newer snapshot or newer requirements exist
  -- invalid   — assembly could not satisfy its own contract (e.g. the hard
  --             constraints alone exceeded the budget). Stored, not hidden:
  --             Part 4.F.5 forbids truncating constraints, so the honest
  --             outcome is a package marked invalid with the reason.
  status        text not null default 'assembled'
                  check (status in ('assembled','stale','invalid')),
  token_budget  int not null check (token_budget > 0),
  token_count   int not null default 0 check (token_count >= 0),
  -- Which sections were included, in what order, and what was dropped for
  -- budget. Mirrors the `StageOutcome`/budget reporting the retrieval layer
  -- already surfaces, so the UI can show "dropped 14 spans for budget"
  -- instead of silently shipping less context.
  manifest      jsonb not null default '{}',
  -- Per-stage retrieval outcomes (ran/skipped + reason) from Pipeline E.
  retrieval     jsonb not null default '{}',
  -- Gaps, contradictions and open questions carried into the package rather
  -- than resolved by guessing (Part 4.F.3). An array of objects.
  unresolved    jsonb not null default '[]',
  analyzer         text not null,
  analyzer_version text not null,
  created_at    timestamptz not null default now(),
  unique (task_id, version)
);

create index context_packages_task_idx on codeaxis.context_packages (task_id, version desc);
create index context_packages_repo_commit_idx
  on codeaxis.context_packages (repo_id, commit_sha);

-- ------------------------------------------------------- requirements
-- Part 4.F.2's extraction schema, one row per statement instead of one JSON
-- blob, so each statement carries its own origin, evidence and status and can
-- be individually confirmed, contradicted or marked ambiguous.

create table codeaxis.requirements (
  id          uuid primary key default gen_random_uuid(),
  task_id     uuid not null references codeaxis.tasks(id) on delete cascade,
  -- Snapshot pair, denormalized from the task (see rule 1 at the top).
  repo_id     uuid not null references codeaxis.repos(id) on delete cascade,
  commit_sha  text not null,
  -- The seven buckets Part 4.F.2 names, as a closed set. `open_question` is
  -- first-class: Part 4.F.3 says never invent a missing requirement, so a gap
  -- is stored as a question rather than filled in.
  kind        text not null check (kind in (
                'objective','functional','technical','architectural',
                'existing_behavior','acceptance_criteria','open_question')),
  statement   text not null check (length(btrim(statement)) > 0),
  -- MoSCoW-ish priority when the input states one; null when it does not.
  -- Null is not "could" — an unstated priority is unknown, not low.
  priority    text check (priority in ('must','should','could')),
  -- open       — extracted and accepted
  -- ambiguous  — understood but under-specified; `question` says what is missing
  -- unresolved — could not be grounded against the snapshot at all
  -- satisfied  — verification_results confirmed it
  -- rejected   — a human removed it
  status      text not null default 'open'
                check (status in ('open','ambiguous','unresolved','satisfied','rejected')),
  -- What exactly is missing or contradictory. Required whenever the row is
  -- not a clean statement, so an ambiguity is never a bare flag.
  question    text,
  origin      text not null check (origin in ('parsed','configured','inferred')),
  -- Where in the input this came from: {source: 'text'|'pdf'|'docx'|'image'
  -- |'repo_doc', page?, region?, path?, line_start?, line_end?}. Page and
  -- region refs are kept so a requirement extracted from a document stays
  -- clickable back to that document (Part 4.F.1).
  source_ref  jsonb not null default '{}',
  evidence    jsonb not null default '{}',
  -- Display/assembly order within a kind, assigned by the extractor.
  ordinal     int not null default 0,
  confidence  real check (confidence >= 0 and confidence <= 1),
  model       text,
  analyzer         text not null,
  analyzer_version text not null,
  created_at  timestamptz not null default now(),
  check (status in ('open','satisfied','rejected') or question is not null)
);

create index requirements_task_idx on codeaxis.requirements (task_id, kind, ordinal);
create index requirements_repo_commit_idx on codeaxis.requirements (repo_id, commit_sha);

-- --------------------------------------------------------- constraints
-- The Constraint Ledger (Part 4.F.8). Constraints are the one section of a
-- context package that is never truncated (Part 4.F.5), which is why they are
-- their own table with their own severity rather than a `requirements.kind`.
--
-- `task_id` is nullable on purpose: auto-detected repo constraints (public
-- API surface, DB schema, auth boundaries, CI/lint rules, "do not touch"
-- paths) belong to the repo at a commit and are reused by every task, and the
-- MCP `get_constraints` tool is per-repo, not per-task. A user-stated
-- constraint for one task carries that task's id.

create table codeaxis.constraints (
  id          uuid primary key default gen_random_uuid(),
  task_id     uuid references codeaxis.tasks(id) on delete cascade,
  repo_id     uuid not null references codeaxis.repos(id) on delete cascade,
  commit_sha  text not null,
  -- What the constraint is *about*, as a closed category, so the ledger can
  -- be grouped and so a detector can be held to one category.
  kind        text not null check (kind in (
                'public_api','db_schema','auth_boundary','ci_lint',
                'do_not_touch','dependency','performance','other')),
  -- What it applies to: a path or glob, a component key, a symbol name, or
  -- 'repo' for repo-wide. Free text because the addressable things differ per
  -- kind; the UI shows it verbatim.
  scope       text not null check (length(btrim(scope)) > 0),
  statement   text not null check (length(btrim(statement)) > 0),
  -- hard — violating it makes the work wrong (a schema, an auth boundary)
  -- soft — a preference that may be traded off with a stated reason
  severity    text not null check (severity in ('hard','soft')),
  -- user_stated  — a human typed it for this task
  -- auto_detected — an analyzer or a validated model pass found it
  source      text not null check (source in ('user_stated','auto_detected')),
  -- Part 5's origin tag. A user-stated constraint is recorded as
  -- 'configured': it is an explicit rule stated for this repository, which is
  -- what 'configured' means here, and it is emphatically not 'inferred' — no
  -- model produced it. `source` keeps the human/analyzer distinction
  -- queryable without overloading the Part 5 enum.
  origin      text not null check (origin in ('parsed','configured','inferred')),
  -- A human has looked at this ledger entry and accepted it. Auto-detected
  -- entries start false: an unconfirmed detection is shown as a detection,
  -- never as a rule the user agreed to.
  confirmed   boolean not null default false,
  confirmed_by text references codeaxis.profiles(id),
  confirmed_at timestamptz,
  rationale   text,
  evidence    jsonb not null default '{}',
  -- Null for a user-stated constraint: no analyzer produced it, and writing
  -- a fake analyzer name would corrupt provenance. Required for every
  -- auto-detected row by the check below.
  analyzer         text,
  analyzer_version text,
  created_at  timestamptz not null default now(),
  check (source <> 'auto_detected' or (analyzer is not null and analyzer_version is not null)),
  check (source <> 'user_stated' or origin = 'configured'),
  check (confirmed = false or confirmed_at is not null)
);

create index constraints_task_idx on codeaxis.constraints (task_id) where task_id is not null;
create index constraints_repo_commit_idx
  on codeaxis.constraints (repo_id, commit_sha, severity);

-- ----------------------------------------------------------- decisions
-- Decisions taken on a task, and the open questions that still need one.
-- Part 4.F.6 persists these so a task survives the end of a session, and
-- Part 4.G.9 locks approved decisions so regeneration cannot quietly undo
-- them. `status = 'open'` with `decision is null` is the normal resting state
-- of an unresolved question — storing it is the point.

create table codeaxis.decisions (
  id          uuid primary key default gen_random_uuid(),
  task_id     uuid not null references codeaxis.tasks(id) on delete cascade,
  repo_id     uuid not null references codeaxis.repos(id) on delete cascade,
  commit_sha  text not null,
  question    text not null check (length(btrim(question)) > 0),
  decision    text,
  -- open       — needs an answer; surfaced in the package's unresolved list
  -- decided    — answered; `locked` decides whether regeneration may revisit it
  -- superseded — replaced by a later decision (`supersedes_id` on that row)
  status      text not null default 'open' check (status in ('open','decided','superseded')),
  -- An approved decision is locked: later passes read it, never rewrite it.
  locked      boolean not null default false,
  decided_by  text references codeaxis.profiles(id),
  decided_at  timestamptz,
  rationale   text,
  -- The decision this one replaces. `set null` rather than cascade: losing
  -- the predecessor must not delete the decision in force.
  supersedes_id uuid references codeaxis.decisions(id) on delete set null,
  -- 'inferred' when a model raised the question; 'parsed'/'configured' when
  -- the question follows from code or an explicit rule.
  origin      text not null check (origin in ('parsed','configured','inferred')),
  evidence    jsonb not null default '{}',
  analyzer         text not null,
  analyzer_version text not null,
  created_at  timestamptz not null default now(),
  check (status <> 'decided' or (decision is not null and decided_at is not null)),
  check (locked = false or status = 'decided')
);

create index decisions_task_idx on codeaxis.decisions (task_id, status);
create index decisions_repo_commit_idx on codeaxis.decisions (repo_id, commit_sha);

-- ------------------------------------------------------- context_items
-- Part 6 names these columns exactly:
--   context_items(id, task_id, kind, content, origin, snapshot_id, evidence jsonb)
-- `snapshot_id` is realized as the `(repo_id, commit_sha)` pair this
-- repository actually keys snapshots by — see rule 1 at the top of this file.
-- Everything else is additive.
--
-- One row per assembled piece of context. Kept as rows, not as one rendered
-- blob, so every line of a delivered package still points at its source and
-- carries its own origin tag.

create table codeaxis.context_items (
  id          uuid primary key default gen_random_uuid(),
  task_id     uuid not null references codeaxis.tasks(id) on delete cascade,
  -- The package this item was assembled into. Null while the item exists but
  -- no package has been built from it yet (extraction and assembly are
  -- separate passes).
  package_id  uuid references codeaxis.context_packages(id) on delete cascade,
  -- The snapshot pair = Part 6's `snapshot_id` for this repository.
  repo_id     uuid not null references codeaxis.repos(id) on delete cascade,
  commit_sha  text not null,
  -- The nine sections of Part 4.F.5, in its order. `ordinal` carries position
  -- *within* a section; the section order itself belongs to the assembler, not
  -- to this enum, so changing presentation never requires a migration.
  kind        text not null check (kind in (
                'task_intent','constraint','requirement','architecture','code',
                'convention','test','limitation','open_question')),
  content     text not null,
  origin      text not null check (origin in ('parsed','configured','inferred')),
  -- The file+line citations (and node/chunk ids) behind this item, in the
  -- shape `app.models.Citation` serializes: {spans: [{path, line_start,
  -- line_end, symbol?, node_id?, chunk_id?}], ...}. An item with no spans is
  -- allowed only for kinds that are not claims about code (task_intent,
  -- open_question) — enforced in the write layer, not here, because a check
  -- constraint cannot read jsonb shape without becoming unmaintainable.
  evidence    jsonb not null default '{}',
  -- Cost accounting for the budgeted assembly (Part 4.F.5).
  token_count int not null default 0 check (token_count >= 0),
  ordinal     int not null default 0,
  analyzer         text not null,
  analyzer_version text not null,
  created_at  timestamptz not null default now()
);

create index context_items_task_idx on codeaxis.context_items (task_id, kind, ordinal);
create index context_items_package_idx
  on codeaxis.context_items (package_id, kind, ordinal) where package_id is not null;
create index context_items_repo_commit_idx on codeaxis.context_items (repo_id, commit_sha);

-- ------------------------------------------------------ task_components
-- Which existing graph entities a task touches (Part 4.F.4's "affected
-- components" seed for retrieval, and the Inspector's entry point from a
-- task). Points at `codeaxis.nodes`, which is where components live in this
-- codebase (`kind = 'cluster'`), so a task can never reference a component
-- that does not exist in the snapshot.

create table codeaxis.task_components (
  task_id     uuid not null references codeaxis.tasks(id) on delete cascade,
  node_id     bigint not null references codeaxis.nodes(id) on delete cascade,
  -- Denormalized snapshot pair: the node already carries one, but a query
  -- that scopes a task's components to a commit must not have to join nodes
  -- to find out, and a mismatch between the two is a bug worth being able to
  -- detect.
  repo_id     uuid not null references codeaxis.repos(id) on delete cascade,
  commit_sha  text not null,
  -- primary    — the work happens here
  -- supporting — needed to understand or call into the primary component
  -- impacted   — may break; the impact analysis found a path to it
  relevance   text not null check (relevance in ('primary','supporting','impacted')),
  rationale   text,
  origin      text not null check (origin in ('parsed','configured','inferred')),
  confidence  real check (confidence >= 0 and confidence <= 1),
  evidence    jsonb not null default '{}',
  analyzer         text not null,
  analyzer_version text not null,
  created_at  timestamptz not null default now(),
  primary key (task_id, node_id)
);

create index task_components_node_idx on codeaxis.task_components (node_id);
create index task_components_repo_commit_idx
  on codeaxis.task_components (repo_id, commit_sha, relevance);

-- ------------------------------------------------------- task_snapshots
-- The snapshots a task has been grounded against, in order. Part 4.F.6
-- requires task state to be updated when repo changes are observed; this is
-- the audit trail of that. The task's own `commit_sha` is the active one, and
-- the row for it here is `active`; earlier rows go `stale` with a reason, so
-- "this package was built against a commit that no longer exists on the
-- branch" is a statement the UI can make instead of a surprise.

create table codeaxis.task_snapshots (
  id          bigserial primary key,
  task_id     uuid not null references codeaxis.tasks(id) on delete cascade,
  repo_id     uuid not null references codeaxis.repos(id) on delete cascade,
  commit_sha  text not null,
  status      text not null check (status in ('active','stale','superseded')),
  -- Why it stopped being active ("newer commit 9f3c... analyzed"). Required
  -- for any non-active row: a stale grounding with no reason is not auditable.
  reason      text,
  created_at  timestamptz not null default now(),
  unique (task_id, repo_id, commit_sha),
  check (status = 'active' or reason is not null)
);

create index task_snapshots_task_idx on codeaxis.task_snapshots (task_id, created_at desc);
-- At most one active grounding per task: two would make "which snapshot is
-- this task about" unanswerable.
create unique index task_snapshots_one_active
  on codeaxis.task_snapshots (task_id) where status = 'active';

-- -------------------------------------------------- verification_results
-- Outcomes of checking a task's acceptance criteria. CodeAxis never executes
-- repository code (Part 0.3 / Part 9), so nothing here is produced by running
-- anything: results are *reported in* — by the user, by a deterministic
-- static analyzer, or by an external CI system — and `source` says which.
-- `outcome = 'not_run'` is therefore the common, honest default.

create table codeaxis.verification_results (
  id             bigserial primary key,
  task_id        uuid not null references codeaxis.tasks(id) on delete cascade,
  repo_id        uuid not null references codeaxis.repos(id) on delete cascade,
  commit_sha     text not null,
  -- The requirement being verified, when it is one of ours. `set null` so a
  -- re-extraction that replaces requirement rows does not delete the history
  -- of what was verified; `criterion` keeps the human-readable statement.
  requirement_id uuid references codeaxis.requirements(id) on delete set null,
  criterion      text not null check (length(btrim(criterion)) > 0),
  outcome        text not null
                   check (outcome in ('passed','failed','inconclusive','not_run')),
  detail         text,
  -- user     — a human reported it
  -- analyzer — one of our static passes established it
  -- external — a CI run or other outside system reported it
  source         text not null check (source in ('user','analyzer','external')),
  evidence       jsonb not null default '{}',
  created_at     timestamptz not null default now()
);

create index verification_results_task_idx
  on codeaxis.verification_results (task_id, created_at desc);
create index verification_results_requirement_idx
  on codeaxis.verification_results (requirement_id) where requirement_id is not null;
create index verification_results_repo_commit_idx
  on codeaxis.verification_results (repo_id, commit_sha);

-- ---------------------------------------------------------- api_tokens
-- Per-repo, read-only tokens for the MCP server (Part 4.F.7, Part 8's
-- `/mcp/{repositoryId}`). Listed under Part 6 "Platform" but repo-scoped: a
-- token grants read access to exactly one repo's analyzed model, so a leaked
-- token cannot reach another repo or another project.
--
-- **Only a hash is stored.** The token itself exists once, in the response
-- that creates it, and is never written to this table, to a log, or to a
-- model (Part 0.3's secrets rule applies to our own credentials too).
-- `token_prefix` is the first few characters, which are shown in the UI for
-- identification and used to narrow the lookup to one row so verification can
-- finish with a constant-time digest comparison in the app rather than an
-- SQL equality on the secret material.

create table codeaxis.api_tokens (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid not null references codeaxis.projects(id) on delete cascade,
  repo_id       uuid not null references codeaxis.repos(id) on delete cascade,
  name          text not null check (length(btrim(name)) between 1 and 200),
  -- Non-secret display/lookup handle (e.g. 'cxa_7f3b'). Unique so a lookup by
  -- prefix returns at most one candidate row.
  token_prefix  text not null unique check (length(btrim(token_prefix)) > 0),
  -- sha256 hex of the full token. Unique: the same secret cannot be
  -- registered twice.
  token_hash    text not null unique check (length(token_hash) = 64),
  -- V1 MCP is read-only, enforced in the schema and not only in code, so no
  -- future route can mint a writing token by accident.
  scope         text not null default 'read' check (scope = 'read'),
  created_by    text references codeaxis.profiles(id),
  created_at    timestamptz not null default now(),
  -- Null until the token is first used. Updated by the worker role, so an
  -- unused or stale token is visible without a separate audit table.
  last_used_at  timestamptz,
  expires_at    timestamptz,
  -- Revocation is a timestamp, not a delete: a revoked token's id stays
  -- referenceable by whatever logged its use.
  revoked_at    timestamptz
);

create index api_tokens_repo_idx on codeaxis.api_tokens (repo_id) where revoked_at is null;
create index api_tokens_project_idx on codeaxis.api_tokens (project_id);

-- ----------------------------------------------------------------- RLS
-- Same two live shapes as the platform tables: the read-only `codeaxis_api`
-- role (FastAPI read paths) and the read/write `codeaxis_worker` role
-- (analysis/extraction passes and every API write). Neither role is
-- BYPASSRLS, so with RLS enabled each needs an explicit policy. Scoping to a
-- user happens in the app layer; see the note at the top of this file for why
-- there is no `authenticated` policy.

alter table codeaxis.tasks                enable row level security;
alter table codeaxis.requirements         enable row level security;
alter table codeaxis.constraints          enable row level security;
alter table codeaxis.decisions            enable row level security;
alter table codeaxis.context_items        enable row level security;
alter table codeaxis.task_components      enable row level security;
alter table codeaxis.task_snapshots       enable row level security;
alter table codeaxis.verification_results enable row level security;
alter table codeaxis.context_packages     enable row level security;
alter table codeaxis.api_tokens           enable row level security;

create policy tasks_api_read on codeaxis.tasks
  for select to codeaxis_api using (true);
create policy requirements_api_read on codeaxis.requirements
  for select to codeaxis_api using (true);
create policy constraints_api_read on codeaxis.constraints
  for select to codeaxis_api using (true);
create policy decisions_api_read on codeaxis.decisions
  for select to codeaxis_api using (true);
create policy context_items_api_read on codeaxis.context_items
  for select to codeaxis_api using (true);
create policy task_components_api_read on codeaxis.task_components
  for select to codeaxis_api using (true);
create policy task_snapshots_api_read on codeaxis.task_snapshots
  for select to codeaxis_api using (true);
create policy verification_results_api_read on codeaxis.verification_results
  for select to codeaxis_api using (true);
create policy context_packages_api_read on codeaxis.context_packages
  for select to codeaxis_api using (true);
-- The api role reads token hashes to verify a presented MCP token. It can
-- never mint, revoke or touch one: those are worker writes.
create policy api_tokens_api_read on codeaxis.api_tokens
  for select to codeaxis_api using (true);

create policy tasks_worker_all on codeaxis.tasks
  for all to codeaxis_worker using (true) with check (true);
create policy requirements_worker_all on codeaxis.requirements
  for all to codeaxis_worker using (true) with check (true);
create policy constraints_worker_all on codeaxis.constraints
  for all to codeaxis_worker using (true) with check (true);
create policy decisions_worker_all on codeaxis.decisions
  for all to codeaxis_worker using (true) with check (true);
create policy context_items_worker_all on codeaxis.context_items
  for all to codeaxis_worker using (true) with check (true);
create policy task_components_worker_all on codeaxis.task_components
  for all to codeaxis_worker using (true) with check (true);
create policy task_snapshots_worker_all on codeaxis.task_snapshots
  for all to codeaxis_worker using (true) with check (true);
create policy verification_results_worker_all on codeaxis.verification_results
  for all to codeaxis_worker using (true) with check (true);
create policy context_packages_worker_all on codeaxis.context_packages
  for all to codeaxis_worker using (true) with check (true);
create policy api_tokens_worker_all on codeaxis.api_tokens
  for all to codeaxis_worker using (true) with check (true);

-- Grants. `scripts/setup_db_roles.py` set `alter default privileges in schema
-- codeaxis` for both roles, so tables created here inherit select (api) and
-- select/insert/update/delete + sequence usage (worker). These statements are
-- the same grants written explicitly: default privileges only apply to
-- objects created by the role that set them, so a migration applied by a
-- different owner connection would otherwise leave the roles without access.
-- Idempotent and cheap; the jobs and llm_calls migrations do the same.

grant select on
  codeaxis.tasks, codeaxis.requirements, codeaxis.constraints, codeaxis.decisions,
  codeaxis.context_items, codeaxis.task_components, codeaxis.task_snapshots,
  codeaxis.verification_results, codeaxis.context_packages, codeaxis.api_tokens
  to codeaxis_api;

grant select, insert, update, delete on
  codeaxis.tasks, codeaxis.requirements, codeaxis.constraints, codeaxis.decisions,
  codeaxis.context_items, codeaxis.task_components, codeaxis.task_snapshots,
  codeaxis.verification_results, codeaxis.context_packages, codeaxis.api_tokens
  to codeaxis_worker;

-- bigserial tables need their sequence as well as the table.
grant usage, select on sequence codeaxis.task_snapshots_id_seq to codeaxis_worker;
grant usage, select on sequence codeaxis.verification_results_id_seq to codeaxis_worker;
