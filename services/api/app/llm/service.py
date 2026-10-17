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
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.llm.pricing import estimate_cost_usd
from app.llm.routing import (
    DEFAULT_ROUTES,
    ModelRoute,
    attempt_models,
    resolve_route,
)
from app.models import LlmCall, LlmCallStatus, LlmOperation, ModelRole

T = TypeVar("T", bound=BaseModel)

UsageRecorder = Callable[[LlmCall], None]


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


# --- errors -----------------------------------------------------------------


class ModelServiceError(Exception):
    """Base class for every failure ModelService raises."""


class ModelCallError(ModelServiceError):
    """The provider request itself failed (after the SDK's own transient retries)."""


class ModelRefusalError(ModelServiceError):
    """The provider declined the request (stop_reason='refusal')."""


class ModelOutputTruncatedError(ModelServiceError):
    """The provider stopped at max_tokens; the output is incomplete."""


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
    def embed(self, texts: Sequence[str], model_role: ModelRole = "embed") -> list[list[float]]: ...

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
    ) -> None:
        self._provider = provider
        self._recorder = recorder
        self._routes = dict(DEFAULT_ROUTES if routes is None else routes)
        self._clock = clock

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

    def embed(self, texts: Sequence[str], model_role: ModelRole = "embed") -> list[list[float]]:
        # STUB(CodeAxis): embedding model is an open decision (build-plan.md Part 13 #2,
        # hosted vs. self-hosted). Wire a provider here once it is decided.
        raise NotImplementedError(
            "ModelService.embed is not implemented: the embedding model is an open "
            "decision (build-plan.md Part 13 #2). No provider is wired."
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
        self._recorder(
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
