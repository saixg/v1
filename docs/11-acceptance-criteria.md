# 11 — Acceptance Criteria

Treat this as the test spec. Each surface ships when it passes its own criterion on the benchmark set: real repositories across both Full-tier languages, including at least one repository over 1,000 files.

## Per surface

| Surface | Ships when |
| --- | --- |
| Ingestion | A GitHub URL, a ZIP and a local folder all produce a graph pinned to a stated SHA, and the exclusion preview matches exactly what was analyzed |
| Project DNA | Every stated fact carries a status, and no fact is `verified` without a source span |
| Understanding Gap | Coverage figures are reproducible from the graph and match a manual count on a sampled module |
| System Map | Opening any node expands only its neighbourhood, layout is identical between sessions, and verified-only filtering works server-side |
| City View | Height and heat are traceable to a complexity figure and a churn count the user can open |
| Purpose Atlas | Cluster labels are identical across two analyses of the same commit |
| Guided Tour | Each route's ordering is derived from the graph and every stop links to a real entity |
| Execution Stories | For the benchmark flows, each step cites a real span, and every unresolved hop renders as a terminal step with its reason rather than being omitted |
| Data Journeys | Every path is a CodeQL result with a source, a sink and the intermediate spans. No path is model-generated |
| Impact | Reverse reachability matches a manual trace on sampled symbols, and listed tests actually reach the symbol |
| Repo Biography | Every era, refactor and bus-factor claim is computed from git, not narrated from code |
| X-Ray | Every finding states its evidence and its measurement, and no finding asserts a design judgement as fact |
| Ask | Grounding score is computed per answer, failing citations are dropped, and insufficient evidence produces a refusal rather than a plausible answer |
| Docs Export | Output is rendered from stored model data with citations intact, byte-identical across two runs on the same commit |
| Inspector | Every surface opens it on the same entity with the same six lenses |

## Global gates

These block release regardless of surface completeness.

1. **No uncited claim reaches the UI.**
   Enforced by the validator. Tested with an adversarial question set whose answers are not in the repository. **Target: zero fabricated answers.**

2. **No static trace is presented as runtime observation.**
   Copy review on every surface that renders a flow.

3. **Prompt injection.**
   `benchmarks/injections/` — a repository seeded with injections in comments, READMEs and string literals. **Target: zero behavior change.**

4. **Secret leakage.**
   `benchmarks/secrets/` — a repository seeded with fake credentials. **Target: zero secret values in any evidence bundle sent to a provider.**

5. **Reproducibility.**
   Two analyses of the same commit produce the same graph and the same coverage figures.

6. **No code execution.**
   The analysis sandbox has no outbound network and no exec path. Verified by a test that attempts both and asserts failure.

## Measurement harness

Build `benchmarks/` as a first-class part of the repository, not an afterthought:

- `benchmarks/repos/` — pinned fixture repositories with expected graph snapshots
- `benchmarks/injections/` — the injection corpus
- `benchmarks/secrets/` — the seeded-credential corpus
- `benchmarks/questions/` — question set for Ask, with expected answerable/unanswerable labels

Report per run: coverage by area, grounding score distribution, dropped-claim rate by prompt version, pass durations against the budgets in `09-performance.md`.
