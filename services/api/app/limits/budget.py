"""Cost caps that refuse work (build-plan.md Part 10 Phase 6 "cost caps",
Part 2 Reliability "per-repo cost cap", Part 13 #6 "Free-tier limits ... drives
cost caps").

Before this module nothing in CodeAxis refused a model call. `llm_calls` and
`app/llm/pricing.py` record an estimated cost *after* the provider has been
paid, which is observability, not a limit. `ProjectBudgetGuard` is the
pre-call check: it reads the append-only ledger and raises rather than letting
the call proceed.

The four decisions, and why
---------------------------
**Window: the calendar month in UTC.** Part 13 #6 states the free tier in
terms of "blueprints/month", so the unit the product sells in is a month. A
calendar month also resets at a moment a user can be told ("your cap resets on
the 1st"), where a rolling 30-day window never resets visibly and makes
"$2.40 of $10 left" unexplainable — the number creeps back up as old calls age
out. The cost of a calendar month, stated plainly: someone can spend a full
cap on the 31st and another full cap on the 1st. Accepted; a rolling window
trades that for a worse explanation.

**Scope: per project.** `project_id` is the only attribution column on
`llm_calls` that is set for everything billable. `repo_id` is null for every
call that is not about one repo (requirement extraction on a task), so a
repo-scoped sum would silently miss real spend; and the ledger has no
`user_id` at all, because a worker pass runs detached from the request that
triggered it and genuinely does not know which end user caused the row.
**Named gap:** there is therefore no per-user cap, so one user who creates
twenty projects gets twenty caps. Closing it needs either a `user_id` column
threaded through every job, or a sum over the projects a user owns — a
different query and a different product call (it would throttle the owner's
collaborators). Not half-built here.

**Where the cap lives: an env default with a per-project override.**
`CODEAXIS_PROJECT_MONTHLY_COST_CAP_USD` is the deployment-wide free-tier
number; `codeaxis.projects.monthly_cost_cap_usd` overrides it for one project
and is NULL by default. The reasoning for a column over a settings table is in
20261022000000_cost_caps.sql. NULL means "use the default", never "unlimited".

**A multi-call pass that crosses the cap mid-way stops at the next call.**
The check is per call, not per pass, and nothing is reserved up front. Three
consequences, all deliberate:

1. *The cap is a soft ceiling with bounded overshoot, not a credit limit.*
   A call whose check passes is paid for in full, and concurrent calls can
   each pass the same check, so actual spend can exceed the cap by roughly
   the cost of the calls in flight. Making it hard would mean reserving an
   estimated cost before the call and reconciling after — a second
   write-ahead ledger whose rows mean something different from `llm_calls`.
   Not worth it for a per-project ceiling in the single-digit dollars.
2. *Work already persisted stays persisted.* `BudgetExceededError` propagates
   out of the pass like any other failure, so (for example) requirement
   extraction leaves the task's previous requirements alone exactly as it
   does for a provider failure. A pass does not roll back to pretend it never
   started, and it does not finish the remaining calls "because it already
   started" either.
3. *The refusal names the operation and the numbers*, so a user told their
   generation stopped half-way can see spend, cap and window rather than a
   bare failure.

`BudgetExceededError` is **not** a `ModelServiceError`. A cap refusal is not a
provider failure: the provider was never asked, retrying will not help, and
the fix is a different one (raise the cap, or wait for the window). Keeping it
outside that hierarchy is what stops the existing
`except ModelServiceError -> 502 model_call_failed` handlers in
`app/context_api.py` from mislabelling it. It is mapped to its own HTTP status
by the handler registered in `app/main.py`.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg

from app.knowledge.db import api_connection, retry_read
from app.knowledge.spend import SpendWindow, project_cost_cap_usd, project_spend_since

logger = logging.getLogger(__name__)

CAP_ENV_VAR = "CODEAXIS_PROJECT_MONTHLY_COST_CAP_USD"
DEFAULT_MONTHLY_CAP_USD = 10.0

# How long a ledger read is reused before another one is made. A cap check on
# every provider attempt would otherwise add a database round trip to every
# model call, including each item of a bulk pass. The staleness this
# introduces is bounded on both sides: by the TTL, and by `note_spend`, which
# adds the cost of calls this process has made since the read, so a tight loop
# inside one process cannot ride a stale total past the cap.
SPEND_CACHE_TTL_SECONDS = 15.0


class BudgetExceededError(Exception):
    """The project's model spend for the window is at or over its cap, so the
    call was refused. Deliberately not a `ModelServiceError` — see the module
    docstring."""

    def __init__(
        self,
        *,
        project_id: str,
        operation: str,
        spend_usd: float,
        cap_usd: float,
        window_start: datetime,
    ) -> None:
        self.project_id = project_id
        self.operation = operation
        self.spend_usd = spend_usd
        self.cap_usd = cap_usd
        self.window_start = window_start
        super().__init__(
            f"this project's model spend for the month beginning "
            f"{window_start.date().isoformat()} is ${spend_usd:.4f} of its ${cap_usd:.2f} "
            f"cap, so the {operation} call was refused before it was sent. "
            f"Nothing was charged for it."
        )


class BudgetCheckUnavailableError(Exception):
    """The ledger could not be read, so whether the cap is exceeded is
    unknown — and the call is refused.

    Failing closed is the choice: the alternative is that a database outage
    silently removes the only spend ceiling, which is the failure mode this
    module exists to prevent. Distinct from `BudgetExceededError` because the
    user is not over budget and should retry, and distinct from
    `ModelServiceError` because the provider was never asked.
    """


def default_cap_usd() -> float:
    """The deployment-wide cap, from the environment. A malformed value is a
    configuration error, not a reason to run uncapped."""
    raw = os.environ.get(CAP_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_MONTHLY_CAP_USD
    try:
        cap = float(raw)
    except ValueError:
        raise RuntimeError(f"{CAP_ENV_VAR} must be a number in USD, got {raw!r}") from None
    if cap < 0:
        raise RuntimeError(f"{CAP_ENV_VAR} must not be negative, got {cap}")
    return cap


def month_start(now: datetime | None = None) -> datetime:
    """The first instant of the current UTC calendar month."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    return moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class BudgetStatus:
    """A project's budget as the spend route reports it."""

    project_id: str
    window_start: datetime
    cap_usd: float
    cap_source: str  # "project" when the column is set, else "deployment_default"
    window: SpendWindow

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.window.spend_usd)

    @property
    def exceeded(self) -> bool:
        return self.window.spend_usd >= self.cap_usd


def read_budget_status(project_id: str, *, now: datetime | None = None) -> BudgetStatus:
    """One ledger read plus the cap, for the observability route and for the
    guard. Retried as a read (both statements are read-only and idempotent)."""
    start = month_start(now)

    def _read() -> tuple[SpendWindow, float | None]:
        with api_connection() as conn:
            return (
                project_spend_since(conn, project_id, start),
                project_cost_cap_usd(conn, project_id),
            )

    window, override = retry_read(_read)
    cap = default_cap_usd() if override is None else override
    return BudgetStatus(
        project_id=project_id,
        window_start=start,
        cap_usd=cap,
        cap_source="deployment_default" if override is None else "project",
        window=window,
    )


@dataclass
class _CachedSpend:
    read_at: float
    spend_usd: float
    cap_usd: float
    window_start: datetime


class ProjectBudgetGuard:
    """Reads the ledger and refuses the call when the project is at its cap.

    A call with no `project_id` is **not** checked and says so loudly in the
    docstring rather than in a log line nobody reads: the eval harness and any
    unattributed call have nothing to charge against, and inventing a project
    for them would attribute spend to the wrong budget. Every caller that
    touches a user's project does pass a `CallContext`.
    """

    def __init__(self, clock=None) -> None:
        from time import monotonic

        self._clock = clock or monotonic
        self._cache: dict[str, _CachedSpend] = {}
        self._lock = threading.Lock()

    def check(self, project_id: str | None, *, operation: str) -> None:
        if project_id is None:
            return
        entry = self._entry(project_id)
        if entry.spend_usd >= entry.cap_usd:
            raise BudgetExceededError(
                project_id=project_id,
                operation=operation,
                spend_usd=entry.spend_usd,
                cap_usd=entry.cap_usd,
                window_start=entry.window_start,
            )

    def note_spend(self, project_id: str | None, cost_usd: float | None) -> None:
        """Add a just-completed call's cost to the cached total.

        Without this, a bulk pass making dozens of calls inside one TTL would
        all see the same pre-pass total. An unpriced call (`cost_usd is None`)
        adds nothing, because the honest answer to "what did it cost" is "not
        known" — the ledger's `unpriced_calls` counter is where that shows up.
        """
        if project_id is None or cost_usd is None:
            return
        with self._lock:
            entry = self._cache.get(project_id)
            if entry is not None:
                entry.spend_usd += cost_usd

    def invalidate(self, project_id: str | None = None) -> None:
        with self._lock:
            if project_id is None:
                self._cache.clear()
            else:
                self._cache.pop(project_id, None)

    def _entry(self, project_id: str) -> _CachedSpend:
        now = self._clock()
        with self._lock:
            cached = self._cache.get(project_id)
            if cached is not None and now - cached.read_at < SPEND_CACHE_TTL_SECONDS:
                return cached
        try:
            status = read_budget_status(project_id)
        except (psycopg.Error, RuntimeError) as exc:
            logger.error("budget check failed for project %s: %s", project_id, exc)
            raise BudgetCheckUnavailableError(
                "the model spend ledger could not be read, so this call was refused "
                "rather than run without a cost cap"
            ) from exc
        entry = _CachedSpend(
            read_at=now,
            spend_usd=status.window.spend_usd,
            cap_usd=status.cap_usd,
            window_start=status.window_start,
        )
        with self._lock:
            self._cache[project_id] = entry
        return entry


_default_guard: ProjectBudgetGuard | None = None
_default_guard_lock = threading.Lock()


def default_budget_guard() -> ProjectBudgetGuard:
    """The process-wide guard, so its spend cache is shared by every
    ModelService built in this process."""
    global _default_guard
    if _default_guard is None:
        with _default_guard_lock:
            if _default_guard is None:
                _default_guard = ProjectBudgetGuard()
    return _default_guard
