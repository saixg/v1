from pathlib import Path

from tree_sitter_language_pack import get_parser

# `app.analysis.scan` is now a submodule of the single `codeaxis-api` app
# (build-plan.md Part 12), so the old "separate package, separate
# pyproject.toml dependency list" check no longer has a manifest to read.
# The invariant it protected — the scan pass itself does no DB/network I/O —
# is checked here at the source level instead.
FORBIDDEN = ("psycopg", "httpx", "requests", "supabase")


def test_tree_sitter_parses_python():
    tree = get_parser("python").parse(b"def f():\n    return 1\n")
    assert tree.root_node.type == "module"
    assert not tree.root_node.has_error


def test_tree_sitter_tolerates_broken_source():
    tree = get_parser("typescript").parse(b"function (((")
    assert tree.root_node.has_error  # error is data, not an exception


def test_scan_has_no_db_or_network_dependencies():
    scan_dir = Path(__file__).parents[3] / "app" / "analysis" / "scan"
    source = "\n".join(f.read_text() for f in scan_dir.glob("*.py"))
    assert not [f for f in FORBIDDEN if f in source]
