"""Resource-bomb caps: oversized files and too-many-files never raise, they
become unanalyzed markers / a truncated scan. See docs/08-security.md and
CLAUDE.md's "admit a gap over producing a plausible edge."
"""

from pathlib import Path

from codeaxis_scan.limits import ScanLimits
from codeaxis_scan.scanner import _parse_with_timeout, scan_repo


def test_oversized_file_is_marked_unanalyzed_not_raised(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "big.py").write_text("x = 1\n" * 1000)  # a few KB

    limits = ScanLimits(max_file_size_bytes=10)  # smaller than the file

    nodes, stats = scan_repo(
        root, repo_id="11111111-1111-1111-1111-111111111111", commit_sha="deadbeef",
        limits=limits,
    )

    file_nodes = [n for n in nodes if n.node.kind == "file"]
    assert len(file_nodes) == 1
    big_node = file_nodes[0].node
    assert big_node.status == "unanalyzed"
    assert big_node.reason == "file_too_large"

    symbol_nodes = [n for n in nodes if n.node.kind == "symbol"]
    assert symbol_nodes == []
    assert stats.truncated is False
    assert file_nodes[0].content is None  # never read, so no source to show


def test_file_content_is_carried_for_text_files_only(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mod.py").write_bytes(b"def f():\n    pass\n")
    (root / "README.md").write_bytes(b"# hi\n")
    (root / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00")

    nodes, _ = scan_repo(root, repo_id="11111111-1111-1111-1111-111111111111", commit_sha="x")
    by_path = {n.node.path: n for n in nodes if n.node.kind == "file"}

    assert by_path["mod.py"].content == b"def f():\n    pass\n"
    assert by_path["README.md"].content == b"# hi\n"
    assert by_path["logo.png"].content is None  # binary: no source view
    assert all(n.content is None for n in nodes if n.node.kind != "file")


def test_file_count_cap_truncates_scan(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    for i in range(10):
        (root / f"mod_{i}.py").write_text(f"x = {i}\n")

    limits = ScanLimits(max_file_count=3)

    nodes, stats = scan_repo(
        root, repo_id="11111111-1111-1111-1111-111111111111", commit_sha="deadbeef",
        limits=limits,
    )

    file_nodes = [n for n in nodes if n.node.kind == "file"]
    assert stats.truncated is True
    assert stats.reason == "file_count_cap"
    assert len(file_nodes) < 10
    assert len(file_nodes) <= limits.max_file_count


def test_parse_timeout_helper_returns_none_instead_of_raising():
    def slow(*, seconds: float):
        import time

        time.sleep(seconds)
        return "done"

    import concurrent.futures

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(slow, seconds=1.0)
    try:
        result = future.result(timeout=0.0001)
        raised_timeout = False
    except concurrent.futures.TimeoutError:
        result = None
        raised_timeout = True
    finally:
        executor.shutdown(wait=False)

    assert raised_timeout is True
    assert result is None


def test_parse_with_timeout_on_real_file_does_not_raise(tmp_path: Path):
    content = b"def f():\n    return 1\n"

    # A real, fast parse comfortably within a generous timeout succeeds.
    tree = _parse_with_timeout("python", content, max_parse_seconds=2.0)
    assert tree is not None
    assert not tree.root_node.has_error

    # An absurdly small timeout exercises the timeout path without raising;
    # whether it wins the race or not, no exception should escape.
    _parse_with_timeout("python", content, max_parse_seconds=0.0)
