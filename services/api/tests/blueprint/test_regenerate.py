"""Partial regeneration: the neighborhood definition, the hard lock rule, and
byte-identical preservation of what was kept (build-plan.md Part 4.G.9).
Pure functions only — no database, no model.
"""

from __future__ import annotations

from app.blueprint.generate import DraftOutcome
from app.blueprint.regenerate import (
    FROZEN_KEY_COLLISION,
    LOCKED_COMPONENT_IN_SCOPE,
    LOCKED_CONNECTION_IN_SCOPE,
    LOCKED_FILE_IN_SCOPE,
    NO_SEEDS,
    REGENERATING_EDITED,
    UNKNOWN_SEED,
    _merge,
    affected_neighborhood,
    carry_component,
    check_locks,
)
from app.models import (
    BlueprintComponent,
    BlueprintConnection,
    BlueprintInterface,
    Citation,
    ContextEvidence,
    ProposedFile,
)

REPO = "11111111-1111-1111-1111-111111111111"
SHA = "cafebabe"


def comp(key: str, *, locked: bool = False, edited: bool = False, **over) -> BlueprintComponent:
    status = "locked" if locked else ("edited" if edited else "generated")
    over.setdefault("kind", "proposed")
    over.setdefault("mapping", "new")
    return BlueprintComponent(
        key=key,
        name=key,
        repo_id=REPO,
        commit_sha=SHA,
        generation_status=status,
        locked=locked,
        **over,
    )


def conn(key: str, source: str, target: str, *, kind: str = "dependency", locked: bool = False):
    return BlueprintConnection(
        key=key,
        kind=kind,  # pyright: ignore[reportArgumentType]
        source_key=source,
        target_key=target,
        locked=locked,
    )


def codes(errors) -> list[str]:
    return [e.code for e in errors]


# --- the neighborhood ---------------------------------------------------------


def test_the_neighborhood_is_the_seeds_plus_one_hop_in_either_direction():
    components = [comp(k) for k in ("a", "b", "c", "d")]
    connections = [conn("e1", "a", "b"), conn("e2", "c", "a"), conn("e3", "c", "d")]
    plan = affected_neighborhood(components, connections, ["a"])
    assert plan.seeds == {"a"}
    assert plan.regenerate == {"a", "b", "c"}
    assert plan.frozen == {"d"}


def test_the_neighborhood_stops_at_one_hop():
    components = [comp(k) for k in ("a", "b", "c")]
    connections = [conn("e1", "a", "b"), conn("e2", "b", "c")]
    plan = affected_neighborhood(components, connections, ["a"])
    assert plan.regenerate == {"a", "b"}
    assert plan.frozen == {"c"}


def test_every_connection_kind_counts_as_a_hop():
    components = [comp("a"), comp("b")]
    for kind in ("dependency", "control_flow", "data_flow", "event"):
        plan = affected_neighborhood(components, [conn("e1", "a", "b", kind=kind)], ["a"])
        assert plan.regenerate == {"a", "b"}, kind


def test_a_locked_neighbour_is_held_out_of_the_regenerated_set():
    components = [comp("a"), comp("b", locked=True)]
    plan = affected_neighborhood(components, [conn("e1", "a", "b")], ["a"])
    assert plan.regenerate == {"a"}
    assert plan.frozen == {"b"}


def test_an_unknown_seed_is_not_silently_ignored():
    rejections, _ = check_locks(
        [comp("a")], [], [], ["ghost"], affected_neighborhood([comp("a")], [], ["ghost"])
    )
    assert codes(rejections) == [UNKNOWN_SEED]


def test_no_seeds_at_all_is_a_rejection():
    rejections, _ = check_locks([comp("a")], [], [], [], affected_neighborhood([comp("a")], [], []))
    assert codes(rejections) == [NO_SEEDS]


# --- locks are hard -----------------------------------------------------------


def test_a_locked_seed_is_rejected_not_skipped():
    components = [comp("a", locked=True)]
    plan = affected_neighborhood(components, [], ["a"])
    rejections, _ = check_locks(components, [], [], ["a"], plan)
    assert codes(rejections) == [LOCKED_COMPONENT_IN_SCOPE]


def test_a_locked_connection_touching_the_scope_is_rejected():
    components = [comp("a"), comp("b")]
    connections = [conn("e1", "a", "b", locked=True)]
    plan = affected_neighborhood(components, connections, ["a"])
    rejections, _ = check_locks(components, connections, [], ["a"], plan)
    assert codes(rejections) == [LOCKED_CONNECTION_IN_SCOPE]


def test_a_locked_file_implementing_a_regenerated_component_is_rejected():
    components = [comp("a")]
    files = [
        ProposedFile(
            path="svc/a.py",
            status="new",
            purpose="p",
            implements_component_key="a",
            locked=True,
        )
    ]
    plan = affected_neighborhood(components, [], ["a"])
    rejections, _ = check_locks(components, [], files, ["a"], plan)
    assert codes(rejections) == [LOCKED_FILE_IN_SCOPE]


def test_a_locked_file_outside_the_scope_is_not_a_rejection():
    components = [comp("a"), comp("b")]
    files = [
        ProposedFile(
            path="svc/b.py",
            status="new",
            purpose="p",
            implements_component_key="b",
            locked=True,
        )
    ]
    plan = affected_neighborhood(components, [], ["a"])
    rejections, _ = check_locks(components, [], files, ["a"], plan)
    assert rejections == []


def test_regenerating_an_edited_component_is_advised_not_blocked():
    components = [comp("a", edited=True)]
    plan = affected_neighborhood(components, [], ["a"])
    rejections, advisories = check_locks(components, [], [], ["a"], plan)
    assert rejections == []
    assert codes(advisories) == [REGENERATING_EDITED]


# --- preservation -------------------------------------------------------------


def test_a_carried_component_differs_only_in_row_identity():
    stored = comp(
        "kept",
        id="55555555-5555-5555-5555-555555555555",
        blueprint_version_id="66666666-6666-6666-6666-666666666666",
        kind="existing",
        mapping="reuse",
        linked_component_id=901,
        module_key="orders",
        responsibility="Stores orders.",
        interfaces=[BlueprintInterface(name="save", kind="function", signature="save(o)")],
        confidence=0.75,
        evidence=ContextEvidence(
            spans=[Citation(path="svc/store.py", line_start=1, line_end=9)], node_ids=[901]
        ),
    )
    carried = carry_component(stored)
    assert carried.id is None
    assert carried.blueprint_version_id is None
    assert carried.created_at is None
    ignored = {"id", "blueprint_version_id", "created_at"}
    before = stored.model_dump(exclude=ignored)  # pyright: ignore[reportArgumentType]
    after = carried.model_dump(exclude=ignored)  # pyright: ignore[reportArgumentType]
    assert before == after


def test_merge_keeps_frozen_rows_and_takes_regenerated_ones_from_the_draft():
    stored_components = [comp("a"), comp("b"), comp("kept")]
    stored_connections = [conn("e1", "a", "b"), conn("e2", "kept", "kept2")]
    stored_files = [
        ProposedFile(path="a.py", status="new", purpose="p", implements_component_key="a"),
        ProposedFile(path="k.py", status="new", purpose="p", implements_component_key="kept"),
    ]
    plan = affected_neighborhood(stored_components, stored_connections, ["a"])
    assert plan.frozen == {"kept"}
    design = DraftOutcome(components=[comp("a"), comp("b2")], connections=[conn("n1", "a", "b2")])
    new_files = [
        ProposedFile(path="a2.py", status="new", purpose="p", implements_component_key="a")
    ]
    components, connections, files, advisories = _merge(
        plan, stored_components, stored_connections, stored_files, design, new_files
    )
    assert sorted(c.key for c in components) == ["a", "b2", "kept"]
    # 'b' was in the regenerated neighborhood and the draft dropped it.
    assert sorted(c.key for c in connections) == ["n1"]
    assert sorted(f.path for f in files) == ["a2.py", "k.py"]
    assert advisories == []


def test_a_draft_reusing_a_frozen_key_loses_to_the_frozen_row():
    stored_components = [comp("a"), comp("kept", responsibility="original")]
    plan = affected_neighborhood(stored_components, [], ["a"])
    design = DraftOutcome(components=[comp("kept", responsibility="rewritten")])
    components, _c, _f, advisories = _merge(plan, stored_components, [], [], design, [])
    kept = next(c for c in components if c.key == "kept")
    assert kept.responsibility == "original"
    assert codes(advisories) == [FROZEN_KEY_COLLISION]


def test_a_connection_between_two_frozen_components_is_carried_over():
    stored_components = [comp("a"), comp("x"), comp("y")]
    stored_connections = [conn("e1", "x", "y")]
    plan = affected_neighborhood(stored_components, stored_connections, ["a"])
    _c, connections, _f, _a = _merge(
        plan, stored_components, stored_connections, [], DraftOutcome(components=[comp("a")]), []
    )
    assert [c.key for c in connections] == ["e1"]
