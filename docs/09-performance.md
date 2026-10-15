# 09 — Performance and Caching

## Cache keys

| Artifact | Key | Invalidated by |
| --- | --- | --- |
| Parse result | file content hash + parser version | file edit, parser upgrade |
| SCIP index | commit SHA + indexer version | any source change in that language |
| Graph edges | commit SHA + analyzer version | re-analysis |
| Symbol summary | symbol body hash + prompt version | symbol edit, prompt change |
| Cluster labels | set of member symbol hashes | membership change |
| Layout | graph topology hash | topology change, not cosmetic edits |
| Taint results | commit SHA + CodeQL pack version | source change, pack update |

**The narration cache is the one that matters commercially**, because it is the only expensive layer. Keying summaries to the symbol body rather than the commit means a 4-file commit on a 2,000-file repository re-narrates 4 files.

## Incremental re-analysis

On a new commit:

1. Diff against the previous analyzed SHA.
2. Re-parse changed files.
3. Re-run SCIP for affected language units.
4. Recompute edges touching changed symbols.
5. **Expand the recompute set by one hop** to catch references broken by the change.
6. Copy everything else forward with provenance intact.

Step 5 is the one people skip and then spend a week debugging stale edges.

## Progressive delivery

Nothing waits for the whole pipeline. Each pass commits its results and emits an event; the workspace subscribes and unlocks surfaces as they become available.

A user exploring the file tree during Pass 1 is a success state, not a degraded one.

## Lazy narration

Narration below module level is generated on first open, not during the initial scan. This keeps cost proportional to attention rather than to repository size.

System, module and cluster summaries are eager; symbol summaries are lazy.

## Budgets

Targets on a 2,000-file repository:

| Stage | Target |
| --- | --- |
| Explorer usable | under 30 seconds |
| Map and X-Ray usable | under 3 minutes |
| Flows and journeys usable | under 10 minutes |
| Full narration complete | under 20 minutes |
| Incremental re-analysis, 10 changed files | under 60 seconds |
| Graph neighbourhood query, depth 2 | under 200 ms |
| Ask, end to end | under 8 seconds |

## Scaling notes

- Parsing is embarrassingly parallel per file; run a worker pool sized to available cores.
- SCIP indexing is per language unit and cannot be split below that boundary.
- CodeQL is the slowest pass by a wide margin. Run it last, asynchronously, and let Data Journeys unlock late rather than delaying everything else.
- Embedding generation is batched and cached by symbol body hash.
- Graph queries bound depth at the API layer, not in the client. An unbounded traversal request is rejected, not truncated silently.
