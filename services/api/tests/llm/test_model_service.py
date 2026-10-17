"""RoutedModelService behavior against a scripted fake provider: routing,
validate-then-one-retry-then-fail, escalation, failure modes, and one usage
row per provider attempt. No network, no API key."""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from pydantic import BaseModel, Field

from app.llm import (
    CallContext,
    Completion,
    CompletionProvider,
    CompletionRequest,
    ImageInput,
    ModelCallError,
    ModelOutputTruncatedError,
    ModelRefusalError,
    ModelServiceError,
    Prompt,
    RoutedModelService,
    StructuredOutputError,
    UnroutedRoleError,
)
from app.models import LlmCall


class Summary(BaseModel):
    title: str
    points: list[str] = Field(min_length=1)


class FakeProvider(CompletionProvider):
    """Returns scripted responses in order; records every request it saw."""

    name = "fake"

    def __init__(self, script: list[Completion | Exception]) -> None:
        self.script = list(script)
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def ok(text: str, *, model: str | None = None, stop: str = "end_turn") -> Completion:
    return Completion(
        text=text,
        stop_reason=stop,
        served_model=model,
        input_tokens=1000,
        output_tokens=200,
        request_id="req_test",
    )


VALID = json.dumps({"title": "Ingest", "points": ["stages a repo"]})


def make(
    script: list[Completion | Exception], clock: Callable[[], float] | None = None
) -> tuple[RoutedModelService, FakeProvider, list[LlmCall]]:
    rows: list[LlmCall] = []
    provider = FakeProvider(script)
    ticks = iter(range(0, 10_000, 1))
    service = RoutedModelService(
        provider=provider,
        recorder=rows.append,
        clock=clock or (lambda: next(ticks) * 0.25),  # each reading 250ms later
    )
    return service, provider, rows


# --- structured: success ------------------------------------------------------


def test_structured_success_returns_validated_model_and_records_one_row():
    service, provider, rows = make([ok(VALID, model="claude-sonnet-5-5")])

    result = service.generate_structured(
        Summary, "summarize", "synth", context=CallContext(repo_id="r1", project_id="p1")
    )

    assert result == Summary(title="Ingest", points=["stages a repo"])
    assert [r.model for r in provider.requests] == ["claude-sonnet-5-5"]
    # The caller's schema is what the provider is asked to constrain to.
    assert provider.requests[0].json_schema == Summary.model_json_schema()

    (row,) = rows
    assert row.status == "succeeded"
    assert row.role == "synth"
    assert row.operation == "structured"
    assert row.provider == "fake"
    assert row.model == row.served_model == "claude-sonnet-5-5"
    assert row.attempt == 1
    assert row.schema_name == "Summary"
    assert (row.input_tokens, row.output_tokens) == (1000, 200)
    assert row.cost_usd == pytest.approx((1000 * 2.0 + 200 * 10.0) / 1e6)
    assert row.latency_ms == 250
    assert row.request_id == "req_test"
    assert (row.repo_id, row.project_id, row.job_id) == ("r1", "p1", None)
    assert row.error is None


def test_bulk_role_routes_to_haiku():
    service, provider, _ = make([ok(VALID)])
    service.generate_structured(Summary, "summarize", "bulk")
    assert provider.requests[0].model == "claude-haiku-5-5"
    assert provider.requests[0].max_tokens == 4096


# --- structured: retry once, then fail ---------------------------------------


def test_invalid_then_valid_retries_once_and_succeeds():
    service, provider, rows = make([ok('{"title": "x"}'), ok(VALID)])

    result = service.generate_structured(Summary, Prompt(user="go", system="sys"), "synth")

    assert result.title == "Ingest"
    assert len(provider.requests) == 2
    retry = provider.requests[1]
    # Retry carries the original prompt plus the validation errors; system is unchanged.
    assert retry.prompt.user.startswith("go")
    assert "did not validate" in retry.prompt.user
    assert "points" in retry.prompt.user
    assert retry.prompt.system == "sys"
    assert [(r.attempt, r.status) for r in rows] == [(1, "validation_failed"), (2, "succeeded")]
    assert rows[0].error is not None and "points" in rows[0].error


def test_two_invalid_responses_fail_visibly_after_exactly_two_attempts():
    service, provider, rows = make([ok("not json"), ok('{"title": 3, "points": []}')])

    with pytest.raises(StructuredOutputError) as info:
        service.generate_structured(Summary, "go", "synth")

    assert info.value.attempts == 2
    assert info.value.schema_name == "Summary"
    assert len(info.value.errors) == 2
    assert len(provider.requests) == 2  # never a third attempt
    assert [r.status for r in rows] == ["validation_failed", "validation_failed"]


def test_validation_errors_do_not_echo_model_output():
    secretish = "AKIA_THIS_CAME_FROM_THE_REPO"
    service, _, rows = make([ok(json.dumps({"title": 1, "points": [secretish]}))] * 2)

    with pytest.raises(StructuredOutputError):
        service.generate_structured(Summary, "go", "synth")

    assert all(secretish not in (r.error or "") for r in rows)


def test_malformed_output_is_never_coerced():
    # `title` must be a string; pydantic's lax mode would not coerce a list.
    service, _, _ = make([ok('{"title": ["a"], "points": ["p"]}')] * 2)
    with pytest.raises(StructuredOutputError):
        service.generate_structured(Summary, "go", "synth")


# --- design escalation --------------------------------------------------------


def test_design_escalates_to_opus_on_failed_validation():
    service, provider, rows = make([ok("{}"), ok(VALID)])

    service.generate_structured(Summary, "plan", "design")

    assert [r.model for r in provider.requests] == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert [r.model for r in rows] == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert rows[1].cost_usd == pytest.approx((1000 * 4.0 + 200 * 20.0) / 1e6)


def test_design_deep_mode_starts_on_opus():
    service, provider, _ = make([ok(VALID)])
    service.generate_structured(Summary, "plan", "design", deep=True)
    assert provider.requests[0].model == "claude-opus-5-5"


def test_deep_mode_is_noop_for_roles_without_escalation():
    service, provider, _ = make([ok(VALID)])
    service.generate_structured(Summary, "go", "synth", deep=True)
    assert provider.requests[0].model == "claude-sonnet-5-5"


# --- failure modes: recorded, raised, not retried -----------------------------


def test_refusal_fails_without_retry_and_is_recorded():
    service, provider, rows = make([ok("", stop="refusal")])
    with pytest.raises(ModelRefusalError):
        service.generate_structured(Summary, "go", "design")
    assert len(provider.requests) == 1
    assert [r.status for r in rows] == ["refused"]


def test_truncation_fails_without_retry_and_is_recorded():
    service, provider, rows = make([ok('{"title": "Ing', stop="max_tokens")])
    with pytest.raises(ModelOutputTruncatedError):
        service.generate_structured(Summary, "go", "synth")
    assert len(provider.requests) == 1
    assert [r.status for r in rows] == ["truncated"]


def test_provider_exception_is_wrapped_and_recorded():
    service, _, rows = make([ConnectionError("network down")])
    with pytest.raises(ModelCallError, match="network down"):
        service.generate_structured(Summary, "go", "synth")
    (row,) = rows
    assert row.status == "error"
    assert row.input_tokens == row.output_tokens == 0
    assert row.cost_usd == 0.0
    assert row.served_model is None
    assert row.error is not None and "ConnectionError" in row.error


def test_recorder_failure_fails_the_call():
    # An unrecorded model call is a bug, not a warning.
    def broken_recorder(_: LlmCall) -> None:
        raise RuntimeError("db down")

    service = RoutedModelService(provider=FakeProvider([ok(VALID)]), recorder=broken_recorder)
    with pytest.raises(RuntimeError, match="db down"):
        service.generate_structured(Summary, "go", "synth")


# --- text, vision, routing errors, stubs --------------------------------------


def test_generate_text_records_one_row():
    service, provider, rows = make([ok("Ingest stages a repo.")])
    assert service.generate_text("explain", "synth") == "Ingest stages a repo."
    assert provider.requests[0].json_schema is None
    (row,) = rows
    assert (row.operation, row.status, row.schema_name) == ("text", "succeeded", None)


def test_generate_text_truncation_fails_visibly():
    service, _, rows = make([ok("partial", stop="max_tokens")])
    with pytest.raises(ModelOutputTruncatedError):
        service.generate_text("explain", "synth")
    assert [r.status for r in rows] == ["truncated"]


def test_images_only_accepted_by_vision_role():
    image = ImageInput(media_type="image/png", data_base64="iVBORw0KGgo=")
    prompt = Prompt(user="describe", images=[image])

    service, provider, rows = make([ok("a diagram")])
    with pytest.raises(ModelServiceError, match="vision"):
        service.generate_text(prompt, "synth")
    assert provider.requests == [] and rows == []  # rejected before any spend

    assert service.generate_text(prompt, "vision") == "a diagram"
    assert provider.requests[0].prompt.images == [image]


def test_unrouted_role_raises_before_any_call():
    service, provider, rows = make([])
    with pytest.raises(UnroutedRoleError):
        service.generate_text("x", "embed")
    assert provider.requests == [] and rows == []


def test_routes_override_is_a_config_change():
    from app.llm import ModelRoute

    rows: list[LlmCall] = []
    provider = FakeProvider([ok(VALID)])
    service = RoutedModelService(
        provider=provider,
        recorder=rows.append,
        routes={"synth": ModelRoute(model="claude-opus-5-5")},
    )
    service.generate_structured(Summary, "go", "synth")
    assert provider.requests[0].model == "claude-opus-5-5"


def test_embed_and_rerank_are_explicit_stubs():
    service, _, rows = make([])
    with pytest.raises(NotImplementedError, match="Part 13"):
        service.embed(["a"])
    with pytest.raises(NotImplementedError, match="eval"):
        service.rerank("q", ["a"])
    assert rows == []
