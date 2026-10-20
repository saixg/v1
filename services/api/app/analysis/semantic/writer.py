"""Pass 2b driver: own the `pass_run` lifecycle for the semantic pass and
degrade cleanly when no model provider is configured.

Unlike the architecture pass (which is pure graph arithmetic and rides inside
the scan's pass_run), this pass makes external model calls, can be stopped by
a budget, and can fail on its own -- so it gets its own `codeaxis.pass_runs`
row. That row is the visible state: a repo with a `failed` semantic pass_run
whose error reads `skipped: ...` has an honest explanation for why it has no
summaries, instead of silently having none.

`PassRunStatus` has no `skipped` member and this module does not invent one:
a skip is recorded as `failed` with an error string that begins `skipped:`.
"""

from __future__ import annotations

import logging

import psycopg

from app.knowledge import finish_pass_run, start_pass_run
from app.llm import (
    CallContext,
    ModelService,
    completion_model_service,
    missing_credential_reason,
)
from app.models import PassRun, PassRunStatus

from .conventions import write_conventions_pass
from .summarize import SummaryBudget, SummaryStats, write_summary_pass

logger = logging.getLogger(__name__)

ANALYZER = "semantic"
ANALYZER_VERSION = "0.1.0"


def model_provider_configured() -> bool:
    """Whether the deployment's selected model vendor has a credential.

    Checked up front rather than discovered one failed call at a time: with
    no key every call would fail identically, and the honest outcome is one
    recorded skip, not hundreds of error rows.

    Which vendor that is, and which variables count for it, belongs to
    `app.llm` -- asking about one vendor's variable here is what made this
    skip a Gemini-only deployment's entire semantic pass.
    """
    return missing_credential_reason() is None


def write_semantic_pass(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    *,
    service: ModelService | None = None,
    budget: SummaryBudget | None = None,
    job_id: str | None = None,
    project_id: str | None = None,
) -> SummaryStats | None:
    """Run summaries + conventions for one snapshot under their own pass_run.

    Returns the summary stats, or None when the pass was skipped. Never
    raises: this is a best-effort enrichment pass appended to a scan that has
    already succeeded, so every failure is recorded on the pass_run and
    logged instead of propagating. Conventions are attempted even when the
    model is unavailable, because the deterministic rules need no model.
    """
    run = start_pass_run(
        conn,
        PassRun(
            repo_id=repo_id,
            commit_sha=commit_sha,
            analyzer=ANALYZER,
            analyzer_version=ANALYZER_VERSION,
            status=PassRunStatus.RUNNING,
        ),
    )
    assert run.id is not None

    if service is None and not model_provider_configured():
        # No key: write the conventions the rules can establish without a
        # model, then record the skip visibly.
        error = f"skipped: {missing_credential_reason()}; summaries were not generated"
        try:
            rules = write_conventions_pass(conn, repo_id, commit_sha, service=None)
            logger.info(
                "semantic pass skipped for repo %s (no model key); wrote %d rule-based conventions",
                repo_id,
                len(rules),
            )
        except Exception as exc:  # noqa: BLE001 - recorded, never propagated
            logger.exception("conventions rules failed for repo %s", repo_id)
            error = f"{error}; conventions rules also failed: {exc}"
        finish_pass_run(conn, run.id, PassRunStatus.FAILED, error=error)
        return None

    if service is None:
        service = completion_model_service()

    context = CallContext(project_id=project_id, repo_id=repo_id, job_id=job_id)
    try:
        stats = write_summary_pass(
            conn, repo_id, commit_sha, service, budget=budget, context=context
        )
        write_conventions_pass(conn, repo_id, commit_sha, service, context=context)
    except Exception as exc:  # noqa: BLE001 - recorded, never propagated
        logger.exception("semantic pass failed for repo %s", repo_id)
        finish_pass_run(
            conn, run.id, PassRunStatus.FAILED, error=str(exc) or type(exc).__name__
        )
        return None

    if stats.aborted_reason is not None:
        # A real outcome, not a crash: the chunk table was empty, or the
        # model-call ceiling was reached. Recorded so the UI can say which.
        finish_pass_run(
            conn, run.id, PassRunStatus.FAILED, error=f"incomplete: {stats.aborted_reason}"
        )
        return stats

    finish_pass_run(conn, run.id, PassRunStatus.SUCCEEDED)
    return stats
