"""AnthropicProvider request shaping and response parsing, with the SDK client
mocked at the `messages.create` boundary. Responses are real
`anthropic.types.Message` objects so parsing is tested against the SDK's own
types. No network, no API key, no spend."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import anthropic
import httpx2
import pytest
from anthropic.types import Message
from pydantic import BaseModel, Field

from app.llm import (
    AnthropicProvider,
    ImageInput,
    ModelCallError,
    Prompt,
    StructuredOutputError,
    anthropic_model_service,
)
from app.llm.anthropic_provider import build_request_params, parse_response
from app.llm.service import CompletionRequest
from app.models import LlmCall


class Plan(BaseModel):
    name: str = Field(max_length=40)
    steps: list[str]


def message(
    text: str,
    *,
    model: str = "claude-sonnet-5-5",
    stop_reason: str = "end_turn",
    thinking: bool = False,
) -> Message:
    content: list[dict[str, Any]] = []
    if thinking:
        content.append({"type": "thinking", "thinking": "", "signature": "sig"})
    content.append({"type": "text", "text": text})
    return Message.model_validate(
        {
            "id": "msg_test",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {
                "input_tokens": 321,
                "output_tokens": 54,
                "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 0,
            },
        }
    )


def mock_client(*responses: Message | Exception) -> MagicMock:
    client = MagicMock(spec=anthropic.Anthropic)
    client.messages = MagicMock()
    client.messages.create.side_effect = list(responses)
    return client


# --- request shaping ----------------------------------------------------------


def test_text_request_params():
    params = build_request_params(
        CompletionRequest(
            model="claude-sonnet-5-5",
            prompt=Prompt(user="explain", system="You narrate facts you are given."),
            max_tokens=16000,
        )
    )
    assert params == {
        "model": "claude-sonnet-5-5",
        "max_tokens": 16000,
        "system": "You narrate facts you are given.",
        "messages": [{"role": "user", "content": "explain"}],
    }


def test_structured_request_uses_output_config_not_forced_tool_choice():
    params = build_request_params(
        CompletionRequest(
            model="claude-opus-5-5",
            prompt=Prompt(user="plan"),
            max_tokens=16000,
            json_schema=Plan.model_json_schema(),
        )
    )
    # Forced tool_choice is a 400 on Sonnet 5.5 / Opus 5.5.
    assert "tool_choice" not in params and "tools" not in params
    assert "system" not in params
    fmt = params["output_config"]["format"]
    assert fmt["type"] == "json_schema"
    schema = fmt["schema"]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"name", "steps"}
    # Unsupported constraints are folded into the description by the SDK
    # transform, which is why Pydantic still validates the response.
    assert "maxLength" not in schema["properties"]["name"]


def test_image_request_params():
    params = build_request_params(
        CompletionRequest(
            model="claude-sonnet-5-5",
            prompt=Prompt(
                user="describe the diagram",
                images=[ImageInput(media_type="image/png", data_base64="iVBORw0KGgo=")],
            ),
            max_tokens=16000,
        )
    )
    content = params["messages"][0]["content"]
    assert content[0] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="},
    }
    assert content[-1] == {"type": "text", "text": "describe the diagram"}


# --- response parsing ---------------------------------------------------------


def test_parse_response_skips_thinking_and_maps_usage():
    completion = parse_response(message('{"a": 1}', model="claude-opus-5-5", thinking=True))
    assert completion.text == '{"a": 1}'
    assert completion.stop_reason == "end_turn"
    assert completion.served_model == "claude-opus-5-5"
    assert (completion.input_tokens, completion.output_tokens) == (321, 54)
    assert completion.cache_read_input_tokens == 7
    assert completion.cache_creation_input_tokens == 0


# --- provider wiring through the full service ---------------------------------


def test_provider_does_not_need_credentials_until_a_request():
    # Constructing the production service must not require ANTHROPIC_API_KEY.
    anthropic_model_service(recorder=lambda _: None)
    AnthropicProvider()


def test_end_to_end_structured_call_through_mocked_client():
    client = mock_client(
        message('{"name": "x"}'),  # invalid: missing steps
        message(json.dumps({"name": "add auth", "steps": ["a", "b"]}), model="claude-opus-5-5"),
    )
    rows: list[LlmCall] = []
    service = anthropic_model_service(
        recorder=rows.append, provider=AnthropicProvider(client=client)
    )

    plan = service.generate_structured(Plan, "plan it", "design")

    assert plan == Plan(name="add auth", steps=["a", "b"])
    calls = client.messages.create.call_args_list
    assert [c.kwargs["model"] for c in calls] == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert all("output_config" in c.kwargs for c in calls)
    assert [(r.provider, r.status, r.served_model) for r in rows] == [
        ("anthropic", "validation_failed", "claude-sonnet-5-5"),
        ("anthropic", "succeeded", "claude-opus-5-5"),
    ]
    assert rows[1].input_tokens == 321


def test_end_to_end_retry_exhaustion_through_mocked_client():
    client = mock_client(message("nope"), message("still nope"))
    service = anthropic_model_service(
        recorder=lambda _: None, provider=AnthropicProvider(client=client)
    )
    with pytest.raises(StructuredOutputError):
        service.generate_structured(Plan, "plan it", "synth")
    assert client.messages.create.call_count == 2


def test_sdk_error_is_wrapped_and_recorded():
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    client = mock_client(anthropic.APIConnectionError(request=request))
    rows: list[LlmCall] = []
    service = anthropic_model_service(
        recorder=rows.append, provider=AnthropicProvider(client=client)
    )

    with pytest.raises(ModelCallError, match="APIConnectionError"):
        service.generate_text("hello", "synth")
    assert [r.status for r in rows] == ["error"]


def test_refusal_through_mocked_client():
    from app.llm import ModelRefusalError

    client = mock_client(message("", stop_reason="refusal"))
    rows: list[LlmCall] = []
    service = anthropic_model_service(
        recorder=rows.append, provider=AnthropicProvider(client=client)
    )
    with pytest.raises(ModelRefusalError):
        service.generate_text("hello", "synth")
    assert client.messages.create.call_count == 1
    assert [r.status for r in rows] == ["refused"]
