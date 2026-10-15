# 12 — Build Order

Dependency order, not a schedule. Each step is testable on its own and unblocks the next.

## 1. Ingestion, parse, Repo Model, Explorer, Inspector shell

Nothing else can be built or tested until a graph exists and can be opened.

- `packages/graph`: schema, migrations, typed query layer
- `packages/analyzers/scan`: tree-sitter pass, file and symbol nodes with spans
- `services/api`: workspace lifecycle, `/repos`, `/status`, `/graph`
- `services/worker`: job runner, pass contract, event emission
- `infra/sandbox`: the analysis container with no network and no exec path
- `apps/web`: workspace shell, Explorer, Inspector with the Mechanics lens only

**Exit:** import a repository, see its file tree and symbols, click a symbol and see its source. Sandbox tests pass.

## 2. SCIP resolution, framework extractors, git analytics

This is where `verified` status becomes real. Everything after this point inherits its quality — do not rush it.

- `packages/analyzers/resolve`: SCIP indexer drivers for TypeScript and Python, `unknown` edges with reasons
- `packages/analyzers/frameworks`: Express, Next.js, NestJS, FastAPI, Django, Flask
- `packages/analyzers/git`: churn, co-change, authorship, file lifecycle
- Coverage counters written by each analyzer as it runs

**Exit:** the call graph is resolved, coverage figures match a manual count on a sampled module, and reproducibility holds across two runs.

## 3. System Map, Project DNA, Understanding Gap, X-Ray, City View, Repo Biography

All deterministic, all readable from the graph alone. Project DNA's narration is a thin layer over detected facts, so it comes here rather than with the rest of narration.

- WebGL renderer, stored layouts, level of detail
- Verified-only filtering end to end
- Status chips and **Show the code** everywhere

**Exit:** a user can orient themselves on an unfamiliar repository without any generated prose beyond Project DNA.

## 4. Execution Stories, Impact Analysis, Purpose Atlas

Graph traversal plus clustering.

- Entry-point declarations consumed from framework extractors
- Terminal-step rendering for unresolved hops
- Reverse reachability with test linkage
- Embedding, clustering, label pinning per commit

**Exit:** flows terminate honestly, impact matches manual traces, cluster labels are stable across runs.

## 5. CodeQL integration and Data Journeys

The hardest analysis layer, isolated so its difficulty cannot block anything above it.

- CodeQL driver in the sandbox
- CodeAxis model pack: sources, sinks, sanitizers as YAML data extensions
- Path rendering with per-stage spans
- Async unlock so a slow taint pass does not delay the workspace

**Exit:** every rendered journey is a CodeQL result, no path is model-generated.

## 6. Evidence assembler, narrator, citation validator, Ask

Built last because the validator needs a complete graph to validate against.

- Hybrid retrieval, graph expansion, budgeting with drop tracking
- Generation contract and provider abstraction
- Validator with drop logging and grounding score
- Injection and secret benchmarks wired into CI

**Exit:** global gates 1, 3 and 4 pass.

## 7. Guided Tour and Docs Export

Pure composition over everything already built. No new analysis.

**Exit:** export output is reproducible byte-for-byte on the same commit.

---

## Parallelization

Steps 3 and 4 can overlap once step 2 is stable. Step 5 can start as soon as the sandbox from step 1 exists, since it depends on the checkout rather than the graph. Step 6 cannot start meaningfully before step 2 finishes — a validator with an incomplete graph will drop correct claims and teach you the wrong lesson.

## The decision that defines the product

If a tradeoff arises between a surface looking impressive and a claim being verifiable, **verifiability wins**.

Generated repository wikis are already free and widely available. What is not available is a tool that tells you precisely how much of your codebase it actually resolved, and refuses to narrate the rest.
