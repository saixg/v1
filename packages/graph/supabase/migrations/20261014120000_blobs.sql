-- Blob store for pinned source: the raw text of each scanned file at the
-- analyzed commit, so "click a symbol -> see its source" is served from the
-- same snapshot the graph was built from, never from a live checkout (which
-- may have moved on, or been deleted after the scan). See docs/04-graph-schema.md.
--
-- Only text files under the scanner's size cap that were actually read get a
-- row. A missing row means "no source available", never "empty file".

create table codeaxis.blobs (
  repo_id       uuid not null references codeaxis.repos(id),
  commit_sha    text not null,
  path          text not null,
  content       text not null,
  content_hash  text not null,
  primary key (repo_id, commit_sha, path)
);

-- Grants mirror scripts/setup_db_roles.py for the existing tables: the worker
-- writes, the api reads only. Default privileges already cover a table created
-- by the same owner; these are explicit so the migration is self-describing.
grant select, insert, update, delete on codeaxis.blobs to codeaxis_worker;
grant select on codeaxis.blobs to codeaxis_api;
