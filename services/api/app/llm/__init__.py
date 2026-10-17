"""Model layer: the provider-independent ModelService (build-plan.md Part 3).

Callers depend on `ModelService` and address models by role. Every provider
attempt writes one codeaxis.llm_calls usage row. Structured output is
validated against the caller's Pydantic schema; one retry, then
`StructuredOutputError`. No caller exists yet (narration is Phase 3+).
"""

from __future__ import annotations

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.pricing import PRICES, estimate_cost_usd
from app.llm.routing import DEFAULT_ROUTES, ModelRoute, UnroutedRoleError
from app.llm.service import (
    CallContext,
    Completion,
    CompletionProvider,
    CompletionRequest,
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


def anthropic_model_service(
    *,
    recorder: UsageRecorder | None = None,
    routes: dict | None = None,
    provider: CompletionProvider | None = None,
) -> ModelService:
    """The production ModelService: Anthropic provider + llm_calls recording."""
    if recorder is None:
        from app.llm.usage import record_llm_call

        recorder = record_llm_call
    return RoutedModelService(
        provider=provider or AnthropicProvider(), recorder=recorder, routes=routes
    )


__all__ = [
    "AnthropicProvider",
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
