# 03 — System Architecture

## Layers

```
┌──────────────────────────────────────────────────────────────────┐
│ INGESTION                                                        │
│ Clone at a pinned SHA · unpack · filter · detect languages and   │
│ frameworks · isolate the checkout in the sandbox                 │
└───────────────────────────┬──────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│ DETERMINISTIC ANALYSIS                                           │
│ ┌────────────┐ ┌────────────┐ ┌─────────────┐ ┌───────────────┐  │
│ │ Syntax     │ │ Semantics  │ │ Frameworks  │ │ Flow          │  │
│ │ tree-sitter│ │ SCIP       │ │ routes,     │ │ CodeQL taint, │  │
│ │ files,     │ │ indexers   │ │ models,     │ │ git history,  │  │
│ │ symbols,   │ │ bind refs  │ │ jobs,config,│ │ metrics       │  │
│ │ spans      │ │ to defs    │ │ entrypoints │ │               │  │
│ └────────────┘ └────────────┘ └─────────────┘ └───────────────┘  │
└───────────────────────────┬──────────────────────────────────────┘
                            ▼
┌══════════════════════════════════════════════════════════════════┐
║ REPO MODEL — the one source of truth                             ║
║ Postgres: nodes, edges, one provenance row per edge,             ║
║ keyed by commit SHA and analyzer version                         ║
║ pgvector for symbol embeddings · blob store for pinned source    ║
└═══════════════════════════┬══════════════════════════════════════┘
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│ QUERY AND NARRATION                                              │
│ ┌─────────────────────┐ ┌──────────────┐ ┌────────────────────┐  │
│ │ Evidence assembler  │ │ Narrator     │ │ Citation validator │  │
│ │ graph walk + hybrid │ │ explains the │ │ drops any claim    │  │
│ │ search              │ │ bundle only  │ │ the graph denies   │  │
│ └─────────────────────┘ └──────────────┘ └────────────────────┘  │
└───────────────────────────┬──────────────────────────────────────┘
                            ▼
┌──────────────────────────────────────────────────────────────────┐
│ SURFACES                                                         │
│ Project DNA · Understanding Gap · Guided Tour · System Map ·     │
│ City View · Purpose Atlas · Execution Stories · Data Journeys ·  │
│ Impact · Biography · X-Ray · Ask · Docs Export                   │
│ ALL of them open the same Component Inspector                    │
└──────────────────────────────────────────────────────────────────┘
```

**No arrow runs upward.** No surface and no model output ever writes back into the Repo Model.

## Technology choices and why

| Layer | Choice | Reason |
| --- | --- | --- |
| Syntax | tree-sitter | Broad grammar coverage at speed, error-tolerant on files that do not compile. It gives syntax, **never resolved symbols** — it cannot be the only parser |
| Semantics | SCIP indexers (`scip-typescript`, `scip-python`) | SCIP is a published protocol whose indexers emit occurrences with symbol roles — exactly the definition-and-reference binding the graph needs. Adding a language becomes adding an indexer, not changing engine code |
| Frameworks | Hand-written extractors per framework | Routes, ORM models, background jobs and middleware chains are conventions. No generic parser recovers them, and they are what makes Execution Stories possible |
| Flow and taint | CodeQL with custom model packs | Taint tracking with declared sources, sinks and sanitizers is solved in CodeQL. Custom sources and sinks are declared in YAML data extension files, so a new framework is a data change not an engine change |
| Store | Supabase Postgres with pgvector | Node and edge tables with provenance columns answer every 1.0 query. A graph database is justified only when real query patterns demand it, which they will not at single-repository scale. Repo Model tables live in a `codeaxis` schema excluded from the Data API — PostgREST never serves graph rows, `services/api` is the only HTTP path to them. See `04-graph-schema.md` for the role split |
| Narration | Any frontier model, swappable behind an interface | Product identity sits in the graph, not the provider |
| Rendering | WebGL renderer with graphology | See `07-frontend.md` |

## Service decomposition

| Service | Responsibility |
| --- | --- |
| `services/api` | HTTP surface, auth, workspace lifecycle, read queries against the Repo Model. Connects with the read-only `codeaxis_api` role; includes a mechanics-only `GET /symbols/{sid}` that returns a node's raw facts and provenance, no narration |
| `services/worker` | Runs analysis passes as jobs; the only thing that writes to the Repo Model. Connects with the read-write `codeaxis_worker` role |
| `packages/analyzers/*` | Pure functions: source in, nodes and edges with provenance out. No DB access |
| `packages/graph` | Schema, migrations, typed query layer. The only module that knows SQL |
| `packages/evidence` | Bundle assembly and retrieval. Read-only against the graph |
| `packages/narrate` | Prompts, generation, citation validator. Depends on `evidence`, never on `graph` directly |
| `apps/web` | Workspace shell and all surfaces |

Analyzers not touching the database is deliberate: it makes each one unit-testable against a fixture repository with no infrastructure, and it keeps provenance assembly in one place.

## The one-way rule

Analysis writes to the Repo Model. Surfaces read from it. Model output never writes back.

This prevents the failure mode where a generated explanation becomes an input to the next generated explanation and the system drifts away from the code while still appearing self-consistent. Enforce it structurally: `packages/narrate` must have no write path to `packages/graph`.
