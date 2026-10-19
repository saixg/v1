-- Phase 5: blueprint generation and regeneration are long-running model
-- calls, so they go through the same job record a scan does (build-plan.md
-- Part 8: "async jobs for anything long" — the client polls GET /jobs/{id}).
--
-- `codeaxis.jobs.kind` was `check (kind in ('scan'))`, written when a scan was
-- the only long task. Widened rather than replaced so existing rows and the
-- scan path are untouched.
--
-- Note what is deliberately NOT changed: `jobs_one_scan_per_repo` is a
-- partial unique index (`where kind = 'scan'`), so it keeps enforcing one
-- scan per repo without constraining blueprint jobs. Blueprint concurrency is
-- handled where it belongs — the route refuses to start one while the repo
-- already has a queued or running job (`get_active_job_for_repo`) — because
-- "one blueprint job per repo" is a policy about what the server will accept,
-- not an invariant of the data.

alter table codeaxis.jobs drop constraint if exists jobs_kind_check;

alter table codeaxis.jobs
  add constraint jobs_kind_check
  check (kind in ('scan', 'blueprint_generate', 'blueprint_regenerate'));
