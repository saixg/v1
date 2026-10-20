"""Phase 6 failure mode: a malformed or hostile repository.

`tests/repository/test_zip_bomb.py` and `tests/analysis/scan/test_resource_caps.py`
already cover size/ratio/count bombs, zip-slip and the parse timeout. This file
holds what they leave out: symlinks (in an archive and in a checkout), a
corrupt archive, files that are not valid source, and degenerate trees. The
common promise is the one CLAUDE.md makes: a bad repository produces a gap
marker or a clean refusal, never an exception that fails the whole scan and
never a read outside the repository root. No database.
"""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

from app.analysis.scan import ScanLimits
from app.analysis.scan.manifests import extract_manifest_dependencies
from app.analysis.scan.scanner import ScannedNode, scan_repo
from app.repository import ingest
from app.repository.ingest import IngestError, materialize_zip

REPO_ID = "11111111-1111-1111-1111-111111111111"
OUTSIDE_TEXT = "TOP-SECRET-HOST-FILE-CONTENT"


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation needs elevated privilege on this platform")


def _scan(root: Path, **kwargs):
    return scan_repo(root, repo_id=REPO_ID, commit_sha="deadbeef", **kwargs)


def _files(nodes: list[ScannedNode]) -> dict[str, ScannedNode]:
    return {n.node.path: n for n in nodes if n.node.kind == "file" and n.node.path is not None}


# --------------------------------------------------------------------- archives


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    target = tmp_path / "extract"

    def fake_mkdtemp(prefix: str = "") -> str:
        target.mkdir()
        return str(target)

    monkeypatch.setattr(ingest.tempfile, "mkdtemp", fake_mkdtemp)
    return target


def test_zip_symlink_entry_is_written_as_a_plain_file_never_a_link(tmp_path):
    outside = tmp_path / "host_secret.txt"
    outside.write_text(OUTSIDE_TEXT)
    archive = tmp_path / "link.zip"
    info = zipfile.ZipInfo("leak.txt")
    info.create_system = 3  # unix: external_attr carries the mode bits
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(info, str(outside))  # what a symlink entry stores: its target path

    checkout = materialize_zip(archive)
    try:
        leaked = checkout.local_path / "leak.txt"
        assert not leaked.is_symlink()
        # The link target is just text in a regular file; the host file was not read.
        assert leaked.read_text() == str(outside)
        assert OUTSIDE_TEXT not in leaked.read_text()
    finally:
        import shutil

        shutil.rmtree(checkout.local_path, ignore_errors=True)


def test_zip_entry_count_cap_rejects_before_extracting(tmp_path, scratch, monkeypatch):
    monkeypatch.setattr(ingest, "ZIP_LIMITS", ScanLimits(max_file_count=5))
    archive = tmp_path / "many.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for i in range(20):
            zf.writestr(f"f{i}.txt", "x")

    with pytest.raises(IngestError, match="entries"):
        materialize_zip(archive)
    assert not scratch.exists()


def test_corrupt_archive_member_is_a_clean_refusal_and_leaves_nothing_behind(tmp_path, scratch):
    archive = tmp_path / "corrupt.zip"
    payload = b"print('hello world')\n" * 50
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("app.py", payload)
    raw = bytearray(archive.read_bytes())
    start = raw.index(payload[:16])
    raw[start + 4] ^= 0xFF  # flip a data byte; the stored CRC no longer matches
    archive.write_bytes(bytes(raw))

    with pytest.raises(IngestError, match="corrupt ZIP"):
        materialize_zip(archive)
    assert not scratch.exists()


def test_truncated_archive_is_refused_not_crashed(tmp_path):
    archive = tmp_path / "ok.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.py", "x = 1\n" * 1000)
    archive.write_bytes(archive.read_bytes()[:-30])  # lose the end-of-central-directory record

    with pytest.raises(IngestError):
        materialize_zip(archive)


# ------------------------------------------------------- symlinks in a checkout


def test_a_symlinked_file_cannot_pull_a_host_file_into_the_graph(tmp_path):
    """A git checkout preserves symlinks, so `notes.md -> /etc/passwd` in a
    hostile repo is a one-line arbitrary file read if the scanner follows it
    and stores the bytes for the UI and the narrator."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "host_secret.txt").write_text(OUTSIDE_TEXT)
    root = tmp_path / "repo"
    root.mkdir()
    (root / "real.py").write_text("def f():\n    return 1\n")
    _symlink(root / "notes.md", outside / "host_secret.txt")

    nodes, _stats, _trees = _scan(root)

    stored = [n.content for n in nodes if n.content is not None]
    assert all(OUTSIDE_TEXT.encode() not in c for c in stored)
    assert "real.py" in _files(nodes)


def test_a_symlinked_directory_is_not_walked_out_of_the_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "stolen.py").write_text(f"X = '{OUTSIDE_TEXT}'\n")
    root = tmp_path / "repo"
    root.mkdir()
    (root / "real.py").write_text("y = 2\n")
    _symlink(root / "vendor_link", outside, directory=True)

    nodes, _stats, _trees = _scan(root)

    paths = {n.node.path for n in nodes}
    assert not any("stolen.py" in (p or "") for p in paths)
    assert all(OUTSIDE_TEXT.encode() not in (n.content or b"") for n in nodes)


def test_a_symlink_loop_terminates(tmp_path):
    root = tmp_path / "repo"
    (root / "a").mkdir(parents=True)
    (root / "a" / "mod.py").write_text("z = 3\n")
    _symlink(root / "a" / "again", root, directory=True)

    nodes, stats, _trees = _scan(root, limits=ScanLimits(max_file_count=500))

    assert stats.truncated is False  # ended because the walk ended, not because a cap tripped
    assert "a/mod.py" in _files(nodes)


def test_a_symlinked_manifest_is_not_parsed(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "reqs.txt").write_text("host-only-package==1.0\n")
    root = tmp_path / "repo"
    root.mkdir()
    _symlink(root / "requirements.txt", outside / "reqs.txt")

    deps = extract_manifest_dependencies(root, REPO_ID, "deadbeef")

    assert all("host-only-package" not in d.name for d in deps)


# ---------------------------------------------------------- malformed source


def test_files_that_are_not_valid_source_become_nodes_not_exceptions(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    latin1 = "# caf\xe9 \xff\xfe\ndef ok():\n    return 1\n"
    (root / "latin1.py").write_bytes(latin1.encode("latin-1"))
    (root / "garbage.py").write_text("def (((:\n  class = = =\n}}}{{{\n")
    (root / "nul.py").write_bytes(b"def f():\n\x00\x00\x00 return 1\n")
    (root / "empty.py").write_text("")
    (root / "longline.js").write_text("var a = [" + "1," * 200_000 + "];\n")
    (root / "weird.ts").write_bytes(b"\xef\xbb\xbf\xff\xff\xff")

    nodes, _stats, _trees = _scan(root)

    files = _files(nodes)
    assert set(files) == {
        "latin1.py", "garbage.py", "nul.py", "empty.py", "longline.js", "weird.ts",
    }
    # NUL bytes mark a binary file: no text is carried forward for the UI.
    assert files["nul.py"].content is None


def test_a_seeded_credential_in_a_file_is_redacted_before_it_is_stored(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    key = "AKIA" + "IOSFODNN7EXAMPLE"  # AWS's published example id, split to stay unscannable
    (root / "settings.py").write_text(f'AWS_KEY = "{key}"\n')

    nodes, _stats, _trees = _scan(root)

    node = _files(nodes)["settings.py"]
    assert key.encode() not in (node.content or b"")
    assert node.node.attrs["secrets_redacted"] is True


def test_an_empty_repository_scans_to_just_its_root(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()

    nodes, stats, trees = _scan(root)

    assert [n.node.kind for n in nodes] == ["directory"]
    assert stats.truncated is False
    assert trees == {}


def test_a_deeply_nested_tree_does_not_exhaust_the_stack(tmp_path):
    root = tmp_path / "repo"
    deep = root
    for _ in range(40):
        deep = deep / "d"
    deep.mkdir(parents=True)
    (deep / "leaf.py").write_text("v = 1\n")

    nodes, _stats, _trees = _scan(root)

    assert any(n.node.name == "leaf.py" for n in nodes)


def test_unreadable_file_is_marked_unanalyzed_with_its_reason(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "locked.py").write_text("a = 1\n")
    (root / "fine.py").write_text("b = 2\n")
    real_read = Path.read_bytes

    def flaky(self: Path) -> bytes:
        if self.name == "locked.py":
            raise PermissionError("denied")
        return real_read(self)

    monkeypatch.setattr(Path, "read_bytes", flaky)

    nodes, _stats, _trees = _scan(root)

    files = _files(nodes)
    assert files["locked.py"].node.status == "unanalyzed"
    assert files["locked.py"].node.reason == "read_error:PermissionError"
    assert files["fine.py"].node.status == "verified"


def test_scan_never_follows_a_path_that_resolves_outside_the_root(tmp_path):
    """Belt and braces for the symlink cases above: whatever the walk yields,
    every file it reports must resolve inside the root."""
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("q = 1\n")
    outside = tmp_path / "out.py"
    outside.write_text("leak = 1\n")
    _symlink(root / "b.py", outside)

    nodes, _stats, _trees = _scan(root)

    resolved_root = root.resolve()
    for n in nodes:
        if n.node.kind == "file":
            assert (resolved_root / n.node.path).resolve().is_relative_to(resolved_root)

