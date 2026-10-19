-- Phase 5, Virtual Development Environment (build-plan.md Part 4.G, Part 6
-- "Virtual env", Part 10 Phase 5). Resolves Part 13 decision #4: the
-- blueprint node/edge taxonomy and schema v1 are locked here.
--
-- This migration is the data model only. No generation, no validator, no
-- routes, no React Flow — those read and write these tables.
--
-- What a blueprint is, and why it is not in the Repo Model
-- -------------------------------------------------------
-- `codeaxis.nodes`/`codeaxis.edges` hold *facts an analyzer established
-- about code that exists*, tagged `origin in (parsed|configured|inferred)`
-- with a resolution status. A blueprint holds *a design a model proposed for
-- code that does not exist yet*. Those are different claims about the world
-- and they get different storage:
--
-- 1. **Separate taxonomy, not an extension of `NodeKind`/`EdgeKind`.** A
--    `proposed` component is not `parsed`, not `configured`, and not even
--    `inferred` (which in this codebase means "a model generalized from
--    observations", per the `summaries`/`conventions` docstrings) — it is
--    authored. Adding blueprint kinds to the graph's enums would mix
--    established fact with proposal inside the one vocabulary the rest of
--    the product treats as ground truth, which is the line CLAUDE.md draws
--    with "analysis writes to the Repo Model; model output never writes
--    back". So blueprint rows carry `generation_status`
--    (`generated|edited|locked`) instead of `origin`.
-- 2. **One seam to the real graph: `blueprint_components.linked_component_id
--    -> codeaxis.nodes(id)`**, following the precedent
--    `codeaxis.task_components` already set. Part 4.G.3 requires every
--    mapped capability to point at a real repo entity, and Part 11 requires
--    "existing components map to real entities" — this column is how that is
--    checkable in SQL rather than asserted in prose.
-- 3. **The snapshot pair `(repo_id, commit_sha)`** is carried denormalized on
--    every row that is about the repo, exactly as every other derived table
--    in this schema does, so snapshot scoping never needs a join.
--
-- References by `key`, not by id
-- ------------------------------
-- A connection names its endpoints by the component's `key`, and a proposed
-- file names `implements_component_key`, rather than by a row id. Part 4.G.9
-- requires stable node ids across regeneration and "regenerate only the
-- affected neighborhood": a new version re-inserts rows (new uuids), so an
-- id reference would break every edge touching an untouched component.
-- The cost, stated plainly: referential integrity for those two references
-- is enforced by the validator (`app/blueprint/validate.py`) and the
-- per-version unique index on `key`, not by a foreign key — Postgres cannot
-- express "this text matches a sibling row's key within the same version"
-- as an FK. That trade is what makes partial regeneration mean anything.
--
-- Versioning
-- ----------
-- `blueprint_versions` is append-only in the same way `context_packages` is:
-- an edit, a regeneration, or a validation-status change creates a new
-- version rather than mutating one, so an exported context package can
-- always be traced to the exact blueprint it came from. `blueprints`
-- carries a pointer to the current one.
--
-- Untrusted text: `title`, `responsibility`, `purpose`, `semantics` and
-- `interfaces` hold model-authored or user-authored prose, some of it
-- derived from repository content. All of it is data (Part 0.3, Part 9).
-- Nothing downstream may treat it as instructions.
--
-- RLS: the same two policy shapes as every migration since
-- `20261017000000_clerk_auth.sql` — `*_api_read` (role `codeaxis_api`) and
-- `*_worker_all` (role `codeaxis_worker`). The `authenticated` shape is
-- structurally dead under Clerk (no `request.jwt.claims`, no browser path to
-- Postgres); authorization is the app-layer project-membership check in
-- FastAPI.

-- ---------------------------------------------------------- blueprints
-- One design effort. Hangs off exactly one Context Engine task (Part 13
-- decision #4, sub-decision C): Part 4.G.1 starts from "extract objective/
-- requirements/constraints", which is Pillar 2's job, so a blueprint reuses
-- `codeaxis.requirements` / `constraints` / `decisions` by reference instead
-- of duplicating them. There is deliberately no task-less entry point.

create table codeaxis.blueprints (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references codeaxis.projects(id) on delete cascade,
  repo_id     uuid not null references codeaxis.repos(id) on delete cascade,
  -- The snapshot the design was grounded against. A blueprint is never
  -- silently re-pointed at a new commit: its versions record where it stood.
  commit_sha  text not null,
  task_id     uuid not null references codeaxis.tasks(id) on delete cascade,
  title       text not null check (length(trim(title)) > 0),
  -- Set after the first version exists; the FK is added below, once
  -- blueprint_versions is declared (the two tables reference each other).
  current_version_id uuid,
  created_by  text references codeaxis.profiles(id) on delete set null,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

create index blueprints_task_idx on codeaxis.blueprints (task_id);
create index blueprints_project_idx on codeaxis.blueprints (project_id, created_at desc);
create index blueprints_repo_commit_idx on codeaxis.blueprints (repo_id, commit_sha);

-- -------------------------------------------------- blueprint_versions
-- One immutable snapshot of a design. Status is stored, never inferred at
-- read time, and `invalid` is a first-class resting state: Part 4.G.5
-- requires an invalid blueprint to be *rejected with reasons*, so the
-- reasons live in `validation_errors` rather than being recomputed or
-- discarded.

create table codeaxis.blueprint_versions (
  id             uuid primary key default gen_random_uuid(),
  blueprint_id   uuid not null references codeaxis.blueprints(id) on delete cascade,
  version        int not null check (version >= 1),
  -- The canonical-JSON contract this row's rows conform to
  -- (packages/schemas/blueprint.v1.json). An unknown value is rejected at
  -- read time, never coerced into the shape the reader happens to expect.
  schema_version text not null default '1.0.0',
  -- draft     — generated or edited, not yet validated
  -- validated — passed every rule in app/blueprint/validate.py
  -- invalid   — failed validation; `validation_errors` says exactly how
  -- approved  — a human accepted it; the decisions it locked are binding
  status         text not null default 'draft'
                   check (status in ('draft','validated','invalid','approved')),
  -- Array of {code, message, entity_kind, entity_key} objects. Empty for a
  -- validated version. Never a bare boolean: "invalid" with no reason is
  -- exactly the unexplained rejection Part 4.G.5 forbids.
  validation_errors jsonb not null default '[]',
  -- Why this version exists, so a version history reads as a story rather
  -- than a list of uuids.
  -- generated    — a fresh generation pass
  -- regenerated  — a partial regeneration (Part 4.G.9); locked entities were
  --                carried forward unchanged
  -- edited       — a human changed something
  -- validated    — a validation run that changed status
  origin_reason  text not null default 'generated'
                   check (origin_reason in ('generated','regenerated','edited','validated')),
  -- Which pass wrote it. The model behind a generation is traceable through
  -- codeaxis.llm_calls; this names the code, as every other derived table does.
  analyzer         text not null,
  analyzer_version text not null,
  created_by     text references codeaxis.profiles(id) on delete set null,
  created_at     timestamptz not null default now(),
  unique (blueprint_id, version)
);

create index blueprint_versions_blueprint_idx
  on codeaxis.blueprint_versions (blueprint_id, version desc);

alter table codeaxis.blueprints
  add constraint blueprints_current_version_fkey
  foreign key (current_version_id)
  references codeaxis.blueprint_versions(id) on delete set null;

-- ------------------------------------------------ blueprint_components
-- One capability in the design. `kind` says what it is relative to the
-- existing repo; `mapping` says what will be done to it. The two are not
-- independent, and the check constraint below is the taxonomy's core
-- invariant rather than a convention the writer is trusted to follow:
--
--   existing + reuse   — call it as it is
--   modified + extend  — change code that exists
--   proposed + new     — code that does not exist yet
--
-- Anything else (a `proposed` component claiming to `reuse`, an `existing`
-- one claiming to be `new`) is a contradiction, so the database refuses it.

create table codeaxis.blueprint_components (
  id                   uuid primary key default gen_random_uuid(),
  blueprint_version_id uuid not null
                         references codeaxis.blueprint_versions(id) on delete cascade,
  -- Stable across versions; what connections and proposed files point at.
  key                  text not null check (length(trim(key)) > 0),
  name                 text not null check (length(trim(name)) > 0),
  kind                 text not null check (kind in ('existing','proposed','modified')),
  mapping              text not null check (mapping in ('reuse','extend','new')),
  -- The real repo entity this maps to: a cluster-kind node (what this
  -- codebase calls a "component"), matching task_components' precedent.
  -- Finer-grained detail — the exact file or symbol to extend — rides in
  -- `evidence.spans`/`evidence.node_ids`, so this stays one clean FK target.
  linked_component_id  bigint references codeaxis.nodes(id) on delete set null,
  -- Snapshot pair, denormalized (see the header). Lets a query scope a
  -- blueprint's components to a commit without joining nodes, and makes a
  -- mismatch between the two detectable.
  repo_id              uuid not null references codeaxis.repos(id) on delete cascade,
  commit_sha           text not null,
  -- A lightweight grouping; `modules` in the canonical JSON. Free text, not
  -- its own table: a module has no identity beyond the components in it.
  module_key           text,
  responsibility       text,
  -- Array of {name, kind: api|event|function, signature} objects.
  interfaces           jsonb not null default '[]',
  confidence           real check (confidence >= 0 and confidence <= 1),
  -- Same `ContextEvidence` shape the context engine uses, reused rather than
  -- re-declared, so the citation validator that already checks chat answers
  -- and context items checks these spans too.
  evidence             jsonb not null default '{}',
  -- generated — written by a generation pass, untouched since
  -- edited    — a human changed it
  -- locked    — a human approved it; regeneration may not alter it
  generation_status    text not null default 'generated'
                         check (generation_status in ('generated','edited','locked')),
  locked               boolean not null default false,
  created_at           timestamptz not null default now(),
  unique (blueprint_version_id, key),
  -- The taxonomy's core invariant (Part 4.G.3/4.G.5).
  constraint blueprint_components_kind_mapping_pair check (
    (kind = 'existing' and mapping = 'reuse')
    or (kind = 'modified' and mapping = 'extend')
    or (kind = 'proposed' and mapping = 'new')
  ),
  -- Part 11: "existing components map to real entities". A component that
  -- claims the repo already contains it must say which node that is.
  constraint blueprint_components_existing_is_linked check (
    kind = 'proposed' or linked_component_id is not null
  ),
  -- `locked` and `generation_status = 'locked'` are one fact, not two.
  constraint blueprint_components_locked_status check (
    locked = (generation_status = 'locked')
  )
);

create index blueprint_components_version_idx
  on codeaxis.blueprint_components (blueprint_version_id, key);
create index blueprint_components_linked_idx
  on codeaxis.blueprint_components (linked_component_id);
create index blueprint_components_repo_commit_idx
  on codeaxis.blueprint_components (repo_id, commit_sha);

-- ----------------------------------------------- blueprint_connections
-- One relationship between two components. `data_flows` in the canonical
-- JSON is this table filtered to `kind = 'data_flow'`, computed at read
-- time — not a second stored collection, so there is one source of truth for
-- an edge and a data flow cannot drift from the connection it describes.

create table codeaxis.blueprint_connections (
  id                   uuid primary key default gen_random_uuid(),
  blueprint_version_id uuid not null
                         references codeaxis.blueprint_versions(id) on delete cascade,
  key                  text not null check (length(trim(key)) > 0),
  -- control_flow — A invokes B
  -- data_flow    — data moves from A to B; `data_shape` describes what
  -- dependency   — A needs B to exist (the only kind a cycle is invalid in)
  -- event        — A emits, B consumes, asynchronously
  kind                 text not null
                         check (kind in ('control_flow','data_flow','dependency','event')),
  -- Endpoints by component `key` within this same version (see the header
  -- for why this is not an FK). The validator rejects a dangling endpoint.
  source_key           text not null check (length(trim(source_key)) > 0),
  target_key           text not null check (length(trim(target_key)) > 0),
  -- {protocol, trigger, description}
  semantics            jsonb not null default '{}',
  -- Only meaningful for a data flow; the constraint keeps it from being set
  -- on a kind it would be ignored for.
  data_shape           jsonb,
  locked               boolean not null default false,
  created_at           timestamptz not null default now(),
  unique (blueprint_version_id, key),
  -- A component connecting to itself carries no design information and
  -- breaks layout; Part 4.G.5's "dependencies coherent" starts here.
  constraint blueprint_connections_no_self_loop check (source_key <> target_key),
  constraint blueprint_connections_data_shape_kind check (
    kind = 'data_flow' or data_shape is null
  )
);

create index blueprint_connections_version_idx
  on codeaxis.blueprint_connections (blueprint_version_id, kind);
create index blueprint_connections_source_idx
  on codeaxis.blueprint_connections (blueprint_version_id, source_key);
create index blueprint_connections_target_idx
  on codeaxis.blueprint_connections (blueprint_version_id, target_key);

-- ----------------------------------------------------- proposed_files
-- Part 4.G.7's file-structure proposal. Every file states a purpose and,
-- where there is one, the component it implements. `status` distinguishes a
-- file that would be created from one that exists and would change — the
-- "existing vs. proposed clearly distinguished" line in Part 11.

create table codeaxis.proposed_files (
  id                   uuid primary key default gen_random_uuid(),
  blueprint_version_id uuid not null
                         references codeaxis.blueprint_versions(id) on delete cascade,
  path                 text not null check (length(trim(path)) > 0),
  -- new       — does not exist in the snapshot
  -- modified  — exists and would change
  -- unchanged — exists, listed for context, would not be touched
  status               text not null check (status in ('new','modified','unchanged')),
  purpose              text not null check (length(trim(purpose)) > 0),
  implements_component_key text,
  -- `codeaxis.conventions` rows the proposal followed to pick this path and
  -- shape (Part 4.G.7: "from actual conventions/language/framework"). An
  -- empty array means the proposal cites no convention — which the UI shows
  -- as an unsupported placement rather than hiding.
  conventions_refs     bigint[] not null default '{}',
  locked               boolean not null default false,
  created_at           timestamptz not null default now(),
  unique (blueprint_version_id, path)
);

create index proposed_files_version_idx
  on codeaxis.proposed_files (blueprint_version_id, status);
create index proposed_files_component_idx
  on codeaxis.proposed_files (blueprint_version_id, implements_component_key);

-- --------------------------------------------------- blueprint_layouts
-- Part 4.G.6: "layout stored separately from semantics". Not in Part 6's
-- five-table list for the virtual env, and deliberately so: putting
-- `layout jsonb` on `blueprint_versions` would make dragging a box in the
-- React Flow view create a new semantic version of the design, which is
-- wrong — a relayout changes nothing about what is being built. One row per
-- version, replaced in place as the view is arranged.

create table codeaxis.blueprint_layouts (
  blueprint_version_id uuid primary key
                         references codeaxis.blueprint_versions(id) on delete cascade,
  -- {nodes: [{key, x, y, w, h}], edges: [{key, points}], viewport: {...}}
  -- Keyed by component/connection `key`, so a layout survives regeneration
  -- of the rows it positions.
  layout               jsonb not null default '{}',
  updated_at           timestamptz not null default now()
);

-- ------------------------------------------------------- RLS + grants

alter table codeaxis.blueprints            enable row level security;
alter table codeaxis.blueprint_versions    enable row level security;
alter table codeaxis.blueprint_components  enable row level security;
alter table codeaxis.blueprint_connections enable row level security;
alter table codeaxis.proposed_files        enable row level security;
alter table codeaxis.blueprint_layouts     enable row level security;

create policy blueprints_api_read on codeaxis.blueprints
  for select to codeaxis_api using (true);
create policy blueprint_versions_api_read on codeaxis.blueprint_versions
  for select to codeaxis_api using (true);
create policy blueprint_components_api_read on codeaxis.blueprint_components
  for select to codeaxis_api using (true);
create policy blueprint_connections_api_read on codeaxis.blueprint_connections
  for select to codeaxis_api using (true);
create policy proposed_files_api_read on codeaxis.proposed_files
  for select to codeaxis_api using (true);
create policy blueprint_layouts_api_read on codeaxis.blueprint_layouts
  for select to codeaxis_api using (true);

create policy blueprints_worker_all on codeaxis.blueprints
  for all to codeaxis_worker using (true) with check (true);
create policy blueprint_versions_worker_all on codeaxis.blueprint_versions
  for all to codeaxis_worker using (true) with check (true);
create policy blueprint_components_worker_all on codeaxis.blueprint_components
  for all to codeaxis_worker using (true) with check (true);
create policy blueprint_connections_worker_all on codeaxis.blueprint_connections
  for all to codeaxis_worker using (true) with check (true);
create policy proposed_files_worker_all on codeaxis.proposed_files
  for all to codeaxis_worker using (true) with check (true);
create policy blueprint_layouts_worker_all on codeaxis.blueprint_layouts
  for all to codeaxis_worker using (true) with check (true);

-- Default privileges only apply to objects created by the role that set
-- them, so these are written explicitly — same as every migration since
-- jobs/llm_calls. The layout table is worker-writable because the API
-- persists a relayout through the worker connection, like every other write.

grant select on
  codeaxis.blueprints, codeaxis.blueprint_versions, codeaxis.blueprint_components,
  codeaxis.blueprint_connections, codeaxis.proposed_files, codeaxis.blueprint_layouts
  to codeaxis_api;

grant select, insert, update, delete on
  codeaxis.blueprints, codeaxis.blueprint_versions, codeaxis.blueprint_components,
  codeaxis.blueprint_connections, codeaxis.proposed_files, codeaxis.blueprint_layouts
  to codeaxis_worker;
