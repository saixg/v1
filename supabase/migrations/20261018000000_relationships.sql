-- Relationship extraction (build-plan.md Part 4.B): edges now carry how
-- their destination resolved, plus a free-form attrs bag for heuristic
-- evidence (e.g. which naming/import rule fired for a `tests` edge).
-- `nodes.kind` / `edges.kind` have no check constraint (see
-- 20261014000000_repo_model.sql: both are plain `text not null`), so the new
-- "dependency" NodeKind and the newly-populated EdgeKind values need no
-- schema change here.
alter table codeaxis.edges add column resolution_status text
  check (resolution_status in ('resolved','ambiguous','unresolved','external'));
alter table codeaxis.edges add column attrs jsonb not null default '{}';
create index on codeaxis.edges (repo_id, commit_sha, kind, resolution_status);
