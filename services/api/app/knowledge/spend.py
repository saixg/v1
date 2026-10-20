"""Reads over the `codeaxis.llm_calls` ledger: what a project has spent, and
what its cap is.

`llm_calls` is append-only by construction — the worker role is granted
`select, insert` and nothing else, and the table has no update or delete
policy (see 20261016120000_llm_calls.sql). That is what makes it sound as the
budget's source of truth: a row that recorded a paid provider attempt cannot
later be rewritten or removed to free up headroom, so summing it is the same
number tomorrow as it is now.

Read-only. The enforcement decision lives in `app/limits/budget.py`; this
module only answers questions, in keeping with app.knowledge being the only
module that knows SQL.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg


@dataclass(frozen=True)
class SpendWindow:
    """One project's model spend over a window, plus the honesty counters.

    `unpriced_calls` matters: `estimate_cost_usd` returns None for a model
    that is not in the price table, and those rows carry `cost_usd is null`.
    They are counted, never guessed at and never silently treated as zero, so
    a caller can say "spend is at least this much" rather than implying the
    total is complete.
    """

    spend_usd: float
    calls: int
    failed_calls: int
    unpriced_calls: int


def project_spend_since(
    conn: psycopg.Connection, project_id: str, since: datetime
) -> SpendWindow:
    """Spend attributed to `project_id` by rows created at or after `since`.

    Served by `llm_calls_project_id_created_at_idx`. Every provider attempt is
    counted, including the failed ones: a refusal, a truncation and a
    validation-failed retry were all paid for, so excluding them would
    under-report exactly in the situation where spend is running away.
    """
    row = conn.execute(
        """
        select
            coalesce(sum(cost_usd), 0)::float8                 as spend_usd,
            count(*)                                           as calls,
            count(*) filter (where status <> 'succeeded')       as failed_calls,
            count(*) filter (where cost_usd is null)            as unpriced_calls
        from codeaxis.llm_calls
        where project_id = %s and created_at >= %s
        """,
        (project_id, since),
    ).fetchone()
    assert row is not None
    return SpendWindow(
        spend_usd=row[0], calls=row[1], failed_calls=row[2], unpriced_calls=row[3]
    )


def project_cost_cap_usd(conn: psycopg.Connection, project_id: str) -> float | None:
    """The project's own cap override, or None to fall back to the deployment
    default. Raises nothing for an unknown project — it returns None, and the
    caller (which has already authorized the project) treats that as "use the
    default"."""
    row = conn.execute(
        """
        select monthly_cost_cap_usd::float8
        from codeaxis.projects
        where id = %s
        """,
        (project_id,),
    ).fetchone()
    return None if row is None else row[0]
