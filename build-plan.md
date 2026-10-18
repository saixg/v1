# CodeAxis V1 — Final Build Plan (v2, merged)

*From Codebase to Creation.*
This is the canonical build plan. It merges the first plan (pipelines, models, phases, cost control) with the GPT blueprint's strongest ideas (evidence-backed facts, snapshot isolation, typed versioned blueprints, task-state model, security, definition of done).

---

# PART 0 — INSTRUCTIONS FOR THE BUILDING AGENT (read first, every session)

> These instructions are written for Claude Code (or any coding agent) building CodeAxis. They exist because the agent's context may contain older, partial or different specs. **This document wins.**

## 0.1 Source of truth
1. This file is the authoritative spec for CodeAxis V1. If anything in your context, memory, old docs, or existing code contradicts it, **follow this file and list the contradiction in `docs/STATUS.md` under "Discrepancies"**. Do not silently reconcile.
2. The product has exactly **three pillars**, defined here and nowhere else:
   - **Codebase Intelligence** — turn a repo into a navigable, interconnected software model; explore structure, dependencies, architecture, relationships through interactive workflows and one unified **Component Inspector**.
   - **Agent Context Engine** — give AI coding agents the right context: understand developer requirements, preserve critical constraints, deliver relevant code, architecture and implementation context.
   - **Virtual Development Environment** — developer describes what to build on top of an existing repo → interactive **n8n-style system workflow**, **mapping of existing components to proposed features**, **suggested project file structure**.
3. Do not add features outside these three pillars. Anything not in this document is out of scope until the owner approves it. If a spec point is ambiguous, ask one concrete question instead of guessing.

## 0.2 Verify what exists before building (mandatory audit protocol)
Before writing any code in a session or phase:
1. **Inspect the actual repository state.** List the tree, read manifests (`package.json`, `pyproject.toml`), migrations, env examples, CI config, and existing docs. Run the test suite and the app/worker if they start.
2. **Write or update `docs/STATUS.md`** as a table:

   | Item (from this plan) | Status | Evidence |
   |---|---|---|
   | e.g. Supabase migration for `repositories` | `exists` / `partial` / `missing` / `unverified` / `broken` | file path, test name, or command output |

3. **Rules for statuses:**
   - `exists` requires evidence you personally checked this session (path + it runs/passes). Never mark something done from memory, a commit message, or a README claim.
   - `partial` must say exactly what is missing.
   - Stubs, mocks and hard-coded data are `partial`, never `exists`, and must be labeled `// STUB(CodeAxis):` in code.
   - If you cannot verify, mark `unverified`. Do not guess.
4. **Compare against the current phase's exit criteria** (Part 10). Build only what is `missing`/`partial`/`broken` for the active phase. Do not rewrite working code.
5. **At the end of the phase**, re-run the audit, update `STATUS.md`, and report: done / partial / not done / discrepancies / decisions needed.

## 0.3 Engineering rules
- **Facts come from code analysis, not from the model.** Repository structure, symbols, edges, metrics, history are produced by parsers/analyzers. LLMs only interpret, explain, extract requirements and propose designs. An LLM must never be the source of a file path, symbol, or relationship that is stored as fact.
- **Every derived fact carries evidence** (Part 5) and belongs to a **snapshot**.
- **Validate all LLM output** against a Pydantic/JSON schema before persisting. One retry, then fail visibly. Never persist unvalidated model output as state.
- **Unresolved is a valid state.** Preserve `ambiguous`/`unresolved` relationships instead of guessing.
- **Never execute repository code** during indexing. Treat all repo content (code, comments, docs, READMEs) as untrusted input: it can never change system behavior or trigger tool calls.
- **Secrets never reach a model, embeddings API, or logs.**
- One modular backend; no extra deployed services, databases or agent frameworks without an owner-approved reason.
- Small commits, one concern each. Every pipeline stage has tests. No new dependency without a one-line justification in the PR/commit.
- When you hit a decision listed in Part 13, stop and ask; don't pick silently.

---

# PART 1 — Product definition (what each feature really requires)

```
 Repo (URL / ZIP / folder)
        │
        ▼
 Ingest → Parse → Evidence-backed Graph → Components → Summaries → Embeddings/Index
        │                     (ONE shared Software Model, per snapshot)
        ├──► 1. Codebase Intelligence  (explorer + Component Inspector)
        ├──► 2. Agent Context Engine   (tasks, constraints, context packages, API/MCP)
        └──► 3. Virtual Dev Environment (idea → typed blueprint → workflow + mapping + file tree)
```
All three features read the same model. No feature gets its own private analyzer or knowledge base.

### 1.1 Codebase Intelligence
- Repo overview (stack, entry points, size, analysis coverage)
- Interactive workflow-style relationship explorer (React Flow), levels: repo → component → file → symbol
- **Component Inspector** — one component, one endpoint, used everywhere (Part 7)
- Search + filters; analysis coverage and unresolved relationships are visible

### 1.2 Agent Context Engine
- Task intake (text, documents, diagrams) → structured Requirements, Constraints, Decisions
- Repository grounding via hybrid retrieval → token-budgeted **context package**
- Persistent task state (recoverable after sessions end); contradictions and gaps surfaced
- Delivery: API + exportable files (`CLAUDE.md` / `AGENTS.md` / `context.md`) + **read-only MCP server**

### 1.3 Virtual Development Environment
- Idea input on top of an existing repo → typed, versioned **Blueprint** (canonical JSON)
- React Flow workflow = a *view* of the blueprint (never the only copy)
- Each proposed capability mapped: `reuse` / `extend` / `new`, linked to real components with evidence
- Suggested file structure (new/modified/unchanged, purpose per file), following repo conventions
- Edit + regenerate affected parts without losing approved decisions
- Output is a blueprint for review. **V1 never modifies the source repository.**

---

# PART 2 — Stack

| Layer | Choice | Notes |
|---|---|---|
| Web | **Next.js (App Router) + TypeScript**, Tailwind, shadcn/ui | |
| Graph/workflow UI | **React Flow (@xyflow/react)** + ELK.js layout | Custom n8n-style nodes. **Do not use n8n itself** to render or store the knowledge graph |
| Code viewer | **Monaco** (read-only, range highlight) | Needed for evidence links |
| State | **Zustand** (UI), **TanStack Query** (server) | |
| API + workers | **Python 3.12 + FastAPI**, Pydantic v2 | One modular app; worker process shares the same codebase |
| Parsing | **tree-sitter** (primary), **Python `ast`** (extra Python semantics) | TS/JS + Python first-class in V1 |
| Symbol precision (later) | **SCIP** indexers for TS/Python | Optional upgrade, not V1 blocker |
| Git | `git` CLI / `pygit2`: SHA, history, churn, diffs | |
| Graph analysis | **NetworkX** in memory per snapshot; **igraph/leidenalg** for clustering | Persist to Postgres; no graph DB in V1 |
| Database | **Supabase Postgres** + **pgvector** + FTS | Single store for everything |
| DB access | **SQLAlchemy 2.0 (async)**; **schema owned by Supabase SQL migrations** | One migration system only (Supabase CLI). Do not also run Alembic |
| Auth | **Supabase Auth** (email + GitHub OAuth) | FastAPI verifies the Supabase JWT |
| Authorization | App-layer project membership checks **and** RLS | FastAPI connects with elevated DB access, which bypasses RLS — so app-layer checks are mandatory, RLS protects direct browser reads |
| Storage | **Postgres** (`codeaxis.blobs`, per-file text keyed by content hash) | Decided 2026-10-17: Supabase Storage was never built; per-file blobs-in-Postgres shipped in Phase 1 and is canonical going forward — see `docs/STATUS.md`. Revisit only if blob volume/size makes Postgres storage impractical |
| Queue | **Supabase Queues (pgmq)** to start; Redis + Arq/Celery only if needed | |
| Realtime | Supabase Realtime for job progress | |
| Containers | **Docker** (dev + deploy); workers on Fly.io / Railway / Cloud Run; web on Vercel | |
| Observability | **Langfuse** (LLM traces/cost), **Sentry**, structured logs | |
| Secret scan | `gitleaks` / `detect-secrets` | Before any external call |

---

# PART 3 — Model layer

**Provider-independent interface** (`ModelService`): `generate_structured(schema, prompt, model_role)`, `generate_text`, `embed`, `rerank`, with usage/cost tracking per call. Swapping a model = config change.

| Role | Default | Escalate when |
|---|---|---|
| `bulk` — per-symbol/file summaries | **Claude Haiku 5.5** (`claude-haiku-5-5`), Batch API | — |
| `synth` — component summaries, Inspector explanations, grounded chat, requirement extraction | **Claude Sonnet 5.5** (`claude-sonnet-5-5`) | — |
| `design` — blueprint, mapping, file tree | **Claude Sonnet 5.5** | **Claude Opus 5.5** (`claude-opus-5-5`) for "deep plan" mode or failed validation |
| `vision` — architecture diagrams/images in task input | Sonnet 5.5 (vision) | OCR first for text-heavy PDFs |
| `embed` — code + text embeddings | Hosted code embedding model (e.g. Voyage code model) **or** open-source (nomic/bge) | Decide in Part 13 |
| `rerank` | Hosted reranker or `bge-reranker` | Only after eval shows it's needed |

**Selection discipline (from GPT plan, kept):** start with this small routing table, not a model per subtask. Change a model only if the **eval harness** (Part 9) shows a measurable win on: schema compliance, repo-grounded accuracy, requirement extraction quality, blueprint completeness, unsupported-claim rate, latency, cost.

**Patterns:** structured output + Pydantic for everything machine-consumed; prompt caching for the repo-overview prefix; hierarchical summaries (parents summarize children's summaries, not raw code); hard per-repo budget cap; explicit "send to external model" setting.

**Embeddings stance (reconciled):** build the retrieval layer so embeddings are a pluggable stage. Phase 3 ships exact-symbol + metadata + FTS + graph retrieval first, then adds embeddings in the same phase *because natural-language-requirement → code retrieval is core to Pillar 2*. Reranking only if eval says retrieval is insufficient.

---

# PART 4 — Pipelines

### A. Ingestion
1. Validate input (URL allowlist/SSRF checks, size, type).
2. Fetch: GitHub shallow clone (GitHub App/OAuth for private); ZIP → safe extract (path-traversal + zip-bomb guards); folder → browser upload (File System Access API with `webkitdirectory` fallback), filtered client-side by `.gitignore`. **V1 decision: server-side analysis**, with explicit consent copy and delete-everything.
3. Resolve commit SHA / content hash → create **snapshot**.
4. Detect language, framework, package manager. Exclude vendored/generated/build output/binaries/oversized files (configurable).
5. Secret scan + redaction.
6. Store each included file's content in `codeaxis.blobs`, keyed by content hash (not a tarball in Storage — see Part 2's Storage row); write file manifest with content hashes.
Limits (configurable): e.g. ≤500 MB raw, ≤20k source files. Clear failure messages beyond that.

### B. Structural + relationship extraction (deterministic)
- Per file (parallel): modules, classes, interfaces, functions/methods, signatures, imports/exports, decorators, docstrings, config files, tests, routes/endpoints (framework queries), ORM/SQL models, env var reads.
- Typed edges: `CONTAINS, IMPORTS, CALLS, REFERENCES, INHERITS, IMPLEMENTS, TESTS, CONFIGURES, EXPOSES (endpoint), READS_WRITES (data)`.
- Each edge gets a **resolution status**: `resolved | ambiguous | unresolved | external`. Name-based call matches that aren't certain are `ambiguous`, never silently promoted.
- Manifests → external dependency nodes. Entry-point detection. Git history → churn, recent commits per file/symbol.
- **Test association** is evidence-based (naming + imports + calls), with the evidence stored.

### C. Architecture analysis
- Cluster the import/call graph (Leiden, seeded by directory structure) → **candidate components**. Membership is algorithmic and reproducible.
- Layer classification (UI / API / service / data / infra / test / config): rules first, LLM only as fallback.
- **Every architectural statement is tagged by origin:** `parsed` (read from code), `configured` (explicit rule/config in repo), or `inferred` (AI observation). The UI shows the origin; inferred items are never presented as fact.
- LLM then names components and writes purpose from retrieved evidence.

### D. Semantic enrichment + index
- AST-aware chunks (one per symbol; splits/merges by size); chunk carries path, symbol, line range, snapshot.
- Hierarchical summaries: symbol → file → directory → component → repo.
- Embeddings + FTS index. Conventions extraction (naming, structure, test style, lint/CI rules).
- **Incremental re-index:** key on `(repo, content_hash)`; re-process only changed files and dependents; new snapshot per commit SHA.

### E. Hybrid retrieval (shared)
exact symbol/identifier → path & metadata filters → FTS keyword → vector (when enabled) → **graph expansion** (callers/callees/importers, 1–2 hops) → reciprocal-rank fusion → rerank (optional) → **token-budgeted assembly** with file+line citations, authoritative evidence and explicit constraints prioritized. Never send the whole repo.

### F. Agent Context Engine
1. **Input processing:** natural language, PDF/DOCX (deterministic text extraction, keep page refs), images/diagrams (OCR/vision, keep region refs), repo docs/config.
2. **Requirement extraction** → schema: objective, functional reqs, technical constraints, architectural constraints, relevant existing behavior, acceptance criteria, unresolved questions.
3. **Validation:** schema check; detect missing info, contradictions, ambiguity. **Never invent missing requirements** — surface them as open questions.
4. **Grounding** via Pipeline E, seeded by requirement + affected components.
5. **Context package assembly** (budgeted): task intent → **constraints (always included, never truncated)** → requirements → architecture slice → relevant code/evidence → patterns/conventions → relevant tests → known limitations → unresolved questions.
6. **Persistence:** task state + decisions + snapshots; updated as requirements change, repo changes are observed, or verification results arrive.
7. **Delivery:** REST API, export files, and a **read-only MCP server** (`search_code`, `get_component`, `get_dependencies`, `get_constraints`, `impact_analysis`, `get_conventions`, `get_context_package`) with per-repo tokens.
8. **Constraint Ledger:** user-stated + auto-detected (public API, DB schema, auth boundaries, CI/lint rules, "do not touch" paths); each has scope, severity (hard/soft), origin, confirmed flag.

### G. Virtual Development Environment
1. Retrieve repo summary/components/interfaces/config (Pillar 1) and extract objective/requirements/constraints (Pillar 2).
2. Ask ≤3 clarifying questions only if truly ambiguous.
3. **Capability mapping:** each proposed capability → `reuse | extend | new`, linked to real components with evidence + confidence. Mappings must come from the supplied repo, not generic assumptions.
4. LLM emits a **typed Blueprint JSON** (versioned schema, Part 6) — never a raw diagram.
5. **Validation:** unique IDs; every connection references valid components; dependencies coherent (no impossible cycles); every `existing` component maps to a real repo entity in the snapshot; proposed vs existing clearly distinguished; schema complete; no collision with existing paths; ledger constraints not violated (conflicts flagged).
6. Render workflow in React Flow from the validated blueprint (layout stored separately from semantics).
7. **File structure proposal** from actual conventions/language/framework; each file has a purpose and a blueprint link; statuses new/modified/unchanged.
8. **Implementation context per component:** responsibility, relevant existing code, dependencies, requirements, constraints, suggested location, verification criteria → exportable as a context package (feeds Pillar 2).
9. **Edit + regenerate:** stable node IDs; approved decisions are locked; regenerate only the affected neighborhood; every change creates a new blueprint version.

---

# PART 5 — Evidence & provenance model (core)

Every important entity/relationship stores:
`snapshot_id · source file · start/end line · type · analyzer + analyzer_version · origin (parsed|configured|inferred) · resolution_status · supporting evidence`

Every derived record belongs to `(repository_id, snapshot_id)` so results from different versions can never mix. Every context item and AI statement in the UI is clickable back to source.

---

# PART 6 — Data model (Supabase Postgres)

Normalized tables; JSONB only for genuinely flexible payloads. Foreign keys, unique constraints, indexes, and migrations enforce integrity.

**Platform:** `profiles, projects, project_memberships, jobs, artifacts, api_tokens, llm_calls`
**Repo intelligence:** `repositories, repository_snapshots, analysis_runs, analysis_issues, files, symbols, relationships, components, component_members, dependencies, conventions, chunks (embedding vector, tsv)`
**Context engine:** `tasks, requirements, constraints, decisions, context_items, task_components, task_snapshots, verification_results, context_packages`
**Virtual env:** `blueprints, blueprint_versions, blueprint_components, blueprint_connections, proposed_files`
**Chat:** `chat_threads, chat_messages (citations)`

Key columns:
- `relationships(id, snapshot_id, kind, source_id, target_id, resolution_status, origin, analyzer, analyzer_version, evidence jsonb)`
- `context_items(id, task_id, kind, content, origin, snapshot_id, evidence jsonb)`
- `blueprint_components(id, blueprint_version_id, key, name, kind[existing|proposed|modified], mapping[reuse|extend|new], linked_component_id, responsibility, source_refs jsonb, locked bool)`
- Indexes: HNSW on `chunks.embedding`; GIN on `chunks.tsv`; btree on `(snapshot_id, source_id)`, `(snapshot_id, target_id)`, `(snapshot_id, path)`.

**Canonical blueprint JSON** is versioned (`schema_version`), includes: project, modules, components (kind, responsibility, interfaces, source_refs, evidence), connections (type + semantics), data flows, requirements, constraints, unresolved decisions, validation status. Workflow layout is a separate `layout` object.

---

# PART 7 — Component Inspector (one component, one endpoint)

`GET /components/{id}` returns an **assembled** payload. Factual fields come from the model; the LLM only writes the grounded explanation.

Identity · Purpose (grounded explanation, with citations) · Source (code + navigation) · Incoming relationships · Outgoing relationships · Execution context (entry points, traceable paths) · Data relationships (models, schemas, DB ops) · Tests (+ association evidence) · Configuration/env deps · History (relevant commits) · Impact (potentially affected components) · Evidence + analyzer provenance · **Resolution status** (known / ambiguous / unsupported).

Same `<ComponentInspector />` is used in Explore, Build (for existing-mapped nodes), and Agent views.

**Explorer views — scope for V1:** Structural, Dependency, Architectural, Historical (git). **Behavioral/runtime view is out of V1** — static analysis can't honestly produce it; label as future. All views are projections of the same relationships table, never separate graphs.

---

# PART 8 — API (async jobs for anything long)

Long-running work returns `job_id` + progress via polling/Realtime.

| Area | Endpoints |
|---|---|
| Repos | `POST /repositories`, `POST /repositories/{id}/analyze`, `GET /repositories/{id}/summary`, `GET /jobs/{id}`, `DELETE /repositories/{id}` |
| Explore | `GET /repositories/{id}/components`, `GET /components/{id}`, `GET /components/{id}/relationships`, `GET /repositories/{id}/workflows`, `GET /repositories/{id}/search`, `GET /repositories/{id}/coverage` |
| Chat | `POST /repositories/{id}/chat` (stream, citations) |
| Context | `POST /tasks`, `POST /tasks/{id}/requirements/extract`, `CRUD /tasks/{id}/constraints`, `POST /tasks/{id}/context`, `GET /tasks/{id}`, `GET /tasks/{id}/export?format=` |
| MCP | `/mcp/{repositoryId}` (token auth, read-only) |
| Blueprint | `POST /blueprints`, `GET/PATCH /blueprints/{id}`, `POST /blueprints/{id}/validate`, `POST /blueprints/{id}/regenerate`, `GET /blueprints/{id}/file-structure`, `POST /blueprints/{id}/export` |

---

# PART 9 — Security, reliability, evaluation

**Security (non-negotiable):** authN + project-level authZ on every route; isolated per-repo storage/jobs; SSRF-safe URL validation; archive traversal/expansion limits; no execution of repo code (any future build/test execution is sandboxed); timeouts + resource limits on parsing and model calls; repo/doc content treated as untrusted (prompt-injection safe: retrieved text is data, never instructions; the model has no tools that mutate anything in V1); secrets redacted from model context and logs; external-model transmission explicit and configurable; full delete (Storage + rows + vectors); provenance and failure states recorded.

**Reliability:** idempotent, resumable jobs with stage checkpoints; retries with backoff; dead-letter handling; per-repo cost cap; visible failure states.

**Evaluation harness (build in Phase 2, grow every phase):**
- 8–10 golden repos (small CLI, Next.js app, FastAPI service, monorepo, messy legacy).
- Graph sanity: known imports, entry points found, no dangling edges, coverage numbers.
- Retrieval: question → expected-files pairs; recall@k / MRR.
- Grounding: every cited path/symbol/line must exist (automated); unsupported-claim rate.
- Requirements: extraction quality, contradiction detection on seeded conflicts.
- Blueprint: automated validity + "accepted without edits" rate.
- **Headline test:** same task to Claude Code with vs. without a CodeAxis context package/MCP — completion, correct files, constraint violations, tokens.

---

# PART 10 — Build phases (vertical slices, with verifiable exit criteria)

Each phase begins with the **audit protocol (0.2)** and ends with an updated `STATUS.md`.

**Phase 0 — Foundation (≈ 1 wk):** monorepo (`apps/web`, `services/api`, `packages/schemas`, `supabase/migrations`, `evals/`, `docs/`), Docker, Supabase project, migrations + RLS, auth, project membership, job tracking, logging, `ModelService`, CI.
*Exit:* user signs in, registers a repo, sees a job with status; unauthorized access blocked (tested).

**Phase 1 — Ingestion (≈ 1–1.5 wk):** URL/ZIP/folder, safe extraction, snapshots, filtering, secret scan, language detection, file tree + Monaco viewer, progress UI, delete.
*Exit:* import malformed/oversized/malicious ZIP safely; browse files; delete removes everything.

**Phase 2 — Structure + graph + Inspector v1 (≈ 2–2.5 wk):** tree-sitter (TS/JS + Python), symbols, typed relationships with resolution status and evidence, manifests, entry points, git history, components (Leiden), explorer UI, Inspector with factual fields only, coverage/unresolved view, **eval harness started**.
*Exit:* on golden repos, edges trace to source; unresolved/ambiguous visible; Inspector uses the same relationships as the explorer.

**Phase 3 — Semantic layer (≈ 1.5–2 wk):** chunks, summaries, FTS + embeddings, hybrid retrieval, grounded chat with citations, Inspector explanations, origin tags (parsed/configured/inferred).
*Exit:* cited answers pass automated citation-exists check; **Pillar 1 complete — safe soft-launch point.**

**Phase 4 — Agent Context Engine (≈ 2–2.5 wk):** task + document ingestion, requirement/constraint extraction and validation, constraint ledger UI, context package assembly, task persistence/snapshots, exports, read-only MCP, with/without eval.
*Exit:* context rebuilt after session end; contradictions/gaps surfaced; measurable lift vs. no context.

**Phase 5 — Virtual Development Environment (≈ 2.5–3 wk):** blueprint schema v1, generation, validator, React Flow view, mapping panel, file-structure proposal, editing + versioning + locked decisions + partial regeneration, export → context package.
*Exit:* blueprint references only real entities; invalid blueprints rejected with reasons; edits preserve approved decisions.

**Phase 6 — Hardening & launch (≈ 1.5 wk):** incremental re-index, cost caps, rate limits, stale-index handling, failure-mode tests (malformed repos, failed model calls, conflicting requirements, unauthorized access), security review, onboarding demo repo, deployment docs, analytics.
*Exit:* Definition of Done (Part 11) fully checked.

Total ≈ 11–14 weeks solo. Compress by cutting scope (below), not by skipping evidence/validation.

**V1 scope cuts if needed:** TS/JS + Python only (others = file tree + summaries + search, labelled "basic"); name-based call edges only (SCIP later); folder = browser upload only; GitHub only (no GitLab/Bitbucket); MCP read-only; blueprints single-user; no repo write-back.

---

# PART 11 — Definition of Done (first publish)

**Codebase Intelligence:** supported repos ingest reliably; entities/relationships queryable; component details backed by source evidence; unresolved relationships and analysis failures visible; explorer and Inspector share one model.
**Agent Context Engine:** requirements/constraints stored as structured records; retrieved info has provenance; context reconstructable after a session; contradictions and missing info surfaced; model output validated before persisting.
**Virtual Dev Environment:** existing vs. proposed clearly distinguished; workflows conform to schema; existing components map to real entities; proposed files have responsibilities; blueprint editable and versioned; failures and unresolved decisions reported.
**Platform:** data isolated per user/project; long jobs show progress/errors; model cost and failures observable; automated tests + reproducible deployment instructions; provenance and limitations understandable to users.
**End-to-end journey passes:** import real repo → inspect → submit idea + documents → generate/edit blueprint → file structure → retrieve context package → source refs, mappings and requirements stay consistent.

---

# PART 12 — Repo layout and doc split for the build agent

```
codeaxis/
├─ apps/web/                  Next.js
├─ services/api/              FastAPI app + worker
│   └─ app/{repository,analysis,knowledge,context,blueprint,models}/   (code-level modules, one deployable)
├─ packages/schemas/          blueprint / inspector / task JSON schemas (shared contract)
├─ supabase/migrations/       single source of schema truth
├─ evals/                     golden repos + graders
└─ docs/
   ├─ STATUS.md               audit table (maintained by the agent, Part 0.2)
   ├─ 00-agent-instructions.md  (Part 0)
   ├─ 01-product-and-architecture.md (Parts 1–2)
   ├─ 02-models.md            (Part 3)
   ├─ 03-pipelines.md         (Parts 4–5)
   ├─ 04-data-and-api.md      (Parts 6–8)
   ├─ 05-security-and-eval.md (Part 9)
   └─ 06-roadmap-and-done.md  (Parts 10–11)
```
Feed the agent **`00` + one phase's docs at a time**, and require the audit before each phase.

---

# PART 13 — Decisions to settle before coding
1. Worker hosting (Fly.io / Railway / Cloud Run).
2. Embedding model: hosted vs. self-hosted (cost, quality, privacy).
3. Precision: tree-sitter-only vs. SCIP for TS/Python in Phase 2.
4. **Lock the blueprint node/edge taxonomy and schema v1 before Phase 5.**
5. Server-side analysis consent copy + data-retention policy.
6. Free-tier limits (repos, LOC, blueprints/month) → drives cost caps.
7. Launch headline: MCP/agent context vs. visual exploration.