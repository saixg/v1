"""Cost caps and rate limits (build-plan.md Part 10 Phase 6).

Two different jobs with two different scopes, and the difference matters:

- `budget` caps **money**, per project, per calendar month, by reading the
  append-only `codeaxis.llm_calls` ledger. Because that ledger is shared, the
  cap is genuinely global across machines.
- `ratelimit` caps **request rate**, per identity, in this process's memory.
  It is per-machine, not global; the module says so at length and marks it.
"""

from app.limits.budget import (
    CAP_ENV_VAR,
    DEFAULT_MONTHLY_CAP_USD,
    BudgetCheckUnavailableError,
    BudgetExceededError,
    BudgetStatus,
    ProjectBudgetGuard,
    default_budget_guard,
    default_cap_usd,
    month_start,
    read_budget_status,
)
from app.limits.ratelimit import (
    DEFAULT_BUCKETS,
    FixedWindowLimiter,
    RateLimit,
    RateLimitDecision,
    bucket_for,
    enforce_mcp_rate_limit,
    enforce_rate_limit,
    limit_for,
    limiter,
    reset_limits,
    token_bucket_key,
)

__all__ = [
    "BudgetCheckUnavailableError",
    "BudgetExceededError",
    "BudgetStatus",
    "CAP_ENV_VAR",
    "DEFAULT_BUCKETS",
    "DEFAULT_MONTHLY_CAP_USD",
    "FixedWindowLimiter",
    "ProjectBudgetGuard",
    "RateLimit",
    "RateLimitDecision",
    "bucket_for",
    "default_budget_guard",
    "default_cap_usd",
    "enforce_mcp_rate_limit",
    "enforce_rate_limit",
    "limit_for",
    "limiter",
    "month_start",
    "read_budget_status",
    "reset_limits",
    "token_bucket_key",
]
