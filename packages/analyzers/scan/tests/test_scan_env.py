import tomllib
from pathlib import Path

from tree_sitter_language_pack import get_parser

FORBIDDEN = ("psycopg", "codeaxis-graph", "httpx", "requests", "supabase")


def test_tree_sitter_parses_python():
    tree = get_parser("python").parse(b"def f():\n    return 1\n")
    assert tree.root_node.type == "module"
    assert not tree.root_node.has_error


def test_tree_sitter_tolerates_broken_source():
    tree = get_parser("typescript").parse(b"function (((")
    assert tree.root_node.has_error  # error is data, not an exception


def test_scan_has_no_db_or_network_dependencies():
    manifest = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    deps = " ".join(manifest["project"]["dependencies"])
    assert not [f for f in FORBIDDEN if f in deps]
