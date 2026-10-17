"""Manifest parsing: package.json / pyproject.toml / requirements.txt at
the checkout root produce `dependency` nodes."""

from pathlib import Path

from app.analysis.scan.manifests import extract_manifest_dependencies

REPO_ID = "11111111-1111-1111-1111-111111111111"
COMMIT = "deadbeef"


def test_package_json_dependencies(tmp_path: Path):
    (tmp_path / "package.json").write_text(
        '{"dependencies": {"react": "^18.0.0"}, "devDependencies": {"vitest": "^1.0.0"}}'
    )

    nodes = extract_manifest_dependencies(tmp_path, REPO_ID, COMMIT)
    by_name = {n.name: n for n in nodes}

    assert by_name["react"].kind == "dependency"
    assert by_name["react"].attrs == {
        "version": "^18.0.0",
        "ecosystem": "npm",
        "manifest_path": "package.json",
    }
    assert by_name["vitest"].attrs["ecosystem"] == "npm"


def test_pyproject_pep621_dependencies(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "x"\ndependencies = ["fastapi>=0.100", "psycopg"]\n'
    )

    nodes = extract_manifest_dependencies(tmp_path, REPO_ID, COMMIT)
    by_name = {n.name: n for n in nodes}

    assert by_name["fastapi"].attrs["ecosystem"] == "pypi"
    assert by_name["fastapi"].attrs["version"] == ">=0.100"
    assert by_name["psycopg"].attrs["version"] is None


def test_pyproject_poetry_fallback(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.poetry.dependencies]\npython = "^3.12"\nrequests = "^2.0"\n'
    )

    nodes = extract_manifest_dependencies(tmp_path, REPO_ID, COMMIT)
    by_name = {n.name: n for n in nodes}

    assert "python" not in by_name
    assert by_name["requests"].attrs["version"] == "^2.0"


def test_requirements_txt(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text(
        "# a comment\nrequests==2.31.0\nnumpy>=1.0\n\n-e ./local-pkg\n"
    )

    nodes = extract_manifest_dependencies(tmp_path, REPO_ID, COMMIT)
    by_name = {n.name: n for n in nodes}

    assert by_name["requests"].attrs["version"] == "==2.31.0"
    assert by_name["numpy"].attrs["version"] == ">=1.0"
    assert len(nodes) == 2  # the "-e ./local-pkg" line is skipped


def test_no_manifests_returns_empty(tmp_path: Path):
    assert extract_manifest_dependencies(tmp_path, REPO_ID, COMMIT) == []
