"""ModelService: the provider-independent model interface (build-plan.md Part 3).

Two layers:

- `ModelService` is what callers depend on: generate_structured /
  generate_text / embed / rerank, addressed by *role*, never by model name.
- `CompletionProvider` is one raw request to one vendor. `RoutedModelService`
  sits between them and owns everything that must not vary by vendor:
  role routing, schema validation, the one-retry-then-fail rule, and writing
  one `llm_calls` usage row per provider attempt.

Rules enforced here (Part 0.3):
- Structured output is validated against the caller's Pydantic schema before
  it is returned. One retry, then `StructuredOutputError`. Malformed output is
  never coerced, repaired, or partially returned.
- A refusal or a max_tokens truncation fails visibly; it is not retried
  (re-sending the same prompt does not fix either).
- Every provider attempt — success or failure — is recorded. If the usage row
  cannot be written, the call fails: an unrecorded model call is a bug.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app.llm.pricing import estimate_cost_usd
from app.llm.routing import (
    DEFAULT_ROUTES,
    ModelRoute,
    attempt_models,
    resolve_route,
)
from app.llm.tokens import estimate_tokens
from app.models import LlmCall, LlmCallStatus, LlmOperation, ModelRole

T = TypeVar("T", bound=BaseModel)

UsageRecorder = Callable[[LlmCall], None]


class BudgetGuard(Protocol):
    """The pre-call cost cap, as this module needs it.

    Structural, and declared here rather than imported, so `app.llm` keeps no
    dependency on `app.limits` (which pulls in FastAPI through `app.auth`).
    `app.limits.budget.ProjectBudgetGuard` is the implementation; the
    production factories in `app/llm/__init__.py` install it lazily.

    `check` raises to refuse the call. The exception it raises is deliberately
    *not* a `ModelServiceError`: a cap refusal is not a provider failure, so it
    must not be caught by handlers that turn provider failures into 502s.
    """

    def check(self, project_id: str | None, *, operation: str) -> None: ...

    def note_spend(self, project_id: str | None, cost_usd: float | None) -> None: ...


# --- request / response shapes ------------------------------------------------


@dataclass(frozen=True)
class ImageInput:
    """A base64-encoded image for the `vision` role."""

    media_type: str  # image/png, image/jpeg, image/gif, image/webp
    data_base64: str


@dataclass(frozen=True)
class Prompt:
    """What the caller sends. `system` is operator-authored instructions;
    `user` carries the task and any (untrusted) repository text."""

    user: str
    system: str | None = None
    images: Sequence[ImageInput] = ()


@dataclass(frozen=True)
class CallContext:
    """Attribution for the usage row. Optional until callers exist."""

    project_id: str | None = None
    repo_id: str | None = None
    job_id: str | None = None


@dataclass(frozen=True)
class CompletionRequest:
    model: str
    prompt: Prompt
    max_tokens: int
    # A JSON schema the provider should constrain output to, or None for text.
    json_schema: dict[str, Any] | None = None


@dataclass(frozen=True)
class Completion:
    text: str
    stop_reason: str | None
    served_model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    request_id: str | None = None


# `document` for the corpus side, `query` for a search string. Voyage (and
# every other asymmetric code embedder) prepends a different fixed instruction
# per side, so indexing and searching must not use the same value.
EmbeddingInputType = Literal["document", "query"]


@dataclass(frozen=True)
class EmbeddingRequest:
    """One embedding request to one vendor: already batched and already
    checked against the route's per-request limits."""

    model: str
    texts: tuple[str, ...]
    input_type: EmbeddingInputType | None = None
    dimensions: int | None = None


@dataclass(frozen=True)
class EmbeddingResponse:
    # One vector per input text, in input order. The provider is responsible
    # for restoring input order; the service verifies the count.
    vectors: list[list[float]]
    served_model: str | None = None
    # The provider's own measured token count for this request -- the number
    # cost is billed on, unlike the chunker's estimate.
    input_tokens: int = 0
    request_id: str | None = None


# --- errors -----------------------------------------------------------------


class ModelServiceError(Exception):
    """Base class for every failure ModelService raises."""


class ModelCallError(ModelServiceError):
    """The provider request itself failed (after the SDK's own transient retries)."""


class ModelRefusalError(ModelServiceError):
    """The provider declined the request (stop_reason='refusal')."""


class ModelOutputTruncatedError(ModelServiceError):
    """The provider stopped at max_tokens; the output is incomplete."""


class EmbeddingNotConfiguredError(ModelServiceError):
    """No embedding provider is wired (e.g. VOYAGE_API_KEY is unset).

    Callers that can usefully continue without vectors -- the chunk pass
    stores chunks with `embedding = NULL` and FTS still works -- catch this
    and record the gap. Nothing silently substitutes a fake vector."""


class EmbeddingShapeError(ModelServiceError):
    """The provider returned the wrong number of vectors, or a vector of the
    wrong dimension. Fails the call: a short or mis-sized result would mean
    silently dropping or mis-assigning texts."""


@dataclass
class StructuredOutputError(ModelServiceError):
    """Output failed schema validation on the first attempt and on the one retry."""

    schema_name: str
    attempts: int
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"{self.schema_name}: model output failed schema validation after "
            f"{self.attempts} attempts; last error: {self.errors[-1] if self.errors else '?'}"
        )


# --- interfaces ---------------------------------------------------------------


class CompletionProvider(ABC):
    """One request to one vendor. No routing, validation, retry, or recording."""

    name: str

    @abstractmethod
    def complete(self, request: CompletionRequest) -> Completion:
        """Raise any exception on transport/API failure; return on any response."""


class EmbeddingProvider(ABC):
    """One embedding request to one vendor. No routing, batching, or recording."""

    name: str

    @abstractmethod
    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        """Raise any exception on transport/API failure; return on any response."""


class ModelService(ABC):
    """Provider-independent model interface. Callers address roles, not models."""

    @abstractmethod
    def generate_structured(
        self,
        schema: type[T],
        prompt: Prompt | str,
        model_role: ModelRole,
        *,
        deep: bool = False,
        context: CallContext | None = None,
    ) -> T: ...

    @abstractmethod
    def generate_text(
        self,
        prompt: Prompt | str,
        model_role: ModelRole,
        *,
        context: CallContext | None = None,
    ) -> str: ...

    @abstractmethod
    def embed(
        self,
        texts: Sequence[str],
        model_role: ModelRole = "embed",
        *,
        input_type: EmbeddingInputType | None = "document",
        context: CallContext | None = None,
    ) -> list[list[float]]: ...

    @abstractmethod
    def rerank(
        self, query: str, candidates: Sequence[str], model_role: ModelRole = "rerank"
    ) -> list[float]: ...


# --- implementation -----------------------------------------------------------

_RETRY_NOTE = (
    "\n\nYour previous response did not validate against the required JSON schema. "
    "Validation errors:\n{errors}\nRespond again with a single JSON value that matches "
    "the schema exactly."
)


def _as_prompt(prompt: Prompt | str) -> Prompt:
    return Prompt(user=prompt) if isinstance(prompt, str) else prompt


def _validation_summary(exc: ValidationError) -> str:
    # include_input=False: never echo model output (which may quote repo text)
    # back into error strings that end up in llm_calls.error.
    parts = [
        f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
        for err in exc.errors(include_input=False, include_url=False)
    ]
    return "; ".join(parts)[:2000]


class RoutedModelService(ModelService):
    def __init__(
        self,
        provider: CompletionProvider,
        recorder: UsageRecorder,
        routes: dict[ModelRole, ModelRoute] | None = None,
        clock: Callable[[], float] = time.perf_counter,
        embedding_provider: EmbeddingProvider | None = None,
        budget: BudgetGuard | None = None,
    ) -> None:
        self._provider = provider
        self._recorder = recorder
        self._routes = dict(DEFAULT_ROUTES if routes is None else routes)
        self._clock = clock
        # None is a supported configuration: with no embedding credentials the
        # chunk pass still runs and stores chunks with no vector (Part 4.D's
        # "embeddings are a pluggable stage"). `embed` then raises
        # EmbeddingNotConfiguredError instead of inventing a provider.
        self._embedding_provider = embedding_provider
        # None means "no cap enforced", which is correct for a unit test with
        # a fake provider and for the eval harness; the production factories
        # always install one. The check is per provider attempt -- the only
        # place every path (first attempt, schema retry, embedding batch)
        # funnels through -- so no caller can bypass it by calling a
        # different method.
        self._budget = budget

    # -- public API --

    def generate_structured(
        self,
        schema: type[T],
        prompt: Prompt | str,
        model_role: ModelRole,
        *,
        deep: bool = False,
        context: CallContext | None = None,
    ) -> T:
        base_prompt = _as_prompt(prompt)
        route = self._route(model_role, base_prompt)
        first_model, retry_model = attempt_models(route, deep=deep)
        json_schema = schema.model_json_schema()
        errors: list[str] = []

        for attempt, model in enumerate((first_model, retry_model), start=1):
            attempt_prompt = base_prompt
            if errors:
                attempt_prompt = Prompt(
                    user=base_prompt.user + _RETRY_NOTE.format(errors=errors[-1]),
                    system=base_prompt.system,
                    images=base_prompt.images,
                )
            request = CompletionRequest(
                model=model,
                prompt=attempt_prompt,
                max_tokens=route.max_tokens,
                json_schema=json_schema,
            )
            completion, latency_ms = self._call(
                request, model_role, "structured", attempt, schema.__name__, context
            )
            try:
                value = schema.model_validate_json(completion.text)
            except ValidationError as exc:
                summary = _validation_summary(exc)
                errors.append(summary)
                self._record(
                    request, model_role, "structured", attempt, "validation_failed", latency_ms,
                    completion=completion, schema_name=schema.__name__, error=summary,
                    context=context,
                )
                continue
            self._record(
                request, model_role, "structured", attempt, "succeeded", latency_ms,
                completion=completion, schema_name=schema.__name__, error=None, context=context,
            )
            return value

        raise StructuredOutputError(schema_name=schema.__name__, attempts=2, errors=errors)

    def generate_text(
        self,
        prompt: Prompt | str,
        model_role: ModelRole,
        *,
        context: CallContext | None = None,
    ) -> str:
        p = _as_prompt(prompt)
        route = self._route(model_role, p)
        request = CompletionRequest(model=route.model, prompt=p, max_tokens=route.max_tokens)
        completion, latency_ms = self._call(request, model_role, "text", 1, None, context)
        self._record(
            request, model_role, "text", 1, "succeeded", latency_ms,
            completion=completion, schema_name=None, error=None, context=context,
        )
        return completion.text

    def embed(
        self,
        texts: Sequence[str],
        model_role: ModelRole = "embed",
        *,
        input_type: EmbeddingInputType | None = "document",
        context: CallContext | None = None,
    ) -> list[list[float]]:
        """One vector per text, in input order.

        Batched to stay under the route's per-request caps, with one
        `llm_calls` row (role `embed`, operation `embed`) per provider
        request. Every failure mode is loud: no provider configured, a text
        longer than the model's context, a transport error, or a response
        whose shape does not match what was sent. Nothing is ever silently
        dropped or truncated.
        """
        if not texts:
            return []  # nothing was sent, so there is nothing to record
        if self._embedding_provider is None:
            raise EmbeddingNotConfiguredError(
                "no embedding provider is configured (VOYAGE_API_KEY unset?); "
                "chunks can still be stored and searched by FTS without vectors"
            )
        route = resolve_route(self._routes, model_role)
        token_estimates = [estimate_tokens(text) for text in texts]
        for index, (text, tokens) in enumerate(zip(texts, token_estimates, strict=True)):
            if not text.strip():
                raise ModelServiceError(f"text {index} is empty; refusing to embed it")
            if route.max_input_tokens is not None and tokens > route.max_input_tokens:
                raise ModelServiceError(
                    f"text {index} is ~{tokens} tokens, over {route.model}'s "
                    f"{route.max_input_tokens}-token context; split it before embedding"
                )

        vectors: list[list[float]] = []
        for batch in _batch_indices(token_estimates, route):
            request = EmbeddingRequest(
                model=route.model,
                texts=tuple(texts[i] for i in batch),
                input_type=input_type,
                dimensions=route.dimensions,
            )
            vectors.extend(self._embed_once(request, model_role, route, context))
        return vectors

    def _embed_once(
        self,
        request: EmbeddingRequest,
        role: ModelRole,
        route: ModelRoute,
        context: CallContext | None,
    ) -> list[list[float]]:
        """One provider request: timed, recorded, shape-checked."""
        assert self._embedding_provider is not None
        if self._budget is not None:
            self._budget.check(context.project_id if context else None, operation="embed")
        started = self._clock()
        try:
            response = self._embedding_provider.embed(request)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:2000]
            self._record_embed(
                request, role, "error", self._elapsed_ms(started),
                response=None, error=message, context=context,
            )
            raise ModelCallError(message) from exc

        latency_ms = self._elapsed_ms(started)
        shape_error = _embedding_shape_error(response, request, route)
        self._record_embed(
            request, role, "error" if shape_error else "succeeded", latency_ms,
            response=response, error=shape_error, context=context,
        )
        if shape_error:
            raise EmbeddingShapeError(shape_error)
        return response.vectors

    def _record_embed(
        self,
        request: EmbeddingRequest,
        role: ModelRole,
        status: LlmCallStatus,
        latency_ms: int,
        *,
        response: EmbeddingResponse | None,
        error: str | None,
        context: CallContext | None,
    ) -> None:
        """Same rule as completions: an unrecorded model call is a bug, so a
        recorder failure fails the call rather than being swallowed."""
        assert self._embedding_provider is not None
        input_tokens = response.input_tokens if response else 0
        served_model = response.served_model if response else None
        self._emit(
            LlmCall(
                role=role,
                provider=self._embedding_provider.name,
                model=request.model,
                served_model=served_model,
                operation="embed",
                attempt=1,  # embeddings are not retried here; the caller decides
                status=status,
                input_tokens=input_tokens,
                output_tokens=0,
                cost_usd=estimate_cost_usd(
                    served_model or request.model,
                    input_tokens=input_tokens,
                    output_tokens=0,
                ),
                latency_ms=latency_ms,
                request_id=response.request_id if response else None,
                error=error,
                project_id=context.project_id if context else None,
                repo_id=context.repo_id if context else None,
                job_id=context.job_id if context else None,
            )
        )

    def rerank(
        self, query: str, candidates: Sequence[str], model_role: ModelRole = "rerank"
    ) -> list[float]:
        # STUB(CodeAxis): reranking ships only if eval shows retrieval is insufficient
        # (build-plan.md Part 3). No provider is wired.
        raise NotImplementedError(
            "ModelService.rerank is not implemented: reranking is added only after the "
            "eval harness shows it is needed (build-plan.md Part 3)."
        )

    # -- internals --

    def _route(self, role: ModelRole, prompt: Prompt) -> ModelRoute:
        route = resolve_route(self._routes, role)
        if prompt.images and not route.accepts_images:
            raise ModelServiceError(
                f"role {role!r} does not accept images; use the 'vision' role"
            )
        return route

    def _call(
        self,
        request: CompletionRequest,
        role: ModelRole,
        operation: LlmOperation,
        attempt: int,
        schema_name: str | None,
        context: CallContext | None,
    ) -> tuple[Completion, int]:
        """One provider attempt. Records and raises on transport error, refusal,
        or truncation; otherwise returns the completion for the caller to judge."""
        if self._budget is not None:
            # Before the request is sent and before anything is recorded: a
            # refused call was never made, so it writes no llm_calls row.
            self._budget.check(context.project_id if context else None, operation=operation)
        started = self._clock()
        try:
            completion = self._provider.complete(request)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:2000]
            self._record(
                request, role, operation, attempt, "error", self._elapsed_ms(started),
                completion=None, schema_name=schema_name, error=message, context=context,
            )
            raise ModelCallError(message) from exc

        latency_ms = self._elapsed_ms(started)

        if completion.stop_reason == "refusal":
            self._record(
                request, role, operation, attempt, "refused", latency_ms,
                completion=completion, schema_name=schema_name, error=None, context=context,
            )
            raise ModelRefusalError(f"{request.model} declined the request (role {role!r})")
        if completion.stop_reason == "max_tokens":
            self._record(
                request, role, operation, attempt, "truncated", latency_ms,
                completion=completion, schema_name=schema_name, error=None, context=context,
            )
            raise ModelOutputTruncatedError(
                f"{request.model} hit max_tokens={request.max_tokens} (role {role!r})"
            )
        return completion, latency_ms

    def _elapsed_ms(self, started: float) -> int:
        return max(0, int((self._clock() - started) * 1000))

    def _record(
        self,
        request: CompletionRequest,
        role: ModelRole,
        operation: LlmOperation,
        attempt: int,
        status: LlmCallStatus,
        latency_ms: int,
        *,
        completion: Completion | None,
        schema_name: str | None,
        error: str | None,
        context: CallContext | None,
    ) -> None:
        c = completion or Completion(text="", stop_reason=None)
        priced_model = c.served_model or request.model
        self._emit(
            LlmCall(
                role=role,
                provider=self._provider.name,
                model=request.model,
                served_model=c.served_model,
                operation=operation,
                attempt=attempt,
                status=status,
                input_tokens=c.input_tokens,
                output_tokens=c.output_tokens,
                cache_read_input_tokens=c.cache_read_input_tokens,
                cache_creation_input_tokens=c.cache_creation_input_tokens,
                cost_usd=estimate_cost_usd(
                    priced_model,
                    input_tokens=c.input_tokens,
                    output_tokens=c.output_tokens,
                    cache_read_input_tokens=c.cache_read_input_tokens,
                    cache_creation_input_tokens=c.cache_creation_input_tokens,
                ),
                latency_ms=latency_ms,
                stop_reason=c.stop_reason,
                request_id=c.request_id,
                schema_name=schema_name,
                error=error,
                project_id=context.project_id if context else None,
                repo_id=context.repo_id if context else None,
                job_id=context.job_id if context else None,
            )
        )

    def _emit(self, call: LlmCall) -> None:
        """Record one usage row, then charge it against the budget's cached
        total. Recording comes first and still fails the call if it fails (an
        unrecorded model call is a bug); the budget is told afterwards so a
        long pass sees its own spend accumulate between ledger reads rather
        than re-checking a stale total per call."""
        self._recorder(call)
        if self._budget is not None:
            self._budget.note_spend(call.project_id, call.cost_usd)


# --- embedding batching -------------------------------------------------------

# Fraction of the provider's hard per-request token cap we actually fill.
# `estimate_tokens` can under-count and an over-cap request is rejected
# outright, so this slack is what keeps a bad estimate from failing a batch.
_BATCH_TOKEN_HEADROOM = 0.8


def _batch_indices(token_estimates: list[int], route: ModelRoute) -> list[list[int]]:
    """Greedy packing of text indices into provider requests, in input order.

    Honours both of the route's caps. A single text that alone exceeds the
    token budget still gets its own batch -- it was already checked against
    the per-text context limit, so sending it is correct, and the headroom
    above is what absorbs the estimate error.
    """
    max_texts = route.max_batch_texts or len(token_estimates)
    token_budget = (
        int(route.max_batch_tokens * _BATCH_TOKEN_HEADROOM) if route.max_batch_tokens else None
    )
    batches: list[list[int]] = []
    current: list[int] = []
    current_tokens = 0
    for index, tokens in enumerate(token_estimates):
        over_texts = len(current) >= max_texts
        over_tokens = (
            token_budget is not None and bool(current) and current_tokens + tokens > token_budget
        )
        if over_texts or over_tokens:
            batches.append(current)
            current, current_tokens = [], 0
        current.append(index)
        current_tokens += tokens
    if current:
        batches.append(current)
    return batches


def _embedding_shape_error(
    response: EmbeddingResponse, request: EmbeddingRequest, route: ModelRoute
) -> str | None:
    """Why this response cannot be trusted, or None if it can."""
    if len(response.vectors) != len(request.texts):
        return (
            f"{request.model} returned {len(response.vectors)} vectors for "
            f"{len(request.texts)} texts"
        )
    expected = route.dimensions
    if expected is not None:
        wrong = [
            (i, len(vector)) for i, vector in enumerate(response.vectors) if len(vector) != expected
        ]
        if wrong:
            index, got = wrong[0]
            return (
                f"{request.model} returned a {got}-dimension vector at index {index}; "
                f"the route (and the chunks.embedding column) expect {expected}"
            )
    return None
