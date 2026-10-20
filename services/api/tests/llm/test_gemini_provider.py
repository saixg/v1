"""GeminiProvider request shaping and response parsing, with the SDK client
mocked at the `models.generate_content` boundary. Responses are real
`google.genai.types.GenerateContentResponse` objects so parsing is tested
against the SDK's own types. No network, no API key, no spend.

Mirrors tests/llm/test_anthropic_provider.py deliberately: the point of a
second provider is that the behaviour above it is unchanged, so the two files
assert the same things about two vendors.
"""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

from app.llm import (
    GEMINI_ROUTES,
    GeminiProvider,
    ImageInput,
    ModelCallError,
    ModelRefusalError,
    Prompt,
    StructuredOutputError,
    gemini_model_service,
)
from app.llm.gemini_provider import build_request_params, parse_response
from app.llm.service import CompletionRequest
from app.models import LlmCall

PNG_BASE64 = "iVBORw0KGgo="


class Plan(BaseModel):
    name: str = Field(max_length=40)
    steps: list[str]


def response(
    text: str,
    *,
    model: str = "gemini-2.5-flash",
    finish_reason: types.FinishReason | None = types.FinishReason.STOP,
    thinking: bool = False,
    no_candidates: bool = False,
    prompt_tokens: int = 321,
    cached_tokens: int = 7,
    answer_tokens: int = 54,
    thought_tokens: int = 0,
) -> types.GenerateContentResponse:
    parts: list[dict[str, Any]] = []
    if thinking:
        parts.append({"text": "deliberating", "thought": True})
    parts.append({"text": text})
    payload: dict[str, Any] = {
        "model_version": model,
        "response_id": "resp_test",
        "usage_metadata": {
            "prompt_token_count": prompt_tokens,
            "cached_content_token_count": cached_tokens,
            "candidates_token_count": answer_tokens,
            "thoughts_token_count": thought_tokens,
        },
    }
    if not no_candidates:
        payload["candidates"] = [
            {"content": {"role": "model", "parts": parts}, "finish_reason": finish_reason}
        ]
    return types.GenerateContentResponse.model_validate(payload)


def mock_client(*responses: types.GenerateContentResponse | Exception) -> MagicMock:
    client = MagicMock(spec=genai.Client)
    client.models = MagicMock()
    client.models.generate_content.side_effect = list(responses)
    return client


# --- request shaping ----------------------------------------------------------


def test_text_request_params():
    params = build_request_params(
        CompletionRequest(
            model="gemini-2.5-flash",
            prompt=Prompt(user="explain", system="You narrate facts you are given."),
            max_tokens=16000,
        )
    )
    assert params["model"] == "gemini-2.5-flash"
    assert params["contents"] == [
        types.Content(role="user", parts=[types.Part(text="explain")])
    ]
    config = params["config"]
    assert config.system_instruction == "You narrate facts you are given."
    assert config.max_output_tokens == 16000
    # No schema asked for: the response must not be forced to JSON.
    assert config.response_mime_type is None
    assert config.response_json_schema is None


def test_structured_request_sends_the_pydantic_schema_unchanged():
    schema = Plan.model_json_schema()
    config = build_request_params(
        CompletionRequest(
            model="gemini-2.5-pro",
            prompt=Prompt(user="plan"),
            max_tokens=16000,
            json_schema=schema,
        )
    )["config"]
    assert config.response_mime_type == "application/json"
    # response_json_schema, not response_schema: the latter is the restricted
    # OpenAPI subset and drops the $defs/$ref a nested model produces.
    assert config.response_schema is None
    assert config.response_json_schema == schema
    assert config.system_instruction is None


def test_image_request_params():
    params = build_request_params(
        CompletionRequest(
            model="gemini-2.5-flash",
            prompt=Prompt(
                user="describe the diagram",
                images=[ImageInput(media_type="image/png", data_base64=PNG_BASE64)],
            ),
            max_tokens=16000,
        )
    )
    parts = params["contents"][0].parts
    assert parts[0].inline_data is not None
    assert parts[0].inline_data.mime_type == "image/png"
    # The SDK base64-encodes Blob.data itself, so the bytes must go in decoded
    # or the model receives base64 text where an image should be.
    assert parts[0].inline_data.data == base64.b64decode(PNG_BASE64)
    assert parts[-1].text == "describe the diagram"


# --- response parsing ---------------------------------------------------------


def test_parse_response_skips_thinking_and_maps_usage():
    completion = parse_response(
        response('{"a": 1}', model="gemini-2.5-pro", thinking=True, thought_tokens=40)
    )
    assert completion.text == '{"a": 1}'
    assert completion.stop_reason == "end_turn"
    assert completion.served_model == "gemini-2.5-pro"
    assert completion.request_id == "resp_test"
    # prompt_token_count (321) includes the 7 cached tokens, so fresh input is
    # 314 and the cached ones are reported separately -- billed at the cache
    # rate rather than counted twice.
    assert completion.input_tokens == 314
    assert completion.cache_read_input_tokens == 7
    # Thinking tokens bill as output and candidates_token_count excludes them.
    assert completion.output_tokens == 54 + 40
    # Gemini caches are created through a separate API this codebase does not use.
    assert completion.cache_creation_input_tokens == 0


def test_parse_response_without_usage_metadata_is_all_zeros():
    bare = types.GenerateContentResponse.model_validate(
        {"candidates": [{"content": {"role": "model", "parts": [{"text": "hi"}]}}]}
    )
    completion = parse_response(bare)
    assert completion.text == "hi"
    assert (completion.input_tokens, completion.output_tokens) == (0, 0)
    assert completion.stop_reason is None


@pytest.mark.parametrize(
    "reason",
    [
        types.FinishReason.SAFETY,
        types.FinishReason.PROHIBITED_CONTENT,
        types.FinishReason.BLOCKLIST,
        types.FinishReason.SPII,
        types.FinishReason.RECITATION,
        types.FinishReason.LANGUAGE,
        types.FinishReason.IMAGE_SAFETY,
    ],
)
def test_declining_finish_reasons_become_refusal(reason: types.FinishReason):
    assert parse_response(response("", finish_reason=reason)).stop_reason == "refusal"


def test_blocked_prompt_with_no_candidates_is_a_refusal():
    completion = parse_response(response("", no_candidates=True))
    assert completion.stop_reason == "refusal"
    assert completion.text == ""


def test_max_tokens_finish_reason_is_mapped():
    completion = parse_response(
        response("half an ans", finish_reason=types.FinishReason.MAX_TOKENS)
    )
    assert completion.stop_reason == "max_tokens"


def test_unrecognised_finish_reason_is_passed_through_not_called_a_success():
    # It must not become "end_turn": that would present a truncated or failed
    # generation as a complete answer. Passed through, it lands in
    # llm_calls.stop_reason and fails schema validation visibly.
    completion = parse_response(response("", finish_reason=types.FinishReason.OTHER))
    assert completion.stop_reason == "other"


# --- provider wiring through the full service ---------------------------------


def test_provider_does_not_need_credentials_until_a_request():
    # The SDK's Client raises at construction without a key, so the provider
    # must not build one until a request is actually made.
    gemini_model_service(recorder=lambda _: None)
    GeminiProvider()


def test_gemini_service_routes_to_gemini_models_not_claude_ones():
    client = mock_client(
        response("nope"),
        response(json.dumps({"name": "add auth", "steps": ["a"]}), model="gemini-2.5-pro"),
    )
    service = gemini_model_service(recorder=lambda _: None, provider=GeminiProvider(client=client))

    service.generate_structured(Plan, "plan it", "design")

    assert [c.kwargs["model"] for c in client.models.generate_content.call_args_list] == [
        GEMINI_ROUTES["design"].model,
        GEMINI_ROUTES["design"].escalation_model,
    ]


def test_end_to_end_structured_call_through_mocked_client():
    client = mock_client(
        response('{"name": "x"}'),  # invalid: missing steps
        response(json.dumps({"name": "add auth", "steps": ["a", "b"]}), model="gemini-2.5-pro"),
    )
    rows: list[LlmCall] = []
    service = gemini_model_service(recorder=rows.append, provider=GeminiProvider(client=client))

    plan = service.generate_structured(Plan, "plan it", "design")

    assert plan == Plan(name="add auth", steps=["a", "b"])
    calls = client.models.generate_content.call_args_list
    assert all(c.kwargs["config"].response_json_schema is not None for c in calls)
    assert [(r.provider, r.status, r.served_model) for r in rows] == [
        ("gemini", "validation_failed", "gemini-2.5-flash"),
        ("gemini", "succeeded", "gemini-2.5-pro"),
    ]
    assert rows[1].input_tokens == 314
    # Priced, not recorded as "not priced": an unpriced row sums to nothing in
    # the ledger and would run with no cost cap.
    assert rows[1].cost_usd is not None and rows[1].cost_usd > 0


def test_end_to_end_retry_exhaustion_through_mocked_client():
    client = mock_client(response("nope"), response("still nope"))
    service = gemini_model_service(recorder=lambda _: None, provider=GeminiProvider(client=client))
    with pytest.raises(StructuredOutputError):
        service.generate_structured(Plan, "plan it", "synth")
    assert client.models.generate_content.call_count == 2


def test_sdk_error_is_wrapped_and_recorded():
    client = mock_client(RuntimeError("connection reset"))
    rows: list[LlmCall] = []
    service = gemini_model_service(recorder=rows.append, provider=GeminiProvider(client=client))

    with pytest.raises(ModelCallError, match="connection reset"):
        service.generate_text("hello", "synth")
    assert [r.status for r in rows] == ["error"]
    assert rows[0].provider == "gemini"


def test_refusal_through_mocked_client():
    client = mock_client(response("", finish_reason=types.FinishReason.SAFETY))
    rows: list[LlmCall] = []
    service = gemini_model_service(recorder=rows.append, provider=GeminiProvider(client=client))
    with pytest.raises(ModelRefusalError):
        service.generate_text("hello", "synth")
    assert client.models.generate_content.call_count == 1
    assert [r.status for r in rows] == ["refused"]


def test_images_are_refused_for_a_role_that_does_not_accept_them():
    from app.llm import ModelServiceError

    service = gemini_model_service(
        recorder=lambda _: None, provider=GeminiProvider(client=mock_client())
    )
    prompt = Prompt(
        user="what is this",
        images=[ImageInput(media_type="image/png", data_base64=PNG_BASE64)],
    )
    with pytest.raises(ModelServiceError, match="does not accept images"):
        service.generate_text(prompt, "synth")
