"""Google Gemini provider for ModelService.

Exists because the owner has a Gemini key and no Anthropic key, so without it
nothing that calls a model runs at all. It is a second `CompletionProvider`
behind the same interface, not a second model layer: routing, schema
validation, the one-retry rule and `llm_calls` recording stay in
`RoutedModelService` and are shared with `AnthropicProvider`.

Structured output uses `response_json_schema`, the raw-JSON-Schema field, and
sends the caller's Pydantic schema unchanged -- the SDK's own transform for
that field is the identity function, so there is nothing to transform. The
older `response_schema` field takes the restricted OpenAPI subset instead and
would silently drop `$defs`/`$ref`, which every nested model produces.
`RoutedModelService` validates the response with Pydantic either way; the
schema on the wire constrains decoding, it does not replace validation.

Thinking is left at the model's default, as on the Anthropic side. Parts
marked `thought=True` are skipped when reading the response text, and
`thoughts_token_count` is added to the output-token count because Gemini bills
thinking tokens as output.

Two mappings are the whole of the vendor-specific risk, so both are stated:

- **`finish_reason` -> the service's vocabulary.** Every reason that means
  "the model declined" (safety, prohibited content, blocklist, SPII,
  recitation, an unsupported language) becomes `refusal`, which
  `RoutedModelService` raises on and does not retry. `MAX_TOKENS` becomes
  `max_tokens`. `STOP` becomes `end_turn`. Anything else is passed through
  lowercased rather than mapped to a success: an unrecognised reason then
  shows up in `llm_calls.stop_reason` and fails schema validation loudly
  instead of being read as a complete answer. A prompt blocked before
  generation comes back with no candidates at all, which is also a refusal.
- **Prompt tokens.** Gemini's `prompt_token_count` *includes*
  `cached_content_token_count`, where the Anthropic usage fields are disjoint.
  The cached count is therefore subtracted out here so the two providers write
  the same meaning into the same `llm_calls` columns and
  `app.llm.pricing` can bill cache reads at the cache rate without
  double-counting them as fresh input.
"""

from __future__ import annotations

import base64
from typing import Any

from google import genai
from google.genai import types

from app.llm.service import Completion, CompletionProvider, CompletionRequest, Prompt

# Gemini finish reasons that mean the model declined to answer. Mapped to the
# one stop reason RoutedModelService raises `ModelRefusalError` for, because
# re-sending the same prompt does not fix any of them.
_REFUSAL_REASONS = frozenset(
    {
        types.FinishReason.SAFETY,
        types.FinishReason.RECITATION,
        types.FinishReason.LANGUAGE,
        types.FinishReason.BLOCKLIST,
        types.FinishReason.PROHIBITED_CONTENT,
        types.FinishReason.SPII,
        types.FinishReason.IMAGE_SAFETY,
        types.FinishReason.IMAGE_PROHIBITED_CONTENT,
        types.FinishReason.IMAGE_RECITATION,
    }
)


def _user_parts(prompt: Prompt) -> list[types.Part]:
    """Images first, then the task text -- the same order as the Anthropic
    provider, which the vision prompts are written against."""
    parts = [
        types.Part(
            inline_data=types.Blob(
                mime_type=image.media_type,
                # The SDK takes raw bytes and base64-encodes them itself; our
                # ImageInput already carries base64, so decode it here rather
                # than double-encoding.
                data=base64.b64decode(image.data_base64),
            )
        )
        for image in prompt.images
    ]
    parts.append(types.Part(text=prompt.user))
    return parts


def build_request_params(request: CompletionRequest) -> dict[str, Any]:
    """Map a provider-neutral request to `models.generate_content` kwargs."""
    config: dict[str, Any] = {"max_output_tokens": request.max_tokens}
    if request.prompt.system:
        config["system_instruction"] = request.prompt.system
    if request.json_schema is not None:
        config["response_mime_type"] = "application/json"
        config["response_json_schema"] = request.json_schema
    return {
        "model": request.model,
        "contents": [types.Content(role="user", parts=_user_parts(request.prompt))],
        "config": types.GenerateContentConfig(**config),
    }


def _stop_reason(candidate: types.Candidate | None) -> str | None:
    if candidate is None:
        # No candidate at all: the prompt itself was blocked before generation.
        return "refusal"
    reason = candidate.finish_reason
    if reason is None:
        return None
    if reason in _REFUSAL_REASONS:
        return "refusal"
    if reason is types.FinishReason.MAX_TOKENS:
        return "max_tokens"
    if reason is types.FinishReason.STOP:
        return "end_turn"
    # OTHER, MALFORMED_FUNCTION_CALL, an unspecified reason, anything the SDK
    # adds later: not claimed to be a success. See the module docstring.
    return reason.value.lower()


def _response_text(candidate: types.Candidate | None) -> str:
    """The answer text, with thinking skipped.

    `response.text` would do this too, but it also warns and returns None for
    a non-STOP finish, and the refusal/truncation paths need the parts
    regardless of how generation ended.
    """
    if candidate is None or candidate.content is None or candidate.content.parts is None:
        return ""
    return "".join(part.text for part in candidate.content.parts if part.text and not part.thought)


def parse_response(response: types.GenerateContentResponse) -> Completion:
    """Map a `GenerateContentResponse` to a provider-neutral Completion."""
    candidates = response.candidates or []
    candidate = candidates[0] if candidates else None
    usage = response.usage_metadata
    prompt_tokens = (usage.prompt_token_count or 0) if usage else 0
    cached_tokens = (usage.cached_content_token_count or 0) if usage else 0
    answer_tokens = (usage.candidates_token_count or 0) if usage else 0
    thought_tokens = (usage.thoughts_token_count or 0) if usage else 0
    return Completion(
        text=_response_text(candidate),
        stop_reason=_stop_reason(candidate),
        served_model=response.model_version,
        # prompt_token_count includes the cached tokens; see the module docstring.
        input_tokens=max(0, prompt_tokens - cached_tokens),
        # Thinking tokens bill as output, and candidates_token_count excludes them.
        output_tokens=answer_tokens + thought_tokens,
        cache_read_input_tokens=cached_tokens,
        # Gemini creates a cache through a separate API this codebase does not
        # use, so a generate_content response never reports cache writes.
        cache_creation_input_tokens=0,
        request_id=response.response_id,
    )


class GeminiProvider(CompletionProvider):
    name = "gemini"

    def __init__(self, client: genai.Client | None = None) -> None:
        # Lazy, like AnthropicProvider: constructing a ModelService must not
        # require credentials (the SDK's Client raises at construction without
        # a key), only an actual request does. The SDK resolves the key itself
        # from GEMINI_API_KEY or GOOGLE_API_KEY.
        self._client = client

    @property
    def client(self) -> genai.Client:
        if self._client is None:
            self._client = genai.Client()
        return self._client

    def complete(self, request: CompletionRequest) -> Completion:
        response = self.client.models.generate_content(**build_request_params(request))
        return parse_response(response)
