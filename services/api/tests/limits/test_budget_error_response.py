"""A cap refusal must reach the client as an explained API error, not a 500.

The handlers are registered on the app rather than caught per route — there
is no single place every model-calling route funnels through — so what is
checked here is that they are registered for the right exception types and
that their payloads are the ones a client can act on. No database and no
model: the handlers are pure functions of the exception.
"""

from __future__ import annotations

import json

import pytest

from app.limits import BudgetCheckUnavailableError, BudgetExceededError, month_start
from app.main import _budget_exceeded, _budget_unavailable, app


def test_both_budget_errors_have_a_registered_handler():
    """Without these, `BudgetExceededError` -- which is deliberately not a
    `ModelServiceError` and so is not caught by the 502 handlers in
    app/context_api.py -- would surface as a generic 500."""
    assert BudgetExceededError in app.exception_handlers
    assert BudgetCheckUnavailableError in app.exception_handlers


@pytest.mark.anyio
async def test_a_cap_refusal_is_a_402_that_explains_itself():
    exc = BudgetExceededError(
        project_id="p-1",
        operation="structured",
        spend_usd=10.5,
        cap_usd=10.0,
        window_start=month_start(),
    )
    response = await _budget_exceeded(None, exc)  # type: ignore[arg-type]
    assert response.status_code == 402
    body = json.loads(bytes(response.body))
    assert body["error"] == "cost_cap_exceeded"
    assert body["project_id"] == "p-1"
    assert body["operation"] == "structured"
    assert body["cap_usd"] == 10.0
    assert body["spend_usd"] == 10.5
    # The window is in the payload so a client can say when the cap resets.
    assert body["window_start"].startswith(month_start().date().isoformat())
    assert "refused" in body["detail"]


@pytest.mark.anyio
async def test_an_unreadable_ledger_is_a_503_not_a_402():
    """Failing closed is still not the same answer as "you are over budget" --
    the user is not, and retrying is the right thing to do."""
    response = await _budget_unavailable(
        None,  # type: ignore[arg-type]
        BudgetCheckUnavailableError("ledger unreachable"),
    )
    assert response.status_code == 503
    assert json.loads(bytes(response.body))["error"] == "cost_cap_unavailable"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
