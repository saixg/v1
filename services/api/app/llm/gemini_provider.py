"""Google Gemini (`generateContent`) provider for ModelService.

One POST to one documented endpoint, so this talks HTTP directly rather than
pulling in a vendor SDK -- the same call the `VoyageProvider` makes, for the
same reason. Routing, schema validation, the one-retry rule and the
`llm_calls` row all stay above this file in `RoutedModelService`; the budget
guard sits above it too, so nothing here can bypass the cost cap.

Authentication is the `x-goog-api-key` header. A `Bearer` token is rejected
with a 401 on this endpoint -- verified against the live API, not assumed.
`GEMINI_API_KEY` is read at request time, never at import, so constructing the
provider (and therefore the whole app) never needs credentials.

Structured output uses `generationConfig.responseSchema` with
`responseMimeType: "application/json"`. The schema on the wire is
`gemini_schema.transform_schema` of the caller's Pydantic schema, because the
API accepts an OpenAPI 3.0 subset and rejects most of what
`model_json_schema()` emits; constraints that subset cannot express are folded
into field descriptions there, which is why `RoutedModelService` still
validates every response with Pydantic.

Empty and blocked responses, and why they raise
-----------------------------------------------
`candidates` can come back missing or empty when the *prompt* is blocked --
there is no candidate to read a finish reason from. That raises
`GeminiApiError`, which `RoutedModelService` records as `status="error"` and
re-raises as `ModelCallError`. The alternative, returning an empty-text
Completion, would make a prompt-level block arrive at the caller as
"the model's output failed schema validation" after two paid attempts, which
misnames the failure and charges for it.

A *candidate* that exists but has no text is different and is returned as an
empty-text Completion, because its `finishReason` is the real diagnosis and
the service layer already knows what to do with it: `MAX_TOKENS` becomes
`ModelOutputTruncatedError`, a safety finish reason becomes
`ModelRefusalError`. Mapping those here would duplicate that logic; losing
them would turn a truncation into a validation failure.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from app.llm.gemini_schema import transform_schema
from app.llm.service import Completion, CompletionProvider, CompletionRequest, Prompt

API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
API_KEY_ENV = "GEMINI_API_KEY"

# Finite but generous. Voyage's 120s is not enough here, measured rather than
# guessed: a trivial `synth` extraction (49 input tokens, 539 output) took
# 103s against gemini-3.8-flash on 2026-10-20, because these models think by
# default and thinking tokens are generated before any text arrives. A
# `design` call on a full evidence bundle is strictly worse, so 120s would
# turn ordinary slowness into a `ModelCallError`. Still finite: a hung request
# must not hang an analysis job forever.
#
# STUB(CodeAxis): streaming (`streamGenerateContent`) is the real fix -- it
# would give first-token latency instead of a wall-clock ceiling -- but
# `CompletionProvider.complete` is a single request/response contract, so
# adding it is a change to the service layer, not to this file.
DEFAULT_TIMEOUT_SECONDS = 600.0

# Gemini's `finishReason` vocabulary -> the stop reasons RoutedModelService
# acts on. Only these two produce behaviour (truncation, refusal); everything
# else is recorded as-is and judged by schema validation.
_FINISH_REASONS: dict[str, str] = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    # Every reason below means "the model had output and withheld it", which
    # is what `refusal` means to the service layer: fail loudly, do not retry
    # (re-sending the same prompt does not change a safety verdict).
    "SAFETY": "refusal",
    "RECITATION": "refusal",
    "BLOCKLIST": "refusal",
    "PROHIBITED_CONTENT": "refusal",
    "SPII": "refusal",
    "IMAGE_SAFETY": "refusal",
}


class GeminiApiError(RuntimeError):
    """A non-2xx response, or a 2xx body with no candidate to read.

    The message carries the status and the model only. Repository text is
    untrusted and may have held a secret before redaction, so an error string
    that could end up in `llm_calls.error` never echoes request or response
    content.
    """


def gemini_enabled() -> bool:
    """Whether a Gemini request could succeed at all. Used to pick a default
    provider without constructing one."""
    return bool(os.environ.get(API_KEY_ENV))


def _parts(prompt: Prompt) -> list[dict[str, Any]]:
    # Images first, then the text that refers to them -- the same ordering the
    # Anthropic provider chose, so a `vision` prompt reads identically on both.
    parts: list[dict[str, Any]] = [
        {"inlineData": {"mimeType": image.media_type, "data": image.data_base64}}
        for image in prompt.images
    ]
    parts.append({"text": prompt.user})
    return parts


def build_request_params(request: CompletionRequest) -> dict[str, Any]:
    """Map a provider-neutral request to the `generateContent` request body."""
    generation_config: dict[str, Any] = {"maxOutputTokens": request.max_tokens}
    if request.json_schema is not None:
        # Both fields are required together: a responseSchema without the JSON
        # mime type is ignored, and the model answers in prose.
        generation_config["responseMimeType"] = "application/json"
        generation_config["responseSchema"] = transform_schema(request.json_schema)

    body: dict[str, Any] = {
        "contents": [{"role": "user", "parts": _parts(request.prompt)}],
        "generationConfig": generation_config,
    }
    if request.prompt.system:
        # systemInstruction, not a first user turn: operator instructions must
        # stay separable from the untrusted repository text in `user`.
        body["systemInstruction"] = {"parts": [{"text": request.prompt.system}]}
    return body


def parse_response(body: Any, *, request_id: str | None = None) -> Completion:
    """Map a `generateContent` response body to a provider-neutral Completion.

    Defensive about every level: a blocked prompt, a candidate with no
    `content`, a part with no `text`, and a missing `usageMetadata` are all
    real response shapes. Raises `GeminiApiError` only when there is no
    candidate at all (see the module docstring); never raises `KeyError`.
    """
    if not isinstance(body, dict):
        raise GeminiApiError(f"expected a JSON object, got {type(body).__name__}")

    candidates = body.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        feedback = body.get("promptFeedback")
        reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
        raise GeminiApiError(
            "response carried no candidates"
            + (f" (promptFeedback.blockReason={reason})" if reason else "")
        )

    candidate = candidates[0] if isinstance(candidates[0], dict) else {}
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    text = "".join(
        part["text"]
        for part in (parts or [])
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )

    finish = candidate.get("finishReason")
    stop_reason = _FINISH_REASONS.get(finish, finish) if isinstance(finish, str) else None

    usage = body.get("usageMetadata")
    usage = usage if isinstance(usage, dict) else {}
    served_model = body.get("modelVersion")

    return Completion(
        text=text,
        stop_reason=stop_reason,
        served_model=served_model if isinstance(served_model, str) else None,
        input_tokens=_count(usage, "promptTokenCount"),
        # candidatesTokenCount excludes thinking tokens, which are reported
        # separately as thoughtsTokenCount but are billed as output.
        output_tokens=_count(usage, "candidatesTokenCount") + _count(usage, "thoughtsTokenCount"),
        cache_read_input_tokens=_count(usage, "cachedContentTokenCount"),
        request_id=request_id,
    )


def _count(usage: dict[str, Any], key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) else 0


class GeminiProvider(CompletionProvider):
    name = "gemini"

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        base_url: str = API_BASE,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # Credentials and the HTTP client are both resolved lazily, so
        # constructing this provider never requires either; only a real
        # request does. Mirrors AnthropicProvider and VoyageProvider.
        self._api_key = api_key
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _key(self) -> str:
        key = self._api_key or os.environ.get(API_KEY_ENV)
        if not key:
            raise GeminiApiError(f"{API_KEY_ENV} is not set")
        return key

    def complete(self, request: CompletionRequest) -> Completion:
        response = self.client.post(
            f"{self._base_url}/{request.model}:generateContent",
            json=build_request_params(request),
            headers={
                # Not `Authorization: Bearer` -- this endpoint 401s on that.
                "x-goog-api-key": self._key(),
                "Content-Type": "application/json",
            },
        )
        if response.status_code >= 400:
            # The body may quote the prompt back, which is repository text;
            # only the status and the model are safe to surface.
            raise GeminiApiError(f"{request.model} returned HTTP {response.status_code}")
        return parse_response(
            response.json(), request_id=response.headers.get("x-request-id")
        )
