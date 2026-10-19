"""A scripted `CompletionProvider` and a `RoutedModelService` around it, for
blueprint tests. Same shape as `tests/llm/test_model_service.py`'s
`FakeProvider`: no network, no API key, no DB.

Loaded by path (pytest runs with `--import-mode=importlib` and `tests/` is
not a package), so this is not a conftest.
"""

from __future__ import annotations

import json
from typing import Any

from app.llm import Completion, CompletionProvider, CompletionRequest, RoutedModelService
from app.models import LlmCall


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


def reply(payload: dict[str, Any] | str) -> Completion:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return Completion(
        text=text,
        stop_reason="end_turn",
        served_model="claude-sonnet-5-5",
        input_tokens=500,
        output_tokens=120,
        request_id="req_blueprint_test",
    )


def fake_service(
    script: list[Completion | Exception],
) -> tuple[RoutedModelService, FakeProvider, list[LlmCall]]:
    rows: list[LlmCall] = []
    provider = FakeProvider(script)
    ticks = iter(range(0, 100_000))
    return (
        RoutedModelService(
            provider=provider, recorder=rows.append, clock=lambda: next(ticks) * 0.25
        ),
        provider,
        rows,
    )
