"""Entry-point detection (build-plan.md Part 4.B): heuristics applied to
already-scanned file nodes, annotating `attrs` in place -- no new nodes.
These are heuristics, not certainties, so they never touch a node's
`status` (that's the analyzer's verification confidence of the scan itself,
already set by `scanner.py`); they only add `attrs["entry_point"]` and
`attrs["entry_point_reason"]`. No DB, no network, no exec.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .scanner import ScannedNode

_PY_MAIN_GUARD_RE = re.compile(r'(?m)^if\s+__name__\s*==\s*[\'"]__main__[\'"]\s*:')
_FASTAPI_RE = re.compile(r"\bFastAPI\s*\(")
_EXPRESS_RE = re.compile(r"\bexpress\s*\(\s*\)")

_JS_LANGUAGES = {"javascript", "typescript", "tsx"}


def annotate_entry_points(scanned: list[ScannedNode], root: Path) -> None:
    """Mutates each matching `file` ScannedNode's `node.attrs` in place:
    `entry_point=True` and `entry_point_reason=<heuristic name>` (or a list,
    if more than one heuristic fired)."""
    main_bin_paths = _package_json_entry_paths(root)

    for sn in scanned:
        node = sn.node
        if node.kind != "file" or node.path is None:
            continue
        reasons: list[str] = []

        if node.path in main_bin_paths:
            reasons.append("package_json_main_or_bin")

        content = sn.content
        if content is not None:
            text = content.decode("utf-8", "replace")
            language = node.attrs.get("language")
            if language == "python":
                if _PY_MAIN_GUARD_RE.search(text):
                    reasons.append("python_main_guard")
                if _FASTAPI_RE.search(text):
                    reasons.append("fastapi_instantiation")
            elif language in _JS_LANGUAGES:
                if _EXPRESS_RE.search(text):
                    reasons.append("express_instantiation")

        if reasons:
            node.attrs["entry_point"] = True
            node.attrs["entry_point_reason"] = reasons[0] if len(reasons) == 1 else reasons


def _package_json_entry_paths(root: Path) -> set[str]:
    path = root / "package.json"
    if path.is_symlink() or not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return set()
    if not isinstance(data, dict):
        return set()

    out: set[str] = set()

    def add(value: object) -> None:
        if isinstance(value, str):
            out.add(value.removeprefix("./"))

    add(data.get("main"))
    bin_field = data.get("bin")
    if isinstance(bin_field, str):
        add(bin_field)
    elif isinstance(bin_field, dict):
        for value in bin_field.values():
            add(value)

    return out
