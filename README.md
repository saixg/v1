# CodeAxis

Turns an unfamiliar repository into a reliable mental model: connected,
evidence-backed views of one shared code graph, where every claim links to the
source that supports it and anything the analyzer could not resolve is shown
as unresolved rather than guessed.

Three pillars, all built:

1. **Codebase Intelligence** — import a repo, scan it statically, and explore
   the resulting graph (Explorer, Inspector, coverage, hybrid search,
   grounded chat with citations).
2. **Agent Context Engine** — tasks, extracted requirements, a constraint
   ledger, budgeted context packages, `CLAUDE.md`/`AGENTS.md` exports, and a
   read-only MCP server.
3. **Virtual Development Environment** — typed, versioned blueprints: an idea
   mapped onto real repo entities, validated, rendered in React Flow, and
   exportable as a context package.

`build-plan.md` is the authoritative specification. `docs/STATUS.md` is the
running build log, including every known gap. `docs/deployment.md` covers
deploying it.

## Layout

```
apps/web/          Next.js 16 workspace UI
services/api/      FastAPI app + in-process analysis worker
packages/schemas/  JSON contracts shared across the web/api boundary
supabase/          migrations — the single source of schema truth
evals/             golden repos + graders
infra/sandbox/     analysis container definition and hardening
```

## Running it locally

**Prerequisites:** Node 20+, [uv](https://docs.astral.sh/uv/), Python 3.12,
and `git` on PATH (the analyzers shell out to it).

```bash
npm install
uv sync
```

Create `.env` at the repo root from `.env.example`, and
`apps/web/.env.local` from `apps/web/.env.local.example`. The minimum to get
a working app:

| Variable | Where | Why |
| --- | --- | --- |
| `WORKER_DATABASE_URL`, `API_DATABASE_URL` | root `.env` | nothing works without them |
| `CLERK_PUBLISHABLE_KEY` | root `.env` | the API fails closed (503) without it |
| `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_SECRET_KEY` | `apps/web/.env.local` | no page renders without them |

Optional, each with defined behaviour when unset: `ANTHROPIC_API_KEY`
(without it, chat, requirement extraction and blueprint generation are
disabled), `VOYAGE_API_KEY` (without it, chunks are stored unembedded and
search stays keyword-only, recorded in `codeaxis.coverage`), and
`LOCAL_INGEST_ALLOWED_ROOTS` (unset disables local-folder import entirely —
see the security note below).

First-time database setup:

```bash
uv run python scripts/setup_db_roles.py    # creates codeaxis_worker / codeaxis_api
npm run db:push                            # see docs/deployment.md on migration drift
```

Then start both processes with one command:

```bash
npm run dev
```

That runs the API on `http://localhost:8000` and the web app on
`http://localhost:3000`.

## Tests

```bash
uv run ruff check . && uv run pyright && uv run pytest -q   # backend
npm run test:web && npm run lint:web && npm run build:web   # frontend
```

The backend suite needs the three database URLs; without them the
DB-integration tests skip. CI sets `CODEAXIS_REQUIRE_DB=1`, which turns such
a skip into a failed run, so CI cannot go green while silently running none of
them.

## Two things worth knowing before you change anything

**The governing rule.** Deterministic analysis establishes facts; the model
only narrates facts it is given. The model never discovers structure. Every
generated sentence carries a citation a validator checks against the graph,
and failures are dropped rather than rephrased. `CLAUDE.md` has the full set
of invariants — they are not stylistic preferences.

**Imported repositories are untrusted.** Nothing from a scanned repository is
ever executed: no install, no build, no test run, no dynamic import. Analyzers
are static, symlinks are not followed, archives are checked for zip-bomb and
zip-slip, git remotes are resolved and refused if they point at private
addresses, and local-path ingestion is disabled unless an operator allowlists
specific roots. Comments, READMEs and string literals from a repository are
data, never instructions.
