"""Model layer: the provider-independent ModelService (build-plan.md Part 3).

Callers depend on `ModelService` and address models by role. Every provider
attempt writes one codeaxis.llm_calls usage row. Structured output is
validated against the caller's Pydantic schema; one retry, then
`StructuredOutputError`.

The `embed` role is hosted voyage-code-3 (Part 13 #2, decided 2026-10-17) and
is optional at runtime: with `VOYAGE_API_KEY` unset, `embedding_enabled()` is
False and callers skip the vector stage rather than failing.
"""

from __future__ import annotations

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.pricing import PRICES, estimate_cost_usd
from app.llm.routing import DEFAULT_ROUTES, ModelRoute, UnroutedRoleError
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


def anthropic_model_service(
    *,
    recorder: UsageRecorder | None = None,
    routes: dict | None = None,
    provider: CompletionProvider | None = None,
    budget: BudgetGuard | None = None,
) -> ModelService:
    """The production ModelService: Anthropic provider + llm_calls recording +
    the per-project cost cap.

    `budget` defaults to the process-wide `ProjectBudgetGuard`, so every
    production call path is capped without each caller remembering to ask.
    Imported lazily, like the recorder, to keep `app.llm` importable without
    dragging in `app.limits` (and through it FastAPI) -- and so a unit test
    that constructs `RoutedModelService` directly still gets no cap and no
    database.
    """
    if recorder is None:
        from app.llm.usage import record_llm_call

        recorder = record_llm_call
    if budget is None:
        from app.limits.budget import default_budget_guard

        budget = default_budget_guard()
    return RoutedModelService(
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

    Both providers are lazy about credentials, so this is safe to build even
    with no keys set; `embedding_enabled()` is the cheap check for whether an
    `embed` call can actually succeed. Returns the same `ModelService`
    interface as `anthropic_model_service` -- callers still address roles.
    """
    if recorder is None:
        from app.llm.usage import record_llm_call

        recorder = record_llm_call
    if budget is None:
        from app.limits.budget import default_budget_guard

        budget = default_budget_guard()
    return RoutedModelService(
        provider=AnthropicProvider(),
        recorder=recorder,
        routes=routes,
        embedding_provider=embedding_provider or VoyageProvider(),
        budget=budget,
    )


__all__ = [
    "AnthropicProvider",
    "BudgetGuard",
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
