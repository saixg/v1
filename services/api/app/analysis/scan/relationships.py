"""Pass 1b: relationship extraction over the parsed tree-sitter trees
`scan_repo` already produced. Source in (trees + the symbol/file index from
the same scan), edges with provenance out. No DB, no network, no exec.

Design note (build-plan.md Part 4.B): `scanner.py`'s `_scan_symbols` already
parses every full-tier file once to pull out function/class/interface
nodes; rather than re-parsing here, `scan_repo` now hands back
`trees_by_file` (the parsed tree per file) alongside the scanned nodes, and
this module walks those trees directly. That is a small, additive change to
`scan_repo`'s return shape (one more dict) rather than a second parse pass.

Resolution discipline (CLAUDE.md: "prefer admitting a gap over producing a
plausible edge"):
  - IMPORTS: a target that resolves to a real file in this scan is
    "resolved"; a bare/third-party specifier that isn't a local file is
    "external" (expected, not a gap); a dynamic specifier (non-literal
    `require(expr)` / `importlib.import_module(expr)`) or an unenumerable
    `from . import *` / multi-name relative import is "unresolved".
  - CALLS / EXTENDS / IMPLEMENTS are name-based only (the V1 cut
    build-plan.md calls out explicitly as "name-based call edges only"):
    a same-file match is "resolved"; a unique same-named match elsewhere in
    the repo is "ambiguous" (name collisions are never certain); no match or
    multiple same-named candidates is "unresolved".
  - TESTS is evidence-based (file-naming convention + an import that
    confirms it): both agreeing is "resolved", naming alone is "ambiguous".

Every edge's `status` (Provenance) is "verified" -- the analyzer is certain
it found a real import/call/inheritance/test-naming construct in the
source; `resolution_status` is what carries the uncertainty about *what it
points to*. Only python/javascript/typescript/tsx are handled, matching
`scanner.FULL_TIER_LANGUAGES`.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from tree_sitter import Node as TSNode
from tree_sitter import Tree as TSTree

from app.models import Edge, ResolutionStatus

from .scanner import ANALYZER, ANALYZER_VERSION, ScannedNode

JS_LIKE = {"javascript", "typescript", "tsx"}
JS_EXTENSIONS = ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
JS_INDEX_SUFFIXES = ("/index.ts", "/index.tsx", "/index.js", "/index.jsx")


def _iter_nodes(node: TSNode):
    yield node
    for child in node.children:
        yield from _iter_nodes(child)


def _text(node: TSNode | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", "replace")


@dataclass
class _ImportTarget:
    line: int
    # The resolved file key, or None when unresolved/external.
    target_key: str | None
    resolution_status: ResolutionStatus
    reason: str | None = None
    raw: str = ""


def extract_relationships(
    scanned: list[ScannedNode],
    trees_by_file: dict[str, TSTree],
    id_by_key: dict[str, int],
    repo_id: str,
    commit_sha: str,
) -> list[Edge]:
    """Build `imports`/`calls`/`extends`/`implements`/`tests` edges. `scanned`
    is the full node list `scan_repo` produced (used to build the symbol and
    file-path indexes); `trees_by_file` is the tree per full-tier file it
    also returned; `id_by_key` maps each `ScannedNode.key` to the database id
    `insert_nodes` assigned it, the same map `jobs.py` already builds for the
    `contains` edges."""
    file_nodes = [sn for sn in scanned if sn.node.kind == "file"]
    file_keys = {sn.key for sn in file_nodes}
    lang_by_file = {sn.key: sn.node.attrs.get("language") for sn in file_nodes}

    symbols_by_name: dict[str, list[ScannedNode]] = defaultdict(list)
    for sn in scanned:
        if sn.node.kind == "symbol":
            symbols_by_name[sn.node.name].append(sn)

    edges: list[Edge] = []
    # Which files each file resolved-imports, used as TESTS evidence below.
    resolved_imports_by_file: dict[str, set[str]] = defaultdict(set)

    for file_key, tree in trees_by_file.items():
        if file_key not in id_by_key:
            continue
        language = lang_by_file.get(file_key)
        if language == "python":
            targets = _python_import_targets(tree, file_key, file_keys)
            edges.extend(_import_edges(file_key, targets, id_by_key, repo_id, commit_sha))
            edges.extend(
                _calls_edges(
                    file_key, tree, "python", symbols_by_name, id_by_key, repo_id, commit_sha
                )
            )
            edges.extend(
                _python_extends_edges(
                    file_key, tree, symbols_by_name, id_by_key, repo_id, commit_sha
                )
            )
        elif language in JS_LIKE:
            targets = _js_import_targets(tree, file_key, file_keys)
            edges.extend(_import_edges(file_key, targets, id_by_key, repo_id, commit_sha))
            edges.extend(
                _calls_edges(
                    file_key, tree, language, symbols_by_name, id_by_key, repo_id, commit_sha
                )
            )
            edges.extend(
                _js_extends_implements_edges(
                    file_key, tree, symbols_by_name, id_by_key, repo_id, commit_sha
                )
            )
        else:
            continue
        for target in targets:
            if target.resolution_status == "resolved" and target.target_key:
                resolved_imports_by_file[file_key].add(target.target_key)

    edges.extend(
        _tests_edges(file_nodes, resolved_imports_by_file, id_by_key, repo_id, commit_sha)
    )
    return edges


# --------------------------------------------------------------- imports


def _dir_parts(file_key: str) -> list[str]:
    parts = file_key.split("/")
    return parts[:-1]


def _resolve_python_module(
    dir_parts: list[str], module_parts: list[str], file_keys: set[str]
) -> str | None:
    base = [p for p in (*dir_parts, *module_parts) if p]
    if not base:
        return None
    candidate = "/".join(base) + ".py"
    if candidate in file_keys:
        return candidate
    candidate_pkg = "/".join(base) + "/__init__.py"
    if candidate_pkg in file_keys:
        return candidate_pkg
    return None


def _python_import_targets(tree: TSTree, file_key: str, file_keys: set[str]) -> list[_ImportTarget]:
    out: list[_ImportTarget] = []
    dir_parts = _dir_parts(file_key)

    for node in _iter_nodes(tree.root_node):
        if node.type == "import_statement":
            for child in node.children:
                dotted = None
                if child.type == "dotted_name":
                    dotted = child
                elif child.type == "aliased_import":
                    name_field = child.child_by_field_name("name")
                    if name_field is not None and name_field.type == "dotted_name":
                        dotted = name_field
                if dotted is None:
                    continue
                raw = _text(dotted)
                module_parts = raw.split(".")
                target = _resolve_python_module([], module_parts, file_keys)
                out.append(
                    _ImportTarget(
                        line=node.start_point[0] + 1,
                        target_key=target,
                        resolution_status="resolved" if target else "external",
                        raw=raw,
                    )
                )

        elif node.type == "import_from_statement":
            module_field = node.child_by_field_name("module_name")
            name_field = node.child_by_field_name("name")
            if module_field is None:
                continue
            raw = _text(module_field)
            if module_field.type == "relative_import":
                prefix = next(
                    (c for c in module_field.children if c.type == "import_prefix"), None
                )
                dots = _text(prefix).count(".") if prefix is not None else 1
                submodule = next(
                    (c for c in module_field.children if c.type == "dotted_name"), None
                )
                rel_dir = dir_parts[: len(dir_parts) - (dots - 1)] if dots >= 1 else dir_parts
                if submodule is not None:
                    module_parts = _text(submodule).split(".")
                    target = _resolve_python_module(rel_dir, module_parts, file_keys)
                    out.append(
                        _ImportTarget(
                            line=node.start_point[0] + 1,
                            target_key=target,
                            resolution_status="resolved" if target else "unresolved",
                            reason=None if target else "relative_import_target_not_found",
                            raw=raw,
                        )
                    )
                else:
                    # "from . import X" / "from .. import X": X (the `name`
                    # field) is itself the submodule, but only when it is a
                    # single plain or aliased name -- a wildcard or a
                    # multi-name list can't be enumerated here.
                    single = None
                    if name_field is not None:
                        if name_field.type == "dotted_name":
                            single = name_field
                        elif name_field.type == "aliased_import":
                            single = name_field.child_by_field_name("name")
                    if single is not None and single.type == "dotted_name":
                        module_parts = _text(single).split(".")
                        target = _resolve_python_module(rel_dir, module_parts, file_keys)
                        out.append(
                            _ImportTarget(
                                line=node.start_point[0] + 1,
                                target_key=target,
                                resolution_status="resolved" if target else "unresolved",
                                reason=None if target else "relative_import_target_not_found",
                                raw=f"{raw}{_text(single)}",
                            )
                        )
                    else:
                        out.append(
                            _ImportTarget(
                                line=node.start_point[0] + 1,
                                target_key=None,
                                resolution_status="unresolved",
                                reason="wildcard_or_multi_relative_import",
                                raw=raw,
                            )
                        )
            else:
                module_parts = raw.split(".")
                target = _resolve_python_module([], module_parts, file_keys)
                out.append(
                    _ImportTarget(
                        line=node.start_point[0] + 1,
                        target_key=target,
                        resolution_status="resolved" if target else "external",
                        raw=raw,
                    )
                )

        elif node.type == "call":
            func = node.child_by_field_name("function")
            if func is not None and _text(func).endswith("import_module"):
                args = node.child_by_field_name("arguments")
                first_arg = (
                    args.children[1] if args is not None and len(args.children) > 1 else None
                )
                if first_arg is not None and first_arg.type == "string":
                    literal = first_arg.text.decode("utf-8", "replace") if first_arg.text else ""
                    module_parts = literal.strip("'\"").split(".")
                    target = _resolve_python_module([], module_parts, file_keys)
                    out.append(
                        _ImportTarget(
                            line=node.start_point[0] + 1,
                            target_key=target,
                            resolution_status="resolved" if target else "external",
                            raw=literal,
                        )
                    )
                else:
                    out.append(
                        _ImportTarget(
                            line=node.start_point[0] + 1,
                            target_key=None,
                            resolution_status="unresolved",
                            reason="dynamic_import_module",
                            raw=_text(node),
                        )
                    )
    return out


def _string_literal_text(node: TSNode | None) -> str | None:
    if node is None or node.type != "string":
        return None
    fragment = next((c for c in node.children if c.type == "string_fragment"), None)
    return _text(fragment) if fragment is not None else None


def _resolve_js_specifier(specifier: str, file_key: str, file_keys: set[str]) -> str | None:
    if not specifier.startswith("."):
        return None  # bare specifier: not a repo-relative path at all
    dir_parts = _dir_parts(file_key)
    parts = specifier.split("/")
    resolved_parts = list(dir_parts)
    for part in parts:
        if part in ("", "."):
            continue
        if part == "..":
            if resolved_parts:
                resolved_parts.pop()
            continue
        resolved_parts.append(part)
    base = "/".join(resolved_parts)
    for ext in JS_EXTENSIONS:
        candidate = base + ext
        if candidate in file_keys:
            return candidate
    for suffix in JS_INDEX_SUFFIXES:
        candidate = base + suffix
        if candidate in file_keys:
            return candidate
    return None


def _js_import_targets(tree: TSTree, file_key: str, file_keys: set[str]) -> list[_ImportTarget]:
    out: list[_ImportTarget] = []
    for node in _iter_nodes(tree.root_node):
        specifier_node = None
        if node.type == "import_statement":
            specifier_node = next((c for c in node.children if c.type == "string"), None)
        elif node.type == "call_expression":
            func = node.child_by_field_name("function")
            if func is not None and func.type == "identifier" and _text(func) == "require":
                args = node.child_by_field_name("arguments")
                if args is not None:
                    inner = [c for c in args.children if c.type not in ("(", ")", ",")]
                    if inner:
                        candidate = inner[0]
                        if candidate.type == "string":
                            specifier_node = candidate
                        else:
                            out.append(
                                _ImportTarget(
                                    line=node.start_point[0] + 1,
                                    target_key=None,
                                    resolution_status="unresolved",
                                    reason="dynamic_require",
                                    raw=_text(node),
                                )
                            )
                            continue
        if specifier_node is None:
            continue
        literal = _string_literal_text(specifier_node)
        if literal is None:
            continue
        resolved = _resolve_js_specifier(literal, file_key, file_keys)
        if literal.startswith("."):
            out.append(
                _ImportTarget(
                    line=node.start_point[0] + 1,
                    target_key=resolved,
                    resolution_status="resolved" if resolved else "unresolved",
                    reason=None if resolved else "relative_import_target_not_found",
                    raw=literal,
                )
            )
        else:
            out.append(
                _ImportTarget(
                    line=node.start_point[0] + 1,
                    target_key=None,
                    resolution_status="external",
                    raw=literal,
                )
            )
    return out


def _import_edges(
    file_key: str,
    targets: list[_ImportTarget],
    id_by_key: dict[str, int],
    repo_id: str,
    commit_sha: str,
) -> list[Edge]:
    src_id = id_by_key.get(file_key)
    if src_id is None:
        return []
    edges: list[Edge] = []
    for target in targets:
        dst_id = id_by_key.get(target.target_key) if target.target_key else None
        edges.append(
            Edge(
                repo_id=repo_id,
                commit_sha=commit_sha,
                kind="imports",
                src_id=src_id,
                dst_id=dst_id,
                line_start=target.line,
                analyzer=ANALYZER,
                analyzer_version=ANALYZER_VERSION,
                status="verified",
                resolution_status=target.resolution_status,
                attrs={"raw": target.raw} if target.raw else {},
                reason=target.reason,
            )
        )
    return edges


# ----------------------------------------------------------------- calls

_CALL_NODE_TYPE = {
    "python": "call",
    "javascript": "call_expression",
    "typescript": "call_expression",
    "tsx": "call_expression",
}


def _call_target_name(func_node: TSNode) -> str | None:
    if func_node.type == "identifier":
        return _text(func_node)
    if func_node.type in ("attribute", "member_expression"):
        # `obj.method(...)` / `this.method(...)` -- the called name is the
        # right-most segment; name-based resolution can't use the receiver.
        text = _text(func_node)
        return text.rsplit(".", 1)[-1] if text else None
    return None


def _resolve_name_edge(
    name: str, file_key: str, symbols_by_name: dict[str, list[ScannedNode]]
) -> tuple[ResolutionStatus, str | None, str | None]:
    """Name-based resolution shared by CALLS/EXTENDS/IMPLEMENTS: a same-file
    match is certain ("resolved"); a unique same-named match elsewhere in
    the repo could be a different symbol of the same name ("ambiguous");
    none or multiple candidates can't be resolved at all."""
    candidates = symbols_by_name.get(name, [])
    if not candidates:
        return "unresolved", None, "no_symbol_named_this"
    same_file = [c for c in candidates if c.key.startswith(f"{file_key}::")]
    if same_file:
        return "resolved", same_file[0].key, None
    if len(candidates) == 1:
        return "ambiguous", candidates[0].key, None
    return "unresolved", None, "multiple_candidates_same_name"


def _calls_edges(
    file_key: str,
    tree: TSTree,
    language: str,
    symbols_by_name: dict[str, list[ScannedNode]],
    id_by_key: dict[str, int],
    repo_id: str,
    commit_sha: str,
) -> list[Edge]:
    src_id = id_by_key.get(file_key)
    if src_id is None:
        return []
    call_type = _CALL_NODE_TYPE[language]
    edges: list[Edge] = []
    for node in _iter_nodes(tree.root_node):
        if node.type != call_type:
            continue
        func = node.child_by_field_name("function")
        if func is None:
            continue
        name = _call_target_name(func)
        if not name or name in ("require", "import_module"):
            # Already captured as an `imports` edge above; counting it again
            # as an unresolved `calls` edge would just be noise.
            continue
        resolution_status, dst_key, reason = _resolve_name_edge(name, file_key, symbols_by_name)
        edges.append(
            Edge(
                repo_id=repo_id,
                commit_sha=commit_sha,
                kind="calls",
                src_id=src_id,
                dst_id=id_by_key.get(dst_key) if dst_key else None,
                line_start=node.start_point[0] + 1,
                analyzer=ANALYZER,
                analyzer_version=ANALYZER_VERSION,
                status="verified",
                resolution_status=resolution_status,
                attrs={"callee_name": name},
                reason=reason,
            )
        )
    return edges


# --------------------------------------------------------- extends/implements


def _python_extends_edges(
    file_key: str,
    tree: TSTree,
    symbols_by_name: dict[str, list[ScannedNode]],
    id_by_key: dict[str, int],
    repo_id: str,
    commit_sha: str,
) -> list[Edge]:
    src_id = id_by_key.get(file_key)
    if src_id is None:
        return []
    edges: list[Edge] = []
    for node in _iter_nodes(tree.root_node):
        if node.type != "class_definition":
            continue
        bases = node.child_by_field_name("superclasses")
        if bases is None:
            continue
        for arg in bases.children:
            if arg.type != "identifier":
                continue
            name = _text(arg)
            resolution_status, dst_key, reason = _resolve_name_edge(name, file_key, symbols_by_name)
            edges.append(
                Edge(
                    repo_id=repo_id,
                    commit_sha=commit_sha,
                    kind="extends",
                    src_id=src_id,
                    dst_id=id_by_key.get(dst_key) if dst_key else None,
                    line_start=node.start_point[0] + 1,
                    analyzer=ANALYZER,
                    analyzer_version=ANALYZER_VERSION,
                    status="verified",
                    resolution_status=resolution_status,
                    attrs={"base_name": name},
                    reason=reason,
                )
            )
    return edges


def _js_extends_implements_edges(
    file_key: str,
    tree: TSTree,
    symbols_by_name: dict[str, list[ScannedNode]],
    id_by_key: dict[str, int],
    repo_id: str,
    commit_sha: str,
) -> list[Edge]:
    src_id = id_by_key.get(file_key)
    if src_id is None:
        return []
    edges: list[Edge] = []
    for node in _iter_nodes(tree.root_node):
        if node.type not in ("class_declaration", "interface_declaration"):
            continue
        extends_names: list[str] = []
        implements_names: list[str] = []
        for child in _iter_nodes(node):
            if child.type == "extends_clause":
                value = child.child_by_field_name("value")
                if value is not None:
                    extends_names.append(_text(value))
            elif child.type == "extends_type_clause":
                for c in child.children:
                    if c.type in ("type_identifier", "identifier"):
                        extends_names.append(_text(c))
            elif child.type == "implements_clause":
                for c in child.children:
                    if c.type in ("type_identifier", "identifier"):
                        implements_names.append(_text(c))
        for name, kind in [(n, "extends") for n in extends_names] + [
            (n, "implements") for n in implements_names
        ]:
            resolution_status, dst_key, reason = _resolve_name_edge(name, file_key, symbols_by_name)
            edges.append(
                Edge(
                    repo_id=repo_id,
                    commit_sha=commit_sha,
                    kind=kind,  # type: ignore[arg-type]
                    src_id=src_id,
                    dst_id=id_by_key.get(dst_key) if dst_key else None,
                    line_start=node.start_point[0] + 1,
                    analyzer=ANALYZER,
                    analyzer_version=ANALYZER_VERSION,
                    status="verified",
                    resolution_status=resolution_status,
                    attrs={"base_name": name},
                    reason=reason,
                )
            )
    return edges


# ----------------------------------------------------------------- tests

_PY_TEST_PREFIXES = ("test_",)
_PY_TEST_SUFFIXES = ("_test",)
_JS_TEST_SUFFIXES = (".test", ".spec")


def _python_test_target_stem(stem: str) -> str | None:
    for prefix in _PY_TEST_PREFIXES:
        if stem.startswith(prefix):
            return stem[len(prefix) :]
    for suffix in _PY_TEST_SUFFIXES:
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return None


def _js_test_target_stem(name: str) -> tuple[str, str] | None:
    """Returns (stem_without_test_suffix, extension) for `foo.test.ts`-style
    names, or None if `name` doesn't look like a test file."""
    for suffix in _JS_TEST_SUFFIXES:
        for ext in (".ts", ".tsx", ".js", ".jsx"):
            marker = suffix + ext
            if name.endswith(marker):
                return name[: -len(marker)], ext
    return None


def _tests_edges(
    file_nodes: list[ScannedNode],
    resolved_imports_by_file: dict[str, set[str]],
    id_by_key: dict[str, int],
    repo_id: str,
    commit_sha: str,
) -> list[Edge]:
    by_dir_stem: dict[tuple[str, str], str] = {}
    for sn in file_nodes:
        path = sn.node.path or ""
        if "/" in path:
            d, base = path.rsplit("/", 1)
        else:
            d, base = "", path
        stem = base.rsplit(".", 1)[0] if "." in base else base
        by_dir_stem[(d, stem)] = path

    edges: list[Edge] = []
    for sn in file_nodes:
        path = sn.node.path or ""
        if "/" in path:
            d, base = path.rsplit("/", 1)
        else:
            d, base = "", path
        stem = base.rsplit(".", 1)[0] if "." in base else base

        target_path: str | None = None
        evidence = ""
        py_target_stem = _python_test_target_stem(stem)
        js_result = _js_test_target_stem(base)

        if py_target_stem is not None:
            target_path = by_dir_stem.get((d, py_target_stem))
            evidence = f"naming:{base}~{py_target_stem}.py"
        elif js_result is not None:
            target_stem, _ext = js_result
            target_path = by_dir_stem.get((d, target_stem))
            evidence = f"naming:{base}~{target_stem}"

        if target_path is None:
            continue

        src_id = id_by_key.get(path)
        dst_id = id_by_key.get(target_path)
        if src_id is None or dst_id is None:
            continue

        import_confirms = target_path in resolved_imports_by_file.get(path, set())
        resolution_status = "resolved" if import_confirms else "ambiguous"
        evidence_full = f"{evidence}+import" if import_confirms else evidence

        edges.append(
            Edge(
                repo_id=repo_id,
                commit_sha=commit_sha,
                kind="tests",
                src_id=src_id,
                dst_id=dst_id,
                analyzer=ANALYZER,
                analyzer_version=ANALYZER_VERSION,
                status="verified",
                resolution_status=resolution_status,
                attrs={"evidence": evidence_full},
            )
        )
    return edges
