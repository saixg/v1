"""Git history extraction: churn + recent commits from a real throwaway git
repo built in a temp dir, and graceful no-op when there's no .git at all."""

import subprocess
from pathlib import Path

import pytest

from app.analysis.scan.history import collect_history

pytestmark_git = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git not installed",
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytestmark_git
def test_churn_and_recent_commits(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")

    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "first commit")

    (repo / "a.py").write_text("x = 2\n")
    (repo / "b.py").write_text("y = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "second commit")

    history = collect_history(repo)

    assert history["a.py"]["churn_commits"] == 2
    assert history["b.py"]["churn_commits"] == 1

    recent = history["a.py"]["recent_commits"]
    assert len(recent) == 2
    assert recent[0]["subject"] == "second commit"  # newest first
    assert recent[1]["subject"] == "first commit"
    assert all({"sha", "date", "subject"} <= set(c) for c in recent)


@pytestmark_git
def test_no_git_directory_returns_empty(tmp_path: Path):
    plain = tmp_path / "no_git"
    plain.mkdir()
    (plain / "a.py").write_text("x = 1\n")

    assert collect_history(plain) == {}
