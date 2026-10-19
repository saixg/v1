"""The file-structure proposal's resolution rules (build-plan.md Part 4.G.7).

`resolve_proposal` reads the snapshot only to decide `new` vs `modified`, so
that one read is stubbed and everything else runs for real.
"""

from __future__ import annotations

import pytest

from app.blueprint import files as files_module
from app.blueprint.files import (
    UNKNOWN_CANDIDATE,
    UNKNOWN_ENDPOINT,
    UNSAFE_PATH,
    DirectoryCandidate,
    FileContext,
    FileProposal,
    _assemble_path,
    build_files_prompt,
    propose_files,
    resolve_proposal,
)
from app.models import Blueprint, BlueprintComponent, Convention

REPO = "11111111-1111-1111-1111-111111111111"
PROJECT = "22222222-2222-2222-2222-222222222222"
TASK = "33333333-3333-3333-3333-333333333333"
SHA = "cafebabe"


def blueprint() -> Blueprint:
    return Blueprint(
        id="44444444-4444-4444-4444-444444444444",
        project_id=PROJECT,
        repo_id=REPO,
        commit_sha=SHA,
        task_id=TASK,
        title="Add order export",
    )


def component(key: str) -> BlueprintComponent:
    return BlueprintComponent(
        key=key,
        name=key,
        kind="proposed",
        mapping="new",
        repo_id=REPO,
        commit_sha=SHA,
        responsibility="Exports orders.",
    )


def convention(cid: int, rule: str) -> Convention:
    return Convention(
        id=cid,
        repo_id=REPO,
        commit_sha=SHA,
        category="structure",
        rule=rule,
        origin="parsed",
        analyzer="semantic.conventions",
        analyzer_version="0.1.0",
    )


def context(**over) -> FileContext:
    return FileContext(
        directories=over.pop(
            "directories",
            [DirectoryCandidate("D0", ""), DirectoryCandidate("D1", "svc")],
        ),
        conventions=over.pop("conventions", [convention(5, "modules live under svc/")]),
        languages=over.pop("languages", [("python", 7)]),
        dependencies=over.pop("dependencies", ["fastapi"]),
        notes=over.pop("notes", []),
    )


def draft(**over) -> FileProposal:
    return FileProposal.model_validate(over)


def item(**over) -> dict:
    return {
        "component_key": "export",
        "directory": "D1",
        "name": "export.py",
        "purpose": "Writes the CSV.",
        "conventions": ["V1"],
        **over,
    }


@pytest.fixture
def no_existing_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files_module, "existing_paths", lambda *a, **k: set())


@pytest.fixture
def all_paths_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files_module, "existing_paths", lambda _c, _r, _s, paths: set(paths))


# --- path assembly ------------------------------------------------------------


def test_paths_are_assembled_from_a_label_and_a_name():
    assert _assemble_path("svc", None, "export.py") == "svc/export.py"
    assert _assemble_path("", None, "export.py") == "export.py"
    assert _assemble_path("svc", "orders/handlers", "run.py") == "svc/orders/handlers/run.py"


@pytest.mark.parametrize(
    "parts",
    [
        ("svc", "..", "x.py"),
        ("svc", None, "../x.py"),
        ("/etc", None, "passwd"),
        ("svc", None, "C:/x.py"),
        ("svc", None, " "),
        ("", None, ""),
    ],
)
def test_traversal_and_absolute_paths_are_refused(parts):
    assert _assemble_path(*parts) is None


# --- resolution ---------------------------------------------------------------


def test_a_file_resolves_its_directory_label_and_convention_refs(no_existing_paths):
    outcome = resolve_proposal(
        None,  # pyright: ignore[reportArgumentType] -- existing_paths is stubbed
        blueprint(),
        draft(files=[item()]),
        [component("export")],
        context(),
    )
    (row,) = outcome.files
    assert row.path == "svc/export.py"
    assert row.status == "new"
    assert row.implements_component_key == "export"
    assert row.conventions_refs == [5]
    assert outcome.dropped == []


def test_the_status_comes_from_the_snapshot_not_the_model(all_paths_exist):
    outcome = resolve_proposal(
        None,  # pyright: ignore[reportArgumentType]
        blueprint(),
        draft(files=[item()]),
        [component("export")],
        context(),
    )
    assert [f.status for f in outcome.files] == ["modified"]


def test_an_unsupplied_directory_label_drops_the_file(no_existing_paths):
    outcome = resolve_proposal(
        None,  # pyright: ignore[reportArgumentType]
        blueprint(),
        draft(files=[item(directory="D9")]),
        [component("export")],
        context(),
    )
    assert outcome.files == []
    assert [d.reason for d in outcome.dropped] == [UNKNOWN_CANDIDATE]


def test_a_file_for_an_unknown_component_is_dropped(no_existing_paths):
    outcome = resolve_proposal(
        None,  # pyright: ignore[reportArgumentType]
        blueprint(),
        draft(files=[item(component_key="ghost")]),
        [component("export")],
        context(),
    )
    assert outcome.files == []
    assert [d.reason for d in outcome.dropped] == [UNKNOWN_ENDPOINT]


def test_a_traversal_path_is_dropped_rather_than_normalised(no_existing_paths):
    outcome = resolve_proposal(
        None,  # pyright: ignore[reportArgumentType]
        blueprint(),
        draft(files=[item(subdirectory="../../etc")]),
        [component("export")],
        context(),
    )
    assert outcome.files == []
    assert [d.reason for d in outcome.dropped] == [UNSAFE_PATH]


def test_an_unknown_convention_label_is_dropped_and_reported(no_existing_paths):
    outcome = resolve_proposal(
        None,  # pyright: ignore[reportArgumentType]
        blueprint(),
        draft(files=[item(conventions=["V1", "V9"])]),
        [component("export")],
        context(),
    )
    (row,) = outcome.files
    assert row.conventions_refs == [5]
    kinds = [u["kind"] for u in outcome.unresolved]
    assert "dropped_convention_citation" in kinds


def test_an_uncited_placement_says_so_rather_than_implying_endorsement(no_existing_paths):
    outcome = resolve_proposal(
        None,  # pyright: ignore[reportArgumentType]
        blueprint(),
        draft(files=[item(conventions=[])]),
        [component("export")],
        context(),
    )
    (row,) = outcome.files
    assert row.conventions_refs == []
    assert [u["kind"] for u in outcome.unresolved] == ["uncited_file_placement"]


def test_a_duplicate_path_is_proposed_once(no_existing_paths):
    outcome = resolve_proposal(
        None,  # pyright: ignore[reportArgumentType]
        blueprint(),
        draft(files=[item(), item()]),
        [component("export")],
        context(),
    )
    assert len(outcome.files) == 1
    assert len(outcome.dropped) == 1


# --- the prompt and the no-component shortcut ---------------------------------


def test_the_prompt_labels_directories_and_conventions():
    prompt = build_files_prompt([component("export")], context())
    assert prompt.system is not None and "untrusted data" in prompt.system
    assert "D1: svc" in prompt.user
    assert "V1: [structure/parsed] modules live under svc/" in prompt.user
    assert "BEGIN DATA" in prompt.user


def test_no_components_means_no_model_call():
    class Boom:
        def generate_structured(self, *a, **k):  # pragma: no cover - must not run
            raise AssertionError("a model was called for an empty design")

    outcome = propose_files(
        None,  # pyright: ignore[reportArgumentType]
        Boom(),  # pyright: ignore[reportArgumentType]
        blueprint(),
        [],
    )
    assert outcome.files == []
    assert [u["kind"] for u in outcome.unresolved] == ["no_files_proposed"]
