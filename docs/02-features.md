# 02 — Feature Set

Thirteen surfaces, one graph. The "powered by" column is what makes each one true rather than plausible.

| Surface | Question it answers | Powered by |
| --- | --- | --- |
| Project DNA | What am I looking at? | Manifests, framework detectors, entry-point analysis; model narrates detected facts only |
| Understanding Gap | What does CodeAxis not know here? | Resolver coverage counters per area |
| Guided Tour | Where do I start, for my goal? | Graph centrality plus dependency order, filtered by goal |
| System Map | How is this organized and connected? | Import and call edges, module aggregation |
| City View | Where is complexity concentrated? | Git churn crossed with cyclomatic complexity and fan-in |
| Purpose Atlas | Where is the code for behavior X? | Embedding clusters over symbols, labelled once and pinned to the commit |
| Execution Stories | What happens when a user does X? | Framework-declared entry points walked through the resolved call graph |
| Data Journeys | Where does this data come from and go? | CodeQL taint tracking with declared sources and sinks |
| Impact Analysis | What breaks if I change this? | Reverse reachability over call and import edges, plus test linkage |
| Repo Biography | How did this codebase get here? | Git history: eras, refactors, file birth, authorship concentration |
| X-Ray | What is structurally risky? | Cycles, fan-in/out, function size, secret detection, dependency advisories |
| Ask CodeAxis | Can I ask anything and verify it? | Hybrid retrieval over the graph, validated citations |
| Docs Export | Can I hand this to someone? | Rendered from the same model, not re-generated |

---

## Surface detail

### Project DNA

A structured identity profile: project identity, primary purpose, application type, frontend, backend, data layer, entry points, main execution flow, architectural style. Every row carries a status chip. A row cannot be `verified` without a source span.

The shareable **Repo Card** is a condensed Project DNA plus health and gap summary. It exposes identity and structure, never source spans.

### Understanding Gap

Per-area table: area, understanding status, reason. Derived entirely from `01-evidence-model.md` counters. Updates live as each analysis pass completes.

### Guided Tour

Goal-based reading paths over the graph, not a generic file list.

- **New here:** purpose → entry point → main components → one workflow → data and integrations → tests and config
- **Fixing a bug:** feature location → entry point → execution path → related state → relevant tests → likely failure site
- **Adding a feature:** implementation boundary → related components → similar features → data models and contracts → extension points → tests

Ordering comes from graph centrality within the goal's filtered subgraph. Progressive disclosure: each stop has four depths (one-line purpose, component behavior, functions and dependencies, full execution path).

### System Map

Architecture and Dependency views as edge filters over one stored layout. Expansion is always from a selection — there is no "show everything" control. See `07-frontend.md` for level-of-detail rules.

### City View

A separate projection, not a map mode. Districts are modules, building footprint is lines of code, height is cyclomatic complexity, colour intensity is git churn over the last 180 days. A building both tall and hot is a hotspot. Every dimension opens to the figure behind it.

This is the one screen designed to be screenshotted and shared. It earns its place by being memorable, and it stays honest because every dimension is a measured number.

### Purpose Atlas

Code grouped by function (authentication, ingestion, retrieval, inference, API handling, UI, persistence, config, observability) rather than directory. Clusters are **computed once per commit, stored, and recomputed only when member symbols change**. Labels that shift between runs destroy trust — this is a hard requirement, not an optimization.

### Execution Stories

A static trace from a framework-declared entry point. Each step shows: concise explanation, exact file and symbol, link to source, incoming and outgoing data where known, next component, and any unresolved assumption.

Step-through: play, pause, expand implementation, jump to a branch or error path, see where the flow ends. Where a step cannot be resolved, **the story says so and stops** — it never invents the next hop.

Labelled "static trace" in the UI, always.

### Data Journeys

Taint paths from declared sources to declared sinks, each stage showing what enters, what processing occurs, what leaves, where it is stored, what consumes it, and what checks are visible.

Sensitive-data journeys are the high-value case: auth tokens, user identifiers, uploads, personal data, DB queries, external API calls, LLM prompt construction. Show the exact path, not merely the presence of a sink. Incomplete or inferred journeys are marked as such.

### Impact Analysis

Reverse reachability from a selected symbol, bounded by depth: direct callers, indirectly dependent services, affected routes, associated tests, config and data behavior involved.

Reports **potentially affected components**, never certainty about breakage. The "compare two approaches" idea is deferred — it requires simulating changes, which 1.0 does not do.

### Repo Biography

Git history as narrative: eras, major refactors, module birth and death, abandoned directions, authorship concentration and bus factor. Every claim computed from git, never narrated from code.

### X-Ray

Structural findings, each stating its evidence and its measurement:

- Circular module dependencies
- Oversized or high-complexity functions
- Modules with excessive incoming dependencies
- Potentially sensitive configuration
- Risky or outdated dependencies
- Unclear layer boundaries

Each finding separates **objective measurement** from **suggested investigation**. A 500-line file is measurable; whether it is badly designed requires context. Never assert a design judgement as fact.

Dead-code and error-handling-gap detection are excluded from 1.0: statically unreliable (framework-invoked and dynamically dispatched symbols produce false positives) and already covered by linters.

### Ask CodeAxis

Grounded Q&A over the graph. Every answer: direct response, source locations, evidence, related components, explicit uncertainty. Every claim carries a **Show the code** action.

The "what evidence contradicts this?" action is **not shipped**. Language models do not reliably retrieve disconfirming evidence on request, and an unreliable contradiction check is worse than none because it reads as verification. The implemented substitute is factual: the inspector lists all edges touching the entity, including those the answer did not mention.

### Docs Export

Architecture walkthrough, onboarding guide, component reference, sequence diagrams, technical handoff. Rendered from the stored model with source references intact — never regenerated by a fresh model pass.

---

## Component Inspector

One panel, opened from every surface, showing a component through six lenses. Each lens is a **distinct query against the graph**, not a different prompt over the same text.

| Lens | Query |
| --- | --- |
| Purpose | Stored symbol summary plus cluster membership |
| Mechanics | Source span plus callee list plus internal control flow summary |
| Connections | Incoming and outgoing edges by type, with status |
| Data | Taint paths touching this symbol, models read and written |
| Security | Auth middleware on reaching routes, secret proximity, sink adjacency |
| Change impact | Reverse reachability plus linked tests |

This is what keeps thirteen surfaces from feeling like thirteen tools. Clicking anything in any visualization must land here, on the same entity, without losing place.
