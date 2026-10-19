"""Unit tests for `materialize_local`'s operator allowlist (no DB needed):
local-path ingestion is disabled by default and, once enabled, is confined
to the configured roots — covering the arbitrary-host-filesystem-read gap
a security audit found in this function."""

from __future__ import annotations

import os

import pytest

from app.repository.ingest import IngestError, materialize_local


def test_materialize_local_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCAL_INGEST_ALLOWED_ROOTS", raising=False)
    with pytest.raises(IngestError, match="disabled"):
        materialize_local(str(tmp_path), "sha")


def test_materialize_local_rejects_path_outside_allowed_roots(
    tmp_path, tmp_path_factory, monkeypatch
):
    other_root = tmp_path_factory.mktemp("not-allowed-root")
    monkeypatch.setenv("LOCAL_INGEST_ALLOWED_ROOTS", str(other_root))
    with pytest.raises(IngestError, match="outside the allowed roots"):
        materialize_local(str(tmp_path), "sha")


def test_materialize_local_allows_path_inside_allowed_root(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_INGEST_ALLOWED_ROOTS", str(tmp_path.parent))
    checkout = materialize_local(str(tmp_path), "sha")
    assert checkout.local_path == tmp_path.resolve()
    assert checkout.commit_sha == "sha"


def test_materialize_local_allows_one_of_several_roots(tmp_path, tmp_path_factory, monkeypatch):
    other_root = tmp_path_factory.mktemp("another-allowed-root")
    monkeypatch.setenv(
        "LOCAL_INGEST_ALLOWED_ROOTS", f"{other_root};{tmp_path.parent}"
    )
    checkout = materialize_local(str(tmp_path), "sha")
    assert checkout.local_path == tmp_path.resolve()


def test_materialize_local_requires_commit_sha(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_INGEST_ALLOWED_ROOTS", str(tmp_path.parent))
    with pytest.raises(IngestError, match="commit_sha"):
        materialize_local(str(tmp_path), "")


def test_materialize_local_rejects_missing_path(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_INGEST_ALLOWED_ROOTS", str(tmp_path.parent))
    with pytest.raises(IngestError, match="does not exist"):
        materialize_local(str(tmp_path / "nope"), "sha")


def test_materialize_local_rejects_file_not_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_INGEST_ALLOWED_ROOTS", str(tmp_path.parent))
    file_path = tmp_path / "a_file.txt"
    file_path.write_text("hi")
    with pytest.raises(IngestError, match="not a directory"):
        materialize_local(str(file_path), "sha")


@pytest.mark.skipif(
    os.name == "nt", reason="symlink creation needs elevated privilege on Windows"
)
def test_materialize_local_rejects_symlink_escaping_allowed_root(
    tmp_path, tmp_path_factory, monkeypatch
):
    allowed_root = tmp_path_factory.mktemp("allowed-root")
    outside = tmp_path_factory.mktemp("outside-root")
    escape_link = allowed_root / "escape"
    escape_link.symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("LOCAL_INGEST_ALLOWED_ROOTS", str(allowed_root))

    with pytest.raises(IngestError, match="outside the allowed roots"):
        materialize_local(str(escape_link), "sha")
