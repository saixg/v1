"""Model layer: the provider-independent ModelService (build-plan.md Part 3).

Callers depend on `ModelService` and address models by role. Every provider
attempt writes one codeaxis.llm_calls usage row. Structured output is
validated against the caller's Pydantic schema; one retry, then
`StructuredOutputError`.

The `embed` role is hosted voyage-code-3 (Part 13 #2, decided 2026-10-17) and
is optional at runtime: with `VOYAGE_API_KEY` unset, `embedding_enabled()` is
False and callers skip the vector stage rather than failing.

Two completion providers are wired. `model_service()` is what production
callers use: it picks one from `CODEAXIS_LLM_PROVIDER` (or from which key is
actually set) and hands `RoutedModelService` that provider's routing table, so
switching vendors is one environment variable and no code edit.
`anthropic_model_service()` stays as the pinned-to-Anthropic factory for when
an Anthropic key exists again.
"""

from __future__ import annotations

import os

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.gemini_provider import GeminiProvider, gemini_enabled
from app.llm.gemini_schema import RecursiveSchemaError, transform_schema
from app.llm.pricing import PRICES, estimate_cost_usd
from app.llm.routing import (
    DEFAULT_ROUTES,
    ModelRoute,
    UnknownProviderError,
    UnroutedRoleError,
    routes_for_provider,
)
from app.llm.service import (
    BudgetGuard,
    CallContext,
    Completion,
    CompletionProvider,
    CompletionRequest,
    EmbeddingInputType,
    EmbeddingNotConfiguredError,
    EmbeddingProvider,
    EmbeddingRequest,
    EmbeddingResponse,
    EmbeddingShapeError,
    ImageInput,
    ModelCallError,
    ModelOutputTruncatedError,
    ModelRefusalError,
    ModelService,
    ModelServiceError,
    Prompt,
    RoutedModelService,
    StructuredOutputError,
    UsageRecorder,
)
from app.llm.tokens import estimate_tokens
from app.llm.voyage_provider import VoyageProvider, embedding_enabled

PROVIDER_ENV = "CODEAXIS_LLM_PROVIDER"

_PROVIDERS: dict[str, type[CompletionProvider]] = {
    "anthropic": AnthropicProvider,
    "gemini": GeminiProvider,
}


def provider_name() -> str:
    """Which completion provider this deployment uses.

    `CODEAXIS_LLM_PROVIDER` wins when set, and an unrecognised value raises
    rather than falling back -- running a whole analysis on a vendor the
    operator did not pick, and billing for it, is worse than refusing to start.

    Unset, it resolves to whichever vendor has a key, Gemini first. That is not
    a quality judgement: it is the only provider the owner can currently
    authenticate, and a default that cannot make a request leaves every
    model-backed feature dead with no diagnostic. With neither key set it still
    returns a name (nothing here reads credentials), so the app imports and
    starts, and the missing key surfaces at the first request.
    """
    configured = os.environ.get(PROVIDER_ENV, "").strip().lower()
    if configured:
        if configured not in _PROVIDERS:
            raise UnknownProviderError(
                f"{PROVIDER_ENV}={configured!r} is not a known provider; "
                f"expected one of: {', '.join(sorted(_PROVIDERS))}"
            )
        return configured
    if gemini_enabled():
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "gemini"


def completion_provider(name: str | None = None) -> CompletionProvider:
    """The configured provider, unconstructed credentials and all -- every
    provider resolves its key at request time, so this never needs one."""
    chosen = name or provider_name()
    try:
        factory = _PROVIDERS[chosen]
    except KeyError:
        raise UnknownProviderError(f"no completion provider named {chosen!r}") from None
    return factory()


def model_service(
    *,
    recorder: UsageRecorder | None = None,
    routes: dict | None = None,
    provider: CompletionProvider | None = None,
    budget: BudgetGuard | None = None,
) -> ModelService:
    """The production ModelService: the configured provider + llm_calls
    recording + the per-project cost cap.

    This is the one factory callers should use. The provider and its routing
    table are chosen together (see `app.llm.routing`), because a role's model
    id only means anything to the vendor it belongs to -- pairing the Gemini
    provider with Claude model names would 404 on every call.
    """
    chosen = provider_name() if provider is None else provider.name
    return _build(
        provider=provider or completion_provider(chosen),
        recorder=recorder,
        routes=routes if routes is not None else routes_for_provider(chosen),
        budget=budget,
    )


def _build(
    *,
    provider: CompletionProvider,
    recorder: UsageRecorder | None,
    routes: dict | None,
    budget: BudgetGuard | None,
    embedding_provider: EmbeddingProvider | None = None,
) -> ModelService:
    """Shared wiring: the lazily-imported recorder and budget guard.

    Both are imported here rather than at module scope to keep `app.llm`
    importable without dragging in `app.limits` (and through it FastAPI) --
    and so a unit test that constructs `RoutedModelService` directly still
    gets no cap and no database.
    """
    if recorder is None:
        from app.llm.usage import record_llm_call

        recorder = record_llm_call
    if budget is None:
        from app.limits.budget import default_budget_guard

        budget = default_budget_guard()
    return RoutedModelService(
        provider=provider,
        recorder=recorder,
        routes=routes,
        embedding_provider=embedding_provider,
        budget=budget,
    )


def anthropic_model_service(
    *,
    recorder: UsageRecorder | None = None,
    routes: dict | None = None,
    provider: CompletionProvider | None = None,
    budget: BudgetGuard | None = None,
) -> ModelService:
    """A ModelService pinned to Anthropic, whatever `CODEAXIS_LLM_PROVIDER`
    says: Anthropic provider + Part 3's Claude routing table + llm_calls
    recording + the per-project cost cap.

    Kept working, and kept out of the request path, for when an Anthropic key
    exists again. Production callers use `model_service()`.

    `budget` defaults to the process-wide `ProjectBudgetGuard`, so every
    production call path is capped without each caller remembering to ask.
    """
    return _build(
        provider=provider or AnthropicProvider(),
        recorder=recorder,
        routes=routes,
        budget=budget,
    )


def embedding_model_service(
    *,
    recorder: UsageRecorder | None = None,
    routes: dict | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    budget: BudgetGuard | None = None,
) -> ModelService:
    """The production ModelService for the `embed` role: Voyage provider +
    llm_calls recording.

    The completion provider still comes from `CODEAXIS_LLM_PROVIDER`, because
    this returns the full `ModelService` interface and a caller that also makes
    a completion call must not get a vendor nobody selected. `embed` itself is
    Voyage under every provider (one shared route -- `chunks.embedding` is
    declared at 1024 dimensions, so changing it is a migration).

    Every provider is lazy about credentials, so this is safe to build with no
    keys set; `embedding_enabled()` is the cheap check for whether an `embed`
    call can actually succeed.
    """
    chosen = provider_name()
    return _build(
        provider=completion_provider(chosen),
        recorder=recorder,
        routes=routes if routes is not None else routes_for_provider(chosen),
        budget=budget,
        embedding_provider=embedding_provider or VoyageProvider(),
    )


__all__ = [
    "AnthropicProvider",
    "BudgetGuard",
    "GeminiProvider",
    "PROVIDER_ENV",
    "RecursiveSchemaError",
    "UnknownProviderError",
    "completion_provider",
    "gemini_enabled",
    "model_service",
    "provider_name",
    "routes_for_provider",
    "transform_schema",
    "EmbeddingInputType",
    "EmbeddingNotConfiguredError",
    "EmbeddingProvider",
    "EmbeddingRequest",
    "EmbeddingResponse",
    "EmbeddingShapeError",
    "VoyageProvider",
    "embedding_enabled",
    "embedding_model_service",
    "estimate_tokens",
    "CallContext",
    "Completion",
    "CompletionProvider",
    "CompletionRequest",
    "DEFAULT_ROUTES",
    "ImageInput",
    "ModelCallError",
    "ModelOutputTruncatedError",
    "ModelRefusalError",
    "ModelRoute",
    "ModelService",
    "ModelServiceError",
    "PRICES",
    "Prompt",
    "RoutedModelService",
    "StructuredOutputError",
    "UnroutedRoleError",
    "UsageRecorder",
    "anthropic_model_service",
    "estimate_cost_usd",
]
