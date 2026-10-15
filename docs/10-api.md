# 10 — API Surface

## Endpoints

Import is split into stage and confirm rather than a single `POST /repos`. docs/07 and docs/11 need an exclusion preview before analysis starts: the user has to see which files will be analyzed and can exclude sensitive paths, and what gets analyzed has to match that preview exactly. So the checkout is staged first, and the scan only starts once the user confirms. Staging fetches the source, pins the SHA (resolved from the checkout for GitHub, `zip:<sha256[:12]>` for ZIP, caller-supplied for a local folder), registers the repo, and returns the preview from the same directory walk the scan uses. Confirm reads the checkout path from the repo row, not from the request. A temporary checkout (GitHub/ZIP) is deleted when the scan ends; a local folder is never deleted. Ingestion failures (bad URL, unknown ref, missing folder, ZIP over the size or expansion-ratio cap, zip-slip entry) return 400.

```
POST   /repos/stage                stage a github or local source; body {kind:"github", remote_url, ref?}
                                   or {kind:"local", local_path, commit_sha}; returns
                                   {id, commit_sha, local_path, preview:[{path, kind, language,
                                   size_bytes, included, reason}]}
POST   /repos/upload               stage a ZIP (multipart field "file"); same response as /repos/stage
POST   /repos/{id}/confirm         body {commit_sha, excluded_paths:[glob]}; starts the scan;
                                   409 if a scan was already started
GET    /repos/{id}/status          pass-by-pass progress and unlocked surfaces
GET    /repos/{id}/coverage        Understanding Gap report
GET    /repos/{id}/dna             Project DNA with per-fact status
GET    /repos/{id}/graph           nodes and edges for a neighbourhood
         ?root=&depth=&kinds=&status=verified&zoom=system|module|file
GET    /repos/{id}/symbols/{sid}   inspector payload, all six lenses
GET    /repos/{id}/flows           entry points
GET    /repos/{id}/flows/{fid}     one execution story, step by step
GET    /repos/{id}/journeys        taint paths; ?data_class=auth_token|upload|pii
GET    /repos/{id}/impact/{sid}    reverse reachability plus tests
GET    /repos/{id}/findings        X-Ray findings, each with evidence
GET    /repos/{id}/history         Biography: eras, churn, authorship
GET    /repos/{id}/clusters        Purpose Atlas
POST   /repos/{id}/ask             question; returns answer, citations, grounding score
GET    /repos/{id}/export/{kind}   onboarding | architecture | handoff
POST   /repos/{id}/reanalyze       new SHA, incremental
DELETE /repos/{id}                 purge source, graph, embeddings, cache
```

## Two conventions that carry the product principle into the API

**1. Every response object carries its provenance block.**

```json
{
  "id": 8812,
  "name": "RetrievalService.search",
  "provenance": {
    "analyzer": "scip-python",
    "analyzer_version": "0.6.1",
    "status": "verified",
    "source_file": "app/services/retrieval.py",
    "line_start": 44,
    "line_end": 91,
    "commit_sha": "a3f91c2"
  }
}
```

**2. `status=verified` is a filter on every graph-returning endpoint**, applied server-side. A client that renders only verified data must be able to do so without client-side filtering.

## Envelopes

Graph response:

```json
{
  "commit_sha": "a3f91c2",
  "zoom": "module",
  "nodes": [ ... ],
  "edges": [ ... ],
  "truncated": { "nodes": 0, "edges": 14, "reason": "depth_limit" },
  "coverage_note": "3 call sites in this area are unresolved (string_dispatch)"
}
```

Ask response:

```json
{
  "answer": "...",
  "claims": [
    { "text": "...", "citations": [ { "node_id": 8812, "line_start": 67, "line_end": 67 } ] }
  ],
  "grounding_score": 0.92,
  "dropped_claims": 1,
  "searched_entities": [ 8812, 8840 ],
  "insufficient_evidence": false
}
```

When `insufficient_evidence` is true, `answer` states that and `claims` is empty. The client renders the refusal, not a degraded answer.

Execution story step:

```json
{
  "index": 3,
  "symbol": { "id": 8812, "name": "...", "path": "...", "line_start": 44, "line_end": 91 },
  "explanation": "...",
  "inputs": "...", "outputs": "...",
  "next": 8840,
  "terminal": false,
  "unresolved": null
}
```

A terminal step caused by an unresolved hop sets `terminal: true` and `unresolved: { "reason": "dynamic_import", "source_file": "...", "line_start": 88 }`.

## Errors and limits

- Unbounded traversal requests are **rejected**, not silently truncated.
- `depth` is capped server-side; the cap is returned in the response.
- Analysis status is polled via `/status` or streamed via SSE on the same path.
- Workspace access is checked on every endpoint, including graph reads.
