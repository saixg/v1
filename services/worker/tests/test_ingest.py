"""Local and git materialization. No DB, no network: the git tests fetch
from a repository created on local disk, with the `file` transport enabled
for the test only."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from codeaxis_worker import ingest
from codeaxis_worker.ingest import IngestError, materialize_github, materialize_local

pytestmark_git = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def test_local_requires_existing_directory(tmp_path):
    with pytest.raises(IngestError, match="does not exist"):
        materialize_local(str(tmp_path / "missing"), "abc")
    (tmp_path / "f.txt").write_text("x")
    with pytest.raises(IngestError, match="not a directory"):
        materialize_local(str(tmp_path / "f.txt"), "abc")
    with pytest.raises(IngestError, match="commit_sha"):
        materialize_local(str(tmp_path), " ")


def test_local_is_read_in_place_and_never_treated_as_scratch(tmp_path):
    checkout = materialize_local(str(tmp_path), "abc123")
    assert checkout.local_path == tmp_path.resolve()
    assert checkout.commit_sha == "abc123"
    assert checkout.remote_url is None
    assert not ingest.is_scratch_checkout(checkout.local_path)


@pytest.mark.parametrize(
    "url", ["file:///etc", "ext::sh -c touch% /tmp/pwned", "git@github.com:a/b.git", "-u"]
)
def test_github_rejects_non_https_urls(url):
    with pytest.raises(IngestError):
        materialize_github(url, None)


def test_github_rejects_option_like_ref():
    with pytest.raises(IngestError, match="invalid ref"):
        materialize_github("https://github.com/a/b.git", "--upload-pack=evil")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path, monkeypatch):
    """A two-commit repo on local disk, reachable over file://."""
    monkeypatch.setattr(ingest, "ALLOWED_GIT_SCHEMES", frozenset({"https", "file"}))
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(repo, "init", "--quiet", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.py").write_text("x = 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "--quiet", "-m", "one")
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("x = 2\n")
    _git(repo, "commit", "--quiet", "-am", "two")
    second = _git(repo, "rev-parse", "HEAD")
    # Allow fetching any reachable SHA directly, like GitHub does.
    _git(repo, "config", "uploadpack.allowReachableSHA1InWant", "true")
    return repo.resolve().as_uri(), first, second


@pytestmark_git
def test_github_default_ref_pins_head(origin):
    url, _, second = origin
    checkout = materialize_github(url, None)
    try:
        assert checkout.commit_sha == second
        assert checkout.remote_url == url
        assert (checkout.local_path / "a.py").read_text() == "x = 2\n"
        assert ingest.is_scratch_checkout(checkout.local_path)
    finally:
        shutil.rmtree(checkout.local_path, ignore_errors=True)


@pytestmark_git
def test_github_pins_older_sha(origin):
    url, first, _ = origin
    checkout = materialize_github(url, first)
    try:
        assert checkout.commit_sha == first
        assert (checkout.local_path / "a.py").read_text() == "x = 1\n"
    finally:
        shutil.rmtree(checkout.local_path, ignore_errors=True)


@pytestmark_git
def test_github_falls_back_to_full_clone(origin, monkeypatch):
    url, first, _ = origin
    monkeypatch.setattr(ingest, "_try_shallow_fetch", lambda *a: "simulated refusal")
    checkout = materialize_github(url, first)
    try:
        assert checkout.commit_sha == first
    finally:
        shutil.rmtree(checkout.local_path, ignore_errors=True)


@pytestmark_git
def test_github_unknown_ref_fails_cleanly(origin):
    url, _, _ = origin
    with pytest.raises(IngestError, match="could not fetch"):
        materialize_github(url, "no-such-branch")
