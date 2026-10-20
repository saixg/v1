"""The blueprint validator's deterministic half (build-plan.md Part 4.G.5):
key uniqueness, endpoint resolution, dependency cycles, the evidence
requirement, schema version, and ledger overlap. No database, no model —
every case is a constructed set of rows.

`check_grounding` is the one part that needs a snapshot and is covered in
`test_blueprint_db.py`.
"""

from __future__ import annotations

from app.blueprint.validate import (
    BLOCKING_CODES,
    CONNECTION_SOURCE_MISSING,
    CONNECTION_TARGET_MISSING,
    DEPENDENCY_CYCLE,
    DUPLICATE_COMPONENT_KEY,
    DUPLICATE_CONNECTION_KEY,
    DUPLICATE_FILE_PATH,
    EMPTY_BLUEPRINT,
    HARD_CONSTRAINT_CONFLICT,
    IMPLEMENTS_COMPONENT_MISSING,
    REPO_WIDE_CONSTRAINT_OVERLAP,
    SOFT_CONSTRAINT_CONFLICT,
    UNKNOWN_SCHEMA_VERSION,
    UNSUPPORTED_EXISTING_CLAIM,
    VersionRows,
    check_constraints,
    check_structure,
    touched_paths,
)
from app.models import (
    Blueprint,
    BlueprintComponent,
    BlueprintConnection,
    BlueprintVersion,
    Citation,
    Constraint,
    ContextEvidence,
    ProposedFile,
)

REPO = "11111111-1111-1111-1111-111111111111"
PROJECT = "22222222-2222-2222-2222-222222222222"
TASK = "33333333-3333-3333-3333-333333333333"
SHA = "deadbeef"


def blueprint(**over) -> Blueprint:
    return Blueprint(
        id="44444444-4444-4444-4444-444444444444",
        project_id=PROJECT,
        repo_id=REPO,
        commit_sha=SHA,
        task_id=TASK,
        title="Add order export",
        **over,
    )


def version(**over) -> BlueprintVersion:
    return BlueprintVersion(
        blueprint_id="44444444-4444-4444-4444-444444444444",
        version=1,
        analyzer="blueprint.generate",
        analyzer_version="0.1.0",
        **over,
    )


def proposed(key: str, **over) -> BlueprintComponent:
    return BlueprintComponent(
        key=key, name=key, kind="proposed", mapping="new", repo_id=REPO, commit_sha=SHA, **over
    )


def existing(key: str, node_id: int = 7, **over) -> BlueprintComponent:
    over.setdefault("evidence", ContextEvidence(node_ids=[node_id]))
    return BlueprintComponent(
        key=key,
        name=key,
        kind="existing",
        mapping="reuse",
        linked_component_id=node_id,
        repo_id=REPO,
        commit_sha=SHA,
        **over,
    )


def dep(key: str, source: str, target: str, kind: str = "dependency") -> BlueprintConnection:
    return BlueprintConnection(
        key=key,
        kind=kind,  # pyright: ignore[reportArgumentType]
        source_key=source,
        target_key=target,
    )


def rows(**over) -> VersionRows:
    return VersionRows(
        blueprint=over.pop("blueprint", blueprint()),
        version=over.pop("version", version()),
        components=over.pop("components", []),
        connections=over.pop("connections", []),
        proposed_files=over.pop("proposed_files", []),
    )


def codes(errors) -> list[str]:
    return [e.code for e in errors]


# --- schema version ----------------------------------------------------------


def test_known_schema_version_is_accepted():
    assert check_structure(rows(components=[proposed("a")])) == []


def test_a_version_with_no_components_is_blocking_invalid():
    """A schema-valid but empty model reply resolves to zero components
    (every field has a default), and every other structural check passes
    trivially on it. Without this rule such a version would validate clean
    and be promoted, silently replacing a good design with a blank one."""
    found = check_structure(rows(components=[]))
    assert EMPTY_BLUEPRINT in codes(found)
    assert EMPTY_BLUEPRINT in BLOCKING_CODES


def test_unknown_schema_version_is_rejected_not_coerced():
    found = check_structure(
        rows(version=version(schema_version="2.0.0"), components=[proposed("a")])
    )
    assert codes(found) == [UNKNOWN_SCHEMA_VERSION]
    assert UNKNOWN_SCHEMA_VERSION in BLOCKING_CODES


# --- uniqueness --------------------------------------------------------------


def test_duplicate_keys_are_reported_per_namespace():
    found = check_structure(
        rows(
            components=[proposed("a"), proposed("b"), proposed("a")],
            connections=[dep("e1", "a", "b"), dep("e1", "b", "a", kind="event")],
            proposed_files=[
                ProposedFile(path="x.py", status="new", purpose="p"),
                ProposedFile(path="x.py", status="new", purpose="p"),
            ],
        )
    )
    assert sorted(codes(found)) == sorted(
        [DUPLICATE_COMPONENT_KEY, DUPLICATE_CONNECTION_KEY, DUPLICATE_FILE_PATH]
    )


def test_a_connection_may_share_a_key_with_a_component():
    found = check_structure(
        rows(components=[proposed("a"), proposed("b")], connections=[dep("a", "a", "b")])
    )
    assert found == []


# --- endpoints ---------------------------------------------------------------


def test_dangling_endpoints_are_reported_with_the_connection_key():
    found = check_structure(
        rows(components=[proposed("a")], connections=[dep("e1", "ghost", "a")])
    )
    assert codes(found) == [CONNECTION_SOURCE_MISSING]
    assert found[0].entity_key == "e1"


def test_both_endpoints_missing_is_two_errors():
    # Carries an unrelated component so this exercises dangling endpoints
    # only: a version with none at all is separately invalid (EMPTY_BLUEPRINT).
    found = check_structure(
        rows(components=[proposed("unrelated")], connections=[dep("e1", "x", "y")])
    )
    assert codes(found) == [CONNECTION_SOURCE_MISSING, CONNECTION_TARGET_MISSING]


# --- cycles ------------------------------------------------------------------


def test_dependency_cycle_names_its_members():
    found = check_structure(
        rows(
            components=[proposed("a"), proposed("b"), proposed("c")],
            connections=[dep("e1", "a", "b"), dep("e2", "b", "c"), dep("e3", "c", "a")],
        )
    )
    assert codes(found) == [DEPENDENCY_CYCLE]
    for key in ("a", "b", "c"):
        assert key in found[0].message


def test_a_dag_of_dependencies_is_fine():
    found = check_structure(
        rows(
            components=[proposed("a"), proposed("b"), proposed("c")],
            connections=[dep("e1", "a", "b"), dep("e2", "b", "c"), dep("e3", "a", "c")],
        )
    )
    assert found == []


def test_other_connection_kinds_may_loop():
    for kind in ("control_flow", "data_flow", "event"):
        found = check_structure(
            rows(
                components=[proposed("a"), proposed("b")],
                connections=[dep("e1", "a", "b", kind=kind), dep("e2", "b", "a", kind=kind)],
            )
        )
        assert found == [], kind


def test_two_separate_cycles_are_reported_separately():
    found = check_structure(
        rows(
            components=[proposed(k) for k in ("a", "b", "c", "d")],
            connections=[
                dep("e1", "a", "b"),
                dep("e2", "b", "a"),
                dep("e3", "c", "d"),
                dep("e4", "d", "c"),
            ],
        )
    )
    assert codes(found) == [DEPENDENCY_CYCLE, DEPENDENCY_CYCLE]
    assert [e.entity_key for e in found] == ["a", "c"]


def test_a_long_dependency_chain_does_not_blow_the_stack():
    keys = [f"c{n}" for n in range(400)]
    found = check_structure(
        rows(
            components=[proposed(k) for k in keys],
            connections=[
                dep(f"e{n}", keys[n], keys[n + 1]) for n in range(len(keys) - 1)
            ],
        )
    )
    assert found == []


# --- evidence and intra-version references -----------------------------------


def test_an_existing_component_without_evidence_is_an_unsupported_claim():
    found = check_structure(
        rows(components=[existing("a", evidence=ContextEvidence())])
    )
    assert codes(found) == [UNSUPPORTED_EXISTING_CLAIM]


def test_spans_alone_satisfy_the_evidence_requirement():
    component = existing(
        "a",
        evidence=ContextEvidence(
            spans=[Citation(path="svc/orders.py", line_start=1, line_end=9)]
        ),
    )
    assert check_structure(rows(components=[component])) == []


def test_a_proposed_component_needs_no_evidence():
    assert check_structure(rows(components=[proposed("a")])) == []


def test_implements_component_key_must_resolve():
    found = check_structure(
        rows(
            components=[proposed("a")],
            proposed_files=[
                ProposedFile(
                    path="x.py", status="new", purpose="p", implements_component_key="ghost"
                )
            ],
        )
    )
    assert codes(found) == [IMPLEMENTS_COMPONENT_MISSING]


# --- the ledger --------------------------------------------------------------


def constraint(**over) -> Constraint:
    base = {
        "repo_id": REPO,
        "commit_sha": SHA,
        "kind": "do_not_touch",
        "scope": "vendor/",
        "statement": "vendored code is updated from upstream",
        "severity": "hard",
        "source": "auto_detected",
        "origin": "parsed",
        "analyzer": "context.ledger",
        "analyzer_version": "0.1.0",
    }
    base.update(over)
    return Constraint(**base)  # pyright: ignore[reportArgumentType]


def test_touched_paths_covers_files_and_mapped_component_evidence():
    found = touched_paths(
        rows(
            components=[
                existing(
                    "a",
                    evidence=ContextEvidence(
                        spans=[Citation(path="svc/orders.py", line_start=1, line_end=4)]
                    ),
                ),
                proposed("b"),
            ],
            proposed_files=[ProposedFile(path="svc/export.py", status="new", purpose="p")],
        )
    )
    assert set(found) == {"svc/orders.py", "svc/export.py"}


def test_a_hard_do_not_touch_overlap_blocks():
    found = check_constraints(
        rows(proposed_files=[ProposedFile(path="vendor/lib/a.py", status="modified", purpose="p")]),
        [constraint()],
    )
    assert codes(found) == [HARD_CONSTRAINT_CONFLICT]
    assert HARD_CONSTRAINT_CONFLICT in BLOCKING_CODES


def test_a_soft_overlap_is_reported_but_not_blocking():
    found = check_constraints(
        rows(proposed_files=[ProposedFile(path="vendor/lib/a.py", status="modified", purpose="p")]),
        [constraint(severity="soft")],
    )
    assert codes(found) == [SOFT_CONSTRAINT_CONFLICT]
    assert SOFT_CONSTRAINT_CONFLICT not in BLOCKING_CODES


def test_a_repo_wide_scope_never_blocks():
    found = check_constraints(
        rows(proposed_files=[ProposedFile(path="a.py", status="new", purpose="p")]),
        [constraint(scope="repo")],
    )
    assert codes(found) == [REPO_WIDE_CONSTRAINT_OVERLAP]
    assert REPO_WIDE_CONSTRAINT_OVERLAP not in BLOCKING_CODES


def test_a_ci_lint_constraint_is_not_a_placement_conflict():
    assert (
        check_constraints(
            rows(proposed_files=[ProposedFile(path="a.py", status="new", purpose="p")]),
            [constraint(kind="ci_lint", scope="a.py")],
        )
        == []
    )


def test_listed_spans_narrow_a_directory_scope_to_those_files():
    # The ledger's `do_not_touch` row on existing migrations lists exactly the
    # files it protects, so a *new* migration beside them is not a conflict.
    protected = constraint(
        scope="db/migrations/",
        evidence=ContextEvidence(
            spans=[Citation(path="db/migrations/0001_init.sql", line_start=1, line_end=1)]
        ),
    )
    added = check_constraints(
        rows(
            proposed_files=[
                ProposedFile(path="db/migrations/0002_orders.sql", status="new", purpose="p")
            ]
        ),
        [protected],
    )
    assert added == []
    edited = check_constraints(
        rows(
            proposed_files=[
                ProposedFile(path="db/migrations/0001_init.sql", status="modified", purpose="p")
            ]
        ),
        [protected],
    )
    assert codes(edited) == [HARD_CONSTRAINT_CONFLICT]


def test_a_truncated_span_list_falls_back_to_the_scope():
    protected = constraint(
        scope="db/migrations/",
        evidence=ContextEvidence(
            spans=[Citation(path="db/migrations/0001_init.sql", line_start=1, line_end=1)],
            truncated=True,
        ),
    )
    found = check_constraints(
        rows(
            proposed_files=[
                ProposedFile(path="db/migrations/0002_orders.sql", status="new", purpose="p")
            ]
        ),
        [protected],
    )
    assert codes(found) == [HARD_CONSTRAINT_CONFLICT]


def test_one_report_per_constraint_however_many_paths_match():
    found = check_constraints(
        rows(
            proposed_files=[
                ProposedFile(path="vendor/a.py", status="modified", purpose="p"),
                ProposedFile(path="vendor/b.py", status="modified", purpose="p"),
            ]
        ),
        [constraint()],
    )
    assert len(found) == 1
