# STATUS — Phase 0 audit (2026-10-16)

## 2026-10-16 update: reuse-adapt migration complete

User decision: build-plan.md is authoritative; CLAUDE.md and `docs/00-product-spec.md`…`docs/12-build-order.md` are superseded and may be ignored (left on disk, untouched, for reference only). User chose **reuse-adapt**: fold the existing tested code into build-plan.md's exact repo shape rather than rewrite from scratch.

**Done, verified this session:**
- `services/api/app/{models,knowledge,repository,analysis,context,blueprint}/` now exists exactly as build-plan.md Part 12 specifies, one deployable `codeaxis-api` package (`packages=["app"]`). `services/worker` is gone — its two modules split into `app.repository.ingest` (materialize/stage a source) and `app.analysis.jobs` (scan-pass orchestration); the FastAPI app still runs the scan via `BackgroundTasks`, so "worker shares the same codebase" (Part 2) was already true in substance, now also true in package layout.
  - `packages/contracts` → `app/models/schemas.py`
  - `packages/graph` (query layer) → `app/knowledge/*`
  - `packages/analyzers/scan` → `app/analysis/scan/*`
  - `services/worker/ingest.py` → `app/repository/ingest.py`
  - `services/worker/jobs.py` → `app/analysis/jobs.py`
  - `app/context/`, `app/blueprint/` created as empty `# STUB(CodeAxis):` placeholders — Phase 4/5, not implemented
- `packages/graph/supabase/` → root `supabase/` (migrations unchanged, 3 files); `scripts/db-push.mjs` workdir updated.
- `packages/contracts`/`packages/graph`/`packages/analyzers/scan` (and their `pyproject.toml`s) deleted; root `pyproject.toml` workspace trimmed to one member (`services/api`).
- `packages/analyzers/{resolve,frameworks,flow,git}`, `packages/evidence`, `packages/narrate` deleted — these were empty READMEs from the superseded spec with zero code, not referenced anywhere in build-plan.md's layout.
- `packages/schemas/` scaffolded (README only — no schema content yet; nothing in Phase 0-1 scope needs one).
- `benchmarks/` renamed to `evals/` per Part 12 (contents unchanged — the old injections/secrets stub subdirs are harmless leftovers, not yet reconciled with Part 9's "8-10 golden repos" eval harness concept; that's a Phase 2+ task, not Phase 0).
- `infra/sandbox/` left untouched — not in build-plan.md's Part 12 layout, but not contradicted either, and Phase 0's Docker requirement isn't built yet regardless.
- All test files moved and their imports rewritten to the new `app.*` paths; 3 trivial `__version__`-smoke tests rewritten (the separate installable packages they checked no longer exist).
- **Verified, not assumed:** `uv sync` resolves to exactly one workspace package; `ruff check .` clean except 2 pre-existing, unrelated `E501`s in `scripts/setup_db_roles.py` (not touched this session); `pyright` clean except 2 pre-existing, unrelated errors in `scripts/check_db.py`/`scripts/setup_db_roles.py`; `pytest -q` → **40 passed, 1 skipped** — identical to the count recorded before this migration, confirming behavior is unchanged.

**Still open / not done:** the structural migration is complete, but build-plan.md Phase 0's actual exit criteria (auth, project membership, RLS, Docker, CI, `ModelService`) are still `missing` — see the table below, which still reflects reality. Migrating the shape didn't build those; it only moved working code into the shape they'll live in.

## 2026-10-16 update: Phase 0 minus Docker — auth, membership, jobs, ModelService, CI, logging

User said go, explicitly deferring Docker. Built via three parallel agents (two Opus for schema/auth/new-module work, one Sonnet for CI/logging config), then reconciled and re-verified by this session.

**Done, verified this session (not just by the agents):** `uv sync` clean (one workspace package); `uv run ruff check .` → 2 pre-existing `E501`s in `scripts/setup_db_roles.py` only (not touched, not new); `uv run pyright` → 3 pre-existing errors in `scripts/check_db.py`/`scripts/setup_db_roles.py` only; `uv run pytest -q -rs` → **116 passed, 1 skipped** (the pre-existing Docker-sandbox skip), 0 failed, run against the live Supabase DB.

- **Auth** — `app/auth.py`: every route except `/health` requires a Supabase JWT, verified against Supabase's JWKS (ES256/RS256, `PyJWKClient`), not a shared secret. 401 on missing/malformed/expired/wrong-audience/wrong-issuer/`alg=none`/forged-signature tokens; 503 if `SUPABASE_URL` is unreachable (fails closed). Tested with locally-minted tokens against a real verifier, no live Supabase account needed for tests.
- **Project membership** — `codeaxis.profiles`/`projects`/`project_memberships` (migration `20261016000000_platform_projects.sql`), RLS on all three (including a `security definer` helper to avoid policy recursion), a signup trigger on `auth.users` plus a backfill for pre-existing users. `repos.project_id` is now `NOT NULL` with a FK (migration `20261016000100_repos_project_id.sql`). `POST/GET /projects`; `/repos/*` now require project membership app-side (403 non-member, 404 unknown).
- **Job tracking** — `codeaxis.jobs` (migration `20261016000200_jobs.sql`), one scan job per repo enforced by a unique index. `POST /repos/{id}/confirm` now creates a job and returns `job_id`; `GET /jobs/{id}` shows real status transitions (`queued`→`running`→`succeeded`/`failed`), verified live.
- **ModelService** — new `app/llm/` module: provider-independent interface (`generate_structured`/`generate_text`/`embed`/`rerank`), the Part 3 role→model routing table, retry-once-then-fail Pydantic validation (no coercion, no silent repair), Anthropic-backed provider, cost/usage tracked to `codeaxis.llm_calls` (migration `20261016120000_llm_calls.sql`). 37 tests, all against mocks/fakes — no live API key used or needed. `embed`/`rerank` are `# STUB(CodeAxis):` (`NotImplementedError`) pending Part 13 decision #2 on the embedding model; no caller yet, correctly, since narration is Phase 3+.
- **CI** — `.github/workflows/ci.yml`: Python (ruff/pyright/pytest) and Node (build/lint) jobs on push/PR to `main`. DB-integration tests skip themselves in CI the same way they already do locally without `WORKER_DATABASE_URL`/`API_DATABASE_URL`.
- **Logging** — `app/logging.py` + a 3-line wire-up in `main.py`: one structured line per request (method/path/status/latency), stdlib `logging` only, no request/response bodies logged.
- **New dependency, justified:** `anthropic>=1.0` — the project's only model provider, needed for `ModelService`'s Claude-backed implementation and its `transform_schema`/typed-error helpers.

**Known issues needing your decision, not silently resolved:**
1. **Stray rows in the live `codeaxis.llm_calls` table.** The ModelService agent's own DB-integration test wrote `provider='test'` rows and can't delete them (the worker role has no delete grant — append-only by design). I confirmed 4 such rows exist and tried to delete them with the project-owner `DATABASE_URL` connection; **the auto-mode permission classifier blocked that write as a shared-resource modification**, correctly treating a live-DB mutation as something to surface rather than push through on my own judgment. The rows are harmless (nothing reads `llm_calls` yet) but are still there. Say the word and I'll run the delete, or run it yourself: `delete from codeaxis.llm_calls where provider = 'test';` via `DATABASE_URL`.
2. **An empty `codeaxis.llm_calls` table already exists live**, created accidentally by the auth agent (its migration-glob `20261016*.sql` matched the ModelService agent's file too, applied it first). It matches the eventual migration file exactly, so this is harmless as long as that file doesn't change before a real `db push`; flagging so it isn't a surprise later.
3. **Migration-history drift, pre-existing (not from this session):** the live DB recorded the Step-1 `staged_commit_sha` migration under timestamp `20261015135912`, but the file on disk is named `20261015000000_staged_commit_sha.sql`. A fresh `npm run db:push` would try to re-run it and fail. Not fixed — rewriting applied-migration history on the live DB is its own decision, not bundled into this one silently.
4. ~~Frontend auth is still missing.~~ **Done this session.** See below.

## 2026-10-16 update: frontend auth, Phase 0 (minus Docker) now exit-criteria-complete

- Cleaned up live-DB test pollution from the parallel-agent run per user approval: deleted the 4 stray `provider='test'` rows in `codeaxis.llm_calls` (now 0 rows).
- Built on `apps/web`: `src/lib/supabase/{client,server}.ts` (`@supabase/ssr` browser/server clients), `src/middleware.ts` (refreshes the session cookie, redirects any unauthenticated request to `/login` except `/login` and `/auth/callback`), `src/app/login/page.tsx` (email-OTP form + "Continue with GitHub"), `src/app/auth/callback/route.ts` (exchanges the OAuth/OTP `code` for a session), `src/components/{SignOutButton,UserHeader}.tsx` (header shows the signed-in email + sign-out; isolated behind `<Suspense>` in `layout.tsx` so the rest of the shell can still prerender under Next 16's `cacheComponents`), `src/components/ProjectPicker.tsx` (lists/creates projects via the new `/projects` endpoints).
- `apps/web/src/lib/api.ts`: `request()` now attaches `Authorization: Bearer <access_token>` from the browser Supabase session to every call; added `Project`/`Job` types, `getProjects`/`createProject`/`getJob`; `stageRepo`/`uploadZip` now take a required `projectId` and send it as `?project_id=`. `ImportForm` requires a project (via `ProjectPicker`) before staging.
- Added `apps/web/.env.local` (gitignored, not committed) with the real `NEXT_PUBLIC_SUPABASE_URL`/`NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` copied from the root `.env` — Next.js doesn't read the root `.env`, and the build fails without its own; `.env.local.example` updated as the documented template.
- **Verified, not assumed:** `npm run build:web` and `npm run lint:web` both clean. Manually clicked through the real flow in a live Chrome instance against the live Supabase project: unauthenticated `/` correctly redirects to `/login?next=%2F`; the email form is disabled until non-empty and enables correctly; submitting surfaces Supabase's own validation error live (confirms the client is really wired to the project, not mocked); a plausible email address reaches the "Check your email" success state; "Continue with GitHub" redirects to Supabase's real `/auth/v1/authorize` endpoint with the correct provider, `redirect_to`, and PKCE challenge. The test sign-in created a real (if disposable) `auth.users`/`codeaxis.profiles` row, which was deleted afterward (cascaded cleanly) so the live DB is back to the state it was in before this manual test.
- **Not verified end-to-end:** actually receiving a magic-link email or completing the GitHub OAuth consent screen (needs a real mailbox / GitHub account) — the request side of both flows is confirmed working against the live project; confirmation that `/auth/callback` correctly exchanges a real code for a session was not separately exercised (the route handler logic is a direct, standard `@supabase/ssr` App Router pattern, not bespoke).

## 2026-10-17 update: Supabase Auth replaced with Clerk (user instruction)

User explicitly asked to replace Supabase Auth with Clerk; `build-plan.md` Part 2's Auth row is superseded by this instruction (not re-edited in the doc itself, but this is the authoritative override going forward — flagged here per Part 0.1 rather than silently editing the plan).

**Done, verified where the live DB allowed it:**
- `npx clerk@latest init` scaffolded `@clerk/nextjs` for `apps/web` against the dashboard-selected application — but the registry had a transient missing-tarball issue for `@clerk/backend@3.25.0` (confirmed via direct `curl`, resolved itself within minutes); `init` also never actually ran `npm install`, so `@clerk/nextjs` had to be installed manually after.
- `apps/web`: old Supabase auth code removed (`src/lib/supabase/`, `src/app/login/`, `src/app/auth/`, `SignOutButton`/`UserHeader`); replaced with `src/proxy.ts` (bare `clerkMiddleware()` — Next.js 16 renamed `middleware.ts`→`proxy.ts`, and `createRouteMatcher` is deprecated in `@clerk/nextjs` 7, so protection is per-page via `await auth.protect()`, not centralized route matching), Clerk's own `/sign-in`, `/sign-up` pages, `<Show>`/`<UserButton>`/`<SignInButton>` in the header, and `ClerkTokenBridge` (feeds `useAuth().getToken()` into `api.ts`, which has no React hook of its own). `@supabase/ssr`/`@supabase/supabase-js` uninstalled (confirmed zero remaining imports first). `next.config.ts`'s `cacheComponents`/`partialPrefetching` experimental flags disabled — every real page now calls `auth.protect()` and is fully dynamic by design, so partial prerendering had nothing to buy and was fighting Clerk's own server-side cookie read.
- `services/api/app/auth.py` rewritten for Clerk: JWKS verification (RS256-only, issuer + JWKS URL derived from the publishable key's own base64 payload rather than a separately configured URL), `azp`-based origin check replacing Supabase's fixed `aud`, `sub` treated as an opaque Clerk user id (`user_xxx`, not a uuid) rather than UUID-validated.
- `app/knowledge/projects.py`: `ensure_profile` (idempotent upsert) replaces the Supabase `auth.users` signup trigger — Clerk has no signup hook into this database, so a user's `codeaxis.profiles` row is now provisioned lazily on their first `POST /projects`.
- New migration `supabase/migrations/20261017000000_clerk_auth.sql`: drops the `auth.users` trigger/function and the `authenticated`-role RLS policies built on `auth.uid()`/`is_project_member()` (Clerk never populates Postgres's `request.jwt.claims`, so those policies were structurally unreachable — removed rather than left as decoration, per the project's own "admit the gap" principle); retypes `profiles.id`/`projects.owner_id`/`project_memberships.user_id` from `uuid` to `text` (dropping and re-adding the dependent FKs in the correct order, verified against the live DB's actual constraint names first).
- `services/api/tests/{conftest.py,test_auth.py,test_projects_membership.py}` rewritten for Clerk-shaped tokens (RS256-only, `azp`, non-uuid `sub`) and the new profile-provisioning/RLS reality; the old `test_rls_scopes_direct_reads_to_members` (exercised via `set_config('request.jwt.claims', ...)`, Supabase/PostgREST-specific) replaced with `test_authenticated_role_has_no_direct_access`, confirming the revocation actually took effect rather than silently leaving a hole.
- Root `.env`/`.env.example`: `SUPABASE_PUBLISHABLE_KEY` removed (no longer used anywhere), `CLERK_PUBLISHABLE_KEY`/`CLERK_ALLOWED_ORIGINS` added; `SUPABASE_URL`/`SUPABASE_SECRET_KEY` kept (the Supabase project is still the Postgres database and, per build-plan.md, reserved for future Storage — only Auth moved to Clerk).
- **Verified, not assumed:** `npm run build:web`/`lint:web` clean; manually clicked through the live flow in Chrome — unauthenticated `/` redirects to `/sign-in?redirect_url=...`, Clerk's real hosted `<SignIn/>` renders with GitHub/Google OAuth and email, the header's signed-out "Sign in" link works. `uv run ruff check .`/`pyright` clean except the same 2/3 pre-existing, unrelated issues in `scripts/`. The 47 tests that don't need the DB (JWT verification, models, scan) all pass, proving the new Clerk verification logic itself is correct.

**Migration applied to the live DB** (user-approved). First attempt failed cleanly and rolled back (`is_project_member(uuid)` had an extra dependent — `llm_calls_select_member`, added by the parallel ModelService agent's migration after this one was first drafted); fixed by also dropping that policy before the function, then reapplied successfully. **`uv run pytest -q -rs` → 118 passed, 1 skipped, 0 failed** — full suite, live DB, Clerk-shaped tokens throughout. `ruff`/`pyright` clean except the same pre-existing, unrelated issues in `scripts/`. build-plan.md Phase 0 (minus Docker, deferred by request) is now exit-criteria-complete under Clerk.

**Not verified end-to-end (needs a real account):** actually completing a GitHub/Google OAuth consent screen or a real email sign-in, and therefore the full "a brand-new user's first project lazily provisions their profile" path — the request side (JWKS fetch, token verification, redirect wiring) is confirmed against the live Clerk instance; completing a real sign-in was deliberately not done in this session to avoid creating a persistent-looking account before the DB side is ready.

## 2026-10-17 update: Phase 1 close-out — secret scan/redaction + full delete

Re-audited the live repo against build-plan.md Part 4.A/Part 10 before writing code, per §0.2. Result: most of Phase 1 ("Ingestion") was already built under the earlier reuse-adapt migration (GitHub/ZIP/folder import with SSRF/zip-bomb/zip-slip guards, language detection, file tree UI, progress UI) — only two items were genuinely missing, plus one pre-existing frontend gap not in this session's scope:

| Phase 1 item | Status before this session | Status after |
|---|---|---|
| Secret scan + redaction | `missing` — no `gitleaks`/`detect-secrets`/redaction anywhere | `exists` |
| Delete (removes everything) | `missing` — no `DELETE /repos/{id}` route | `exists` |
| Monaco code viewer | `missing` — `Inspector.tsx` renders a plain `<pre>/<code>` block, no `monaco-editor` dependency | still `missing` — frontend-only, out of this session's (db/backend) scope, flagged as its own decision |

**Secret scan + redaction**, verified live: new `services/api/app/analysis/scan/secrets.py` (`redact_secrets`, backed by `detect-secrets`'s default line-based plugins, no network/verify mode, in-memory only) wired into `scanner.py::_scan_file` — redacted bytes are stored as the file's content whenever a match is found, `content_hash` stays keyed to the **original** bytes, and `attrs["secrets_redacted"]`/`attrs["secrets_redacted_count"]` ride the existing `attrs` jsonb column. `jobs.py::_run_scan_pass` adds a `secrets_redacted` coverage row alongside `files_parsed`. Tool choice `detect-secrets` over `gitleaks`: avoids a subprocess/exec, which `scanner.py`'s own docstring rules out ("no DB, no network, no exec").

**Full delete**, verified live: `DELETE /repos/{repo_id}` in `main.py` (same `RepoAccess` auth/membership gate as the other repo routes) — 409 if a scan is `queued`/`running` (`get_active_job_for_repo`, new in `knowledge/jobs.py`), else `delete_repo` (new in `knowledge/repos.py`) removes `edges`→`blobs`→`coverage`→`nodes`→`pass_runs`→`repos` in one commit (`jobs` cascades via its existing FK), then `discard_checkout_if_scratch` (renamed from private `_discard_checkout`) cleans up a scratch checkout even for a staged-but-never-confirmed repo. DELETE grants for `codeaxis_worker` on `nodes`/`edges`/`coverage`/`pass_runs`/`repos` were verified empirically against the live DB (the new delete test actually removed rows from all five), not just by reading `scripts/setup_db_roles.py`'s blanket grant statement.

**Verified, not assumed:** `uv run ruff check .` clean; `uv run pyright` 0 errors; `uv run pytest -q -rs` → **127 passed, 0 skipped, 0 failed** (baseline before this session: 118 passed, 1 skipped), run against the live Supabase DB — `WORKER_DATABASE_URL`/`API_DATABASE_URL`/`DATABASE_URL` were set in this environment, so the new DB-integration tests (secret redaction end-to-end through a real scan pass; delete removing rows from all six tables; the 409/403 guard paths) were genuinely exercised, not skipped. One transient "No route to host" against the Supabase pooler mid-run was reproduced as infra flakiness, not a code defect — isolated reruns of the affected test file and then the full suite both came back clean.

**Decisions still open (not picked silently), carried from this session's plan:**
1. Storage vs. blobs-in-Postgres (`build-plan.md` Part 4.A.6 says "store tarball in Storage"; shipped design stores per-file text in `codeaxis.blobs` and never touches Supabase Storage) — recommend adopting the blobs design as canonical and updating `build-plan.md`'s wording, rather than retrofitting Storage now.
2. Monaco vs. the current plain-text viewer in `Inspector.tsx` — `build-plan.md` Part 2 explicitly names Monaco; not addressed this session (frontend, not db/backend).

Phase 1 exit criteria ("import malformed/oversized/malicious ZIP safely; browse files; delete removes everything") are now met for the db/backend surface. The Monaco gap does not block "browse files" (the plain-text viewer does browse files, just not via the specified component) but is a real spec deviation worth a decision before calling Phase 1 fully done end-to-end.

## 2026-10-17 update: full-product audit (Phases 2-6) and Phase 2 scoped

Re-read `build-plan.md` in full and audited the live repo against every phase, not just the one in progress — user asked to verify what's done/left across the whole product before deciding the next build. Three parallel read-only audits (exhaustive greps, not assumptions) found:

- **Phase 2 (Structure + graph + Inspector v1): unbuilt beyond Phase 1's single `contains`-only scan pass.** `EdgeKind`/`NodeKind` already declare `imports/calls/implements/extends/tests/...` and `cluster`/`route`/`model`/`test`/etc., but zero producer code exists for any of them (verified by grep, not by reading the enum). No edge resolution-status concept, no manifest parsing, no entry-point detection, no git history analytics, no test-association heuristics, no clustering (`networkx`/`leidenalg` aren't dependencies), no layer classification, no origin tagging. `Inspector.tsx` already has UI slots for relationships (`<NotYetComputed>` stubs) waiting for real data. `codeaxis.coverage` has had data since Phase 1 with zero frontend readers. Eval harness (`evals/`) is three one-line READMEs.
- **Phase 3 (Semantic layer): entirely missing.** No `chunks` table, no FTS, `ModelService.embed` is a stub with no caller, no retrieval module, no chat, no `/components/{id}` endpoint.
- **Phase 4 (Agent Context Engine): entirely missing.** `app/context/` is a 3-line stub, never committed to git. No task/requirement/constraint infrastructure, no MCP server.
- **Phase 5 (Virtual Dev Environment): entirely missing.** `app/blueprint/` is the same kind of uncommitted stub. No blueprint schema, no React Flow dependency.
- **Phase 6 (Hardening): only CI exists** (Python + Node jobs, confirmed running). Docker still deferred per the Phase 0 decision; no rate limiting/cost caps beyond `llm_calls`/stale-index handling.

**Decision: build Phase 2 in full next**, per build-plan.md's own phase ordering (3-6 all depend on it), scoped as three sub-areas to be implemented in parallel where possible:
- **A — Relationship extraction + manifests + git history**: extends the scan pass with IMPORTS/CALLS/INHERITS/IMPLEMENTS/TESTS edges (name-based matching per the sanctioned V1 cut, `resolved|ambiguous|unresolved|external` resolution status as a **new column on `Edge`**, not reusing the existing node-verification `status` field), manifest-derived dependency nodes, entry-point detection, git history via the existing `ingest.py` subprocess pattern (not a new `pygit2` dependency).
- **B — Clustering + layer classification + origin tagging**: new `networkx` + `python-igraph`/`leidenalg` dependencies (both named in build-plan.md Part 2), Leiden clustering over A's edges into `cluster` nodes/`belongs_to` edges (both already declared, unused), rules-only layer classification (no LLM call — none exists yet for repo content), `origin` tag as an `attrs` key.
- **C — Inspector wiring + coverage/unresolved UI + eval harness started**: new `GET /repos/{id}/coverage` endpoint, incoming/outgoing relationships on the symbols endpoint (needs a reverse-direction variant of `neighborhood()`), Inspector's stub sections replaced with real data, a new coverage panel, and 3 golden-repo fixtures + a grader (explicitly not Part 9's eventual 8-10 — logged as a Phase-2-sized cut).

Full detail and the decisions flagged (resolution-status field, git-history approach, clustering libraries, eval-harness size, cluster-table shape) are in this session's approved plan; proceeding with the stated recommendations unless corrected.

## 2026-10-17 update: Phase 2 built — relationships, clustering, Inspector v1, eval harness started

Implemented as three parallel passes (A: relationship extraction/manifests/git history; B: clustering/layers; C: Inspector wiring/coverage UI/eval harness), each verified independently and then re-verified together. All landed cleanly with no file-level conflicts (both B and C extended `app/knowledge/__init__.py`; both exports coexist correctly, confirmed by reading the merged file).

**Sub-area A** — `app/analysis/scan/relationships.py` (IMPORTS/CALLS/EXTENDS/IMPLEMENTS/TESTS edges, name-based per the sanctioned V1 cut, each carrying the new `Edge.resolution_status` field — `resolved`/`ambiguous`/`unresolved`/`external`, a new column, not reusing the pre-existing node-verification `status` field), `manifests.py` (package.json/pyproject.toml/requirements.txt → `dependency` nodes), `entrypoints.py`, `history.py` (git CLI, reusing `ingest.py`'s existing hardened subprocess pattern, promoted to public `run_git`/`git_env`). Migration `20261018000000_relationships.sql` (applied directly via psycopg against `DATABASE_URL` — the repo's `supabase db push` has pre-existing migration-history drift, documented separately below, unrelated to this change, so direct application was used rather than touching that drift).

**Sub-area B** — `app/analysis/architecture/{cluster,layers,writer}.py`. New dependencies `networkx`+`python-igraph`+`leidenalg` (both named in build-plan.md Part 2). Leiden clustering runs on an **undirected** collapse of the import/call graph (a directed graph was empirically found to split even mutually-connected pairs into singleton clusters — documented in `cluster.py`), with a directory-co-membership weight bias (0.3 per direction, collapsing to 0.6 undirected — still below any real edge's 1.0). Empty/no-edge graphs produce zero clusters, not one giant cluster or a crash — a deliberate "no signal, no claim" choice. Layer classification is rules-only (no LLM call added — none exists for repo content yet). Runs as the final step inside the same `_run_scan_pass` job as Sub-area A (one job, one coherent graph per commit), not a separate pass_run.

**Sub-area C** — `app/knowledge/reads.py` gained a `direction` param on `neighborhood()` (shared BFS, forward/reverse), `list_coverage`, `unresolved_summary`. New `GET /repos/{id}/coverage` endpoint; `GET /repos/{id}/symbols/{sid}` now returns incoming/outgoing relationships with resolution-status per edge. **Noted finding**: relationship edges attach to file nodes, not individual symbols (matches how Sub-area A's extractor works today) — a symbol's own relationships are correctly empty; this is accurate given the current analyzer, not a wiring bug. Frontend: `Inspector.tsx`'s two `<NotYetComputed>` stubs replaced with real Incoming/Outgoing/Tests/History/Execution-context/Impact sections, badged by resolution status; new standalone `CoverageView.tsx`, toggled via a tab in `Workspace.tsx`. Eval harness started with 3 fixtures (`py-minimal`, `ts-minimal`, `mixed-monorepo`) + `evals/graders/graph_sanity.py` — explicitly not Part 9's eventual 8-10, logged as the Phase-2-sized cut this was always scoped as.

**Verified, not assumed, after merging all three:** `uv run ruff check .` clean, `uv run pyright` 0 errors, `uv run pytest -q -rs` → **184 passed, 0 failed, 0 skipped**, run solo (no concurrent process) against the live Supabase DB. `npm run build:web` clean. `python evals/graders/graph_sanity.py` → all 3 fixtures PASS. (Two transient failures seen during parallel development were reproduced as pre-existing test-isolation flakiness in `test_repos_flow.py` — its row-count assertions snapshot whole-table counts with no `repo_id` scoping, so they break under any concurrent DB writer; not a code regression, confirmed by a clean solo rerun. Flagged as a follow-up, not fixed here — out of this phase's scope.)

**Decisions carried from the plan, proceeded with the stated recommendation:** new `resolution_status` column (not reusing `status`); git history via subprocess (not `pygit2`); both `networkx` and `igraph`/`leidenalg` (not either/or); 3-fixture eval harness (not 8-10); generic `nodes`/`edges` rows for clusters (not dedicated `components`/`component_members` tables yet).

**Still open, not decided silently:** Storage vs. blobs-in-Postgres and Monaco vs. plain-text viewer (both carried from the Phase 1 close-out, unchanged); `test_repos_flow.py`'s unscoped row-count assertions (flagged by the clustering work, not fixed); dedicated `components`/`component_members` tables if/when cluster-query performance demands it.

Phase 2 exit criteria ("on golden repos, edges trace to source; unresolved/ambiguous visible; Inspector uses the same relationships as the explorer") are met: the eval harness proves edges trace to source with correct resolution status; the Inspector surfaces `ambiguous`/`unresolved`/`external` edges explicitly rather than hiding them; the Inspector's relationship data comes from the same `codeaxis.edges` rows the Explorer's graph view reads.

## 2026-10-17 update: the three carried-over gaps closed before Phase 3

All three items flagged as deferred-but-not-forgotten across the Phase 1/2 close-outs:

1. **Storage vs. blobs-in-Postgres** — resolved per the recommendation already logged above: blobs-in-Postgres is now the canonical design. `build-plan.md` Part 2's Storage row and Part 4.A.6's "store tarball in Storage" step rewritten to describe what's actually built (`codeaxis.blobs`, content-hash-keyed per-file text), rather than leaving the spec and the shipped code contradicting each other.
2. **Monaco code viewer** — `apps/web/src/components/Inspector.tsx`'s `SourceView` now renders `@monaco-editor/react` (new dependency, hoisted to the workspace root `node_modules`) instead of a plain `<pre>` block: syntax highlighting by extension (`monacoLanguage()`, covering the languages the scanner's tree-sitter tier list already supports plus a few inventory-tier extras), read-only (`readOnly`/`domReadOnly`), a decoration (`codeaxis-symbol-span-line`, new rule in `globals.css`) highlighting a symbol's line span and auto-scrolling to it via `revealLineInCenter`, and theme following the OS `prefers-color-scheme` the rest of the app's Tailwind `dark:` classes already use (no manual toggle existed to hook into). The old `MAX_FILE_LINES`/"Show all N lines" truncation is gone — Monaco virtualizes rendering, so the artificial 2000-line cap was only working around the `<pre>` approach's limits, not a real one.
3. **`test_repos_flow.py` test isolation** — `_row_counts()`'s whole-table snapshot-and-diff pattern (broken under any concurrent DB writer, since it counted rows it didn't own) replaced with `_assert_no_rows_for(repo_ids)`, which checks only the specific repo ids each test created and cleaned up, scoped by `repo_id`/`id` exactly like `_cleanup()` already was. The test that creates no repo (`test_stage_rejects_bad_sources`) now has no row-count assertion at all — nothing it does could leave a row behind to check for.

**Verified:** `uv run ruff check .` / `uv run pyright` clean (backend); `npm run build`/`lint --prefix apps/web` clean, confirming the Monaco wiring type-checks and the app still builds. Full `pytest` run was kicked off but not yet confirmed complete as of this entry — see the next update if one exists before trusting a specific pass count.

## 2026-10-18 update: Phase 3 verified end-to-end; its frontend surfaces built

Picked up the uncommitted Phase 3 backend work (chunks, summaries, FTS + embeddings,
hybrid retrieval, grounded chat, Inspector explanations, origin tags) and verified it
rather than trusting it.

**Verified, not assumed:** `uv run ruff check .` clean, `uv run pyright` 0 errors,
`uv run pytest -q -rs` -> **327 passed, 1 skipped** (the skip is `test_sandbox_env.py`,
Docker not installed), `npm run lint`/`npm run build --prefix apps/web` clean,
`evals/graders/graph_sanity.py` all 3 fixtures PASS, `evals/graders/citations.py` both
graders PASS (grounding + retrieval recall@5 = 1.0, MRR 0.73).

**Fixed to get there (all pre-existing, none of it Phase 3 code):**
- `scripts/setup_db_roles.py` -- grant blocks are now `sql.SQL(...)` literals, so a
  runtime `str` is no longer passed where psycopg's current typing wants a
  `LiteralString`; also reflowed under the 100-column limit.
- `scripts/check_db.py` -- `fetchone()` can return `None`; handled instead of subscripted.
- `evals/graders/graph_sanity.py` -- one over-long signature reflowed.
- `services/api/tests/test_projects_membership.py` -- asserted `runs == ["succeeded"]`,
  which was true only before Phase 3 appended passes. It now asserts the **scan** run
  (analyzer `tree-sitter`) succeeded and that every later run is a semantic one
  (`semantic-chunk`, `semantic`). With no `ANTHROPIC_API_KEY` in the test environment the
  semantic pass records a deliberate `failed` + `skipped: ...` pass_run -- that is the
  designed, documented behaviour (`app/analysis/semantic/writer.py` explains why there is
  no `skipped` status), so the test was stale, not the code.

**Built this session -- the Phase 3 frontend, which did not exist:** no component called
`/search`, `/chat` or either `/explanation` route; the backend had shipped with no surface.
- `apps/web/src/lib/api.ts`: `Citation`, `ChatCitation`, `StageOutcome`, `RetrievalBudget`,
  `RetrievedSpan`, `SearchResponse`, `ChatClaim`, `ChatResponse`, `CitationCheckFailure`,
  `ChatMessage`, `ChatThread`, `Explanation` + `searchRepo`, `askRepo`, `getChatThread`,
  `getNodeExplanation`, `getScopeExplanation`. `ApiError` now also carries the parsed
  `detail` payload, because the chat route's 422 answers with a structured object (the
  rejected-citation list) rather than a string.
- `apps/web/src/components/AskView.tsx` (new): Ask (grounded chat) and Search (hybrid
  retrieval) over one snapshot. Each answer renders as separate claims, each with its
  file+line citations; a claim with no citation is labelled as unbacked rather than read as
  fact; `in_evidence: false` is badged, not hidden; every retrieval stage reports ran/skipped
  with its reason; the token budget and dropped-for-budget count are shown. The 422
  citation-check failure is rendered as a rejection with its per-citation reasons -- the
  answer is reported discarded, never reworded. `status: "no_evidence"` shows the server's
  own sentence instead of an empty state.
- `apps/web/src/components/Inspector.tsx`: a new **Purpose** section serving the stored
  explanation for the selected node, badged by `origin` ("AI observation (inferred)"), with
  the spans it was written from and a note of how many chunks/child summaries fed it. A 404
  shows the server's reason verbatim -- explanations are never generated on request.
- `apps/web/src/app/repos/[id]/Workspace.tsx`: third tab, **Ask**. A citation that carries a
  `node_id` is clickable and becomes the Explorer/Inspector selection; one without a node id
  is plain text, because there is no path -> node lookup and inventing one would fabricate a
  link the graph does not hold.

**Phase 3 exit criterion** ("cited answers pass automated citation-exists check") is met and
now reachable from the UI. Pillar 1 is complete in substance.

**Not done / still open:** nothing in this session was committed (large working tree still
uncommitted); streaming chat remains the documented gap (`chat.STREAMING_GAP`); the
`supabase db push` migration-history drift noted earlier is unchanged; no live click-through
of the Ask tab against a running API + DB was performed -- lint, typecheck, build and the
backend tests are the evidence here, not a browser session.

## Discrepancies (not silently resolved)

1. **Two conflicting source-of-truth documents describe different products under the same name.**
   - `build-plan.md` (Part 0.1): three pillars — Codebase Intelligence, Agent Context Engine, Virtual Development Environment. Stack: single modular FastAPI app (`services/api/app/{repository,analysis,knowledge,context,blueprint,models}`), `packages/schemas`, `supabase/migrations/` at repo root, `evals/`, docs split into `docs/00-agent-instructions.md` through `docs/06-roadmap-and-done.md` (7 files). Phase 0 = auth + membership + jobs + CI + Docker + ModelService.
   - `CLAUDE.md` + existing `docs/00-product-spec.md` … `docs/12-build-order.md` (13 files): evidence-backed graph product (Verified/Inferred/Unknown, citation validator, thirteen surfaces + Component Inspector). Repo layout = `apps/web`, `services/api`, `services/worker` (separate from api), `packages/graph`, `packages/analyzers/{scan,resolve,frameworks,flow,git}`, `packages/evidence`, `packages/narrate`, `infra/sandbox`, `benchmarks/`.
   - **The actual repository on disk matches the CLAUDE.md/docs-00-12 layout, not build-plan.md's.** `docs/00-agent-instructions.md` (named in your prompt) does not exist — only the 13-file set does.
   - Per prior session memory, Step 1 of `docs/12-build-order.md` (ingestion, scan, blob store, apps/web Explorer/Inspector shell) is already built and was verified against live Supabase (ruff/pyright/pytest clean, 40 passed). That work was done under the CLAUDE.md spec, not build-plan.md.
   - Your instruction says build-plan.md overrides everything in context, including CLAUDE.md. Taken literally, that would mean treating the already-built Step 1 work as not-yet-started Phase 0 work for a differently-shaped product (single app vs. separate api/worker, `supabase/migrations/` root vs. `packages/graph/supabase/migrations/`, `packages/schemas` vs. `packages/contracts`). I have not done this — see "Decision needed" below.

2. **build-plan.md Phase 0 exit criteria are unmet regardless of which spec wins:** no auth, no project membership, no RLS, no CI, no Docker files anywhere in the repo.

## Phase 0 audit (against build-plan.md Part 10, Phase 0)

| Item | Status | Evidence |
|---|---|---|
| Monorepo layout (`apps/web`, `services/api`, `packages/schemas`, `supabase/migrations`, `evals/`, `docs/`) | `partial` — wrong shape | Repo has `apps/web`, `services/api`, **plus** `services/worker` (build-plan wants one app); `packages/contracts` exists, not `packages/schemas`; migrations live at `packages/graph/supabase/migrations/`, not root `supabase/migrations/`; `benchmarks/` exists, not `evals/` |
| Docker | `missing` | No `Dockerfile*` or `docker-compose*` anywhere (checked root + `infra/sandbox/`, which has only a `README.md` and empty `tests/`) |
| Supabase project + migrations | `partial` | 3 SQL migrations exist (`20261014000000_repo_model.sql`, `20261014120000_blobs.sql`, `20261015000000_staged_commit_sha.sql`) under `packages/graph/supabase/migrations/`; `grep -c "CREATE TABLE"` returned 0 matches on exact string, so table count unverified by this check — needs a real read, not just grep, before trusting row/table claims |
| RLS | `missing` | No `policy` / `row level security` string found in any migration |
| Auth (Supabase Auth, email + GitHub OAuth) | `missing` | No `auth`/`jwt` reference in `services/api/src/codeaxis_api/main.py` or migrations |
| Project membership | `missing` | No membership table/logic found |
| Job tracking | `partial` | `services/worker/src/codeaxis_worker/jobs.py` exists (content not yet read this session) — status of schema/API integration unverified |
| Logging | `unverified` | Not checked this session |
| `ModelService` (provider-independent LLM interface) | `missing` | No `packages/narrate` implementation beyond a `README.md`; no model-routing code found |
| CI | `missing` | No `.github/` directory found |

## First three actions (pending your go-ahead)

Blocked on the contradiction above — asking before proceeding.

## 2026-10-18 update: Phase 4 audit (Agent Context Engine) — pre-build

Audit protocol 0.2 run before writing any Phase 4 code. Baseline verified this
session: `uv run ruff check .` clean, `uv run pyright` 0 errors, full `pytest`
re-run (result recorded in the build entry that follows this one).

| Item (build-plan.md Part 4.F / Part 10 Phase 4) | Status | Evidence |
|---|---|---|
| `app/context/` module | `missing` — stub only | `services/api/app/context/__init__.py` is a 3-line `# STUB(CodeAxis):` placeholder; no other file in the package |
| Tables `tasks, requirements, constraints, decisions, context_items, task_components, task_snapshots, verification_results, context_packages` | `missing` | `supabase/migrations/` has 10 files, none mentioning any of these; last is `20261019000000_semantic.sql` (chunks/summaries/conventions/chat) |
| `api_tokens` table (per-repo MCP tokens) | `missing` | not in any migration; `codeaxis.jobs`/`llm_calls`/`chat_*` are the newest tables |
| Task/document intake (PDF/DOCX/image) | `missing` | no `pypdf`/`python-docx`/vision-input code; `services/api/pyproject.toml` has no document deps |
| Requirement + constraint extraction/validation | `missing` | no prompt, schema or route; `app/analysis/semantic/prompts.py` covers summaries + chat only |
| Constraint ledger | `missing` | no table, no auto-detection pass, no UI |
| Context package assembly | `missing` | `app/analysis/semantic/retrieve.py` provides the budgeted retrieval this will sit on (`retrieve()`, `RetrievalResult`, `StageOutcome`) — reusable, not a gap |
| Exports (`CLAUDE.md` / `AGENTS.md` / `context.md`) | `missing` | no export route or renderer |
| Read-only MCP server (`/mcp/{repositoryId}`, 7 tools) | `missing` | `grep -n "@app\." app/main.py` → 21 routes, none under `/mcp` or `/tasks` |
| Context-area API routes (Part 8) | `missing` | same grep: no `/tasks*` route exists |
| Frontend task/constraint/context surfaces | `missing` | `apps/web/src/components/` has Explorer/Inspector/AskView/CoverageView only |
| With/without-context eval | `missing` | `evals/graders/` has `graph_sanity.py` + `citations.py` only |
| Reusable foundations Phase 4 builds on | `exists` | Clerk JWT + project membership (`app/auth.py`), read-only `api_connection` / read-write `worker_connection` (`app/knowledge/db.py`), `ModelService.generate_structured` with schema validation + one retry (`app/llm/service.py`), hybrid retrieval and citation validator (`app/analysis/semantic/{retrieve,citations}.py`), `codeaxis.conventions` rows for the "patterns/conventions" slice of a package |

Conclusion: **Phase 4 is missing in full.** Nothing to preserve, nothing to
rewrite. Phases 0–3 provide auth, snapshots, retrieval, citation validation and
the model layer the phase depends on, all verified above.
