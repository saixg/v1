"""Role -> model routing table (build-plan.md Part 3).

Start with this small table, not a model per subtask. Change a model only
when the eval harness (Part 9) shows a measurable win; swapping one is a
change to this table (or a `routes=` override), never to a caller.

There is now one completion table **per provider**, selected by
`CODEAXIS_LLM_PROVIDER` (see `app.llm.provider_name`). Part 3's table names
Claude models; the Gemini table is a deliberate override recorded in
`docs/STATUS.md` (2026-10-20), because the owner has a working Gemini key and
no Anthropic one, which otherwise leaves every model-backed feature dead.
`build-plan.md` is not edited for this, per Part 0.1.

The `embed` role is shared: it is Voyage either way and is **not** per
provider. `chunks.embedding` declares 1024 dimensions, so changing the
embedding model is a migration, not a routing change.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import ModelRole

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
VOYAGE_CODE_3 = "voyage-code-3"

# Gemini model ids, each confirmed to actually serve `generateContent` on the
# owner's key by a live one-token request on 2026-10-20.
#
# ListModels is not sufficient evidence and was wrong here: it still lists the
# whole `gemini-2.5-*` family, and every one of them 404s with "no longer
# available to new users". An id is only put in this table after a real call
# to it returned 200.
GEMINI_FLASH_LITE = "gemini-3.1-flash-lite"
GEMINI_FLASH = "gemini-3.8-flash"


@dataclass(frozen=True)
class ModelRoute:
    model: str
    # Used instead of `model` in "deep plan" mode, and for the single retry
    # after a failed schema validation. None = retry on the same model.
    escalation_model: str | None = None
    max_tokens: int = 16000  # non-streaming ceiling; keeps requests under SDK HTTP timeouts
    accepts_images: bool = False
    # --- embedding roles only (None on every completion role) ---------------
    # Output vector size. Must match the `chunks.embedding` column's declared
    # dimension, or every insert fails: changing it is a migration, not a
    # config tweak.
    dimensions: int | None = None
    # The provider's hard per-request caps. RoutedModelService.embed batches
    # to stay under both and fails visibly rather than dropping texts.
    max_batch_texts: int | None = None
    max_batch_tokens: int | None = None
    # Per-text context length. A longer text is rejected, never truncated.
    max_input_tokens: int | None = None


# `embed` is hosted voyage-code-3: Part 13 #2 was settled by the owner on
# 2026-10-17 (hosted, not self-hosted) and 1024 dimensions is what the
# `chunks.embedding` column in supabase/migrations/20261019000000_semantic.sql
# declares. Shared by every completion provider -- see the module docstring.
EMBED_ROUTE = ModelRoute(
    model=VOYAGE_CODE_3,
    # Embeddings have no output-token budget; this field is unused here.
    max_tokens=0,
    dimensions=1024,
    # voyage-code-3 per-request limits, docs.voyageai.com (read 2026-10-17):
    # at most 1,000 texts and 120K tokens per request, 32K tokens per text.
    max_batch_texts=1000,
    max_batch_tokens=120_000,
    max_input_tokens=32_000,
)

# `rerank` is absent on purpose in both tables: it ships only if eval shows
# retrieval is insufficient (Part 3).
ANTHROPIC_ROUTES: dict[ModelRole, ModelRoute] = {
    "bulk": ModelRoute(model=HAIKU, max_tokens=4096),
    "synth": ModelRoute(model=SONNET),
    "design": ModelRoute(model=SONNET, escalation_model=OPUS),
    "vision": ModelRoute(model=SONNET, accepts_images=True),
    "embed": EMBED_ROUTE,
}

# Gemini stand-ins for Part 3's roles. Stable models only: `gemini-3-flash-
# preview` does serve on this key, but a preview can change behaviour or be
# withdrawn under a running analysis -- which the 2.5 family's withdrawal just
# demonstrated is not hypothetical -- and that would silently alter every
# narration the graph is read through. Both ids below are GA, serve themselves
# rather than aliasing forward (`gemini-3.5-flash` and `gemini-3.7-flash` both
# answer as a *different* `modelVersion`, so routing to them would record a
# served_model the table never chose), and have published list prices.
GEMINI_ROUTES: dict[ModelRole, ModelRoute] = {
    # bulk is per-symbol/file summaries: thousands of small, independent calls
    # on one repository, so unit price dominates quality. flash-lite is the
    # cheapest GA model that serves on this key ($0.25/$1.50 per MTok) and
    # these prompts are "summarise this one function" -- the easiest work in
    # the system.
    "bulk": ModelRoute(model=GEMINI_FLASH_LITE, max_tokens=4096),
    # synth is grounded chat, Inspector explanations and requirement
    # extraction: user-visible prose that must cite, plus the schema-heavy
    # extraction call. 3.8-flash is the newest GA flash tier and the one
    # Google's own 404 body names as the replacement for the withdrawn 2.5
    # models, which is the closest thing to a vendor statement of "current".
    "synth": ModelRoute(model=GEMINI_FLASH),
    # design is blueprint generation: the longest reasoning chain and the
    # largest schema in the product.
    #
    # STUB(CodeAxis): no escalation model. Part 3's shape is mid-tier by
    # default escalating to a top tier for deep-plan mode and after a failed
    # schema validation, but **no Pro model is reachable on this key**:
    # `gemini-3.1-pro-preview` returns 429 "exceeded your current quota" on a
    # one-token request and `gemini-2.5-pro` is withdrawn (both verified live
    # 2026-10-20). Naming an unreachable escalation model would make deep-plan
    # mode and every post-validation retry fail 100% of the time instead of
    # degrading; None means both stay on 3.8-flash, which is honest and works.
    # Fill this in when the account has Pro quota.
    "design": ModelRoute(model=GEMINI_FLASH),
    # vision is diagrams in task input. The flash models are natively
    # multimodal, so the same model serves both roles and there is no separate
    # vision tier to price or evaluate.
    "vision": ModelRoute(model=GEMINI_FLASH, accepts_images=True),
    "embed": EMBED_ROUTE,
}

PROVIDER_ROUTES: dict[str, dict[ModelRole, ModelRoute]] = {
    "anthropic": ANTHROPIC_ROUTES,
    "gemini": GEMINI_ROUTES,
}

# Kept as the Anthropic table: it is what `RoutedModelService(routes=None)`
# falls back to and what build-plan.md Part 3 specifies. Production factories
# pass the selected provider's table explicitly.
DEFAULT_ROUTES: dict[ModelRole, ModelRoute] = ANTHROPIC_ROUTES


class UnknownProviderError(LookupError):
    """`CODEAXIS_LLM_PROVIDER` names a provider with no routing table.

    Loud rather than defaulted: silently falling back would run a whole
    analysis on a provider the operator did not choose, and bill for it."""


def routes_for_provider(provider: str) -> dict[ModelRole, ModelRoute]:
    try:
        return PROVIDER_ROUTES[provider]
    except KeyError:
        raise UnknownProviderError(
            f"no routing table for provider {provider!r}; "
            f"known providers: {', '.join(sorted(PROVIDER_ROUTES))}"
        ) from None


class UnroutedRoleError(LookupError):
    """The role has no entry in the routing table."""


def resolve_route(routes: dict[ModelRole, ModelRoute], role: ModelRole) -> ModelRoute:
    try:
        return routes[role]
    except KeyError:
        raise UnroutedRoleError(f"no model is routed for role {role!r}") from None


def attempt_models(route: ModelRoute, *, deep: bool) -> tuple[str, str]:
    """The (first attempt, retry) models for one structured call.

    Deep-plan mode starts on the escalation model. A failed validation always
    retries on the escalation model when the role has one.
    """
    first = route.escalation_model if deep and route.escalation_model else route.model
    retry = route.escalation_model or route.model
    return first, retry
