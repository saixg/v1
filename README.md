# CodeAxis

**From Codebase to Creation.**

CodeAxis turns a repository into a navigable software model — then uses that model to
give AI coding agents real context, and to plan new work on top of code that already
exists.

> ### ⚠️ Status: Refining phase
>
> CodeAxis is being actively refined and is **not yet a general-availability product**.
> The architecture, data model and analysis pipelines are in place; the surfaces on top
> of them are being hardened, re-measured and tightened repository by repository.
>
> Expect rough edges, changing interfaces, and features that are intentionally labelled
> *partial* rather than shipped. **This README will be replaced with a full product and
> setup guide once the working build is ready.** Watch the repository for that update.

---

## The problem

AI coding agents are good at writing code and bad at knowing where it goes.

Drop an agent into a repository of any real size and it guesses: which module owns this
behaviour, which function is the one actually called in production, which constraint will
break if you touch the auth boundary. Most tools answer those questions by handing a model
a pile of files and hoping. The answers sound right and cite paths that don't exist.

CodeAxis takes the opposite position: **facts about a codebase come from analysing the
codebase.** Parsers, resolvers and graph analysis produce the structure. Models only
interpret, explain and propose — they are never the source of a file path, a symbol, or a
relationship that gets stored as fact.

---

## How it works


Repo (URL · ZIP · folder)
│
▼
Ingest → Parse → Evidence-backed Graph → Components → Summaries → Index
│ (one shared software model, per snapshot)
│
├──► Codebase Intelligence
├──► Agent Context Engine
└──► Virtual Development Environment


One model, three surfaces. No feature gets its own private analyser or its own private
notion of truth.

---

## The three pillars

### 1. Codebase Intelligence

Understand a repository you didn't write.

- Repo overview: stack, entry points, size, and **how much of it was actually analysed**
- An interactive, workflow-style relationship explorer — repo → component → file → symbol
- Structural, dependency, architectural and historical (git) views, all projections of the
  same relationship set rather than four separate graphs
- A single **Component Inspector**: identity, purpose, source, incoming and outgoing
  relationships, execution paths, data relationships, tests, configuration, history,
  impact — and the evidence behind each
- Search and filters, with unresolved relationships visible instead of hidden

### 2. Agent Context Engine

Give an agent the context a senior engineer would have given it.

- Task intake from text, documents and diagrams → structured requirements, constraints
  and decisions
- A **constraint ledger**: public API surfaces, DB schema, auth boundaries, CI and lint
  rules, "do not touch" paths — each with scope, severity and origin
- Hybrid retrieval (exact symbol → metadata → keyword → vector → graph expansion) assembled
  into a **token-budgeted context package** where constraints are never truncated away
- Persistent task state that survives the end of a session
- Delivery as a REST API, exportable `CLAUDE.md` / `AGENTS.md` / `context.md`, and a
  **read-only MCP server**

### 3. Virtual Development Environment

Plan a feature against the repo that already exists.

- Describe what you want to build → a typed, versioned **Blueprint** (canonical JSON,
  validated before it is ever rendered)
- Every proposed capability mapped as **reuse / extend / new**, linked to real components
  with evidence — not generic architectural advice
- An n8n-style workflow view of the blueprint, where the diagram is a *view* of the
  blueprint rather than the only copy of it
- A suggested file structure that follows the repository's own conventions, with a purpose
  per file and new / modified / unchanged status
- Edit and regenerate the affected neighbourhood without losing approved decisions

**CodeAxis never modifies your source repository.** The output is a blueprint for review.

---

## What makes it different

**Every fact carries evidence.** A relationship, a symbol, a component membership — each
one records its snapshot, source file, line range, the analyser and analyser version that
produced it, and whether it was *parsed* from code, *configured* by an explicit rule, or
*inferred* by a model. The UI shows which. Inferred claims are never dressed up as facts.

**Unresolved is a valid answer.** A call edge the resolver can't confidently attribute is
marked `ambiguous`, not quietly promoted to `resolved`. An honest Unknown is worth more
than a plausible wrong edge — and the understanding-gap counters are visible rather than
buried.

**Snapshots keep versions from mixing.** Every derived record belongs to a
`(repository, snapshot)` pair, so results from two commits can never blend into one
confident-sounding mush.

**Model output is validated before it is persisted.** Structured schema, one retry, then
visible failure. Nothing unvalidated becomes state.

**Repository content is untrusted input.** Code, comments, READMEs and docs are data. They
never become instructions, never trigger tool calls, and repository code is never executed
during analysis. Secrets are scanned and redacted before anything reaches a model, an
embeddings API, or a log.

---

## Where things stand

| Area | State |
|---|---|
| Foundation — auth, projects, job tracking, migrations, model layer | In place |
| Ingestion — URL / ZIP / folder, safe extraction, snapshots, secret scan | In place |
| Structure & graph — parsing, symbols, typed relationships, components | In place, being refined |
| Semantic layer — chunks, summaries, retrieval, grounded answers with citations | In place, being refined |
| Agent Context Engine — requirements, constraints, context packages, MCP | Built, under measurement |
| Virtual Development Environment — blueprints, mapping, file structure | Built, under measurement |
| Hosting & onboarding | Web deployed; API hosting and public setup path in progress |

"Being refined" means the path works end to end and is now being tested against real
repositories, corrected, and measured — not that it is polished. Specific gaps and their
evidence are tracked in `docs/STATUS.md`.

---

## Scope, stated honestly

**In V1:** TypeScript / JavaScript and Python as first-class languages. GitHub. Read-only
MCP. Single-user blueprints. Other languages get file tree, summaries and search, clearly
labelled as basic.

**Not in V1:** behavioural or runtime analysis (static analysis cannot honestly produce
it), write-back to your repository, and anything that would require guessing where
evidence should be.

---

## Getting started

Setup and deployment instructions land with the next update, alongside the working build.
Until then this repository is best read as the architecture and the specification: start
with `CLAUDE.md` for the canonical build plan, then `docs/STATUS.md` for what is verified
today.

---

## Roadmap

- Harden the analysis pipeline against messy, large and legacy repositories
- Publish measured results from the evaluation harness — including the headline test: the
  same task given to a coding agent with and without a CodeAxis context package
- Incremental re-indexing, cost caps and stale-index handling
- Hosted API, onboarding demo repository, and real setup documentation
- Replace this README with the full product guide

---

*CodeAxis is in active development. Interfaces, schemas and surfaces will change. If a
claim here isn't backed by evidence you can click through to, treat it as a gap worth
reporting.*
