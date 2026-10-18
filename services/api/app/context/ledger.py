"""Constraint Ledger auto-detection (build-plan.md Part 4.F.8).
**Deterministic only — no model is called anywhere in this module.**

Reads facts the analyzers already stored for one snapshot — file nodes,
route/model nodes, `codeaxis.conventions` rows, and the pinned (already
secret-redacted) text in `codeaxis.blobs` — and proposes ledger entries in
the five categories Part 4.F.8 names:

- `public_api`    — HTTP routes (route nodes when the graph has them; route
  decorators / `app.get(...)` calls read literally from source otherwise)
  and a package's declared entry points (`package.json` main/exports/bin).
- `db_schema`     — migration directories, schema files, ORM model nodes.
- `auth_boundary` — files on an authentication/authorization path
  (`auth/`, `middleware`, `jwt`, `oauth`, `permissions`, ...).
- `ci_lint`       — the `lint`/`ci` conventions that were read from a config
  file (`origin='configured'`); inferred conventions are never promoted.
- `do_not_touch`  — lock files, generated files (by name, by an
  `@generated` / `DO NOT EDIT` header, or by `.gitattributes`
  `linguist-generated`), vendored directories, and *existing* migrations.

Every row is `source='auto_detected'`, `confirmed=False` (a detection is
shown as a detection until a human accepts it), carries this analyzer's name
and version, and evidence spans pointing at the lines or files it was read
from. `origin` is honest per row: `configured` only when the fact was read out
of an explicit config/declaration file (`package.json`, `.gitattributes`, a
lint or CI config), `parsed` when it was read from paths or code.

These are heuristics over names and literal text. They can miss things (a
router built dynamically has no literal route) and can over-match (a file
called `session.py` that is not about auth); both failure modes are why the
rows start unconfirmed.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field

import psycopg

from app.knowledge import list_conventions, list_nodes, replace_detected_constraints
from app.models import (
    Citation,
    Constraint,
    ConstraintKind,
    ConstraintSeverity,
    ContextEvidence,
    Convention,
    Node,
    OriginTag,
)

ANALYZER = "context.ledger"
ANALYZER_VERSION = "0.1.0"

# Per-row and per-kind caps. A ledger with 900 auth entries is not a ledger
# anyone reads; the cap is recorded (`truncated=True`) rather than hidden.
MAX_SPANS_PER_ROW = 200
MAX_ROWS_PER_KIND = 60
MAX_ROUTES_LISTED = 15


@dataclass(frozen=True)
class FileFact:
    path: str
    node_id: int | None = None
    loc: int | None = None


@dataclass
class LedgerSnapshot:
    """The deterministic inputs. `blobs` holds only the files the detectors
    read text from (route candidates, generated-header candidates, manifests,
    `.gitattributes`) — never the whole repo."""

    files: list[FileFact]
    route_nodes: list[Node] = field(default_factory=list)
    model_nodes: list[Node] = field(default_factory=list)
    conventions: list[Convention] = field(default_factory=list)
    blobs: dict[str, str] = field(default_factory=dict)


# --- helpers --------------------------------------------------------------------


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _clean(text: str, limit: int = 120) -> str:
    """Repo text quoted into a statement: control characters removed, length
    capped. Still untrusted data downstream; this only keeps it one line."""
    return _CONTROL.sub(" ", text).strip()[:limit]


def _file_span(f: FileFact) -> Citation:
    return Citation(path=f.path, line_start=1, line_end=max(f.loc or 1, 1))


def _files_evidence(files: list[FileFact], notes: str) -> ContextEvidence:
    listed = files[:MAX_SPANS_PER_ROW]
    return ContextEvidence(
        spans=[_file_span(f) for f in listed],
        node_ids=[f.node_id for f in listed if f.node_id is not None],
        notes=notes,
        truncated=len(files) > MAX_SPANS_PER_ROW,
    )


def _node_ids(files: list[FileFact]) -> list[int]:
    return [f.node_id for f in files if f.node_id is not None][:MAX_SPANS_PER_ROW]


def _dir_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _segments(path: str) -> list[str]:
    return path.lower().split("/")


_TEST_SEGMENTS = {"tests", "test", "__tests__", "spec", "specs", "fixtures"}
_TEST_FILE = re.compile(r"(^test_.+\.py$|_test\.py$|\.(test|spec)\.[cm]?[tj]sx?$)")


def _is_test(path: str) -> bool:
    segs = _segments(path)
    return bool(_TEST_SEGMENTS & set(segs[:-1])) or bool(_TEST_FILE.search(segs[-1]))


class _Rows:
    """Collects rows, de-duplicating on (kind, scope) and enforcing the
    per-kind cap."""

    def __init__(self, repo_id: str, commit_sha: str) -> None:
        self.repo_id = repo_id
        self.commit_sha = commit_sha
        self.rows: list[Constraint] = []
        self._seen: set[tuple[str, str]] = set()
        self._per_kind: dict[str, int] = defaultdict(int)
        self.capped: dict[str, int] = defaultdict(int)

    def add(
        self,
        kind: ConstraintKind,
        scope: str,
        statement: str,
        severity: ConstraintSeverity,
        origin: OriginTag,
        evidence: ContextEvidence,
        rationale: str | None = None,
    ) -> None:
        key = (kind, scope)
        if key in self._seen:
            return
        if self._per_kind[kind] >= MAX_ROWS_PER_KIND:
            self.capped[kind] += 1
            return
        self._seen.add(key)
        self._per_kind[kind] += 1
        self.rows.append(
            Constraint(
                repo_id=self.repo_id,
                commit_sha=self.commit_sha,
                kind=kind,
                scope=scope,
                statement=statement,
                severity=severity,
                source="auto_detected",
                origin=origin,
                confirmed=False,
                rationale=rationale,
                evidence=evidence,
                analyzer=ANALYZER,
                analyzer_version=ANALYZER_VERSION,
            )
        )


# --- public_api -----------------------------------------------------------------

# FastAPI/Flask/Starlette-style decorators: `@app.get("/x")`, `@router.post(
# path="/y")`, `@bp.route("/z")`. Read literally; nothing is imported.
_PY_ROUTE = re.compile(
    r"^\s*@\s*([A-Za-z_][A-Za-z0-9_]*)\.(get|post|put|patch|delete|options|head|route|"
    r"api_route|websocket)\(\s*(?:path\s*=\s*)?[rRbBuU]?[\"']([^\"'\n]*)[\"']"
)
# Express/Koa/Fastify-style calls: `app.get('/x', ...)`, `router.post(...)`.
_JS_ROUTE = re.compile(
    r"\b(app|router|server|api)\.(get|post|put|patch|delete|all|options|head)\(\s*"
    r"[\"'`]([^\"'`\n]+)[\"'`]"
)
_CODE_EXT = (".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx")


def _route_lines(path: str, content: str) -> list[tuple[int, str, str]]:
    """(line, METHOD, route path) for each literal route declaration."""
    found: list[tuple[int, str, str]] = []
    pattern = _PY_ROUTE if path.endswith(".py") else _JS_ROUTE
    for n, line in enumerate(content.splitlines(), start=1):
        m = pattern.search(line)
        if m is None:
            continue
        method = m.group(2).upper()
        if method in ("ROUTE", "API_ROUTE"):
            method = "ANY"
        found.append((n, method, _clean(m.group(3), 100) or "/"))
    return found


def detect_public_api(rows: _Rows, snap: LedgerSnapshot, by_path: dict[str, FileFact]) -> None:
    # Route nodes, when a framework extractor has produced them.
    nodes_by_file: dict[str, list[Node]] = defaultdict(list)
    for node in snap.route_nodes:
        if node.path:
            nodes_by_file[node.path].append(node)
    for path, nodes in sorted(nodes_by_file.items()):
        names = ", ".join(_clean(n.name, 80) for n in nodes[:MAX_ROUTES_LISTED])
        spans = [
            Citation(path=path, line_start=n.line_start, line_end=n.line_end or n.line_start,
                     node_id=n.id)
            for n in nodes[:MAX_SPANS_PER_ROW] if n.line_start is not None
        ]
        rows.add(
            "public_api", path,
            f"Routes in {path} ({names}) are an externally visible API; changing a path, "
            "method or request/response shape breaks callers.",
            "hard", "parsed",
            ContextEvidence(spans=spans, node_ids=[n.id for n in nodes if n.id is not None],
                            notes="route nodes in the graph",
                            truncated=len(nodes) > MAX_SPANS_PER_ROW),
        )

    # Literal route declarations in source, for files the graph has no route
    # nodes for.
    for path, content in sorted(snap.blobs.items()):
        if not path.endswith(_CODE_EXT) or path in nodes_by_file or _is_test(path):
            continue
        routes = _route_lines(path, content)
        if not routes:
            continue
        listed = ", ".join(f"{m} {p}" for _, m, p in routes[:MAX_ROUTES_LISTED])
        extra = len(routes) - MAX_ROUTES_LISTED
        more = f" and {extra} more" if extra > 0 else ""
        fact = by_path.get(path)
        rows.add(
            "public_api", path,
            f"HTTP routes declared in {path} ({listed}{more}) are an externally visible API; "
            "changing a path, method or request/response shape breaks callers.",
            "hard", "parsed",
            ContextEvidence(
                spans=[Citation(path=path, line_start=n, line_end=n)
                       for n, _, _ in routes[:MAX_SPANS_PER_ROW]],
                node_ids=[fact.node_id] if fact and fact.node_id is not None else [],
                notes="route declarations read literally from source (decorator / call syntax)",
                truncated=len(routes) > MAX_SPANS_PER_ROW,
            ),
        )

    # package.json: the published entry points are configured, not guessed.
    for path, content in sorted(snap.blobs.items()):
        if path.rsplit("/", 1)[-1] != "package.json":
            continue
        try:
            data = json.loads(content)
        except ValueError:
            continue  # a malformed manifest is a gap, not a guess
        if not isinstance(data, dict):
            continue
        declared = [k for k in ("main", "module", "types", "exports", "bin") if k in data]
        if not declared:
            continue
        lines = content.splitlines()
        spans = [
            Citation(path=path, line_start=i, line_end=i)
            for i, line in enumerate(lines, start=1)
            if any(re.match(rf'\s*"{k}"\s*:', line) for k in declared)
        ]
        private = data.get("private") is True
        name = _clean(str(data.get("name", "")), 80)
        scope = _dir_of(path) + "/" if _dir_of(path) else "repo"
        rows.add(
            "public_api", scope,
            f"Package {name or path} declares its public entry points in {path} "
            f"({', '.join(declared)}); importers depend on them"
            + (" (package is marked private)." if private else "."),
            "soft" if private else "hard", "configured",
            ContextEvidence(spans=spans, notes=f"package.json fields: {', '.join(declared)}"),
        )


# --- db_schema ------------------------------------------------------------------

_MIGRATION_DIR = re.compile(
    r"(^|/)(migrations|alembic/versions|db/migrate|prisma/migrations)/", re.IGNORECASE
)
_MIGRATION_EXT = (".sql", ".py", ".ts", ".js", ".rb", ".mjs", ".cjs")
_SCHEMA_FILE = re.compile(r"(^|/)(schema\.prisma|[^/]+\.prisma|schema\.sql|structure\.sql)$")


def _migration_dirs(files: list[FileFact]) -> dict[str, list[FileFact]]:
    groups: dict[str, list[FileFact]] = defaultdict(list)
    for f in files:
        if not _MIGRATION_DIR.search(f.path) or not f.path.lower().endswith(_MIGRATION_EXT):
            continue
        if f.path.rsplit("/", 1)[-1] == "__init__.py" or _is_test(f.path):
            continue
        groups[_dir_of(f.path)].append(f)
    return groups


def detect_db_schema(rows: _Rows, snap: LedgerSnapshot) -> None:
    for directory, files in sorted(_migration_dirs(snap.files).items()):
        files = sorted(files, key=lambda f: f.path)
        rows.add(
            "db_schema", directory + "/",
            f"{len(files)} migration file(s) under {directory}/ define the database schema; "
            "schema changes are made by adding a new migration.",
            "hard", "parsed",
            _files_evidence(files, "migration directory, identified by path"),
        )
    for f in snap.files:
        if _SCHEMA_FILE.search(f.path) and not _is_test(f.path):
            rows.add(
                "db_schema", f.path,
                f"{f.path} declares the database schema; changes to it are schema changes.",
                "hard", "parsed",
                _files_evidence([f], "schema declaration file, identified by name"),
            )
    by_file: dict[str, list[Node]] = defaultdict(list)
    for node in snap.model_nodes:
        if node.path:
            by_file[node.path].append(node)
    for path, nodes in sorted(by_file.items()):
        names = ", ".join(_clean(n.name, 60) for n in nodes[:MAX_ROUTES_LISTED])
        rows.add(
            "db_schema", path,
            f"Data models in {path} ({names}) map to stored data; changing their fields "
            "changes the schema.",
            "hard", "parsed",
            ContextEvidence(
                spans=[Citation(path=path, line_start=n.line_start,
                                line_end=n.line_end or n.line_start, node_id=n.id)
                       for n in nodes if n.line_start is not None][:MAX_SPANS_PER_ROW],
                node_ids=[n.id for n in nodes if n.id is not None],
                notes="model nodes in the graph",
            ),
        )


# --- auth_boundary --------------------------------------------------------------

_AUTH_DIRS = {"auth", "authn", "authz", "authentication", "authorization", "middleware",
              "middlewares", "permissions", "security"}
_AUTH_FILE = re.compile(
    r"(^|[_\-.])(auth|authn|authz|authenticat\w*|authoriz\w*|jwt|oauth\w*|middleware|"
    r"permissions?|rbac|acl|session|login|passport|guard)s?([_\-.]|$)"
)


def detect_auth(rows: _Rows, snap: LedgerSnapshot) -> None:
    dir_groups: dict[str, list[FileFact]] = defaultdict(list)
    file_hits: list[tuple[FileFact, str]] = []
    for f in snap.files:
        if not f.path.lower().endswith(_CODE_EXT) or _is_test(f.path):
            continue
        segs = _segments(f.path)
        auth_dir = next(
            (i for i, s in enumerate(segs[:-1]) if s in _AUTH_DIRS), None
        )
        if auth_dir is not None:
            dir_groups["/".join(f.path.split("/")[: auth_dir + 1])].append(f)
            continue
        stem = segs[-1].rsplit(".", 1)[0]
        m = _AUTH_FILE.search(stem)
        if m:
            file_hits.append((f, m.group(2)))
    for directory, files in sorted(dir_groups.items()):
        rows.add(
            "auth_boundary", directory + "/",
            f"{directory}/ holds authentication/authorization code ({len(files)} file(s)); "
            "changes here alter who can do what and need security review.",
            "hard", "parsed",
            _files_evidence(sorted(files, key=lambda f: f.path),
                            f"directory name {directory.rsplit('/', 1)[-1]!r}"),
        )
    for f, matched in sorted(file_hits, key=lambda t: t[0].path):
        rows.add(
            "auth_boundary", f.path,
            f"{f.path} looks like an authentication/authorization boundary (file name matches "
            f"{matched!r}); changes here need security review.",
            "hard", "parsed",
            _files_evidence([f], f"file name matches {matched!r}"),
        )


# --- ci_lint --------------------------------------------------------------------


def detect_ci_lint(rows: _Rows, snap: LedgerSnapshot, by_path: dict[str, FileFact]) -> None:
    """Promote configured lint/CI conventions. `inferred` and `parsed`
    conventions are not rules anyone configured, so they stay conventions."""
    for conv in snap.conventions:
        if conv.category not in ("lint", "ci") or conv.origin != "configured":
            continue
        source = conv.evidence.get("source") if isinstance(conv.evidence, dict) else None
        fact = by_path.get(source) if isinstance(source, str) else None
        evidence = ContextEvidence(
            spans=[_file_span(fact)] if fact else [],
            node_ids=[fact.node_id] if fact and fact.node_id is not None else [],
            notes=f"from codeaxis.conventions row {conv.id} "
            f"({conv.analyzer} {conv.analyzer_version})"
            + (f", read from {source}" if isinstance(source, str) else ""),
        )
        rows.add(
            "ci_lint", source if isinstance(source, str) else "repo",
            _clean(conv.rule, 500),
            "hard" if conv.category == "ci" else "soft",
            "configured",
            evidence,
            rationale="CI runs this on every change" if conv.category == "ci"
            else "configured lint/format rule",
        )


# --- do_not_touch ---------------------------------------------------------------

_LOCK_FILES = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
    "bun.lock", "uv.lock", "poetry.lock", "pipfile.lock", "pdm.lock", "cargo.lock", "go.sum",
    "gemfile.lock", "composer.lock", "pubspec.lock", "packages.lock.json", "flake.lock",
}
_GENERATED_NAME = re.compile(
    r"(\.min\.(js|css)|_pb2(_grpc)?\.pyi?|\.pb\.go|\.pb\.(cc|h)|\.generated\.[a-z]+|"
    r"\.g\.dart|\.designer\.cs|\.gen\.(go|ts))$"
)
_GENERATED_DIRS = {"__generated__", "generated", "_generated"}
_VENDORED_DIRS = {"vendor", "vendors", "vendored", "third_party", "third-party", "thirdparty"}
# Header markers code generators actually write, looked for only near the top
# of a file (a comment deep in hand-written code mentioning "do not edit" is
# not a generator stamp).
_GENERATED_MARKER = re.compile(
    r"(@generated|\bDO NOT EDIT\b|\bCode generated\b.*\bDO NOT EDIT\b|"
    r"\bauto-?generated\b|\bThis file was automatically generated\b)",
    re.IGNORECASE,
)
_HEADER_LINES = 10


def _gitattributes(rows: _Rows, path: str, content: str) -> None:
    for n, raw in enumerate(content.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        pattern, attrs = parts[0], parts[1:]
        for attr, what in (("linguist-generated", "generated"),
                           ("linguist-vendored", "vendored")):
            if attr in attrs or f"{attr}=true" in attrs:
                rows.add(
                    "do_not_touch", _clean(pattern, 200),
                    f"Files matching {_clean(pattern, 200)} are marked {what} in {path}; "
                    "change their source, not the files.",
                    "hard", "configured",
                    ContextEvidence(spans=[Citation(path=path, line_start=n, line_end=n)],
                                    notes=f"{attr} attribute"),
                )


def detect_do_not_touch(rows: _Rows, snap: LedgerSnapshot,
                        by_path: dict[str, FileFact]) -> None:
    for path, content in sorted(snap.blobs.items()):
        if path.rsplit("/", 1)[-1] == ".gitattributes":
            _gitattributes(rows, path, content)

    vendored: dict[str, list[FileFact]] = defaultdict(list)
    gen_dirs: dict[str, list[FileFact]] = defaultdict(list)
    for f in sorted(snap.files, key=lambda f: f.path):
        segs = _segments(f.path)
        name = segs[-1]
        v = next((i for i, s in enumerate(segs[:-1]) if s in _VENDORED_DIRS), None)
        if v is not None:
            vendored["/".join(f.path.split("/")[: v + 1])].append(f)
            continue
        g = next((i for i, s in enumerate(segs[:-1]) if s in _GENERATED_DIRS), None)
        if g is not None:
            gen_dirs["/".join(f.path.split("/")[: g + 1])].append(f)
            continue
        if name in _LOCK_FILES:
            rows.add(
                "do_not_touch", f.path,
                f"{f.path} is a package-manager lock file; change the manifest and regenerate "
                "it with the package manager instead of editing it.",
                "hard", "parsed", _files_evidence([f], "lock file, identified by name"),
            )
        elif _GENERATED_NAME.search(name):
            rows.add(
                "do_not_touch", f.path,
                f"{f.path} is generated output (file name pattern); change its source instead.",
                "hard", "parsed", _files_evidence([f], "generated file name pattern"),
            )

    # Prefix semantics for whole vendored/generated directories: no spans, so
    # *any* path under them — including a new file — counts as touching them.
    for directory, files in sorted(vendored.items()):
        rows.add(
            "do_not_touch", directory + "/",
            f"{directory}/ contains vendored third-party code ({len(files)} file(s)); "
            "update it from upstream rather than editing it.",
            "hard", "parsed",
            ContextEvidence(node_ids=_node_ids(files),
                            notes="vendored directory, identified by name"),
        )
    for directory, files in sorted(gen_dirs.items()):
        rows.add(
            "do_not_touch", directory + "/",
            f"{directory}/ holds generated output ({len(files)} file(s)); regenerate it "
            "instead of editing it.",
            "hard", "parsed",
            ContextEvidence(node_ids=_node_ids(files),
                            notes="generated directory, identified by name"),
        )

    for path, content in sorted(snap.blobs.items()):
        if path.rsplit("/", 1)[-1] in (".gitattributes", "package.json"):
            continue
        for n, line in enumerate(content.splitlines()[:_HEADER_LINES], start=1):
            if _GENERATED_MARKER.search(line):
                rows.add(
                    "do_not_touch", path,
                    f"{path} carries a code-generator header (line {n}); change its source "
                    "or generator instead of editing it.",
                    "hard", "parsed",
                    ContextEvidence(spans=[Citation(path=path, line_start=n, line_end=n)],
                                    node_ids=_node_ids([by_path[path]]) if path in by_path
                                    else [],
                                    notes="generated-file header marker"),
                )
                break

    # Existing migrations are applied history. Spans list exactly the files
    # that exist now, so adding a *new* migration beside them is not flagged
    # by the contradiction check — only editing one that already exists.
    for directory, files in sorted(_migration_dirs(snap.files).items()):
        files = sorted(files, key=lambda f: f.path)
        rows.add(
            "do_not_touch", directory + "/",
            f"The {len(files)} existing migration file(s) under {directory}/ are applied "
            "history; add a new migration rather than editing one.",
            "hard", "parsed",
            _files_evidence(files, "existing migrations; new files in this directory are fine"),
        )


# --- driver ---------------------------------------------------------------------


def detect_constraints(repo_id: str, commit_sha: str, snap: LedgerSnapshot) -> list[Constraint]:
    """All five detectors over one snapshot. Pure: no I/O, no model."""
    rows = _Rows(repo_id, commit_sha)
    by_path = {f.path: f for f in snap.files}
    detect_do_not_touch(rows, snap, by_path)
    detect_db_schema(rows, snap)
    detect_public_api(rows, snap, by_path)
    detect_auth(rows, snap)
    detect_ci_lint(rows, snap, by_path)
    return rows.rows


# Server-side prefilters so only candidate blobs cross the wire: literal
# route syntax anywhere in the file, or a generator stamp in its first 1,500
# characters. POSIX regexes (Postgres `~`); the exact match is redone in
# Python above, so these only need to be a superset.
_SQL_ROUTE = (
    r"@[A-Za-z_][A-Za-z0-9_]*\.(get|post|put|patch|delete|options|head|route|api_route"
    r"|websocket)\(|(app|router|server|api)\.(get|post|put|patch|delete|all|options|head)\("
)
_SQL_GENERATED = r"(@generated|do not edit|code generated|auto-?generated|automatically generated)"


def load_ledger_snapshot(conn: psycopg.Connection, repo_id: str, commit_sha: str) -> LedgerSnapshot:
    """Read the detectors' inputs for one snapshot from the Repo Model."""
    files = [
        FileFact(
            path=n.path,
            node_id=n.id,
            loc=n.attrs.get("loc") if isinstance(n.attrs.get("loc"), int) else None,
        )
        for n in list_nodes(conn, repo_id, commit_sha, kind="file")
        if n.path
    ]
    route_nodes = list_nodes(conn, repo_id, commit_sha, kind="route")
    model_nodes = list_nodes(conn, repo_id, commit_sha, kind="model")
    conventions = list_conventions(conn, repo_id, commit_sha)
    with conn.cursor() as cur:
        rows = cur.execute(
            """
            select path, content from codeaxis.blobs
            where repo_id = %s and commit_sha = %s
              and (
                path ~ '(^|/)(package\\.json|\\.gitattributes)$'
                or (path ~ '\\.(py|js|jsx|mjs|cjs|ts|tsx)$' and content ~ %s)
                or left(content, 1500) ~* %s
              )
            order by path
            limit 5000
            """,
            (repo_id, commit_sha, _SQL_ROUTE, _SQL_GENERATED),
        ).fetchall()
    return LedgerSnapshot(
        files=files,
        route_nodes=route_nodes,
        model_nodes=model_nodes,
        conventions=conventions,
        blobs={path: content for path, content in rows},
    )


def write_ledger_pass(conn: psycopg.Connection, repo_id: str, commit_sha: str) -> list[Constraint]:
    """Detect and persist the repo-level ledger for one snapshot, replacing the
    previous auto-detected rows (user-stated and task-level rows are never
    touched — see `replace_detected_constraints`). `conn` must be a worker
    connection. Returns the rows with their new ids."""
    rows = detect_constraints(repo_id, commit_sha, load_ledger_snapshot(conn, repo_id, commit_sha))
    ids = replace_detected_constraints(conn, repo_id, commit_sha, rows)
    return [r.model_copy(update={"id": i}) for r, i in zip(rows, ids, strict=True)]
