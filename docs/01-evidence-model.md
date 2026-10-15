# 01 — Evidence Model

This is the spine of the product. It is not a feature of one screen; it is a column on every table in the database and a check on every generated sentence.

## Four states

| Status | Assigned when | Example |
| --- | --- | --- |
| `verified` | A compiler-grade or framework-aware pass bound the relationship to a specific definition | `ChatService.query` calls `RetrievalService.search`, resolved by a SCIP indexer |
| `inferred` | A heuristic, name match, or model-generated relationship with no binding | A route handler probably writes to the `documents` table, matched by ORM naming |
| `unknown` | The analyzer reached a construct it cannot follow | Dynamic import, reflective call, string-dispatched task name |
| `unanalyzed` | The file is in a language CodeAxis does not parse at this tier | A Scala file in an Inventory-tier repository |

`unknown` and `unanalyzed` are **reported as coverage**, never as absence of structure. The difference matters: `unknown` means "there is something here I cannot follow"; `unanalyzed` means "I did not look."

Status is assigned by the analyzer. **The model never assigns status.** Model-produced edges are always `inferred`.

## Provenance record

Every edge and every node carries:

```
analyzer           text      scip-typescript | tree-sitter | codeql | framework:fastapi | git | llm
analyzer_version   text      pinned version string
status             enum      verified | inferred | unknown | unanalyzed
source_file        text      repo-relative path, nullable for llm edges
line_start         int       nullable
line_end           int       nullable
commit_sha         text      the pinned analysis commit
confidence         real      nullable; only where the analyzer itself emits one
reason             text      nullable; required when status = unknown
```

`reason` on an `unknown` edge is user-facing copy: `dynamic_import`, `reflective_call`, `unresolved_import`, `string_dispatch`, `depth_limit`. The Understanding Gap report groups by it.

Two consequences follow, and both are worth protecting:

1. A re-analysis with a newer analyzer can **upgrade** an `inferred` edge to `verified` without re-running the model.
2. Any view can be filtered to `status = verified` only. This must work server-side on every graph-returning endpoint — a security reviewer will do it first.

## The citation validator

Model output passes through the validator before it reaches the user. This is a hard gate, not a lint.

For each claim the model made, check:

1. The cited symbol exists in the graph at the pinned commit.
2. The cited line range falls within that symbol's span.
3. Any relationship the claim asserts exists as an edge.

**Claims failing validation are dropped, not rephrased.** The surviving share is the grounding score. If the score falls below threshold, the whole answer is replaced by a statement that evidence was insufficient, listing the entities that were searched.

This mirrors the established pattern in grounded repository Q&A: every answer span cites a `file:line` range, every citation is re-verified against the file on disk, and a per-answer grounding score reports how well the answer was supported.

The validator is also the second line of defense against prompt injection: an injected instruction that produces an uncited claim is dropped before display.

## The Understanding Gap report

Coverage is **computed, not estimated**. Per area of the repository:

- resolved symbols / total symbols
- resolved call edges / total call sites
- the specific constructs that blocked resolution, grouped by `reason`
- language tier for files in that area

**No overall confidence percentage is ever shown.** A single number invites exactly the false confidence this model exists to prevent. Report per-area counts and the blocking constructs, and let the user judge.

The report gives actionable next steps derived from `reason` values: inspect a dynamic import, configure an additional framework extractor, confirm whether a dependency is active.

## Implementation note

Write the coverage counters inside each analyzer as it runs. Coverage derived after the fact from the finished graph cannot distinguish "no edge because there is no call" from "no edge because resolution failed," and that distinction is the entire feature.
