"""Model layer: the provider-independent ModelService (build-plan.md Part 3).

Callers depend on `ModelService` and address models by role. Every provider
attempt writes one codeaxis.llm_calls usage row. Structured output is
validated against the caller's Pydantic schema; one retry, then
`StructuredOutputError`.

The completion roles are served by Anthropic or by Gemini, chosen per
deployment: `completion_model_service()` is what application code calls, and
`selected_provider_name()` decides from `CODEAXIS_MODEL_PROVIDER` or, failing
that, from which vendor's key is set. Which vendor is in use changes one line
of configuration and nothing in any caller.

The `embed` role is hosted voyage-code-3 (Part 13 #2, decided 2026-10-17) and
is optional at runtime: with `VOYAGE_API_KEY` unset, `embedding_enabled()` is
False and callers skip the vector stage rather than failing.
"""

from __future__ import annotations

import os

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.gemini_provider import GeminiProvider
from app.llm.pricing import PRICES, estimate_cost_usd
from app.llm.routing import (
    DEFAULT_ROUTES,
    GEMINI_ROUTES,
    ModelRoute,
    UnroutedRoleError,
    routed_models,
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

# --- which vendor serves the completion roles ---------------------------------

PROVIDER_ENV_VAR = "CODEAXIS_MODEL_PROVIDER"

# Vendor name -> (provider factory, its routing table). A route table belongs
# with the provider that serves it: `GeminiProvider` asked for a
# `claude-sonnet-5-5` is a 404, so the two are never chosen separately.
_PROVIDERS: dict[str, tuple[type[CompletionProvider], dict]] = {
    "anthropic": (AnthropicProvider, DEFAULT_ROUTES),
    "gemini": (GeminiProvider, GEMINI_ROUTES),
}

# The environment variables each vendor's SDK resolves a credential from, in
# the SDK's own order. Nothing here reads a key's *value* -- a provider gets
# its credential from its own SDK, and these names exist only so selection and
# the up-front "is a model available at all" check agree about what counts.
_CREDENTIAL_VARS: dict[str, tuple[str, ...]] = {
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}


class ProviderNotConfiguredError(RuntimeError):
    """`CODEAXIS_MODEL_PROVIDER` names a vendor this build has no provider for.

    A configuration error, not a provider failure: deliberately a RuntimeError
    rather than a `ModelServiceError`, so it is not caught by the handlers that
    turn provider failures into 502s.
    """


def selected_provider_name() -> str:
    """Which vendor serves the completion roles, from the environment.

    Precedence, and why each step is where it is:

    1. `CODEAXIS_MODEL_PROVIDER` wins. An operator with both keys set needs a
       way to say which one to spend, and inferring it from the keys would
       make that unexpressible. An unknown value is an error rather than a
       fallback -- a typo must not silently bill the other vendor.
    2. Otherwise, whichever vendor has a key. This is the case that matters
       in practice: a deployment holding only `GEMINI_API_KEY` should just
       work, which is why this exists at all.
    3. With both keys or neither, `anthropic` -- today's behaviour unchanged.
       Neither key still resolves to a provider rather than raising, because
       nothing here needs credentials: the failure belongs at the first
       request, where it names the vendor and the missing variable, not at
       import time in a process that may never call a model.
    """
    explicit = os.environ.get(PROVIDER_ENV_VAR, "").strip().lower()
    if explicit:
        if explicit not in _PROVIDERS:
            raise ProviderNotConfiguredError(
                f"{PROVIDER_ENV_VAR}={explicit!r} is not a known provider; "
                f"expected one of {', '.join(sorted(_PROVIDERS))}"
            )
        return explicit
    if _has_credential("gemini") and not _has_credential("anthropic"):
        return "gemini"
    return "anthropic"


def _has_credential(provider_name: str) -> bool:
    return any(os.environ.get(var, "").strip() for var in _CREDENTIAL_VARS[provider_name])


def missing_credential_reason() -> str | None:
    """Why the selected vendor cannot be called, or None if it can.

    Worth asking before a pass that would make hundreds of calls: with no
    credential every one of them fails identically, and the honest outcome is
    one recorded skip rather than hundreds of error rows. It is not a
    substitute for handling a failed call -- a key can be present and
    rejected.
    """
    name = selected_provider_name()
    if _has_credential(name):
        return None
    return (
        f"no model provider configured: the selected provider is {name!r} and none of "
        f"{', '.join(_CREDENTIAL_VARS[name])} is set"
    )


def selected_provider_and_routes() -> tuple[str, type[CompletionProvider], dict]:
    """The selected vendor's name, provider class and routing table, together.

    Public because the pairing is the thing worth asking for -- a provider
    without its own table is a 404 on every call -- and because
    `scripts/check_model_provider.py` needs exactly this to check the
    configuration without reaching into a private mapping.
    """
    name = selected_provider_name()
    provider_cls, routes = _PROVIDERS[name]
    return name, provider_cls, routes


def completion_model_service(
    *,
    recorder: UsageRecorder | None = None,
    budget: BudgetGuard | None = None,
) -> ModelService:
    """The production ModelService for the completion roles, on whichever
    vendor this deployment is configured for.

    This is what application code should call. It keeps the Part 3 rule intact
    one level further up than `ModelService` does: callers address roles, and
    now they do not name a vendor either.

    It takes no `provider` or `routes` override on purpose. Either one could be
    passed without the other and silently produce a provider holding the other
    vendor's model names, which is a 404 on every call. A caller that genuinely
    means one vendor asks for it by name -- `anthropic_model_service` or
    `gemini_model_service` -- and gets that vendor's table with it.
    """
    _, provider_cls, provider_routes = selected_provider_and_routes()
    return _model_service(
        provider=provider_cls(),
        routes=provider_routes,
        recorder=recorder,
        budget=budget,
    )


def _model_service(
    *,
    provider: CompletionProvider,
    routes: dict | None,
    recorder: UsageRecorder | None,
    budget: BudgetGuard | None,
    embedding_provider: EmbeddingProvider | None = None,
) -> ModelService:
    """Shared body of the production factories: default recorder, default cost
    cap, nothing vendor-specific.

    Both defaults are imported here rather than at module scope so `app.llm`
    stays importable without `app.limits` (and through it FastAPI) -- and so a
    unit test that constructs `RoutedModelService` directly still gets no cap
    and no database.
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


def gemini_model_service(
    *,
    recorder: UsageRecorder | None = None,
    routes: dict | None = None,
    provider: CompletionProvider | None = None,
    budget: BudgetGuard | None = None,
) -> ModelService:
    """The production ModelService on Gemini: Gemini provider + llm_calls
    recording + the per-project cost cap.

    Defaults to `GEMINI_ROUTES`, not `DEFAULT_ROUTES` -- the Anthropic table
    names Claude models, which this provider cannot serve.
    """
    return _model_service(
        provider=provider or GeminiProvider(),
        routes=GEMINI_ROUTES if routes is None else routes,
        recorder=recorder,
        budget=budget,
    )


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
    return _model_service(
        provider=provider or AnthropicProvider(),
        routes=routes,
        recorder=recorder,
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
    interface as `completion_model_service` -- callers still address roles.

    The completion provider attached here is the deployment's selected one,
    even though the `embed` role does not use it. The service is one object
    with both halves, and giving it the wrong vendor's completion provider
    would make any non-embed call through this handle fail on a model the
    provider does not serve.
    """
    _, provider_cls, provider_routes = selected_provider_and_routes()
    return _model_service(
        provider=provider_cls(),
        routes=provider_routes if routes is None else routes,
        recorder=recorder,
        budget=budget,
        embedding_provider=embedding_provider or VoyageProvider(),
    )


__all__ = [
    "AnthropicProvider",
    "GEMINI_ROUTES",
    "GeminiProvider",
    "PROVIDER_ENV_VAR",
    "ProviderNotConfiguredError",
    "completion_model_service",
    "gemini_model_service",
    "missing_credential_reason",
    "routed_models",
    "selected_provider_and_routes",
    "selected_provider_name",
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
