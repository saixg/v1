"""Anthropic Messages API provider for ModelService.

Structured output uses `output_config.format` (JSON-schema constrained
decoding), not a forced tool call: forced `tool_choice` ("any"/"tool") is
rejected with a 400 on claude-sonnet-5-5 and claude-opus-5-5, which are two of
the three routed models. The schema sent on the wire is the SDK's
`transform_schema` of the caller's Pydantic schema; constraints the API can't
enforce (e.g. maxLength) are folded into descriptions by that transform, which
is why RoutedModelService still validates every response with Pydantic.

Thinking is left at each model's default (adaptive on every routed model;
explicitly disabling it is a 400 on Sonnet 5.5 / Opus 5.5). Thinking blocks
are skipped when reading the response text.
"""

from __future__ import annotations

from typing import Any

import anthropic

from app.llm.service import Completion, CompletionProvider, CompletionRequest, Prompt


def _user_content(prompt: Prompt) -> str | list[dict[str, Any]]:
    if not prompt.images:
        return prompt.user
    blocks: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": image.media_type,
                "data": image.data_base64,
            },
        }
        for image in prompt.images
    ]
    blocks.append({"type": "text", "text": prompt.user})
    return blocks


def build_request_params(request: CompletionRequest) -> dict[str, Any]:
    """Map a provider-neutral request to `messages.create` keyword arguments."""
    params: dict[str, Any] = {
        "model": request.model,
        "max_tokens": request.max_tokens,
        "messages": [{"role": "user", "content": _user_content(request.prompt)}],
    }
    if request.prompt.system:
        params["system"] = request.prompt.system
    if request.json_schema is not None:
        params["output_config"] = {
            "format": {
                "type": "json_schema",
                "schema": anthropic.transform_schema(request.json_schema),
            }
        }
    return params


def parse_response(message: Any) -> Completion:
    """Map an `anthropic.types.Message` to a provider-neutral Completion."""
    text = "".join(
        block.text for block in message.content if getattr(block, "type", None) == "text"
    )
    usage = message.usage
    return Completion(
        text=text,
        stop_reason=message.stop_reason,
        served_model=message.model,
        input_tokens=usage.input_tokens or 0,
        output_tokens=usage.output_tokens or 0,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
        request_id=getattr(message, "_request_id", None),
    )


class AnthropicProvider(CompletionProvider):
    name = "anthropic"

    def __init__(self, client: anthropic.Anthropic | None = None) -> None:
        # The client is built lazily so importing / constructing ModelService
        # never requires credentials; only an actual request does. The SDK
        # resolves credentials itself (ANTHROPIC_API_KEY, auth token, or profile).
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def complete(self, request: CompletionRequest) -> Completion:
        message = self.client.messages.create(**build_request_params(request))
        return parse_response(message)
