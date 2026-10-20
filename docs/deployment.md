# Deploying CodeAxis

Written for an operator doing this for the first time. Everything here was
derived by reading the code; where a claim could not be verified by running it,
it says so.

Two deployables, two platforms (Part 13 decision #1):

| | Where | Why |
| --- | --- | --- |
| `services/api` (FastAPI; the worker shares this process) | **Fly.io**, as a container | Shells out to the `git` binary, loads native C extensions (`tree-sitter-language-pack`, `python-igraph`, `leidenalg`), runs minutes-long scans, needs a writable scratch filesystem. None of that fits serverless. |
| `apps/web` (Next.js 16) | **Vercel** | First-party Next.js host; nothing in the app needs more. |

The database is Supabase Postgres and is **not** deployed by either of these.

---

## 0. Read this before you start

Four things will bite you. They are real, current, and none of them is fixed by
this document.

1. **`supabase db push` does not work against the existing database.** There is
   recorded migration-history drift. See §2.
2. **CORS in the API is hardcoded to `http://localhost:3000`**
   (`services/api/app/main.py`, `allow_origins=["http://localhost:3000"]`).
   A browser on a Vercel domain will be blocked by the API on every request.
   **This must be changed before the web app can talk to the deployed API.**
   It is a one-line change in `main.py` to read an allowlist from the
   environment; it is called out here rather than done here because that file
   is owned elsewhere. Until then, the deployed web app is non-functional
   against the deployed API.
3. **Scan jobs run in-process** via FastAPI `BackgroundTasks`
   (`app/analysis/jobs.py`, marked `STUB(CodeAxis)`). There is no queue, no
   retry and no stale-job reaper. Any restart of the API machine during a scan
   leaves that `codeaxis.jobs` row at `running` forever. §5 explains how the
   Fly config minimises this and where it cannot.
4. **`.env.example` has drift.** Do not use it as the list of what to set; §1
   is that list, built from `grep -rn "os.environ" services/api/app`.

---

## 1. Environment variables

### 1.1 What `services/api` actually reads

Every entry below is a real `os.environ` read in `services/api/app`. Nothing is
inferred from `.env.example`.

**Required. The API does not work without these.**

| Variable | Read by | Notes |
| --- | --- | --- |
| `WORKER_DATABASE_URL` | `app/knowledge/db.py` | `codeaxis_worker` role, read/write on the `codeaxis` schema. Used by every analyzer write path. Missing → `RuntimeError` on first use. |
| `API_DATABASE_URL` | `app/knowledge/db.py` | `codeaxis_api` role, read-only. Used by every read route. Missing → `RuntimeError` on first use. |
| `CLERK_PUBLISHABLE_KEY` | `app/auth.py` | JWT verification. **The API fails closed without it** — `tests/test_auth.py` asserts exactly that, so an unset value is a total outage, not a degraded mode. |

> **One key, two names.** The API reads `CLERK_PUBLISHABLE_KEY`; the web app
> reads `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`. The *value* is the same Clerk
> publishable key — only the name differs, because Next.js exposes a variable
> to the browser solely when it is `NEXT_PUBLIC_`-prefixed. So set
> `CLERK_PUBLISHABLE_KEY` on Fly and `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` on
> Vercel. Do not rename the API's one to the prefixed form: `app/auth.py`
> looks up that exact name and fails closed, which is a total outage.
> (`.env.example` briefly carried that rename; it has been corrected.)

**Optional. Each has a defined behaviour when unset.**

| Variable | Read by | Unset behaviour |
| --- | --- | --- |
| `CLERK_ALLOWED_ORIGINS` | `app/auth.py` | Comma-separated; checked against a token's `azp` claim. Unset skips the check. Set it in production. |
| `ANTHROPIC_API_KEY` *or* `ANTHROPIC_AUTH_TOKEN` | `app/analysis/semantic/writer.py` | Semantic enrichment is **skipped** and records that as a coverage gap. The scan still completes. |
| `VOYAGE_API_KEY` | `app/llm/voyage_provider.py` | Chunks are stored without vectors, stay keyword-searchable, gap recorded in `codeaxis.coverage`. |
| `LOG_LEVEL` | `app/logging.py` | `INFO`. |
| `LOCAL_INGEST_ALLOWED_ROOTS` | `app/repository/ingest.py` | Local-path ingestion is **disabled**, which is what you want. Setting it on a multi-user deployment turns `POST /repos/stage` into an arbitrary-host-path read for any authenticated user. Leave it unset. |
| `CODEAXIS_SCAN_MAX_FILES` | `app/analysis/scan/limits.py` | 50,000. |
| `CODEAXIS_SCAN_MAX_FILE_SIZE_BYTES` | same | 5 MB. |
| `CODEAXIS_SCAN_MAX_TOTAL_SIZE_BYTES` | same | 2 GB. Lower it if §5's disk check is tight. |
| `CODEAXIS_SCAN_MAX_PARSE_SECONDS` | same | 2.0. |
| `CODEAXIS_DB_POOL_MAX_API` | `app/knowledge/db.py` | Max connections in the read-only pool. Supabase's pooler has its own ceiling — size this below it, counting *all* machines. |
| `CODEAXIS_DB_POOL_MAX_WORKER` | same | Same, for the read/write pool. |
| `CODEAXIS_PROJECT_MONTHLY_COST_CAP_USD` | `app/limits/budget.py` | `10.0`. Per-project model spend cap. |
| `CODEAXIS_RATE_LIMIT_<BUCKET>` | `app/limits/ratelimit.py` | Per-bucket override; buckets are `INGEST`, `CHAT`, `SEARCH`, `EXTRACT`, `BLUEPRINT`, `MCP`, `READ`. Defaults are in `DEFAULT_BUCKETS`. |

**Set on Fly? No.** These appear in `.env.example` but **nothing in
`services/api/app` reads them**:

- `DATABASE_URL` — project-owner connection. Used only by migrations and
  `scripts/setup_db_roles.py`, from your workstation. Giving the API the owner
  role would discard the whole point of the two role-scoped URLs above.
- `SUPABASE_URL` — read only by `scripts/setup_db_roles.py`.
- `SUPABASE_SECRET_KEY` — read by nothing in this repository today.
- `CLERK_SECRET_KEY` — read by nothing in `services/api`. It belongs on Vercel
  (see §1.2).

### 1.2 What `apps/web` needs

| Variable | Why |
| --- | --- |
| `NEXT_PUBLIC_API_URL` | `apps/web/src/lib/api.ts` resolves the API base URL from it and **falls back to `http://localhost:8000`**. So the app is deploy-ready in that it already reads an env var — but if you forget to set it, every request from the Vercel deployment goes to the visitor's own localhost and silently fails. Set it to `https://codeaxis-api.fly.dev` (or your custom domain), no trailing slash needed — the module strips one. |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` | `@clerk/nextjs`, client side. |
| `CLERK_SECRET_KEY` | `@clerk/nextjs`, server side — every page calls `auth.protect()`. Not referenced in our source; the SDK reads it from the environment itself. Mark it as a Vercel secret, not a plain variable. |

---

## 2. Database migrations — the honest version

`supabase/migrations/` holds the schema. `npm run db:push` wraps
`supabase db push` against `DATABASE_URL`.

**It will fail on the existing database.** Two documented facts (`docs/STATUS.md`):

- The live database recorded the Step-1 `staged_commit_sha` migration under
  timestamp `20261015135912`, while the file on disk is
  `20261015000000_staged_commit_sha.sql`. A fresh `db push` therefore tries to
  re-run it, and fails.
- Several later migrations (`20261018000000_relationships.sql`, the blueprint
  pair, and others) were applied **directly via psycopg against
  `DATABASE_URL`** precisely to avoid touching that drift. The applied-history
  table does not know about them.

So there are two different procedures, and you must know which database you
have.

### 2a. A brand-new database (recommended for a real deployment)

```bash
cp .env.example .env            # fill in DATABASE_URL and SUPABASE_URL
npm install
uv run python scripts/check_db.py      # confirms the server and that `vector` is available
npm run db:push                        # applies every migration, in order
```

On an empty database there is no recorded history to drift from, so this is the
clean path. Verify afterwards with §2c.

If you have no Supabase CLI or project, `scripts/apply_migrations.py` replays
the same files in filename order against `DATABASE_URL` using psycopg:

```bash
uv run python scripts/apply_migrations.py
```

Read its docstring first. It **tracks no state**: it exists for CI's throwaway
Postgres, and pointing it at a database that already has the migrations applied
will fail partway through. It is a fresh-database tool only, and it does not
populate `supabase_migrations.schema_migrations`, so §2c's second query will
come back empty even though the schema is correct.

### 2b. The existing, drifted database

Do **not** run `npm run db:push`. Apply any new migration file directly and
individually:

```bash
# one file, reviewed first, against the owner connection
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f supabase/migrations/<file>.sql
```

Repairing the history properly is `supabase migration repair --status applied
<version>` for each already-applied migration, and that rewrites
applied-migration state on a live database. It is a decision with
consequences, it has not been made, and this document does not make it for
you. Until it is made, this repository's migrations are applied by hand and
`db push` stays unusable.

### 2c. Verify the schema, whichever path you took

```bash
psql "$DATABASE_URL" -c "select table_name from information_schema.tables
  where table_schema = 'codeaxis' order by 1;"
psql "$DATABASE_URL" -c "select * from supabase_migrations.schema_migrations order by version;"
```

Compare the second output against `ls supabase/migrations`. Any file not listed
was either applied by hand (2b) or not applied at all — and the only way to
tell the difference is to look for its tables in the first output.

---

## 3. Database roles

The API never connects as the project owner. Two scoped roles do the work, and
`scripts/setup_db_roles.py` creates them. It is idempotent: a role that already
exists is skipped rather than having its password rotated.

```bash
uv run python scripts/setup_db_roles.py
```

It prints the connection URL for each role it created. **Capture that output —
it is the only time the generated password is shown.** Put the read/write URL in
`WORKER_DATABASE_URL` and the read-only one in `API_DATABASE_URL`.

Run it *after* migrations, and again after any migration that adds tables: the
grants cover existing tables plus `alter default privileges` for future ones,
so a table created by a migration applied as a *different* role may not be
covered. Verify:

```bash
psql "$API_DATABASE_URL" -c "select count(*) from codeaxis.nodes;"      # must succeed
psql "$API_DATABASE_URL" -c "insert into codeaxis.projects(name) values ('x');"  # must FAIL
```

That second command failing is the test. If it succeeds, the read-only role is
not read-only and you should stop and fix the grants before deploying.

---

## 4. Deploy the API to Fly.io

### 4.1 One-time setup

```bash
fly auth login
fly apps create codeaxis-api          # must match `app` in fly.toml
```

`fly.toml` is at the **workspace root**, not in `services/api`, because the
Docker build context has to be the root: `uv.lock` and the root
`pyproject.toml` live there and `uv sync --frozen` needs both.

### 4.2 Secrets

Never in the image, never in `fly.toml`. `fly secrets set` stores them
encrypted and injects them as environment variables at boot. Setting secrets
triggers a machine restart, so do this **before** the first deploy.

```bash
fly secrets set \
  WORKER_DATABASE_URL="postgresql://codeaxis_worker:...@...:5432/postgres" \
  API_DATABASE_URL="postgresql://codeaxis_api:...@...:5432/postgres" \
  CLERK_PUBLISHABLE_KEY="pk_live_..." \
  CLERK_ALLOWED_ORIGINS="https://your-app.vercel.app" \
  ANTHROPIC_API_KEY="sk-ant-..." \
  VOYAGE_API_KEY="pa-..."
```

Confirm the names (not the values) landed:

```bash
fly secrets list
```

The last two are optional — see §1.1 for exactly how the product degrades
without each.

### 4.3 Build locally first (optional but worth it)

```bash
docker build -f services/api/Dockerfile -t codeaxis-api .
docker images codeaxis-api                    # check the size
infra/sandbox/run-hardened.sh --env-file .env # runs it with the hardening flags
curl -s localhost:8000/health                 # {"status":"ok"}
```

On an ARM workstation add `--platform=linux/amd64` to the build; Fly machines
are x86_64. Every native dependency has wheels for both, so either builds.

### 4.4 Deploy

From the **repo root**:

```bash
fly deploy
```

`fly deploy` uploads the root as the build context (`.dockerignore` keeps
`node_modules`, `.git` and `.env*` out of it) and builds
`services/api/Dockerfile` on Fly's remote builder.

### 4.5 Verify

```bash
fly status                                   # one machine, state "started"
fly checks list                              # the /health check must be passing
curl -s https://codeaxis-api.fly.dev/health  # {"status":"ok"}
fly logs                                     # one line per request, from app/logging.py
```

Then verify the three things `/health` cannot tell you, because `/health`
returns `{"status":"ok"}` unconditionally — it touches neither the database nor
Clerk:

```bash
# 1. Auth is wired. No token must be rejected, not served.
curl -s -o /dev/null -w "%{http_code}\n" https://codeaxis-api.fly.dev/projects   # expect 401

# 2. The database is reachable with a real token.
curl -s -H "Authorization: Bearer <a real Clerk JWT>" \
     https://codeaxis-api.fly.dev/projects                                        # expect 200 + JSON

# 3. git is in the image and the process can use it.
fly ssh console -C "git --version"

# 4. Scratch is writable and the process is not root.
fly ssh console -C "id"                      # expect uid=10001
fly ssh console -C "df -h /tmp"              # see §5 on sizing
```

A 500 on step 2 with a connection error in `fly logs` means a database URL
secret is wrong or the region cannot reach the pooler. A 401 on step 2 with a
*valid* token means `CLERK_PUBLISHABLE_KEY` is wrong or unset.

### 4.6 Roll back

Fly keeps every release.

```bash
fly releases                 # find the last good version number
fly deploy --image <the image ref from that release>
```

or, for the image currently deployed being the problem:

```bash
fly machine restart <machine-id>
```

**A rollback does not undo a migration.** Migrations are forward-only here:
none of the files in `supabase/migrations/` has a down script. If a release
needed a schema change, rolling the code back leaves the new schema in place —
which is safe for additive migrations (new tables, new nullable columns) and
is not safe for anything else. Check what the migration did before assuming
the rollback is clean.

To roll back a secret, `fly secrets set` the old value; to remove one,
`fly secrets unset NAME`. Both restart the machine, which (see §5) kills any
running scan.

---

## 5. The thing to understand about the API machine

Scan jobs run **in-process**, as FastAPI `BackgroundTasks`, *after* the HTTP
response has been sent (`app/analysis/jobs.py`). Three consequences, and the
config deals with two of them:

1. **Scale-to-zero is off** (`auto_stop_machines = "off"`,
   `min_machines_running = 1`). To Fly's idle heuristics a machine running a
   scan looks idle — the request already completed. Autostop would suspend it
   mid-scan.
2. **Graceful shutdown is long.** The Dockerfile passes
   `--timeout-graceful-shutdown 3600` so uvicorn waits for in-flight ASGI calls,
   which is where a background task lives.
3. **It is still capped at 300 seconds**, because `kill_timeout` in `fly.toml`
   is at Fly's maximum. A scan that has been running longer than five minutes
   when you deploy, restart, or when Fly replaces the machine **is killed**,
   and because nothing reaps orphaned jobs its `codeaxis.jobs` row stays
   `running` forever. The user sees a scan that never finishes.

So: **deploy when no scan is running.** Check first:

```bash
psql "$API_DATABASE_URL" -c "select id, repo_id, kind, status, created_at
  from codeaxis.jobs where status in ('queued','running') order by created_at;"
```

The real fix is the queue that `app/analysis/jobs.py`'s `STUB(CodeAxis)` note
already names. Until then this is an operational constraint, not a solved
problem.

**Disk.** Scratch checkouts go to `/tmp` on the machine's own rootfs; there is
no Fly volume, because Fly volumes mount root-owned and the process runs as uid
10001 with no way to chown the mountpoint (the reasoning is in `fly.toml`).
`ScanLimits` defaults allow a 2 GB checkout plus a 500 MB ZIP upload. Check the
real headroom with `fly ssh console -C "df -h /tmp"` and lower
`CODEAXIS_SCAN_MAX_TOTAL_SIZE_BYTES` if it is tight.

**Scaling out multiplies the rate limits.** `app/limits/ratelimit.py` keeps its
counters in process memory and says so in its own docstring. With M machines
running W uvicorn workers each, a client gets up to `M * W * limit`. The
Dockerfile runs one worker, so today the multiplier is just the machine count —
another reason not to `fly scale count 3` casually. Divide the
`CODEAXIS_RATE_LIMIT_*` values by the machine count if you do.

**Memory.** `fly.toml` asks for 4 GB. That number is reasoned, not measured: a
Full-tier scan of a 1,000+ file repository holds tree-sitter trees, a
`python-igraph` graph and `leidenalg` clustering in one process. Confirm it
against `benchmarks/repos/` and watch `fly logs` for OOM kills — an OOM looks
exactly like the stuck-job failure above.

---

## 6. Deploy the web app to Vercel

**There is no `vercel.json`, and that is deliberate.** Everything a Next.js app
in an npm-workspace subdirectory needs is a project setting; a config file
asserting the framework and build command Vercel already detects would be a
file that does nothing.

In the Vercel dashboard, **New Project → import the repository**, then:

| Setting | Value |
| --- | --- |
| Root Directory | `apps/web` |
| Framework Preset | Next.js (auto-detected) |
| Install / Build / Output | leave as detected — Vercel runs the install at the workspace root for an npm workspace and builds in `apps/web` |
| Node version | 20 or later (the root `package.json` sets `engines.node >= 20`) |

Then add the three variables from §1.2 under Settings → Environment Variables,
for Production **and** Preview. `CLERK_SECRET_KEY` is a secret; the two
`NEXT_PUBLIC_*` values are inlined into the client bundle at build time, which
is correct for a publishable key and means **changing either one requires a
redeploy**, not just a restart.

### Verify

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://your-app.vercel.app/   # expect 200 or a 307 to Clerk
```

Then open it in a browser, sign in, and watch the Network tab: requests must go
to your Fly hostname, not `localhost:8000`. If they go to localhost,
`NEXT_PUBLIC_API_URL` was not set at **build** time — set it and redeploy.

If they go to the right host but every one fails with a CORS error, that is
§0.2, not a misconfiguration on your part.

### Roll back

Vercel Deployments → the last good deployment → **Promote to Production**.
Instant, and it does not rebuild.

---

## 7. First-deploy checklist

- [ ] Migrations applied, and you know which of §2a / §2b you used
- [ ] `scripts/setup_db_roles.py` run; the read-only role proven read-only (§3)
- [ ] `fly secrets list` shows the three required names (§1.1)
- [ ] `fly checks list` passing; `/health` returns `ok`
- [ ] Unauthenticated `/projects` returns 401; authenticated returns 200
- [ ] `fly ssh console -C "id"` shows uid 10001
- [ ] Vercel has all three variables for Production and Preview
- [ ] Browser Network tab shows requests hitting the Fly hostname
- [ ] CORS in `main.py` fixed, or you have accepted that §0.2 blocks the UI
