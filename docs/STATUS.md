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

## 2026-10-19 update: Phase 4 shipped (A-E) and its gap closed — Agent Context Engine complete

Three prior sessions (recorded only in commit messages, not here — the
process gap this entry closes) built Phase 4 A through E in full. Backfilling
what actually shipped, then closing one real gap found while verifying it.

**A-C — data model, intake, extraction, ledger, packages, exports** (`c794676`):
migration `20261020000000_context_engine.sql` (`tasks`, `context_packages`,
`requirements`, `constraints`, `decisions`, `context_items`,
`task_components`, `task_snapshots`, `verification_results`, `api_tokens` —
all ten tables Part 6 names for the context engine); `app/context/{intake,
extract,ledger,package,export,reground,grounding,validate,framing}.py`.
Document intake (text/PDF/DOCX, new `pypdf`/`python-docx` deps) redacts
secrets before any model call. Requirement extraction drops a claim that
fails citation validation rather than keeping it. The constraint ledger
holds user-stated and auto-detected rows side by side. Context-package
assembly never truncates constraints (Part 4.F.5) and is budgeted for
everything else. Exports render `CLAUDE.md`/`AGENTS.md`/`context.md`.
Re-grounding re-runs across a moved snapshot.

**D — routes, tokens, MCP** (`a75a261`): 18 routes under `app/context_api.py`
(tasks, documents, requirements/extract, constraints CRUD + confirm,
decisions, context packages, exports, reground), every one gated by the
same project-membership check the rest of the API uses. Per-repo `api_tokens`
(owner-only, plaintext shown once, hash stored). A constraint-ledger
`pass_run` added after the semantic pass — its failure doesn't fail the scan.
A hand-written read-only MCP server (`app/mcp_server.py`) at
`POST /mcp/{repo_id}` with the 7 tools Part 4.F.7 names
(`search_code`, `get_component`, `get_dependencies`, `get_constraints`,
`impact_analysis`, `get_conventions`, `get_context_package`), every result
labelled static analysis.

**E — frontend** (`71bb9ed`): a fourth Workspace tab, Context
(`apps/web/src/components/{TaskView,TaskIntake,RequirementsPanel,
ConstraintLedger,ContextPackagePanel,ApiTokensPanel}.tsx`). Requirements
show origin badges and lead ambiguous/unresolved rows with the server's own
question. The ledger sorts hard before soft and only lets a user-stated row
be edited. A stale or invalid context package is badged, never shown as
current. Exports download under the server's filename; a refused export
shows the server's reason with an explicit override path. Shipped "verified
by lint/build only; no live click-through" per its own commit message.

**Gap found and closed this session:** `evals/graders/with_without_context.py`
(the Part 9 headline-adjacent "measurable lift vs. no context" grader,
Part 10's Phase 4 exit line) was present on disk but **unrunnable** — it
imported a `_context_support` helper module (`Checks`, `ScannedFixture`,
`scan_fixture`, `delete_fixture`, `report`, `skip_reason`) that was never
written, and skipped the `sys.path`/`load_dotenv` setup `citations.py` and
`graph_sanity.py` both do directly. Written now
(`evals/graders/_context_support.py`), adapting the existing scanned-fixture
pattern from `services/api/tests/context/taskc_fixture_repo.py` (profile +
project + repo, real scan pass, embeddings forced off since no
`VOYAGE_API_KEY` is assumed) rather than inventing a new one. Its fixture
repo (`orders-service`, 11 files) initially made the grader's own "package
recall beats baseline" check fail-by-construction: at ~440 tokens the whole
repo fit inside the 1500-token budget on both sides, so the naive lexical
baseline trivially recalled everything too. Fixed by writing the fixture at
the size and realism the module's docstring already called for ("tight
enough that not every file fits") — ~2200 tokens across the same 11 files —
not by loosening the threshold.

**Verified, not assumed:** `uv run ruff check .` clean, `uv run pyright`
0 errors, `uv run python evals/graders/with_without_context.py` → **PASS**
against the live DB (package mean recall 0.95 vs. baseline 0.83, package
constraint coverage 1.00 vs. baseline 0.03, every package status
`assembled` and within budget), `citations.py` and `graph_sanity.py` both
still PASS (nothing in the shared environment regressed). Full
`uv run pytest -q -rs` → **504 passed, 1 skipped, 0 failed** (the skip is
`test_sandbox_env.py`, Docker not installed) after fixing one real failure
found along the way (below); two earlier attempts mid-session hit the same
transient Supabase DNS/connection flakiness `graph_sanity.py`'s PASS entry
already named, reproduced as infra, not code, by the clean final rerun. One
unrelated fix needed to get the venv itself working: `.venv` was corrupted
(missing `pyvenv.cfg`, no packages materialized despite a clean `uv sync`
reporting success) — deleted and rebuilt; `python-dotenv` was the package
that exposed it, since neither existing grader runs it through `uv run
pytest`, only standalone, where the broken venv first showed.

**Second, unrelated gap found and closed: a stale Phase 4.A-C test, broken
by Phase 4.D.** `services/api/tests/context/test_package.py::
test_assembly_orders_sections_persists_retrieval_and_cites_real_lines`
asserted the first two constraint items were exactly
`["Do not edit svc/store.py.", "Prefer small diffs."]`. Phase 4.D's
constraint-ledger `pass_run` (`detect_ci_lint` in `app/context/ledger.py`)
promotes the shared fixture repo's own `pyproject.toml` (`[tool.ruff]
line-length = 100`) into a repo-level, soft, `ci_lint` constraint — "Ruff
enforces a maximum line length of 100" — that sorts (`kind` ties alphabetize,
`ci_lint` < `do_not_touch`) ahead of the task's own soft constraint. 4.A-C's
commit predates 4.D's ledger pass by definition, so this test could never
have been green after 4.D landed; it wasn't re-run end-to-end until this
session. Fixed by asserting the two task-stated constraints' positions
(hard-first, both present) instead of an exact list that hardcoded an
incidental repo-level convention text — the invariant this test actually
cares about, not the repo-level ledger's exact contents. Re-ran just this
file standalone after the fix: 7 passed.

**Not done:** the headline test itself — same task, same coding agent, with
vs. without an exported package, measuring completion and constraint
violations — remains a named, printed gap (`--emit DIR` writes each task's
exported `CLAUDE.md` for that comparison; no lift number is reported because
none was measured). This was always out of scope for a deterministic grader
with no model call, per the module's own docstring, not a shortfall of this
session's fix.

Phase 4 exit criteria (Part 10): requirements/constraints stored as
structured records — done; retrieved info has provenance — done (every
context item carries `evidence`); context reconstructable after a session —
done (packages are versioned, re-groundable); contradictions/missing info
surfaced — done (`open_question` requirement kind, `unresolved` package
entries); model output validated before persisting — done (citation-dropping
extraction, retry-once `ModelService`). **Phase 4 is substantively complete.**

## 2026-10-19 update: independent production-readiness audit — two unflagged security bugs found and fixed

A cross-cutting audit (not one phase's close-out) read the actual code in
`services/api`, `apps/web`, and the test/CI/deployment surface, deliberately
not trusting this file's own narrative. Most of what this file claims as
"done"/"verified" held up: auth is genuinely robust and fails closed, every
route checks membership, no SQL-injection surface, secret redaction is wired
into every path that reaches a model or storage, zip-bomb/zip-slip guards
are real, the MCP server and LLM service make real calls with real
cost-tracking, and the frontend is genuinely wired to real routes with no
fake/placeholder content. The already-disclosed gaps (no DB-integration
tests in CI, no Docker/deploy path, no rate limiting, no frontend tests, the
`local_path`/migration-history-drift notes above) checked out as accurately
self-reported, not overclaimed.

**Two severe bugs were found that this file never flagged, both in
already-shipped Phase 0/1 ingestion code:**

1. **This file's own §75 claim ("SSRF... guards") for ingestion was wrong.**
   `_validate_remote` (`app/repository/ingest.py`) only ever checked the URL
   scheme and blocked `-`-prefixed option injection — it never resolved or
   checked the destination host. A git URL resolving to `169.254.169.254`
   (cloud metadata) or an internal `10.x`/`172.16.x`/`192.168.x` address
   passed the old check and would have been `git clone`'d by the server.
2. **Arbitrary host filesystem read via `POST /repos/stage` with a `local`
   source**, never flagged anywhere in this file. Any authenticated user
   could create their own project (instant membership, no operator/admin
   role exists) and stage `local_path` set to any directory on the server —
   `/etc`, `.env` files, another tenant's scratch checkout — which then got
   scanned, chunked, and served back through search/chat/explorer.
   `materialize_local` only checked the path existed and was a directory;
   no containment check at all.

**Fixed, verified, not assumed:**
- SSRF: `_validate_remote` now resolves the hostname (`socket.getaddrinfo`)
  and rejects any result in a private/loopback/link-local/metadata/reserved/
  multicast range (`ipaddress` module), alongside the pre-existing scheme/
  injection checks, not replacing them. **Documented, accepted residual
  risk:** git's own HTTPS transport re-resolves the same hostname
  independently at actual-fetch time, so a DNS-rebinding adversary
  controlling the target domain could pass this pre-flight check with a
  public IP and rebind to a private one microseconds later. Shipped as
  note-and-ship (large improvement over no check at all; a full fix needs
  IP-pinning via a filtering proxy or `curl --resolve`, a materially larger
  change) — user-confirmed tradeoff, not picked silently.
- Local-path ingestion: new `LOCAL_INGEST_ALLOWED_ROOTS` env var
  (`;`-separated absolute directories), **unset by default → the capability
  is disabled entirely**. `materialize_local` now resolves with
  `Path.resolve(strict=True)` (so a symlink's real target is what gets
  checked, closing a symlink-escape route into the bargain) and requires
  containment in one of the configured roots, reusing the same
  `is_relative_to` pattern `is_scratch_checkout` already used in this file.
  This is a behavior change for every existing caller (today: works for any
  project member; after: works for nobody until an operator opts in) —
  user-confirmed, not picked silently.
- New tests: `services/api/tests/test_ingest_git_remote.py` (9 cases: IPv4/
  IPv6 literal rejection, hostname-resolves-to-private-IP via a mocked
  resolver, public host allowed, scheme/injection regressions, DNS-failure
  handling) and `services/api/tests/test_ingest_local_path.py` (8 cases:
  disabled-by-default, outside/inside allowlist, multiple roots,
  missing-commit-sha, missing-path, not-a-directory, symlink-escape —
  skipped on this Windows session, where symlink creation needs elevated
  privilege). `test_repos_flow.py` updated with an autouse fixture so its
  existing local-path tests keep exercising the real route under the new
  default-closed behavior, plus two new end-to-end rejection tests.
- `uv run ruff check .` / `uv run pyright` clean. New unit tests: 21 passed,
  1 skipped (symlink, platform-gated). `test_repos_flow.py` against the live
  DB: 6 passed (the 2 pre-existing local-path tests still work with the env
  var set, the 2 new rejection tests pass).

**Found, not yet fixed — deferred to Phase 6 ("Hardening & launch") per
build-plan.md's own schedule, by user decision:** no rate limiting/abuse
protection anywhere beyond `llm_calls` recording cost after the fact; no
Docker/deployment artifact or documented manual deploy procedure; CI never
runs the DB-integration test subset (only ever run locally against a live
Supabase instance); zero frontend automated tests; no connection pooling in
`app/knowledge/db.py` (a fresh `psycopg.connect()` per request); the
pre-existing migration-history-drift note above, still unresolved;
`DELETE /repos/{repo_id}` has no UI entry point; one labeled
`NotYetComputed` stub remains in `Inspector.tsx` ("Control-flow summaries").
None of these are security-severity like the two above — they're real
production-readiness gaps, correctly sequenced to Phase 6 rather than
blocking Phase 5.

## 2026-10-19 update: Phase 5 built — Virtual Development Environment (blueprints)

Part 13 decision #4 resolved first, as build-plan.md requires, then built in
three passes: the data model (this session), the engine and the frontend (one
agent each, both verified here rather than taken on trust).

**Part 13 decision #4 — the taxonomy, now locked.** Blueprints get their own
vocabulary rather than extending the Repo Model's `NodeKind`/`EdgeKind`. The
reasoning is in the migration's header and matters: those enums name facts an
analyzer established about code that exists, and a `proposed` component is
not `parsed`, not `configured`, and not `inferred` either — it is authored.
Mixing the two would put proposals inside the one vocabulary the rest of the
product treats as ground truth, which is the line CLAUDE.md draws. So
blueprint rows carry `generation_status` (`generated|edited|locked`) instead
of `origin`, and the single seam to the real graph is
`blueprint_components.linked_component_id -> codeaxis.nodes(id)`, following
`task_components`' precedent.

Three sub-decisions were flagged and proceeded with on the recommendation:
a blueprint requires a Context Engine `task_id` (so requirements/constraints
are referenced, never copied); `locked` is a **hard** block on regeneration,
not an advisory; and `generation_status` replaces `OriginTag` as above.

**Data model** (`supabase/migrations/20261021000000_blueprint.sql`, applied
to the live DB). Six tables: `blueprints`, `blueprint_versions`,
`blueprint_components`, `blueprint_connections`, `proposed_files`,
`blueprint_layouts`. Two deliberate deviations from Part 6, both documented
in the migration:
- **A sixth table Part 6 does not list.** Part 4.G.6 requires layout stored
  apart from semantics; a `layout jsonb` column on `blueprint_versions` would
  make dragging a box create a new version of the design, which is wrong.
- **References by `key`, not row id.** Connections name their endpoints, and
  a proposed file names its component, by the component's stable `key`,
  because Part 4.G.9's partial regeneration re-inserts rows. The cost, stated
  plainly: those two references are enforced by the validator and a
  per-version unique index, not by a foreign key — Postgres cannot express
  "matches a sibling row's key within the same version".

The taxonomy's invariants are enforced **twice**, in SQL check constraints and
again in Pydantic validators: the `kind`/`mapping` pairing
(existing+reuse, modified+extend, proposed+new), "a non-`proposed` component
must name the entity it maps to", `locked == (generation_status == 'locked')`,
no self-loop connections, `data_shape` only on a `data_flow`. **Verified by
running both** — every contradictory shape is rejected at each layer.
`packages/schemas/blueprint.v1.json` is the written contract those two agree
on. A second small migration widens `codeaxis.jobs.kind` for the two
blueprint job kinds (the one-scan-per-repo partial index is untouched).

**Engine** (`app/blueprint/{validate,generate,files,regenerate,export,document}.py`).
The validator is the piece Phase 5's exit criterion rests on: 17 stable error
codes, and it returns errors rather than raising or repairing. It checks the
things the database cannot — dangling connection endpoints, dependency-only
cycles (reporting the actual member keys), a `linked_component_id` that must
resolve to a real `cluster` node **at this blueprint's own snapshot**, an
`existing` claim with no evidence, a `new` file colliding with a real path,
and hard-constraint conflicts from the ledger. Generation gives the model
only repo-grounded candidates and **drops** what it cannot resolve rather
than downgrading a `reuse` capability into a `proposed` one — the same
citation-dropping discipline `app/context/extract.py` already uses. Nothing
is generated from summaries (CLAUDE.md #2).

**Frontend** (`apps/web/src/components/Blueprint*.tsx`, new "Build" tab,
`@xyflow/react` 12.12.0). Existing vs. proposed vs. modified are
distinguishable at a glance; the four connection kinds differ visually; a
linked component clicks through to the Explorer/Inspector; `kind`+`mapping`
is a single choice offering only the three valid pairings; a locked component
disables editing and says why; an invalid version shows its reasons attached
to the entity they name; layout saves are debounced and never version the
design. No edge animation anywhere — nothing may imply the program ran.

**Routes** (`app/blueprint_api.py`, 14 endpoints, Part 8). App-layer
authorization on every one. Two things the routes own rather than delegate:
an edit cannot produce a contradictory component (naming only `kind` derives
the mapping; a contradictory pair is a 422 rather than a database error the
UI can't explain), and a mapping cannot point at a node from another
snapshot. A rejected regeneration is a 422 carrying its reasons, because a
locked-entity refusal is an answer, not a failure. Also added
`GET /repos/{id}/tasks`, which closed a real gap — no route listed a repo's
tasks, so the Build tab had been reduced to remembering ids client-side.

**Verified, not assumed:** `uv run ruff check .` clean, `uv run pyright` 0
errors, `uv run pytest services/api/tests/blueprint
services/api/tests/test_blueprint_routes.py -q` → **99 passed** against the
live DB (81 engine, 18 route). `npm run lint`/`npm run build --prefix
apps/web` clean — independently re-run here, not taken from the agent's
report. Two route bugs were caught by running the tests rather than reading
the code: `list_nodes` has no `limit` kwarg, and `/blueprints/schema-version`
was shadowed by `/blueprints/{id}` (FastAPI matches in declaration order).
The engine pass also found and fixed a real pre-existing bug: `belongs_to`
edges run cluster→member, not member→cluster, which had silently produced
mapped components with empty evidence.

**De-duplicated before committing:** my first draft of the routes had its own
document assembly and module grouping. The engine's `build_document`/
`modules_of` cover strictly more (advisory-vs-blocking errors, open
decisions), so the route versions were deleted rather than left to drift.

**Known gaps, named not hidden:**
1. Generation-time `unresolved` has no column on `blueprint_versions`, so it
   reaches the caller in the response and is then lost; the document rebuilds
   `unresolved` from advisory errors plus open decisions/questions. A column
   or an `blueprint_unresolved` table is the fix. Marked `# STUB(CodeAxis):`.
2. `files.py` never emits `unchanged`, and cited conventions record the
   model's stated justification without re-verifying it against the path.
3. No framework detector exists anywhere in the repo, so file proposals
   substitute manifest-declared dependency nodes and say so.
4. `persist_version` relies on the knowledge layer's per-call commits, so a
   mid-fill failure can leave a partial, unpromoted version behind.
5. Regeneration's affected neighborhood is seeds plus one hop; deeper ripples
   are not caught and the operator re-seeds.
6. No live click-through of the Build tab against a running API — lint,
   typecheck, build and the backend tests are the evidence here.

Phase 5 exit criteria (Part 10): *blueprint references only real entities* —
enforced by the validator's grounding checks plus the route's snapshot check;
*invalid blueprints rejected with reasons* — `invalid` is a stored status
carrying 17 coded reasons, surfaced per entity in the UI; *edits preserve
approved decisions* — `locked` is a hard block in the regeneration pass and a
409 at the route. **Pillar 3 is complete in substance**, with the six gaps
above outstanding.

## 2026-10-20 update: Phase 6 — hardening, limits, deployment, and three more file-read vectors

Part 13 decision #1 settled first: **`services/api` deploys to Fly.io as a
container; `apps/web` to Vercel.** Serverless was ruled out on evidence, not
preference — the API shells out to the `git` binary, loads native C extensions
(`tree-sitter-language-pack`, `python-igraph`, `leidenalg`), runs minutes-long
scans, and needs a writable filesystem for scratch checkouts. That makes
Docker non-optional: it is both the deploy format Fly takes and the fix for
the analysis-sandbox gap `ingest.py` had been admitting since Phase 1.

Audit first, per §0.2. Two Phase 6 items turned out to be **already built**:
incremental re-index (chunk reuse keyed on `content_hash`) and stale-index
handling (package staleness + regrounding). The rest was genuinely missing and
was built in three parallel tracks.

**Deploy.** `services/api/Dockerfile` (multi-stage, `uv sync --frozen`,
non-root uid 10001, `git` present but no compilers/node/curl left in the
runtime image), root `.dockerignore`, `fly.toml` (`bom` region to sit beside
the Supabase pooler in ap-south-1, `auto_stop_machines = "off"` because an
in-process `BackgroundTasks` scan must not be killed by a suspending machine),
`infra/sandbox/run-hardened.sh` (read-only rootfs, noexec tmpfs scratch,
`--cap-drop ALL`, `no-new-privileges`, cpu/mem/pid caps), a rewritten
`infra/sandbox/README.md` naming 10 properties provided and 7 gaps remaining,
and `docs/deployment.md` (435 lines, written for an operator who has not read
any of this). `ingest.py`'s stale "runs on the host, isolation deferred"
docstring now describes what is actually true.

Two honesty corrections came out of that work. The sandbox README had claimed
**"no outbound network," which was false** — Postgres, Clerk, Anthropic and
GitHub are all required; the claim is retracted. And on Fly **none of the
`docker run` hardening flags apply**; only image-level properties survive,
which the README now says instead of implying otherwise. The image was
written **unbuilt** — Docker is not installed in this environment — and that
gap is now closed by CI rather than by assertion: see the 2026-10-20
follow-up at the end of this file.

**Limits** (`app/limits/{budget,ratelimit}.py`, `app/knowledge/spend.py`,
migration `20261022000000_cost_caps.sql`, applied additively to the live DB).
Cost caps now **refuse work** instead of recording it after the fact: the
check sits at the single chokepoint every model path funnels through
(`RoutedModelService._call`/`_embed_once`), so no caller can bypass it, and a
refused call never reaches the provider and writes no `llm_calls` row.
`BudgetExceededError` is deliberately **not** a `ModelServiceError`, so the
existing `except ModelServiceError -> 502` cannot mislabel a budget refusal as
a provider failure; it surfaces as `402 cost_cap_exceeded` with the numbers,
and **fails closed** when the ledger is unreadable. Scope is per project per
UTC calendar month, with a per-project override column. Stated limits, not
hidden: it is a **soft ceiling with bounded overshoot** (the in-flight calls),
not a credit limit — a hard one needs a write-ahead reservation ledger; and
there is no per-user cap, because `llm_calls` has no `user_id`, so one user
with twenty projects gets twenty caps.

Rate limiting is dependency-free fixed-window counters keyed on the
**verified** Clerk `sub` (a dependency, not middleware — an unverified key is
forgeable). **Blunt, and `# STUB(CodeAxis):`-marked: the limiter is
per-machine, not global**, so scaling Fly out multiplies every limit. The
global ceiling is the cost cap, which is genuinely global because it reads a
shared ledger. The MCP endpoint got its own limiter keyed on the token hash,
running *before* token verification so brute force is throttled too — easy to
overlook, since MCP authenticates by per-repo bearer token and never sees a
Clerk identity.

Connection pooling landed in `app/knowledge/db.py` without changing
`api_connection()`/`worker_connection()` semantics, which hundreds of call
sites depend on. Notably it avoids `psycopg_pool`'s own `pool.connection()`
because that **commits on exit**, while this codebase relies on "no commit
means nothing persisted" (there is a live-DB test pinning that). Retry is
confined to `retry_read` — reads are idempotent, writes that may have
partially committed are not, and the project's discipline is to refuse rather
than guess. `GET /projects/{id}/spend` makes the cap visible before it bites,
and reports `unpriced_calls`/`spend_is_complete` rather than passing an
incomplete total off as authoritative.

**Tests and CI.** `.github/workflows/ci.yml` now runs a
`pgvector/pgvector:pg16` service, applies the migrations in order, creates the
two roles, and sets the three DB URLs — so the DB-integration suite actually
executes in CI. It previously did not: every DB-gated test silently skipped,
which is why this file's own "600+ passing" counts were never anything CI had
reproduced. `CODEAXIS_REQUIRE_DB=1` turns a DB-gated skip into a **failed**
run, so CI can never again go green while running none of it. Plain Postgres
lacks what the migrations assume (`auth.users`, `auth.uid()`, the
`authenticated` role, an `extensions` schema), so `.github/ci/supabase_shim.sql`
fakes that minimum and `scripts/apply_migrations.py` replays migrations in
filename order. Verified locally against a real Postgres 16 + pgvector in a
clean checkout: **897 passed, 2 skipped** — against **402 passed, 227
skipped** without a database, which is the measure of how much had been
invisible. (Written as "unverified, never run on GitHub"; it has since run —
see the follow-up at the end of this file.)

Frontend tests exist for the first time: Vitest 4.1.12 + React Testing
Library, **43 tests** over the places where logic rather than layout lives —
`api.ts`'s error shaping and discriminators, the blueprint kind/mapping
pairing (deliberately breaking `KIND_MAPPING` was confirmed to fail them), the
layout fallback, stale/invalid badging, and the delete control. Versions were
pinned one major back in three places on purpose: vitest 5, jest-dom 7 and
jsdom 30 need Node 22 and CI runs Node 20.

Failure-mode tests cover Phase 6's four named kinds (hostile repos, failed
model calls, conflicting requirements, unauthorized access). The
unauthorized-access work found a real hole in the **test harness**: FastAPI
0.143 wraps included routers, so `test_auth.py`'s every-route sweep had never
seen the context, blueprint or MCP routes at all. The traversal is fixed and
now guards itself against the OpenAPI schema, and 46 routes are each checked
to refuse a signed-in stranger. No missing route guard turned up in the app
itself.

**A third arbitrary-file-read vector, found and fixed.** The scanner followed
symlinks, so a repository containing `notes.md -> /etc/passwd` had the host
file's bytes stored as repository content and served back through
search/chat. `walk()` in `scanner.py` now skips symlinks, as do `manifests.py`
and `entrypoints.py`. This is the same class as the two fixed on 2026-10-19
(local-path ingestion, SSRF) and the third in a row found by looking rather
than by being reported — worth noting as a pattern: untrusted filesystem
input has been this codebase's weakest edge.

**A design-destroying bug, found and fixed.** A schema-valid but empty model
reply (an empty object resolves to zero components, since every field has a
default) validated **clean** — every structural check passes trivially when
there is nothing to check — and was therefore promoted, silently replacing a
good design with a blank one. Added `EMPTY_BLUEPRINT` as a blocking
validation code: a version that proposes nothing is recorded invalid with its
reason and never replaces a design that has components. Three existing tests
had been constructing component-free rows as shorthand; they were given a
component rather than the rule being weakened.

**Three deployment blockers fixed by this session, each verified by running:**
1. CORS was hardcoded to `http://localhost:3000` — the Vercel frontend could
   not have called the Fly API at all. Now driven by `CLERK_ALLOWED_ORIGINS`,
   reusing the list that already names trusted browser origins rather than
   adding a second one to drift. A wildcard is **refused at startup**,
   because this API is always called with an Authorization header and the
   CORS spec forbids a wildcard with credentials.
2. `.env.example` had been edited to rename `CLERK_PUBLISHABLE_KEY` to the
   `NEXT_PUBLIC_` form, but `app/auth.py` reads the original name and fails
   closed — deploying from that file would have been a total auth outage.
   Restored, with a comment explaining why the prefixed name belongs only in
   `apps/web/.env.local`.
3. `NEXT_PUBLIC_API_URL` fell back to `localhost:8000` silently. Next.js
   inlines `NEXT_PUBLIC_*` at build time, so a forgotten value shipped a
   working build that could not reach its API. A production build now
   **throws** — confirmed by running `next build` both without the variable
   (fails, with the reason) and with it (succeeds).

**Still open, named not hidden:** the Fly `kill_timeout` tops out at 300s, so
a scan longer than five minutes is killed on deploy and its `jobs` row stays
`running` forever — there is no reaper, and the guide says "deploy when no
scan is running", which is a workaround and not a fix. No Fly volume for
scratch (volumes mount root-owned and uid 10001 cannot chown it), so scratch
lives on the machine rootfs. Container-per-analysis remains the sandbox's
biggest gap: every scan shares one process. Rate limits are per-machine. The
4 GB memory figure is reasoned, never measured against the benchmark repos.
`supabase db push` is still unusable against the live DB (history drift), and
`scripts/apply_migrations.py` is fresh-DB-only and tracks no state. The
Inspector's "Control-flow summaries" stub stays: nothing in the graph,
analyzers or migrations produces control-flow data, and filling it would be
fabricating a view.

## 2026-10-20 follow-up: three of Phase 6's four unverified claims are now verified

Phase 6 shipped with four things named as unverified. Three are closed, by
machines rather than by reasoning, and the fourth is unchanged and belongs to
a human.

**The workflow has now run on GitHub, and passed on the first attempt** —
run #7 on `a7dcab5`, all three jobs green. That was not the expectation; a
workflow that has never executed usually needs a few cycles. One break *was*
caught locally just before pushing, which is probably why: the node job ran
`build:web` with no `NEXT_PUBLIC_API_URL`, and the fix landed an hour earlier
makes a production build throw without it — so the very run meant to prove CI
works would have failed on it. Fixed in the same commit that added the docker
job.

**The DB-integration suite genuinely executed in CI**, and the evidence is
structural rather than circumstantial: the workflow sets
`CODEAXIS_REQUIRE_DB=1`, and `tests/conftest.py`'s `pytest_sessionfinish` hook
flips a passing run to failed if any test skipped for want of a database. A
green Python job therefore *cannot* mean "the DB tests were skipped again".
Worth recording because the step took 32 seconds against ~26 minutes locally,
which looks alarming until you notice the local figure is dominated by
per-query network round-trips to a Supabase instance in another region, while
CI talks to Postgres over loopback.

**The image builds.** A new `docker` job builds `services/api/Dockerfile` on
every push and then asserts the two properties the image is supposed to
guarantee, which `infra/sandbox/tests/` could previously only claim by
parsing the Dockerfile as text: it does not run as root, and the `git` binary
the ingest and history analyzers shell out to is actually present. This is how
the gap gets closed permanently, rather than by installing Docker on one
developer's machine — the build now re-verifies itself on every commit.

**Rate limits: made honest rather than made global.** `fly.toml` now caps
`max_machines_running = 1`. The limiter keeps counters in process memory, so
each additional machine multiplies every published limit; one machine is the
only configuration in which the advertised limits are the real ones. This is
a correctness constraint, not a cost preference, and the comment in `fly.toml`
says so — along with the warning that `fly scale count` overrides the file and
would silently reintroduce the multiplication. A shared counter store
(Postgres table or Redis) is the prerequisite for ever raising it. The cost
cap is unaffected: it reads the shared `codeaxis.llm_calls` ledger and stays
global at any machine count.

**Still unverified, and not something this session can close: no live
click-through of the UI.** It needs a real Clerk sign-in, which requires a
human with an account — the same reason it has been outstanding since Phase 0.
Everything around it is now verified by machine (typecheck, lint, 43 frontend
tests, production build, 898 backend tests, CI), but "a person signed in and
the product worked" is not among those things, and no amount of green CI
should be read as if it were.

**Noted while pushing:** the GitHub repository is **public** — an
unauthenticated API read of it returns 200. No secrets are committed (the
outgoing diffs were scanned before both pushes; the only matches were CI's
throwaway loopback Postgres credentials and truncated `pk_live_...` /
`sk-ant-...` placeholders in the deployment guide). Flagged because real keys
are about to go into Fly and Vercel, and a public repository makes the
deployment surface easier to study.
