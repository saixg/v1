# 08 — Security and Privacy

CodeAxis ingests source code it did not write, on behalf of users who may not own it. Two separate threats follow, and they need separate controls.

## Threat 1 — the repository is hostile

An imported repository may carry malicious build scripts, compromised dependencies, or prompt injections planted in comments and documentation.

### Never execute imported code

No install, no build, no test run, no script evaluation, no dynamic import of repository modules. Every analyzer is static.

This single rule removes most of the attack surface. It is also why runtime tracing is out of scope for 1.0 — adding it means adding an execution sandbox with a much higher bar.

### Sandbox the checkout

Analysis runs in a container with:

- no outbound network
- a read-only mount of the source
- a disk quota and a wall-clock limit
- a non-root user
- no access to host credentials or the application database

### Treat all repository text as untrusted data

Comments, READMEs, docstrings, commit messages and string literals are wrapped as untrusted content for the narrator. The citation validator drops whatever an injection produces.

Test against `benchmarks/injections/`. Target: zero behavior change.

### Resource bombs

Cap archive expansion ratio, file count, individual file size, total size, and parse time per file. A file exceeding the parse-time cap becomes an `unanalyzed` node — it does not stall the pass.

---

## Threat 2 — the code is confidential

### Disclose before uploading

The pre-scan screen states exactly what will be sent to the model provider, **before any analysis begins**. This is the first question a paying team asks, and SaaS-only competitors are weakest here.

### Never send detected secrets

Secret scanning runs **before** narration. Matched spans are redacted from every evidence bundle. The finding is reported in X-Ray; the value never leaves the sandbox.

Test against `benchmarks/secrets/` — a repository seeded with fake credentials. Target: zero secret values in any outbound bundle.

### Exclusions are honored upstream

An excluded path is never parsed, never embedded, never narrated. Exclusion is applied at the scan walker, not as a display filter.

### Retention is explicit

Source is retained only while the workspace exists. Deleting a workspace deletes the checkout, the graph, the embeddings and the cached narration. **Show a deletion receipt.**

### Local-first path

For folder import, parsing and graph construction can run in the browser via the File System Access API, with only the graph and the evidence bundles leaving the machine.

This is a meaningful trust differentiator, not a convenience feature. Build the scan and resolve passes so they can run in either environment.

---

## Access and sharing

- A shared **Repo Card** exposes project identity and structure, never source spans.
- Workspace sharing is per-workspace and revocable.
- A shared link never grants the recipient the Ask surface against a private repository.
- Workspace access is checked on every graph-returning endpoint, not only at page load.

## Dependency and advisory handling

X-Ray reports risky or outdated dependencies from manifest data and public advisory databases. It never fetches or executes a dependency to inspect it.
