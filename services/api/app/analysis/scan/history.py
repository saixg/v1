"""Git history facts (build-plan.md Part 4.B): churn and recent commits per
scanned file, annotated onto the file node's `attrs`. Read-only `git log`
subprocess calls against the checkout -- never executes repository code.

Reuses `app.repository.ingest`'s hardened git-subprocess pattern
(`git_env()`/`run_git()`: `GIT_TERMINAL_PROMPT=0`, disabled hooks, a 120s
timeout that returns a non-zero CompletedProcess instead of raising)
rather than duplicating it. `app.analysis` already depends on
`app.repository.ingest` (`jobs.py` imports `MaterializedCheckout` etc. from
it) so this is an existing coupling, not a new one.

Run once per repo, not once per file: a single `git log --name-only` walk
over the whole history is grouped by path in Python, which is far cheaper
than N `git log -- path` subprocess calls. V1 simplification: churn is a
commit count per path (how many commits touched it), not a +/- line count
-- `--numstat` would give exact line churn but doubles the parsing surface
for a feature that's explicitly a "nice to have" counter, not load-bearing.

If the checkout has no `.git` directory (e.g. a ZIP upload), this returns
an empty history gracefully -- not an error, not a crash.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_RECENT_COMMITS_PER_FILE = 5
# \x1f/\x1e (unit/record separator) avoid collisions with '|' or newlines
# that could appear in a real commit subject line.
_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"


def collect_history(root: Path) -> dict[str, dict[str, Any]]:
    """Returns `{path: {"churn_commits": int, "recent_commits": [...]}}` for
    every path git's history ever touched, built from one `git log` walk.
    Returns `{}` if `root` has no `.git` directory or git is unavailable."""
    if not (root / ".git").exists():
        return {}

    # Imported lazily: app.repository.ingest imports app.analysis.scan
    # (for ScanLimits), so a module-level import here would be circular.
    from app.repository.ingest import run_git

    result = run_git(
        [
            "log",
            "--name-only",
            f"--format={_RECORD_SEP}%H{_FIELD_SEP}%ad{_FIELD_SEP}%s",
            "--date=short",
        ],
        root,
    )
    if result.returncode != 0:
        return {}

    churn_commits: dict[str, int] = {}
    recent_commits: dict[str, list[dict[str, str]]] = {}

    records = result.stdout.split(_RECORD_SEP)
    for record in records:
        record = record.strip("\n")
        if not record.strip():
            continue
        lines = record.split("\n")
        header = lines[0]
        parts = header.split(_FIELD_SEP)
        if len(parts) != 3:
            continue
        sha, date, subject = parts
        paths = [line for line in lines[1:] if line.strip()]
        for path in paths:
            churn_commits[path] = churn_commits.get(path, 0) + 1
            bucket = recent_commits.setdefault(path, [])
            if len(bucket) < _RECENT_COMMITS_PER_FILE:
                bucket.append({"sha": sha, "date": date, "subject": subject})

    return {
        path: {"churn_commits": churn_commits[path], "recent_commits": recent_commits[path]}
        for path in churn_commits
    }
