-- Phase 6 hardening: the per-project model cost cap (build-plan.md Part 10
-- Phase 6 "cost caps", Part 2 Reliability "per-repo cost cap", Part 11
-- "model cost and failures observable", Part 13 #6 "Free-tier limits ...
-- drives cost caps").
--
-- Until now nothing in this system refused work: `codeaxis.llm_calls` and
-- `app/llm/pricing.py` recorded an estimated cost *after* the provider had
-- already been paid. This column is the other half -- the number
-- `app/limits/budget.py` compares the ledger against *before* each call.
--
-- Why a column on `projects` rather than a settings table or an env var
-- ---------------------------------------------------------------------
-- An env var alone cannot express a paid tier: one deployment-wide number
-- means every project on the instance has the same ceiling, so the first
-- customer who needs more forces the free-tier limit up for everybody. A
-- settings table would be a table holding one value with no other reader.
-- So: `CODEAXIS_PROJECT_MONTHLY_COST_CAP_USD` is the deployment default and
-- this column is the per-project override. **NULL means "use the default"** --
-- not "unlimited" -- because a nullable column with a null-is-unlimited
-- reading would make every newly created project uncapped, which is exactly
-- the state this migration exists to end. Explicit `0` means "no model work
-- at all", which is a usable state (suspend a project without deleting it).
--
-- Why the cap is not per repo
-- ---------------------------
-- Part 2 says "per-repo cost cap", but `llm_calls.repo_id` is null for every
-- call that is not about one repo (requirement extraction on a task, the eval
-- harness), so a repo-scoped ledger read would silently exclude real spend.
-- `project_id` is the attribution column that is set on everything billable
-- and is also the unit the free tier is sold in. Scoping to the project is
-- therefore strictly tighter than scoping to the repo.
--
-- Why there is no user-scoped cap here
-- ------------------------------------
-- `llm_calls` has no `user_id` column and cannot get a trustworthy one: a
-- worker pass runs detached from the request that triggered it, so the
-- ledger genuinely does not know which end user caused most rows. A per-user
-- cap would have to sum over the projects a user owns, which is a different
-- query and a different product decision (it caps collaborators too). Named
-- as a gap in the budget module rather than half-built here.
--
-- Nothing is dropped or altered: this is one additive, nullable column.
-- It inherits the table's existing grants and RLS (`projects_api_read`,
-- `projects_worker_all` from 20261016000000_platform_projects.sql), so the
-- api role can read the cap and the worker role can read and set it; no new
-- policy or grant is needed, and deliberately no `authenticated` write path
-- exists -- a project raising its own ceiling is not a feature.

alter table codeaxis.projects
  add column monthly_cost_cap_usd numeric(12, 6)
    check (monthly_cost_cap_usd is null or monthly_cost_cap_usd >= 0);

comment on column codeaxis.projects.monthly_cost_cap_usd is
  'Per-project model spend ceiling in USD for the current calendar month (UTC). '
  'NULL = use the CODEAXIS_PROJECT_MONTHLY_COST_CAP_USD deployment default. '
  '0 = refuse all model calls for this project. Enforced in app/limits/budget.py '
  'against the codeaxis.llm_calls ledger, which is append-only.';
