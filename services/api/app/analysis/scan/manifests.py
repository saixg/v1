"""Manifest parsing (build-plan.md Part 4.B): declared third-party packages
become `dependency` nodes -- not edges, there is nothing in the repo to
point them at. No DB, no network, no exec: files are read and parsed as
plain text/JSON/TOML, never imported or installed.

V1 cut: root-level manifests only (`package.json`, `pyproject.toml`,
`requirements.txt` directly under the checkout root), not nested ones in
sub-packages/monorepo workspaces. `pyproject.toml` handles the PEP 621
`[project.dependencies]` array of strings and, as a fallback, Poetry's
`[tool.poetry.dependencies]` table.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

from app.models import Node

from .scanner import ANALYZER, ANALYZER_VERSION

_REQUIREMENT_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)")


def extract_manifest_dependencies(root: Path, repo_id: str, commit_sha: str) -> list[Node]:
    """Parse whichever root-level manifests exist under `root` and return
    one `dependency` Node per declared package. Never raises on a malformed
    manifest -- a parse error just means that manifest contributes no
    dependency nodes, same "never fail the pass" discipline as the scanner."""
    nodes: list[Node] = []
    nodes.extend(_from_package_json(root, repo_id, commit_sha))
    nodes.extend(_from_pyproject_toml(root, repo_id, commit_sha))
    nodes.extend(_from_requirements_txt(root, repo_id, commit_sha))
    return nodes


def _dependency_node(
    name: str,
    version: str | None,
    ecosystem: str,
    manifest_path: str,
    repo_id: str,
    commit_sha: str,
) -> Node:
    return Node(
        repo_id=repo_id,
        commit_sha=commit_sha,
        kind="dependency",
        name=name,
        attrs={"version": version, "ecosystem": ecosystem, "manifest_path": manifest_path},
        analyzer=ANALYZER,
        analyzer_version=ANALYZER_VERSION,
        status="verified",
        source_file=manifest_path,
    )


def _from_package_json(root: Path, repo_id: str, commit_sha: str) -> list[Node]:
    path = root / "package.json"
    if path.is_symlink() or not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    nodes: list[Node] = []
    for key in ("dependencies", "devDependencies"):
        deps = data.get(key)
        if not isinstance(deps, dict):
            continue
        for name, version in deps.items():
            if not isinstance(name, str):
                continue
            nodes.append(
                _dependency_node(
                    name,
                    str(version) if isinstance(version, str) else None,
                    "npm",
                    "package.json",
                    repo_id,
                    commit_sha,
                )
            )
    return nodes


def _from_pyproject_toml(root: Path, repo_id: str, commit_sha: str) -> list[Node]:
    path = root / "pyproject.toml"
    if path.is_symlink() or not path.is_file():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, OSError):
        return []
    nodes: list[Node] = []

    project_deps = data.get("project", {}).get("dependencies")
    if isinstance(project_deps, list):
        for entry in project_deps:
            if not isinstance(entry, str):
                continue
            match = _REQUIREMENT_NAME_RE.match(entry)
            if not match:
                continue
            name = match.group(1)
            version = entry[match.end() :].strip() or None
            nodes.append(
                _dependency_node(name, version, "pypi", "pyproject.toml", repo_id, commit_sha)
            )

    if not nodes:
        poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies")
        if isinstance(poetry_deps, dict):
            for name, spec in poetry_deps.items():
                if name == "python":
                    continue
                version = spec if isinstance(spec, str) else None
                nodes.append(
                    _dependency_node(name, version, "pypi", "pyproject.toml", repo_id, commit_sha)
                )

    return nodes


def _from_requirements_txt(root: Path, repo_id: str, commit_sha: str) -> list[Node]:
    path = root / "requirements.txt"
    if path.is_symlink() or not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    nodes: list[Node] = []
    for raw_line in lines:
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _REQUIREMENT_NAME_RE.match(line)
        if not match:
            continue
        name = match.group(1)
        version = line[match.end() :].strip() or None
        nodes.append(
            _dependency_node(name, version, "pypi", "requirements.txt", repo_id, commit_sha)
        )
    return nodes
