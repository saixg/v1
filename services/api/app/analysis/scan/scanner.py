"""Pass 1: walk a checkout, parse with tree-sitter, emit `directory`, `file`
and `symbol` nodes plus the `contains` edges between them. No DB, no network,
no exec — source in, nodes and edges out. See docs/05-analysis-pipeline.md.

This pass must never fail on a file: an unreadable, oversized, slow-to-parse
or unparseable file still gets a `file` node, just with status='unanalyzed'
and no symbols under it.
"""

from __future__ import annotations

import concurrent.futures
import fnmatch
import hashlib
from dataclasses import dataclass
from dataclasses import replace as _dc_replace
from pathlib import Path, PurePosixPath

from tree_sitter import Node as TSNode
from tree_sitter import Tree as TSTree
from tree_sitter_language_pack import get_parser

from app.models import Node

from .limits import DEFAULT_LIMITS, ScanLimits
from .secrets import redact_secrets

ANALYZER = "tree-sitter"
ANALYZER_VERSION = "0.1.0"

EXCLUDED_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}

LANGUAGE_BY_EXT = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
}

FULL_TIER_LANGUAGES = {"python", "javascript", "typescript", "tsx"}

SYMBOL_NODE_TYPES = {
    "python": {"function_definition", "class_definition"},
    "javascript": {"function_declaration", "class_declaration", "method_definition"},
    "typescript": {
        "function_declaration", "class_declaration", "method_definition", "interface_declaration",
    },
    "tsx": {
        "function_declaration", "class_declaration", "method_definition", "interface_declaration",
    },
}


@dataclass(frozen=True)
class ScannedNode:
    """A node plus the key of the node that contains it, so the caller can
    insert in order and wire up `contains` edges once rows have real ids."""

    key: str
    parent_key: str | None
    node: Node
    # Raw bytes the scan already read to hash the file, carried through so the
    # worker can store pinned source without re-reading the checkout. Only set
    # on `file` nodes whose bytes were read (under the size cap) and are text;
    # None for oversized, unreadable or binary files, which have no source view.
    content: bytes | None = None


@dataclass(frozen=True)
class ScanStats:
    """Whether a resource cap cut the walk short, and which one."""

    truncated: bool
    reason: str | None  # "file_count_cap" | "total_size_cap" | None


def scan_repo(
    root: Path,
    repo_id: str,
    commit_sha: str,
    extra_excludes: frozenset[str] = frozenset(),
    limits: ScanLimits = DEFAULT_LIMITS,
) -> tuple[list[ScannedNode], ScanStats, dict[str, TSTree]]:
    """Returns `(scanned_nodes, stats, trees_by_file)`. `trees_by_file` carries
    the parsed tree-sitter tree for every full-tier file that parsed
    successfully (keyed by the file's `rel_file` path, the same string used
    as its `ScannedNode.key`), so a later pass (relationships.py) can walk
    import/call/inheritance syntax without re-parsing. Only populated for
    full-tier languages; a file that timed out, was unanalyzed, or whose
    language has no tree-sitter grammar here is simply absent from the map."""
    root = root.resolve()
    out: list[ScannedNode] = [
        ScannedNode(
            key="",
            parent_key=None,
            node=Node(
                repo_id=repo_id,
                commit_sha=commit_sha,
                kind="directory",
                name=root.name or str(root),
                path="",
                analyzer=ANALYZER,
                analyzer_version=ANALYZER_VERSION,
                status="verified",
            ),
        )
    ]

    file_count = 0
    total_bytes = 0
    stats = ScanStats(truncated=False, reason=None)
    trees_by_file: dict[str, TSTree] = {}

    for dirpath, dirnames, filenames in walk(root, extra_excludes):
        rel_dir = _posix_rel(dirpath, root)
        for dirname in dirnames:
            child_rel = f"{rel_dir}/{dirname}" if rel_dir else dirname
            out.append(
                ScannedNode(
                    key=child_rel,
                    parent_key=rel_dir,
                    node=Node(
                        repo_id=repo_id,
                        commit_sha=commit_sha,
                        kind="directory",
                        name=dirname,
                        path=child_rel,
                        analyzer=ANALYZER,
                        analyzer_version=ANALYZER_VERSION,
                        status="verified",
                    ),
                )
            )

        cap_hit = False
        for filename in filenames:
            file_path = dirpath / filename
            rel_file = f"{rel_dir}/{filename}" if rel_dir else filename

            try:
                size = file_path.stat().st_size
            except OSError:
                size = 0

            file_count += 1
            total_bytes += size

            file_node = _scan_file(file_path, rel_file, rel_dir, repo_id, commit_sha, limits)
            symbol_nodes: list[ScannedNode] = []
            if file_node.node.status != "unanalyzed":
                symbol_nodes, timed_out, tree = _scan_symbols(
                    file_path, rel_file, repo_id, commit_sha, limits
                )
                if timed_out:
                    file_node = _mark_unanalyzed(file_node, "parse_timeout")
                    symbol_nodes = []
                elif tree is not None:
                    trees_by_file[rel_file] = tree
            out.append(file_node)
            out.extend(symbol_nodes)

            if file_count >= limits.max_file_count:
                stats = ScanStats(truncated=True, reason="file_count_cap")
                cap_hit = True
                break
            if total_bytes >= limits.max_total_size_bytes:
                stats = ScanStats(truncated=True, reason="total_size_cap")
                cap_hit = True
                break

        if cap_hit:
            break

    return out, stats, trees_by_file


def walk(root: Path, extra_excludes: frozenset[str] = frozenset()):
    """Yield `(dirpath, dirnames, filenames)` for every directory under
    `root`, breadth-first, with `dirnames` already filtered against
    `EXCLUDED_DIRS` (exact directory-name match, wherever it appears) and
    `extra_excludes` (fnmatch glob patterns matched against each candidate
    directory's path relative to `root`). The real scan and the exclusion
    preview both call this function so they structurally cannot drift apart.
    """
    stack = [root]
    while stack:
        current = stack.pop(0)
        try:
            entries = list(current.iterdir())
        except OSError:
            continue
        rel_dir = _posix_rel(current, root)
        dirnames: list[str] = []
        for entry in sorted(entries, key=lambda e: e.name):
            # A symlink in an untrusted checkout can point anywhere on the host
            # (`notes.md -> /etc/passwd`), and following one would store that
            # file's bytes as repository content. Never followed, in either
            # direction; the scan and the exclusion preview share this walk.
            if entry.is_symlink() or not entry.is_dir():
                continue
            name = entry.name
            if name in EXCLUDED_DIRS:
                continue
            child_rel = f"{rel_dir}/{name}" if rel_dir else name
            if any(fnmatch.fnmatch(child_rel, pattern) for pattern in extra_excludes):
                continue
            dirnames.append(name)
        filenames = sorted(e.name for e in entries if e.is_file() and not e.is_symlink())
        yield current, dirnames, filenames
        stack = [current / d for d in dirnames] + stack


def _posix_rel(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    return "" if rel == Path(".") else str(PurePosixPath(*rel.parts))


def _mark_unanalyzed(scanned: ScannedNode, reason: str) -> ScannedNode:
    updated_node = scanned.node.model_copy(update={"status": "unanalyzed", "reason": reason})
    return _dc_replace(scanned, node=updated_node)


def _scan_file(
    file_path: Path,
    rel_file: str,
    parent_key: str,
    repo_id: str,
    commit_sha: str,
    limits: ScanLimits = DEFAULT_LIMITS,
) -> ScannedNode:
    language = LANGUAGE_BY_EXT.get(file_path.suffix.lower())

    try:
        size = file_path.stat().st_size
    except OSError as exc:
        return ScannedNode(
            key=rel_file,
            parent_key=parent_key,
            node=Node(
                repo_id=repo_id,
                commit_sha=commit_sha,
                kind="file",
                name=file_path.name,
                path=rel_file,
                attrs={"language": language, "tier": "unanalyzed"},
                analyzer=ANALYZER,
                analyzer_version=ANALYZER_VERSION,
                status="unanalyzed",
                reason=f"stat_error:{exc.__class__.__name__}",
            ),
        )

    if size > limits.max_file_size_bytes:
        return ScannedNode(
            key=rel_file,
            parent_key=parent_key,
            node=Node(
                repo_id=repo_id,
                commit_sha=commit_sha,
                kind="file",
                name=file_path.name,
                path=rel_file,
                attrs={"language": language, "tier": "unanalyzed", "size_bytes": size},
                analyzer=ANALYZER,
                analyzer_version=ANALYZER_VERSION,
                status="unanalyzed",
                reason="file_too_large",
            ),
        )

    try:
        content = file_path.read_bytes()
    except OSError as exc:
        return ScannedNode(
            key=rel_file,
            parent_key=parent_key,
            node=Node(
                repo_id=repo_id,
                commit_sha=commit_sha,
                kind="file",
                name=file_path.name,
                path=rel_file,
                attrs={"language": language, "tier": "unanalyzed"},
                analyzer=ANALYZER,
                analyzer_version=ANALYZER_VERSION,
                status="unanalyzed",
                reason=f"read_error:{exc.__class__.__name__}",
            ),
        )

    tier = "full" if language in FULL_TIER_LANGUAGES else "inventory"
    status = "verified" if language else "unanalyzed"

    # NUL bytes mark a binary file; Postgres text cannot hold them anyway.
    is_text = 0 not in content
    stored_content = content
    secrets_redacted = False
    secrets_redacted_count = 0
    if is_text:
        redacted, secrets_redacted_count = redact_secrets(content)
        secrets_redacted = secrets_redacted_count > 0
        if secrets_redacted:
            stored_content = redacted

    return ScannedNode(
        key=rel_file,
        parent_key=parent_key,
        node=Node(
            repo_id=repo_id,
            commit_sha=commit_sha,
            kind="file",
            name=file_path.name,
            path=rel_file,
            attrs={
                "language": language,
                "tier": tier,
                "loc": content.count(b"\n") + 1,
                # Hashed from the original bytes, never the redacted ones, so
                # the hash still matches what was actually on disk.
                "content_hash": hashlib.sha256(content).hexdigest(),
                "secrets_redacted": secrets_redacted,
                "secrets_redacted_count": secrets_redacted_count,
            },
            analyzer=ANALYZER,
            analyzer_version=ANALYZER_VERSION,
            status=status,
        ),
        content=stored_content if is_text else None,
    )


def _scan_symbols(
    file_path: Path,
    rel_file: str,
    repo_id: str,
    commit_sha: str,
    limits: ScanLimits = DEFAULT_LIMITS,
) -> tuple[list[ScannedNode], bool, TSTree | None]:
    """Returns `(symbol_nodes, timed_out, tree)`. `timed_out=True` means the
    parse did not finish within `limits.max_parse_seconds`; the caller
    amends the file node's status to unanalyzed/parse_timeout in that case.
    `tree` is the parsed tree-sitter tree on success (so `scan_repo` can hand
    it to the relationship-extraction pass without re-parsing), or None when
    there is no tree-sitter grammar for this file or the parse timed out."""
    language = LANGUAGE_BY_EXT.get(file_path.suffix.lower())
    if language is None:
        return [], False, None

    try:
        content = file_path.read_bytes()
    except OSError:
        return [], False, None

    tree = _parse_with_timeout(language, content, limits.max_parse_seconds)
    if tree is None:
        return [], True, None

    symbol_types = SYMBOL_NODE_TYPES[language]
    scanned: list[ScannedNode] = []
    for ts_node in _iter_nodes(tree.root_node):
        if ts_node.type not in symbol_types:
            continue
        name_node = ts_node.child_by_field_name("name")
        name = (
            name_node.text.decode("utf-8", "replace")
            if name_node is not None and name_node.text is not None
            else "<anonymous>"
        )
        key = f"{rel_file}::{name}:{ts_node.start_point[0] + 1}:{ts_node.start_point[1]}"
        scanned.append(
            ScannedNode(
                key=key,
                parent_key=rel_file,
                node=Node(
                    repo_id=repo_id,
                    commit_sha=commit_sha,
                    kind="symbol",
                    name=name,
                    path=rel_file,
                    line_start=ts_node.start_point[0] + 1,
                    line_end=ts_node.end_point[0] + 1,
                    col_start=ts_node.start_point[1],
                    col_end=ts_node.end_point[1],
                    attrs={"node_type": ts_node.type},
                    analyzer=ANALYZER,
                    analyzer_version=ANALYZER_VERSION,
                    status="verified",
                ),
            )
        )
    return scanned, False, tree


def _parse_with_timeout(language: str, content: bytes, max_parse_seconds: float):
    """Best-effort bound on parse time: runs the parse in a single worker
    thread and gives up waiting after `max_parse_seconds`, returning `None`.

    This is deliberately best-effort, not true preemption: the spawned
    thread is never killed, just abandoned (the executor is shut down with
    `wait=False`), so a pathological input can still burn CPU in the
    background after we've moved on. Real wall-clock enforcement needs the
    sandboxed container (infra/sandbox), which this project has deliberately
    deferred — this is a known, intentional gap, not a silent omission.
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(get_parser(language).parse, content)
    try:
        return future.result(timeout=max_parse_seconds)
    except concurrent.futures.TimeoutError:
        executor.shutdown(wait=False)
        return None
    except Exception:
        executor.shutdown(wait=False)
        return None


def _iter_nodes(node: TSNode):
    yield node
    for child in node.children:
        yield from _iter_nodes(child)
