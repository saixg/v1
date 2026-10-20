"""GeminiProvider request shaping and response parsing, with `httpx` mocked at
the `Client.post` boundary — the same level the Anthropic tests mock the SDK's
`messages.create`. Responses are real `httpx.Response` objects, so status
handling and JSON decoding are exercised rather than stubbed.

No network, no API key, no spend.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import BaseModel, Field

from app.llm import (
    GeminiProvider,
    ImageInput,
    ModelCallError,
    ModelOutputTruncatedError,
    ModelRefusalError,
    Prompt,
    StructuredOutputError,
    model_service,
)
from app.llm.gemini_provider import (
    API_KEY_ENV,
    GeminiApiError,
    build_request_params,
    parse_response,
)
from app.llm.service import CompletionRequest
from app.models import LlmCall


class Plan(BaseModel):
    name: str = Field(max_length=40)
    steps: list[str]


def body(
    text: str,
    *,
    model: str = "gemini-3.8-flash",
    finish: str | None = "STOP",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": [{"text": text}]}}
    if finish is not None:
        candidate["finishReason"] = finish
    return {
        "candidates": [candidate],
        "modelVersion": model,
        "usageMetadata": {"promptTokenCount": 321, "candidatesTokenCount": 54}
        if usage is None
        else usage,
    }


def response(payload: dict[str, Any], status: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=payload,
        request=httpx.Request("POST", "https://generativelanguage.googleapis.com/x"),
    )


def mock_client(*results: httpx.Response | Exception) -> MagicMock:
    client = MagicMock(spec=httpx.Client)
    client.post.side_effect = list(results)
    return client


def gemini_service(*results: httpx.Response | Exception, recorder=None) -> tuple[Any, MagicMock]:
    client = mock_client(*results)
    service = model_service(
        recorder=recorder or (lambda _: None),
        provider=GeminiProvider(api_key="test-key", client=client),
    )
    return service, client


# --- request shaping ----------------------------------------------------------


def test_text_request_body():
    params = build_request_params(
        CompletionRequest(
            model="gemini-3.8-flash",
            prompt=Prompt(user="explain", system="You narrate facts you are given."),
            max_tokens=16000,
        )
    )
    assert params == {
        "contents": [{"role": "user", "parts": [{"text": "explain"}]}],
        "generationConfig": {"maxOutputTokens": 16000},
        "systemInstruction": {"parts": [{"text": "You narrate facts you are given."}]},
    }


def test_no_system_instruction_when_there_is_no_system_prompt():
    params = build_request_params(
        CompletionRequest(model="gemini-3.8-flash", prompt=Prompt(user="hi"), max_tokens=512)
    )
    assert "systemInstruction" not in params
    assert params["generationConfig"] == {"maxOutputTokens": 512}


def test_structured_request_sets_mime_type_and_transformed_schema():
    params = build_request_params(
        CompletionRequest(
            model="gemini-3.1-flash-lite",
            prompt=Prompt(user="plan"),
            max_tokens=16000,
            json_schema=Plan.model_json_schema(),
        )
    )
    config = params["generationConfig"]
    # A responseSchema without the JSON mime type is ignored by the API.
    assert config["responseMimeType"] == "application/json"
    schema = config["responseSchema"]
    assert schema["type"] == "OBJECT"
    assert set(schema["required"]) == {"name", "steps"}
    # Rejected keywords are gone; the constraint survives in the description.
    assert "additionalProperties" not in schema
    assert "maxLength" not in schema["properties"]["name"]
    assert "maxLength: 40" in schema["properties"]["name"]["description"]


def test_images_come_before_the_text_part():
    params = build_request_params(
        CompletionRequest(
            model="gemini-3.8-flash",
            prompt=Prompt(
                user="describe the diagram",
                images=[ImageInput(media_type="image/png", data_base64="iVBORw0KGgo=")],
            ),
            max_tokens=16000,
        )
    )
    parts = params["contents"][0]["parts"]
    assert parts[0] == {"inlineData": {"mimeType": "image/png", "data": "iVBORw0KGgo="}}
    assert parts[-1] == {"text": "describe the diagram"}


def test_url_and_auth_header():
    client = mock_client(response(body('{"a": 1}')))
    provider = GeminiProvider(api_key="test-key", client=client)
    provider.complete(
        CompletionRequest(model="gemini-3.8-flash", prompt=Prompt(user="hi"), max_tokens=64)
    )
    call = client.post.call_args
    assert call.args[0] == (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.8-flash:generateContent"
    )
    # x-goog-api-key, not Authorization: Bearer (which this endpoint 401s on).
    assert call.kwargs["headers"]["x-goog-api-key"] == "test-key"
    assert "Authorization" not in call.kwargs["headers"]


# --- response parsing ---------------------------------------------------------


def test_parse_response_maps_text_usage_and_served_model():
    payload = body('{"a": 1}', model="gemini-3.1-flash-lite")
    completion = parse_response(payload, request_id="req-1")
    assert completion.text == '{"a": 1}'
    assert completion.stop_reason == "end_turn"
    assert completion.served_model == "gemini-3.1-flash-lite"
    assert (completion.input_tokens, completion.output_tokens) == (321, 54)
    assert completion.request_id == "req-1"


def test_parse_response_concatenates_parts_and_skips_non_text():
    payload = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "a"},
                        {"thought": True},
                        {"inlineData": {"mimeType": "image/png", "data": "x"}},
                        {"text": "b"},
                    ]
                },
                "finishReason": "STOP",
            }
        ]
    }
    assert parse_response(payload).text == "ab"


def test_parse_response_counts_thinking_as_output_and_reads_cache():
    completion = parse_response(
        body(
            "x",
            usage={
                "promptTokenCount": 100,
                "candidatesTokenCount": 10,
                "thoughtsTokenCount": 40,
                "cachedContentTokenCount": 25,
            },
        )
    )
    # Thinking tokens bill as output, so they must be in the output count.
    assert completion.output_tokens == 50
    assert completion.cache_read_input_tokens == 25


def test_parse_response_survives_missing_usage_metadata():
    payload = {"candidates": [{"content": {"parts": [{"text": "hi"}]}, "finishReason": "STOP"}]}
    completion = parse_response(payload)
    assert completion.text == "hi"
    assert (completion.input_tokens, completion.output_tokens) == (0, 0)
    assert completion.cache_read_input_tokens == 0
    assert completion.served_model is None


def test_parse_response_survives_a_candidate_with_no_content():
    # Real shape: MAX_TOKENS reached before any part was emitted.
    payload = {"candidates": [{"finishReason": "MAX_TOKENS"}]}
    completion = parse_response(payload)
    assert completion.text == ""
    # Mapped so RoutedModelService raises truncation, not a validation failure.
    assert completion.stop_reason == "max_tokens"


def test_safety_finish_reason_becomes_a_refusal():
    for reason in ("SAFETY", "PROHIBITED_CONTENT", "RECITATION", "BLOCKLIST", "SPII"):
        payload = {"candidates": [{"content": {"parts": []}, "finishReason": reason}]}
        assert parse_response(payload).stop_reason == "refusal"


def test_unknown_finish_reason_is_passed_through_not_guessed():
    payload = {"candidates": [{"content": {"parts": [{"text": "x"}]}, "finishReason": "OTHER"}]}
    assert parse_response(payload).stop_reason == "OTHER"


def test_empty_candidates_raise_rather_than_returning_empty_text():
    # A prompt-level block has no candidate to read a finish reason from.
    with pytest.raises(GeminiApiError, match="SAFETY"):
        parse_response({"promptFeedback": {"blockReason": "SAFETY"}})
    with pytest.raises(GeminiApiError, match="no candidates"):
        parse_response({"candidates": []})


def test_non_object_body_raises():
    with pytest.raises(GeminiApiError):
        parse_response(["nope"])


# --- provider wiring ----------------------------------------------------------


def test_provider_does_not_need_credentials_until_a_request(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    GeminiProvider()  # constructing must not read or require a key
    model_service(recorder=lambda _: None)


def test_missing_key_is_named_at_request_time(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    provider = GeminiProvider(client=mock_client(response(body("x"))))
    with pytest.raises(GeminiApiError, match=API_KEY_ENV):
        provider.complete(
            CompletionRequest(model="gemini-3.8-flash", prompt=Prompt(user="hi"), max_tokens=8)
        )


def test_http_error_surfaces_as_a_raised_exception_without_echoing_the_body():
    provider = GeminiProvider(
        api_key="k", client=mock_client(response({"error": {"message": "secret-ish"}}, status=429))
    )
    with pytest.raises(GeminiApiError) as exc:
        provider.complete(
            CompletionRequest(model="gemini-3.8-flash", prompt=Prompt(user="hi"), max_tokens=8)
        )
    assert "429" in str(exc.value)
    assert "secret-ish" not in str(exc.value)


# --- through the full service -------------------------------------------------


def test_api_error_is_wrapped_and_recorded():
    rows: list[LlmCall] = []
    service, _ = gemini_service(
        response({"error": {}}, status=500), recorder=rows.append
    )
    with pytest.raises(ModelCallError, match="GeminiApiError"):
        service.generate_text("hello", "synth")
    assert [(r.provider, r.status) for r in rows] == [("gemini", "error")]


def test_transport_error_is_wrapped_and_recorded():
    rows: list[LlmCall] = []
    service, _ = gemini_service(
        httpx.ConnectError("no route", request=httpx.Request("POST", "https://x/y")),
        recorder=rows.append,
    )
    with pytest.raises(ModelCallError, match="ConnectError"):
        service.generate_text("hello", "synth")
    assert [r.status for r in rows] == ["error"]


def test_truncation_and_refusal_reach_the_service_layer():
    service, _ = gemini_service(response({"candidates": [{"finishReason": "MAX_TOKENS"}]}))
    with pytest.raises(ModelOutputTruncatedError):
        service.generate_text("hello", "synth")

    service, _ = gemini_service(response({"candidates": [{"finishReason": "SAFETY"}]}))
    with pytest.raises(ModelRefusalError):
        service.generate_text("hello", "synth")


def test_end_to_end_structured_call_retries_and_records(monkeypatch):
    monkeypatch.setenv("CODEAXIS_LLM_PROVIDER", "gemini")
    rows: list[LlmCall] = []
    service, client = gemini_service(
        response(body('{"name": "x"}')),  # invalid: missing steps
        response(body(json.dumps({"name": "add auth", "steps": ["a", "b"]}))),
        recorder=rows.append,
    )

    plan = service.generate_structured(Plan, "plan it", "design")

    assert plan == Plan(name="add auth", steps=["a", "b"])
    # No Pro model is reachable on this key, so the `design` retry stays on the
    # default model rather than naming an escalation that would 429.
    urls = [c.args[0] for c in client.post.call_args_list]
    assert [u.endswith("gemini-3.8-flash:generateContent") for u in urls] == [True, True]
    assert all(
        "responseSchema" in c.kwargs["json"]["generationConfig"]
        for c in client.post.call_args_list
    )
    assert [(r.provider, r.status, r.served_model) for r in rows] == [
        ("gemini", "validation_failed", "gemini-3.8-flash"),
        ("gemini", "succeeded", "gemini-3.8-flash"),
    ]
    # The ledger is priced, not zeroed: gemini-3.8-flash at 321 in / 54 out,
    # rounded to the 6 decimal places `estimate_cost_usd` records.
    assert rows[1].cost_usd == round(321 * 0.75e-6 + 54 * 3.75e-6, 6)


def test_retry_exhaustion_raises_structured_output_error():
    service, client = gemini_service(response(body("nope")), response(body("still nope")))
    with pytest.raises(StructuredOutputError):
        service.generate_structured(Plan, "plan it", "synth")
    assert client.post.call_count == 2


def test_real_shipping_schema_end_to_end_through_a_mocked_response(monkeypatch):
    """`generate_structured` with a schema that actually ships, so the
    transform is proven on the real thing and not only on a test model."""
    monkeypatch.setenv("CODEAXIS_LLM_PROVIDER", "gemini")
    from app.context.extract import RequirementExtraction

    payload = {
        "requirements": [
            {
                "key": "R1",
                "kind": "functional",
                "statement": "The importer accepts a ZIP archive.",
                "source_chunk": "D1.4",
                "quote": "accepts a ZIP archive",
                "priority": "must",
                "verifies": [],
                "region": None,
            }
        ],
        "open_questions": [
            {"question": "What is the size cap?", "source_chunk": None, "concerns": ["R1"]}
        ],
    }
    service, client = gemini_service(response(body(json.dumps(payload))))

    out = service.generate_structured(RequirementExtraction, "extract", "synth")

    assert isinstance(out, RequirementExtraction)
    assert out.requirements[0].key == "R1"
    assert out.open_questions[0].concerns == ["R1"]

    # The schema that went on the wire is the transformed one, and the
    # constraint Gemini cannot enforce rode along in the description.
    sent = client.post.call_args.kwargs["json"]["generationConfig"]["responseSchema"]
    requirement = sent["properties"]["requirements"]["items"]
    assert requirement["type"] == "OBJECT"
    assert "pattern: ^R[0-9]{1,4}$" in requirement["properties"]["key"]["description"]
    assert requirement["properties"]["priority"]["nullable"] is True
    assert "$defs" not in json.dumps(sent)
