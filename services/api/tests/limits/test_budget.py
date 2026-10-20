"""The cost cap refuses work before the provider is called.

No database and no model: the guard is driven through a fake spend reader and
a `FakeProvider`, the same way `tests/llm/test_model_service.py` does. What is
checked is the behaviour the audit found missing — that a call is *refused*,
with a typed error distinguishable from a provider failure, and that nothing
is recorded for a call that never happened.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from app.limits.budget import (
    CAP_ENV_VAR,
    DEFAULT_MONTHLY_CAP_USD,
    BudgetCheckUnavailableError,
    BudgetExceededError,
    ProjectBudgetGuard,
    default_cap_usd,
    month_start,
)
from app.llm import (
    CallContext,
    Completion,
    CompletionProvider,
    CompletionRequest,
    ModelServiceError,
    RoutedModelService,
)
from app.models import LlmCall

PROJECT = "11111111-1111-1111-1111-111111111111"


class Answer(BaseModel):
    text: str


class CountingProvider(CompletionProvider):
    name = "fake"

    def __init__(self, cost_per_call: float = 1.0) -> None:
        self.calls = 0
        self._cost = cost_per_call

    def complete(self, request: CompletionRequest) -> Completion:
        self.calls += 1
        return Completion(
            text=json.dumps({"text": "ok"}),
            stop_reason="end_turn",
            served_model=request.model,
            input_tokens=1000,
            output_tokens=100,
        )


class FakeGuard:
    """A guard whose spend is whatever the test says, with no ledger."""

    def __init__(self, spend_usd: float, cap_usd: float) -> None:
        self.spend_usd = spend_usd
        self.cap_usd = cap_usd
        self.checked: list[tuple[str | None, str]] = []
        self.noted: list[float | None] = []

    def check(self, project_id: str | None, *, operation: str) -> None:
        self.checked.append((project_id, operation))
        if project_id is None:
            return
        if self.spend_usd >= self.cap_usd:
            raise BudgetExceededError(
                project_id=project_id,
                operation=operation,
                spend_usd=self.spend_usd,
                cap_usd=self.cap_usd,
                window_start=month_start(),
            )

    def note_spend(self, project_id: str | None, cost_usd: float | None) -> None:
        self.noted.append(cost_usd)
        if cost_usd:
            self.spend_usd += cost_usd


def _service(provider: CompletionProvider, guard) -> tuple[RoutedModelService, list[LlmCall]]:
    rows: list[LlmCall] = []
    return (
        RoutedModelService(provider=provider, recorder=rows.append, budget=guard),
        rows,
    )


# ------------------------------------------------------------- the refusal


def test_a_project_over_its_cap_is_refused_before_the_provider_is_called():
    provider = CountingProvider()
    service, rows = _service(provider, FakeGuard(spend_usd=10.0, cap_usd=10.0))

    with pytest.raises(BudgetExceededError) as exc:
        service.generate_structured(
            Answer, "hello", "bulk", context=CallContext(project_id=PROJECT)
        )

    assert provider.calls == 0, "the provider must not be asked once the cap is reached"
    assert rows == [], "a call that never happened writes no usage row"
    assert exc.value.cap_usd == 10.0
    assert exc.value.project_id == PROJECT
    assert exc.value.operation == "structured"
    # The message has to explain itself, not just fail.
    assert "cap" in str(exc.value) and "Nothing was charged" in str(exc.value)


def test_a_cap_refusal_is_not_a_model_service_error():
    """The distinction the existing 502 handlers depend on: a cap refusal must
    not be caught by `except ModelServiceError`, which means "the provider
    failed, a retry may help"."""
    assert not issubclass(BudgetExceededError, ModelServiceError)
    assert not issubclass(BudgetCheckUnavailableError, ModelServiceError)

    service, _ = _service(CountingProvider(), FakeGuard(spend_usd=99.0, cap_usd=1.0))
    with pytest.raises(BudgetExceededError):
        try:
            service.generate_text("hi", "bulk", context=CallContext(project_id=PROJECT))
        except ModelServiceError:  # pragma: no cover - would mean the types collapsed
            pytest.fail("a cost-cap refusal was caught as a provider failure")


def test_a_call_under_the_cap_proceeds_and_is_recorded():
    provider = CountingProvider()
    guard = FakeGuard(spend_usd=0.0, cap_usd=10.0)
    service, rows = _service(provider, guard)

    service.generate_text("hi", "bulk", context=CallContext(project_id=PROJECT))

    assert provider.calls == 1
    assert len(rows) == 1
    assert guard.checked == [(PROJECT, "text")]
    # The completed call's cost was charged against the guard's running total.
    assert guard.noted == [rows[0].cost_usd]
    assert guard.spend_usd > 0


def test_an_unattributed_call_is_not_capped():
    """No project_id means nothing to charge against (the eval harness, an
    unattributed call). Checked, then allowed — never charged to a guessed
    project."""
    provider = CountingProvider()
    guard = FakeGuard(spend_usd=1e9, cap_usd=1.0)
    service, _ = _service(provider, guard)

    service.generate_text("hi", "bulk")

    assert provider.calls == 1
    assert guard.checked == [(None, "text")]


def test_a_multi_call_pass_stops_at_the_first_call_over_the_cap():
    """The documented mid-pass behaviour: calls already made stand, the pass
    stops where the cap was crossed rather than finishing or rolling back."""
    provider = CountingProvider()
    guard = FakeGuard(spend_usd=0.0, cap_usd=0.0001)
    service, rows = _service(provider, guard)

    # First call is under the cap and pushes spend over it.
    service.generate_text("one", "bulk", context=CallContext(project_id=PROJECT))
    assert provider.calls == 1 and len(rows) == 1

    with pytest.raises(BudgetExceededError):
        service.generate_text("two", "bulk", context=CallContext(project_id=PROJECT))

    assert provider.calls == 1, "the second call was refused, not sent"
    assert len(rows) == 1, "and the completed first call's row is untouched"


def test_the_schema_retry_is_also_checked():
    """Every provider attempt funnels through the same check, so a structured
    call whose first attempt exhausts the budget does not get a free retry."""

    class BadThenGood(CompletionProvider):
        name = "fake"

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, request: CompletionRequest) -> Completion:
            self.calls += 1
            return Completion(
                text="not json",
                stop_reason="end_turn",
                served_model=request.model,
                input_tokens=1_000_000,
                output_tokens=1_000_000,
            )

    provider = BadThenGood()
    guard = FakeGuard(spend_usd=0.0, cap_usd=1.0)
    service, _ = _service(provider, guard)

    with pytest.raises(BudgetExceededError):
        service.generate_structured(
            Answer, "hello", "bulk", context=CallContext(project_id=PROJECT)
        )
    assert provider.calls == 1, "the retry attempt was refused on cost"


# ---------------------------------------------------- guard internals


def test_the_guard_fails_closed_when_the_ledger_cannot_be_read(monkeypatch):
    """A database outage must not silently remove the only spend ceiling."""
    import app.limits.budget as budget_module

    def _boom(project_id, *, now=None):
        raise RuntimeError("API_DATABASE_URL is not set")

    monkeypatch.setattr(budget_module, "read_budget_status", _boom)
    with pytest.raises(BudgetCheckUnavailableError):
        ProjectBudgetGuard().check(PROJECT, operation="text")


def test_the_guard_caches_one_ledger_read_per_ttl(monkeypatch):
    import app.limits.budget as budget_module
    from app.knowledge.spend import SpendWindow

    reads = []
    now = [1000.0]

    def _read(project_id, *, now=None):
        reads.append(project_id)
        return budget_module.BudgetStatus(
            project_id=project_id,
            window_start=datetime(2026, 10, 1, tzinfo=UTC),
            cap_usd=10.0,
            cap_source="deployment_default",
            window=SpendWindow(spend_usd=0.0, calls=0, failed_calls=0, unpriced_calls=0),
        )

    monkeypatch.setattr(budget_module, "read_budget_status", _read)
    guard = ProjectBudgetGuard(clock=lambda: now[0])

    for _ in range(5):
        guard.check(PROJECT, operation="text")
    assert len(reads) == 1, "the ledger is not re-read on every model call"

    now[0] += budget_module.SPEND_CACHE_TTL_SECONDS + 1
    guard.check(PROJECT, operation="text")
    assert len(reads) == 2, "and the cache does expire"


def test_locally_noted_spend_cannot_ride_a_stale_read_past_the_cap(monkeypatch):
    import app.limits.budget as budget_module
    from app.knowledge.spend import SpendWindow

    def _read(project_id, *, now=None):
        return budget_module.BudgetStatus(
            project_id=project_id,
            window_start=datetime(2026, 10, 1, tzinfo=UTC),
            cap_usd=1.0,
            cap_source="deployment_default",
            window=SpendWindow(spend_usd=0.9, calls=1, failed_calls=0, unpriced_calls=0),
        )

    monkeypatch.setattr(budget_module, "read_budget_status", _read)
    guard = ProjectBudgetGuard(clock=lambda: 500.0)  # frozen: the TTL never expires

    guard.check(PROJECT, operation="text")
    guard.note_spend(PROJECT, 0.2)
    with pytest.raises(BudgetExceededError):
        guard.check(PROJECT, operation="text")


def test_an_unpriced_call_adds_nothing_rather_than_guessing(monkeypatch):
    import app.limits.budget as budget_module
    from app.knowledge.spend import SpendWindow

    monkeypatch.setattr(
        budget_module,
        "read_budget_status",
        lambda project_id, now=None: budget_module.BudgetStatus(
            project_id=project_id,
            window_start=datetime(2026, 10, 1, tzinfo=UTC),
            cap_usd=1.0,
            cap_source="deployment_default",
            window=SpendWindow(spend_usd=0.5, calls=1, failed_calls=0, unpriced_calls=1),
        ),
    )
    guard = ProjectBudgetGuard(clock=lambda: 1.0)
    guard.check(PROJECT, operation="text")
    guard.note_spend(PROJECT, None)
    guard.check(PROJECT, operation="text")  # still under: nothing was invented


# ------------------------------------------------------------ configuration


def test_the_default_cap_comes_from_the_environment(monkeypatch):
    monkeypatch.delenv(CAP_ENV_VAR, raising=False)
    assert default_cap_usd() == DEFAULT_MONTHLY_CAP_USD
    monkeypatch.setenv(CAP_ENV_VAR, "2.5")
    assert default_cap_usd() == 2.5


@pytest.mark.parametrize("bad", ["abc", "-1"])
def test_a_malformed_cap_is_a_configuration_error_not_an_uncapped_run(monkeypatch, bad):
    monkeypatch.setenv(CAP_ENV_VAR, bad)
    with pytest.raises(RuntimeError):
        default_cap_usd()


def test_the_window_is_the_utc_calendar_month():
    start = month_start(datetime(2026, 10, 20, 13, 45, tzinfo=UTC))
    assert start == datetime(2026, 10, 1, tzinfo=UTC)
