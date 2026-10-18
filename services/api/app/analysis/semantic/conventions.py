"""Pass 2b (part 2): conventions extraction -- naming, structure, test style,
lint and CI rules (build-plan.md Part 4.D).

**Rules first, model only as a fallback**, the same discipline the layer
classifier follows. Each row states its origin honestly:

- `configured` -- read out of a real config file in the repo (an ESLint
  config, `ruff.toml` / `pyproject.toml`, a CI workflow, `tsconfig.json`).
  The value comes from parsing that file, never from a model.
- `parsed`     -- counted from code: the casing the repo's own symbol and
  file names actually use, where its tests live, what they are called.
  `confidence` is the share of candidates that follow the rule.
- `inferred`   -- a model generalized a pattern. Reached only when the rules
  produced nothing in the `naming`/`structure` categories, and bounded to a
  single model call.

Everything deterministic here reads the Repo Model (`nodes` for paths and
symbol names, `blobs` for the pinned, already-redacted text of config files).
No checkout access, no repository code execution, no network.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from collections import Counter
from dataclasses import dataclass

import psycopg
from pydantic import BaseModel, Field

from app.knowledge import upsert_conventions
from app.llm import CallContext, ModelService, ModelServiceError
from app.models import Convention

from .prompts import CONVENTIONS_SYSTEM, material_prompt

logger = logging.getLogger(__name__)

ANALYZER = "semantic.conventions"
ANALYZER_VERSION = "0.1.0"

# A naming rule is only worth stating if the repo is consistent about it and
# there is enough evidence to call it a convention rather than a coincidence.
_MIN_NAMING_SAMPLES = 5
_MIN_NAMING_SHARE = 0.6

# Hard caps: the model fallback is one call, and only this many names/paths
# are shown to it.
_MAX_FALLBACK_PATHS = 120
_MAX_FALLBACK_SYMBOLS = 120

_SNAKE = re.compile(r"^[a-z_][a-z0-9_]*$")
_CAMEL = re.compile(r"^[a-z][a-zA-Z0-9]*$")
_PASCAL = re.compile(r"^[A-Z][a-zA-Z0-9]*$")
_SCREAMING = re.compile(r"^[A-Z][A-Z0-9_]*$")

_PY_EXT = (".py",)
_TS_EXT = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

# Config files whose *presence and contents* are a configured convention.
_LINT_CONFIG_NAMES = (
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
    ".prettierrc",
    ".prettierrc.json",
    ".prettierrc.yml",
    "prettier.config.js",
    ".editorconfig",
    "ruff.toml",
    ".ruff.toml",
    "setup.cfg",
    ".flake8",
    "mypy.ini",
)


class ConventionGuess(BaseModel):
    """What the model fallback is allowed to return: short rule strings in
    fixed categories. No paths, no symbols, no file references -- those are
    facts, and facts come from the analyzers."""

    naming: list[str] = Field(default_factory=list, max_length=6)
    structure: list[str] = Field(default_factory=list, max_length=6)


@dataclass
class _Snapshot:
    """The deterministic inputs: file paths, symbol names, config blobs."""

    file_paths: list[str]
    # (symbol name, owning file path)
    symbols: list[tuple[str, str]]
    blobs: dict[str, str]


def _load(conn: psycopg.Connection, repo_id: str, commit_sha: str) -> _Snapshot:
    with conn.cursor() as cur:
        file_rows = cur.execute(
            """
            select path from codeaxis.nodes
            where repo_id = %s and commit_sha = %s and kind = 'file' and path is not null
            order by path
            """,
            (repo_id, commit_sha),
        ).fetchall()
        symbol_rows = cur.execute(
            """
            select name, path from codeaxis.nodes
            where repo_id = %s and commit_sha = %s and kind = 'symbol' and path is not null
            order by path, line_start
            """,
            (repo_id, commit_sha),
        ).fetchall()
    paths = [r[0] for r in file_rows]
    wanted = _config_paths(paths)
    blobs: dict[str, str] = {}
    if wanted:
        with conn.cursor() as cur:
            rows = cur.execute(
                """
                select path, content from codeaxis.blobs
                where repo_id = %s and commit_sha = %s and path = any(%s)
                """,
                (repo_id, commit_sha, wanted),
            ).fetchall()
        blobs = {path: content for path, content in rows}
    return _Snapshot(file_paths=paths, symbols=[(r[0], r[1]) for r in symbol_rows], blobs=blobs)


def _is_config_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if name in _LINT_CONFIG_NAMES or name in ("pyproject.toml", "tsconfig.json"):
        return True
    return path.startswith(".github/workflows/") and name.endswith((".yml", ".yaml"))


def _config_paths(paths: list[str]) -> list[str]:
    return [p for p in paths if _is_config_path(p)]


def _row(
    repo_id: str,
    commit_sha: str,
    category: str,
    rule: str,
    origin: str,
    *,
    confidence: float | None = None,
    evidence: dict | None = None,
) -> Convention:
    return Convention(
        repo_id=repo_id,
        commit_sha=commit_sha,
        category=category,  # pyright: ignore[reportArgumentType]
        rule=rule,
        origin=origin,  # pyright: ignore[reportArgumentType]
        confidence=confidence,
        evidence=evidence or {},
        analyzer=ANALYZER,
        analyzer_version=ANALYZER_VERSION,
    )


# --- parsed: counted from code ----------------------------------------------


def _dominant(names: list[str], patterns: dict[str, re.Pattern[str]]) -> tuple[str, float] | None:
    counts: Counter[str] = Counter()
    for name in names:
        for label, pattern in patterns.items():
            if pattern.match(name):
                counts[label] += 1
                break
    total = len(names)
    if total < _MIN_NAMING_SAMPLES or not counts:
        return None
    label, hits = counts.most_common(1)[0]
    share = hits / total
    return (label, share) if share >= _MIN_NAMING_SHARE else None


def naming_conventions(repo_id: str, commit_sha: str, snap: _Snapshot) -> list[Convention]:
    rows: list[Convention] = []

    def add_symbol_rule(label: str, exts: tuple[str, ...], language: str) -> None:
        names = [n for n, p in snap.symbols if p.endswith(exts)]
        # Class-like (PascalCase) and function-like names are mixed in the
        # `symbol` kind, so a single distribution over all of them is what
        # the graph actually supports. Stated as "most symbol names", not as
        # a claim about functions specifically.
        result = _dominant(
            names,
            {
                "snake_case": _SNAKE,
                "PascalCase": _PASCAL,
                "camelCase": _CAMEL,
                "SCREAMING_SNAKE_CASE": _SCREAMING,
            },
        )
        if result is None:
            return
        style, share = result
        rows.append(
            _row(
                repo_id,
                commit_sha,
                "naming",
                f"{language} symbol names are predominantly {style}",
                "parsed",
                confidence=round(share, 3),
                evidence={"sampled_symbols": len(names), "matching": round(share * len(names)),
                          "label": label},
            )
        )

    add_symbol_rule("python", _PY_EXT, "Python")
    add_symbol_rule("ts", _TS_EXT, "TypeScript/JavaScript")

    # File naming: the stem's casing, per language family.
    for exts, language in ((_PY_EXT, "Python"), (_TS_EXT, "TypeScript/JavaScript")):
        stems = [
            p.rsplit("/", 1)[-1].split(".", 1)[0]
            for p in snap.file_paths
            if p.endswith(exts)
        ]
        stems = [s for s in stems if s]
        result = _dominant(
            stems,
            {
                "snake_case": _SNAKE,
                "PascalCase": _PASCAL,
                "camelCase": _CAMEL,
            },
        )
        if result is None:
            continue
        style, share = result
        rows.append(
            _row(
                repo_id,
                commit_sha,
                "naming",
                f"{language} file names are predominantly {style}",
                "parsed",
                confidence=round(share, 3),
                evidence={"sampled_files": len(stems)},
            )
        )
    return rows


def structure_conventions(repo_id: str, commit_sha: str, snap: _Snapshot) -> list[Convention]:
    top: Counter[str] = Counter()
    for path in snap.file_paths:
        head = path.split("/", 1)[0] if "/" in path else ""
        if head:
            top[head] += 1
    if not top:
        return []
    listed = ", ".join(f"{name}/" for name, _ in sorted(top.items()))
    return [
        _row(
            repo_id,
            commit_sha,
            "structure",
            f"Source is organized under these top-level directories: {listed}",
            "parsed",
            confidence=1.0,
            evidence={"file_counts": dict(sorted(top.items()))},
        )
    ]


_TEST_DIR_SEGMENTS = ("tests", "test", "__tests__", "spec")


def testing_conventions(repo_id: str, commit_sha: str, snap: _Snapshot) -> list[Convention]:
    rows: list[Convention] = []
    test_paths = [
        p
        for p in snap.file_paths
        if any(seg in p.split("/")[:-1] for seg in _TEST_DIR_SEGMENTS)
        or re.search(r"(^|/)(test_[^/]+\.py|[^/]+_test\.py)$", p)
        or re.search(r"\.(test|spec)\.[tj]sx?$", p)
    ]
    if not test_paths:
        return rows

    dirs = Counter(p.rsplit("/", 1)[0] if "/" in p else "" for p in test_paths)
    rows.append(
        _row(
            repo_id,
            commit_sha,
            "testing",
            "Tests live under: " + ", ".join(f"{d or '(root)'}/" for d, _ in sorted(dirs.items())),
            "parsed",
            confidence=1.0,
            evidence={"test_file_count": len(test_paths), "dir_counts": dict(sorted(dirs.items()))},
        )
    )

    patterns = Counter[str]()
    for path in test_paths:
        name = path.rsplit("/", 1)[-1]
        if re.match(r"^test_.+\.py$", name):
            patterns["test_*.py"] += 1
        elif re.match(r"^.+_test\.py$", name):
            patterns["*_test.py"] += 1
        elif re.search(r"\.test\.[tj]sx?$", name):
            patterns["*.test.ts(x)/js(x)"] += 1
        elif re.search(r"\.spec\.[tj]sx?$", name):
            patterns["*.spec.ts(x)/js(x)"] += 1
    if patterns:
        label, hits = patterns.most_common(1)[0]
        rows.append(
            _row(
                repo_id,
                commit_sha,
                "testing",
                f"Test files are named {label}",
                "parsed",
                confidence=round(hits / len(test_paths), 3),
                evidence={"pattern_counts": dict(patterns), "test_file_count": len(test_paths)},
            )
        )
    return rows


# --- configured: read out of config files ------------------------------------


def _ruff_rules(repo_id: str, commit_sha: str, path: str, data: dict) -> list[Convention]:
    rows: list[Convention] = []
    ruff = data.get("tool", {}).get("ruff") if path.endswith("pyproject.toml") else data
    if not isinstance(ruff, dict):
        return rows
    line_length = ruff.get("line-length")
    if isinstance(line_length, int):
        rows.append(
            _row(
                repo_id,
                commit_sha,
                "lint",
                f"Ruff enforces a maximum line length of {line_length}",
                "configured",
                evidence={"source": path, "key": "line-length"},
            )
        )
    target = ruff.get("target-version")
    if isinstance(target, str):
        rows.append(
            _row(
                repo_id,
                commit_sha,
                "lint",
                f"Ruff targets Python {target}",
                "configured",
                evidence={"source": path, "key": "target-version"},
            )
        )
    select = ruff.get("lint", {}).get("select") if isinstance(ruff.get("lint"), dict) else None
    if select is None:
        select = ruff.get("select")
    if isinstance(select, list) and all(isinstance(s, str) for s in select):
        rows.append(
            _row(
                repo_id,
                commit_sha,
                "lint",
                "Ruff lint rule sets enabled: " + ", ".join(select),
                "configured",
                evidence={"source": path, "key": "lint.select"},
            )
        )
    return rows


def configured_conventions(repo_id: str, commit_sha: str, snap: _Snapshot) -> list[Convention]:
    rows: list[Convention] = []
    for path, content in sorted(snap.blobs.items()):
        name = path.rsplit("/", 1)[-1]

        if name in ("pyproject.toml", "ruff.toml", ".ruff.toml"):
            try:
                data = tomllib.loads(content)
            except tomllib.TOMLDecodeError:
                # A malformed manifest is a gap, not a guess.
                continue
            rows.extend(_ruff_rules(repo_id, commit_sha, path, data))
            if name == "pyproject.toml":
                pytest_cfg = data.get("tool", {}).get("pytest", {})
                ini = pytest_cfg.get("ini_options") if isinstance(pytest_cfg, dict) else None
                if isinstance(ini, dict) and isinstance(ini.get("testpaths"), list):
                    paths = [p for p in ini["testpaths"] if isinstance(p, str)]
                    if paths:
                        rows.append(
                            _row(
                                repo_id,
                                commit_sha,
                                "testing",
                                "pytest is configured to collect tests from: "
                                + ", ".join(paths),
                                "configured",
                                evidence={
                                    "source": path,
                                    "key": "tool.pytest.ini_options.testpaths",
                                },
                            )
                        )
            continue

        if name == "tsconfig.json":
            try:
                data = json.loads(content)
            except ValueError:
                continue
            options = data.get("compilerOptions") if isinstance(data, dict) else None
            if isinstance(options, dict) and options.get("strict") is True:
                rows.append(
                    _row(
                        repo_id,
                        commit_sha,
                        "lint",
                        "TypeScript runs in strict mode",
                        "configured",
                        evidence={"source": path, "key": "compilerOptions.strict"},
                    )
                )
            continue

        if path.startswith(".github/workflows/"):
            # The commands CI actually runs are the enforced rules. Read as
            # literal `run:` lines -- no YAML dependency, and no execution.
            commands = sorted(
                {
                    m.group(1).strip()
                    for m in re.finditer(r"^\s*-?\s*run:\s*(.+)$", content, re.MULTILINE)
                    if m.group(1).strip() and not m.group(1).strip().startswith("|")
                }
            )[:20]
            if commands:
                rows.append(
                    _row(
                        repo_id,
                        commit_sha,
                        "ci",
                        f"CI workflow {path} runs: " + "; ".join(commands),
                        "configured",
                        evidence={"source": path, "command_count": len(commands)},
                    )
                )
            continue

        if name in _LINT_CONFIG_NAMES:
            rows.append(
                _row(
                    repo_id,
                    commit_sha,
                    "lint",
                    f"Repository is linted/formatted according to {path}",
                    "configured",
                    evidence={"source": path},
                )
            )
    return rows


# --- inferred: the bounded model fallback ------------------------------------


def _fallback(
    repo_id: str,
    commit_sha: str,
    snap: _Snapshot,
    service: ModelService,
    context: CallContext | None,
) -> list[Convention]:
    """One `synth` call, used only when the rules found no naming/structure
    convention. Rows are marked `inferred` and carry no confidence."""
    paths = snap.file_paths[:_MAX_FALLBACK_PATHS]
    symbols = [n for n, _ in snap.symbols][:_MAX_FALLBACK_SYMBOLS]
    if not paths:
        return []
    blocks = ["File paths:\n" + "\n".join(paths)]
    if symbols:
        blocks.append("Symbol names:\n" + "\n".join(symbols))
    prompt = material_prompt(CONVENTIONS_SYSTEM, blocks)
    try:
        guess = service.generate_structured(ConventionGuess, prompt, "synth", context=context)
    except ModelServiceError as exc:
        logger.warning("conventions fallback failed for repo %s: %s", repo_id, exc)
        return []
    rows: list[Convention] = []
    for category, rules in (("naming", guess.naming), ("structure", guess.structure)):
        for rule in rules:
            text = rule.strip()
            if text:
                rows.append(
                    _row(
                        repo_id,
                        commit_sha,
                        category,
                        text[:500],
                        "inferred",
                        evidence={
                            "generalized_from": {
                                "file_paths": len(paths),
                                "symbol_names": len(symbols),
                            }
                        },
                    )
                )
    return rows


# --- driver ------------------------------------------------------------------


def write_conventions_pass(
    conn: psycopg.Connection,
    repo_id: str,
    commit_sha: str,
    service: ModelService | None = None,
    *,
    context: CallContext | None = None,
) -> list[Convention]:
    """Extract and persist this snapshot's conventions. Deterministic rules
    run always; the single-call model fallback runs only when they found no
    naming or structure rule *and* a `service` was supplied."""
    snap = _load(conn, repo_id, commit_sha)
    rows: list[Convention] = []
    rows.extend(configured_conventions(repo_id, commit_sha, snap))
    rows.extend(naming_conventions(repo_id, commit_sha, snap))
    rows.extend(structure_conventions(repo_id, commit_sha, snap))
    rows.extend(testing_conventions(repo_id, commit_sha, snap))

    have = {r.category for r in rows}
    if service is not None and not ({"naming", "structure"} & have):
        rows.extend(_fallback(repo_id, commit_sha, snap, service, context))

    # De-duplicate on the table's unique key, keeping the first (and so the
    # most authoritative: configured rows are generated before parsed ones).
    seen: set[tuple[str, str]] = set()
    unique: list[Convention] = []
    for row in rows:
        key = (row.category, row.rule)
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    if unique:
        upsert_conventions(conn, unique)
    return unique
