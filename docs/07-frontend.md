# 07 — Frontend

## Screens

### 1. Open a codebase

Three options: import from GitHub (URL plus branch or commit), open a local folder, upload a ZIP.

Before analysis: show which files will be included, allow exclusion of generated artifacts, dependencies, build output and sensitive paths, and **state what will be sent to the model provider**.

### 2. Analysis

Transparent, pass-by-pass progress. Distinguish completed analysis from optional or unsupported analysis. Surfaces unlock as their pass completes (`05-analysis-pipeline.md`). The Understanding Gap report updates live.

Never show a spinner with nothing behind it.

### 3. Workspace

Persistent, pinned to one commit shown in the header.

| Section | Purpose |
| --- | --- |
| Overview | Project DNA, Repo Card, Understanding Gap |
| System Map | Architecture and dependency views |
| City View | Complexity and churn projection |
| Execution Flows | Trace application behaviors |
| Data Journeys | Follow information through the system |
| Impact | Consequences of a potential change |
| History | Repo Biography |
| X-Ray | Structural findings |
| Explorer | Files, symbols, Purpose Atlas |
| Ask | Grounded investigation |
| Docs | Generate and browse explanations |

### 4. Component Inspector

A persistent side panel, opened from every surface, with the six lenses from `02-features.md`. **Clicking an item in any visualization leads to the same inspector on the same entity.** Users must never feel they are moving between disconnected tools.

---

## Graph rendering

A repository graph is far larger than a readable picture. **The user never sees the whole graph, only a bounded neighbourhood of what they selected.**

### Renderer

SVG degrades past roughly a thousand nodes; canvas past about five thousand; WebGL stays usable into the ten-thousand range. Use a WebGL renderer with **graphology** as the in-memory graph structure, **ForceAtlas2** layout in a worker, and an overlap-removal pass after convergence.

### Level of detail

| Zoom | Nodes shown | Edges |
| --- | --- | --- |
| System | Top-level modules, 10–40 nodes | Aggregated, weighted by underlying edge count |
| Module | Files in one module plus boundary neighbours | Imports |
| File | Symbols in one file plus direct callers and callees | Calls |
| Symbol | The Inspector, not the graph | — |

Expansion is always from a selection. **There is no "show everything" control** — the view it produces cannot be read, and its existence invites users to judge the product by its worst screen.

### Layout stability

Layout is computed once per commit and **stored with the graph**. A node sits in the same place every time the user returns. This is what makes a map navigable rather than merely pretty.

### Map modes

Architecture and Dependency are edge filters over the same stored layout.

Execution and Data modes are different: they render a **path**, not a graph — an ordered sequence with the cited span beside each step. Config mode is a **table**, because configuration influence is a lookup rather than a topology.

Do not force all six "map modes" from the original brief into one graph widget. Three of them are not graphs.

### Status filtering

A verified-only toggle is present on every graph view and is applied **server-side** (`?status=verified`), never by client-side filtering.

### City View

A separate projection, not a map mode. Districts are modules, footprint is LOC, height is cyclomatic complexity, colour intensity is churn over 180 days. Every dimension opens to the figure behind it. Designed to be screenshotted.

---

## UI rules

- Every claim renders with its status chip. Verified, Inferred and Unknown are visually distinct, not a tooltip.
- Every claim has **Show the code**.
- Execution Stories are labelled "static trace" wherever they appear.
- An unresolved hop renders as a terminal node with its reason, styled as an endpoint — not as an error and not as empty space.
- No overall confidence percentage anywhere in the product.
