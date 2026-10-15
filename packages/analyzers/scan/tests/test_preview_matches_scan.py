"""The exclusion preview must list exactly the files the real scan analyzes.
This is the direct enforcement of docs/11's "the exclusion preview matches
exactly what was analyzed" Ingestion acceptance criterion."""

from pathlib import Path

from codeaxis_scan import list_included_files, scan_repo


def _build_fixture_tree(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src" / "nested").mkdir(parents=True)
    (root / "node_modules" / "left-pad").mkdir(parents=True)
    (root / "vendor" / "secrets").mkdir(parents=True)

    (root / "src" / "main.py").write_text("def main():\n    return 1\n")
    (root / "src" / "nested" / "util.ts").write_text("export function util() {}\n")
    (root / "src" / "nested" / "app.js").write_text("function app() {}\n")
    (root / "node_modules" / "left-pad" / "index.js").write_text("module.exports = {};\n")
    (root / "vendor" / "secrets" / "creds.env").write_text("TOKEN=abc123\n")
    (root / "README.md").write_text("# fixture\n")

    return root


def test_preview_included_files_match_scan_output(tmp_path: Path):
    root = _build_fixture_tree(tmp_path)
    # "vendor/*" is a glob matched against the *relative path from root* of
    # each candidate directory, not an exact name — unlike EXCLUDED_DIRS'
    # name-only match for things like "node_modules" below.
    extra_excludes = frozenset({"vendor/*"})

    preview_entries = list_included_files(root, extra_excludes=extra_excludes)
    scanned_nodes, stats = scan_repo(
        root, repo_id="11111111-1111-1111-1111-111111111111", commit_sha="deadbeef",
        extra_excludes=extra_excludes,
    )

    assert stats.truncated is False

    preview_included_paths = {
        entry.path for entry in preview_entries if entry.kind == "file" and entry.included
    }
    scanned_file_paths = {
        node.node.path for node in scanned_nodes if node.node.kind == "file"
    }

    assert preview_included_paths == scanned_file_paths
    # Sanity: the exclusions actually took effect, so the assertion above
    # isn't trivially true because nothing was excluded.
    assert "node_modules/left-pad/index.js" not in preview_included_paths
    assert "vendor/secrets/creds.env" not in preview_included_paths
    assert "src/main.py" in preview_included_paths
    assert "src/nested/util.ts" in preview_included_paths


def test_preview_marks_excluded_dirs(tmp_path: Path):
    root = _build_fixture_tree(tmp_path)
    extra_excludes = frozenset({"vendor/*"})

    preview_entries = list_included_files(root, extra_excludes=extra_excludes)
    excluded_dirs = {
        entry.path
        for entry in preview_entries
        if entry.kind == "directory" and not entry.included
    }

    assert "node_modules" in excluded_dirs
    assert "vendor/secrets" in excluded_dirs
    for entry in preview_entries:
        if entry.path in excluded_dirs:
            assert entry.reason == "excluded_dir"
