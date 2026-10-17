"""Entry-point detection: __main__ guard / FastAPI(/express( instantiation /
package.json main-or-bin all set attrs["entry_point"], and files without
any of those don't."""

from pathlib import Path

from app.analysis.scan import scan_repo
from app.analysis.scan.entrypoints import annotate_entry_points

REPO_ID = "11111111-1111-1111-1111-111111111111"
COMMIT = "deadbeef"


def _scan(root: Path):
    scanned, _stats, _trees = scan_repo(root, repo_id=REPO_ID, commit_sha=COMMIT)
    annotate_entry_points(scanned, root)
    return {sn.node.path: sn.node for sn in scanned if sn.node.kind == "file"}


def test_python_main_guard_detected(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "cli.py").write_text("def run():\n    pass\n\nif __name__ == '__main__':\n    run()\n")
    (root / "lib.py").write_text("def helper():\n    return 1\n")

    by_path = _scan(root)
    assert by_path["cli.py"].attrs.get("entry_point") is True
    assert by_path["cli.py"].attrs.get("entry_point_reason") == "python_main_guard"
    assert "entry_point" not in by_path["lib.py"].attrs


def test_fastapi_instantiation_detected(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "main.py").write_text("from fastapi import FastAPI\n\napp = FastAPI()\n")

    by_path = _scan(root)
    assert by_path["main.py"].attrs.get("entry_point") is True
    assert by_path["main.py"].attrs.get("entry_point_reason") == "fastapi_instantiation"


def test_express_instantiation_detected(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "server.js").write_text("const express = require('express');\nconst app = express();\n")

    by_path = _scan(root)
    assert by_path["server.js"].attrs.get("entry_point") is True
    assert by_path["server.js"].attrs.get("entry_point_reason") == "express_instantiation"


def test_package_json_main_field_detected(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "package.json").write_text('{"main": "./index.js"}')
    (root / "index.js").write_text("module.exports = {};\n")

    by_path = _scan(root)
    assert by_path["index.js"].attrs.get("entry_point") is True
    assert by_path["index.js"].attrs.get("entry_point_reason") == "package_json_main_or_bin"


def test_no_heuristic_matches_leaves_attrs_unset(tmp_path: Path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "util.py").write_text("def add(a, b):\n    return a + b\n")

    by_path = _scan(root)
    assert "entry_point" not in by_path["util.py"].attrs
