-- The commit a repo was staged at, resolved server-side at stage time.
-- `confirm` must scan this SHA, never one echoed back by the client.
alter table codeaxis.repos add column staged_commit_sha text;
