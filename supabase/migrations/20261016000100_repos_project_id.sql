-- Every repo belongs to exactly one project; access to a repo (and to every
-- graph row keyed by its repo_id) is decided by membership in that project.
--
-- NOT NULL with no default: a repo with no owning project would be readable
-- by nobody, so none may exist. This migration fails on a database that still
-- holds pre-project repos rows — delete or assign them first, deliberately.

alter table codeaxis.repos
  add column project_id uuid not null references codeaxis.projects(id);

create index repos_project_id_idx on codeaxis.repos (project_id);
