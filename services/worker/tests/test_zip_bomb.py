"""ZIP bomb and zip-slip protection in materialize_zip. No DB needed."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from codeaxis_scan import ScanLimits
from codeaxis_worker import ingest
from codeaxis_worker.ingest import IngestError, materialize_zip

MB = 1024 * 1024


def _dir_bytes(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """Route materialize_zip's mkdtemp to a known directory so the test can
    inspect what (if anything) was written before the abort."""
    target = tmp_path / "extract"

    def fake_mkdtemp(prefix: str = "") -> str:
        target.mkdir()
        return str(target)

    monkeypatch.setattr(ingest.tempfile, "mkdtemp", fake_mkdtemp)
    return target


def _zeros_zip(path: Path, name: str, size: int) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf, zf.open(name, "w") as fh:
        block = b"\0" * MB
        for _ in range(size // MB):
            fh.write(block)
    return path


def test_expansion_ratio_rejected_before_extracting(tmp_path, scratch):
    bomb = _zeros_zip(tmp_path / "bomb.zip", "zeros.bin", 64 * MB)
    ratio = 64 * MB / bomb.stat().st_size
    assert ratio > ingest.MAX_ZIP_EXPANSION_RATIO  # the fixture really is a bomb

    with pytest.raises(IngestError, match="expansion ratio"):
        materialize_zip(bomb)

    # Rejected on declared sizes alone: the extraction dir was never created.
    assert not scratch.exists()


def test_total_size_cap_rejected_before_extracting(tmp_path, scratch, monkeypatch):
    monkeypatch.setattr(ingest, "ZIP_LIMITS", ScanLimits(max_total_size_bytes=2 * MB))
    monkeypatch.setattr(ingest, "MAX_ZIP_EXPANSION_RATIO", 1e9)
    archive = _zeros_zip(tmp_path / "big.zip", "zeros.bin", 4 * MB)

    with pytest.raises(IngestError, match="expands to"):
        materialize_zip(archive)
    assert not scratch.exists()


def test_streaming_copy_aborts_mid_entry(tmp_path):
    """The byte-counted copy is the backstop for archives whose headers lie
    about their size: it must stop writing as soon as the cap is passed, not
    after the entry has been fully inflated."""
    archive = _zeros_zip(tmp_path / "big.zip", "zeros.bin", 16 * MB)
    dest = tmp_path / "out"
    dest.mkdir()
    cap = 1 * MB

    with zipfile.ZipFile(archive) as zf, pytest.raises(IngestError, match="aborted"):
        ingest._extract_stream(
            zf, dest, compressed_size=archive.stat().st_size, max_total_bytes=cap
        )

    written = _dir_bytes(dest)
    assert written <= cap  # the chunk that crossed the cap was never written
    assert written < 16 * MB


def test_failed_extraction_removes_partial_output(tmp_path, scratch, monkeypatch):
    # Pass the declared-size checks but trip the streaming cap.
    archive = _zeros_zip(tmp_path / "big.zip", "zeros.bin", 4 * MB)
    monkeypatch.setattr(ingest, "MAX_ZIP_EXPANSION_RATIO", 1e9)
    real_extract = ingest._extract_stream

    def capped(zf, dest_root, compressed_size, max_total_bytes=None):
        return real_extract(zf, dest_root, compressed_size, max_total_bytes=MB)

    monkeypatch.setattr(ingest, "_extract_stream", capped)

    with pytest.raises(IngestError, match="aborted"):
        materialize_zip(archive)
    assert not scratch.exists()


@pytest.mark.parametrize(
    "evil", ["../../evil.txt", "/abs/evil.txt", "a/../../evil.txt", "C:/evil.txt"]
)
def test_zip_slip_rejected(tmp_path, scratch, evil):
    archive = tmp_path / "slip.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("ok.txt", "fine")
        zf.writestr(zipfile.ZipInfo(evil), "pwned")

    with pytest.raises(IngestError, match="escapes the extraction directory"):
        materialize_zip(archive)

    assert not any(p.name == "evil.txt" for p in tmp_path.rglob("*"))
    assert not scratch.exists()


def test_valid_zip_extracts_with_stable_commit_sha(tmp_path):
    archive = tmp_path / "ok.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("src/app.py", "def main():\n    pass\n")
        zf.writestr("src/", "")

    first = materialize_zip(archive)
    second = materialize_zip(archive)
    try:
        assert (first.local_path / "src" / "app.py").read_text() == "def main():\n    pass\n"
        assert first.commit_sha == second.commit_sha
        assert first.commit_sha.startswith("zip:")
        assert first.remote_url is None
        assert ingest.is_scratch_checkout(first.local_path)
    finally:
        import shutil

        shutil.rmtree(first.local_path, ignore_errors=True)
        shutil.rmtree(second.local_path, ignore_errors=True)


def test_not_a_zip(tmp_path):
    bogus = tmp_path / "x.zip"
    bogus.write_bytes(b"definitely not a zip")
    with pytest.raises(IngestError, match="not a valid ZIP"):
        materialize_zip(bogus)
