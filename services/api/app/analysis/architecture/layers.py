"""Layer classification: pure path-pattern rules, no LLM call. build-plan.md
Part 4.C says "rules first, LLM only as fallback" -- the LLM fallback is
explicitly out of scope for this pass (nothing in this codebase calls
ModelService for repo content yet, and this pass doesn't add a first
caller). An unmatched file legitimately has no determinable layer under a
rules-only pass; `classify_layer` returns `None` rather than guessing
("prefer admitting a gap over producing a plausible edge" -- CLAUDE.md).
"""

from __future__ import annotations

import fnmatch

_FRONTEND_EXTENSIONS = (".tsx", ".jsx", ".vue", ".svelte")


def classify_layer(path: str) -> str | None:
    """Classify a repo-relative file path into an architectural layer.
    Case-insensitive, first matching rule wins."""
    lower = path.lower()
    segments = lower.split("/")
    name = segments[-1]

    # api: routes/, controllers/, /api/, *router*, *endpoint*
    if "routes" in segments or "controllers" in segments or "api" in segments:
        return "api"
    if "router" in name or "endpoint" in name:
        return "api"

    # data: models/, schemas/, entities/
    if "models" in segments or "schemas" in segments or "entities" in segments:
        return "data"

    # service: services/
    if "services" in segments:
        return "service"

    # test: *.test.*, *.spec.*, __tests__/, tests/, test_*.py
    if "__tests__" in segments or "tests" in segments:
        return "test"
    if fnmatch.fnmatch(name, "*.test.*") or fnmatch.fnmatch(name, "*.spec.*"):
        return "test"
    if fnmatch.fnmatch(name, "test_*.py"):
        return "test"

    # config: *.config.*, .env*, config/, settings.py
    if "config" in segments:
        return "config"
    if fnmatch.fnmatch(name, "*.config.*") or fnmatch.fnmatch(name, ".env*"):
        return "config"
    if name == "settings.py":
        return "config"

    # infra: infra/, docker/, terraform/, .github/workflows/
    if "infra" in segments or "docker" in segments or "terraform" in segments:
        return "infra"
    if ".github" in segments and "workflows" in segments:
        return "infra"

    # ui: components/, pages/, app/ -- scoped to frontend file extensions
    # only, so a Python `app/` package (e.g. services/api/app/) is never
    # misclassified as a UI layer.
    if name.endswith(_FRONTEND_EXTENSIONS) and (
        "components" in segments or "pages" in segments or "app" in segments
    ):
        return "ui"

    return None
