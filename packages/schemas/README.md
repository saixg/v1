# packages/schemas

Blueprint / Inspector / task JSON schemas shared between `apps/web` and `services/api` (build-plan.md Part 6, Part 12).

Populate this only as a feature defines a contract that genuinely crosses the web/api boundary — no placeholder schemas ahead of the thing they describe.

## `blueprint.v1.json`

The canonical blueprint JSON (build-plan.md Part 6, Part 4.G), added in Phase 5. Resolves Part 13 decision #4: it is the written form of the blueprint node/edge taxonomy, and `schema_version` is pinned to `1.0.0`.

The database is the source of truth, not this file: `supabase/migrations/20261021000000_blueprint.sql` defines the tables, `services/api/app/models/schemas.py` holds the Pydantic models the API actually validates with, and this JSON Schema is the shared contract those two agree on — what the API serves, what the React Flow view reads, and what an export renders from. Three invariants worth knowing before reading it:

- **`kind` and `mapping` are paired, never independent**: `existing`+`reuse`, `modified`+`extend`, `proposed`+`new`. Anything else is a contradiction, refused in SQL, in Pydantic, and by the validator.
- **References are by `key`, not by row id.** A new version re-inserts rows, and Part 4.G.9 requires untouched parts of a design to survive a partial regeneration, so connections and proposed files name components by their stable `key`.
- **`layout` is positions only**, stored apart from semantics (Part 4.G.6) — arranging the diagram never creates a new version of the design.

The Inspector payload (Part 7) still has no schema here: it is assembled and served by one endpoint and consumed by one component, so the Pydantic models are the only contract it has needed so far.
