"""Relationship extraction: IMPORTS/CALLS/EXTENDS/IMPLEMENTS/TESTS edges
with the right resolution_status, over small fixture trees. See
app/analysis/scan/relationships.py for the resolution discipline."""

from pathlib import Path

from app.analysis.scan import scan_repo
from app.analysis.scan.relationships import extract_relationships

REPO_ID = "11111111-1111-1111-1111-111111111111"
COMMIT = "deadbeef"


def _scan_and_extract(root: Path):
    scanned, _stats, trees = scan_repo(root, repo_id=REPO_ID, commit_sha=COMMIT)
    id_by_key = {sn.key: i for i, sn in enumerate(scanned, start=1)}
    edges = extract_relationships(scanned, trees, id_by_key, REPO_ID, COMMIT)
    return scanned, id_by_key, edges


def _by_kind(edges, kind):
    return [e for e in edges if e.kind == kind]


def test_python_imports_resolve_local_external_and_dynamic(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "foo.py").write_text("def helper():\n    return 1\n")
    (root / "pkg" / "bar.py").write_text(
        "from .foo import helper\n"
        "import requests\n"
        "import importlib\n"
        "importlib.import_module(some_expr)\n"
    )

    _scanned, _id_by_key, edges = _scan_and_extract(root)
    imports = _by_kind(edges, "imports")
    by_raw = {e.attrs.get("raw"): e for e in imports}

    assert by_raw[".foo"].resolution_status == "resolved"
    assert by_raw[".foo"].dst_id is not None

    assert by_raw["requests"].resolution_status == "external"
    assert by_raw["requests"].dst_id is None

    dynamic = [e for e in imports if e.reason == "dynamic_import_module"]
    assert len(dynamic) == 1
    assert dynamic[0].resolution_status == "unresolved"
    assert dynamic[0].dst_id is None


def test_calls_same_file_resolved_cross_file_ambiguous(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "foo.py").write_text(
        "def helper():\n    return 1\n\ndef local_caller():\n    return helper()\n"
    )
    (root / "pkg" / "bar.py").write_text("def other_caller():\n    return helper()\n")

    _scanned, id_by_key, edges = _scan_and_extract(root)
    calls = _by_kind(edges, "calls")
    foo_id = id_by_key["pkg/foo.py"]
    bar_id = id_by_key["pkg/bar.py"]

    # local_caller() in foo.py calling helper() defined in the same file.
    foo_call = next(e for e in calls if e.src_id == foo_id)
    assert foo_call.attrs.get("callee_name") == "helper"
    assert foo_call.resolution_status == "resolved"
    assert foo_call.dst_id is not None

    # other_caller() in bar.py calling the same-named helper() in foo.py.
    bar_call = next(e for e in calls if e.src_id == bar_id)
    assert bar_call.attrs.get("callee_name") == "helper"
    assert bar_call.resolution_status == "ambiguous"
    assert bar_call.dst_id is not None


def test_calls_no_match_is_unresolved(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "mod.py").write_text("def f():\n    return undefined_fn()\n")

    _scanned, _id_by_key, edges = _scan_and_extract(root)
    calls = _by_kind(edges, "calls")
    call = next(e for e in calls if e.attrs.get("callee_name") == "undefined_fn")
    assert call.resolution_status == "unresolved"
    assert call.dst_id is None


def test_python_extends_resolution(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "base.py").write_text("class Base:\n    pass\n")
    (root / "derived.py").write_text("from base import Base\n\nclass Derived(Base):\n    pass\n")

    _scanned, _id_by_key, edges = _scan_and_extract(root)
    extends = _by_kind(edges, "extends")
    assert len(extends) == 1
    assert extends[0].resolution_status == "ambiguous"  # cross-file name match only
    assert extends[0].attrs["base_name"] == "Base"


def test_ts_extends_and_implements(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "base.ts").write_text("export class Base {}\nexport interface Something {}\n")
    (root / "derived.ts").write_text(
        "class Derived extends Base implements Something {\n  run() {}\n}\n"
    )

    _scanned, _id_by_key, edges = _scan_and_extract(root)
    extends = _by_kind(edges, "extends")
    implements = _by_kind(edges, "implements")
    assert any(e.attrs.get("base_name") == "Base" for e in extends)
    assert any(e.attrs.get("base_name") == "Something" for e in implements)


def test_tests_edge_records_evidence(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "foo.py").write_text("def do_thing():\n    return 1\n")
    (root / "test_foo.py").write_text(
        "from foo import do_thing\n\ndef test_it():\n    do_thing()\n"
    )

    _scanned, _id_by_key, edges = _scan_and_extract(root)
    tests = _by_kind(edges, "tests")
    assert len(tests) == 1
    edge = tests[0]
    assert edge.resolution_status == "resolved"  # naming + import agree
    assert "naming:test_foo.py~foo.py" in edge.attrs["evidence"]
    assert "import" in edge.attrs["evidence"]


def test_tests_edge_naming_only_is_ambiguous(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "foo.py").write_text("def do_thing():\n    return 1\n")
    # Naming convention matches, but the test file never imports foo.py.
    (root / "test_foo.py").write_text("def test_it():\n    assert True\n")

    _scanned, _id_by_key, edges = _scan_and_extract(root)
    tests = _by_kind(edges, "tests")
    assert len(tests) == 1
    assert tests[0].resolution_status == "ambiguous"
