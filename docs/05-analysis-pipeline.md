# 05 — Analysis Pipeline

## Language tiers

Language support is a tier, not a yes or no, and **the tier is shown to the user**.

| Tier | Languages at 1.0 | What the graph gets |
| --- | --- | --- |
| Full | TypeScript, JavaScript, Python | Resolved calls, types, routes, models, taint, test linkage |
| Structural | Go, Java, Rust, C#, Ruby, PHP | Symbols, imports, name-resolved calls marked `inferred` |
| Inventory | everything else tree-sitter parses | Files and symbols only, reported as `unanalyzed` in coverage |

The split exists because a name-only resolver mis-wires calls across language boundaries, and a graph that silently mis-wires is worse than one that admits a gap. **Tier placement is a correctness statement, not a roadmap note.** Do not promote a language to Full without its SCIP indexer and framework extractors.

---

## Pass 1 — Scan and parse

Walk the checkout, hash files, apply exclusions, detect language per file, parse with tree-sitter, emit `file` and `symbol` nodes with spans.

**This pass must never fail on a file.** Unparseable files become Inventory-tier nodes with `status = unanalyzed`. A parse error is data, not an exception.

Outputs: `file`, `symbol`, `directory` nodes; `contains` edges (directory → file, file → symbol); per-file parse status. `defines` edges — binding a symbol to the definition that satisfies it — are emitted by the SCIP resolver in Pass 2, not here: tree-sitter gives syntax, never resolved symbols (`03-architecture.md`).
Unlocks: Explorer and file tree.

## Pass 2 — Resolve, graph, git

**Semantic resolution.** Run the SCIP indexer for each Full-tier language. Bind references to definitions and emit `calls`, `defines`, `implements`, `extends` edges as `verified`. Unresolved call sites emit an `unknown` edge carrying `reason` — this is what the Understanding Gap report counts. Structural-tier languages get name-resolved `calls` edges marked `inferred`, scoped within a single language to avoid cross-language mis-wiring.

**Framework extraction.** Per-framework modules emit `route`, `model`, `test`, `external_service`, `config_value` nodes and their edges. Target set for 1.0: Express, Next.js, NestJS, FastAPI, Django, Flask. Each extractor declares the entry-point kinds it can produce; Execution Stories reads that declaration.

**Git analytics.** Commit count and author spread per file, co-change pairs, file birth and death, churn over the last 180 days. This feeds City View, Repo Biography, and X-Ray hotspots.

Outputs: the resolved call and import graph, framework nodes, git metrics, coverage counters.
Unlocks: System Map, City View, Repo Biography, X-Ray.

## Pass 3 — Flows and taint

**Execution Stories.** Start at framework-declared entry points (route handler, CLI command, event subscriber, scheduled job). Walk the resolved call graph breadth-first. Stop at a depth limit, an external service, or an `unknown` edge. **Render the stop with its reason — never skip it and continue to a guessed next hop.**

**Data Journeys.** Run CodeQL taint queries with a CodeAxis model pack declaring the sources and sinks the product cares about.

| Sources | Sinks |
| --- | --- |
| request bodies and query params | database writes |
| uploaded files | external HTTP calls |
| auth tokens | LLM prompt construction |
| user identifiers | logs |
| environment and config reads | file writes, queue publishes |

Custom sources and sinks go in YAML data extension files, not engine code. Adding a framework's conventions is then a data change.

**Impact.** Reverse reachability over `calls` and `imports`, bounded by depth, annotated with tests that reach each affected symbol. Reports affected components; never asserts breakage.

Unlocks: Execution Stories, Data Journeys, Impact Analysis.

## Pass 4 — Narrate and validate

Hierarchical summarization, bottom-up: symbol → file → cluster → system. Each level receives **only the level below it plus the graph edges**, never raw repository text at system level.

Every generated sentence carries a citation. The validator (`01-evidence-model.md`) removes those that fail.

Clustering for the Purpose Atlas runs here: embed symbols, cluster, label each cluster once, store as `cluster` nodes with `belongs_to` edges. **Recompute only when member symbols change.**

Caching: `(symbol_hash, analyzer_version, prompt_version)`. A commit touching 4 files re-narrates 4 files.

**Narration below module level is lazy.** A symbol summary is generated when a user first opens that symbol, not during the initial scan. This keeps cost proportional to attention rather than repository size.

Unlocks: Project DNA, Guided Tour, Ask, Docs Export.

---

## Pass contract

Every pass:

1. Reads only the pinned checkout and the graph written by prior passes.
2. Emits nodes and edges with complete provenance.
3. Writes coverage counters for what it resolved and what blocked it.
4. Commits its results and emits a completion event before the next pass starts.
5. Is idempotent for a given `(commit_sha, analyzer_version)`.

Point 4 is what makes progressive delivery work: a user exploring the file tree during Pass 1 is a success state, not a degraded one.
