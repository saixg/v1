"""Requirement validation: grounding drops, duplicates, acceptance links,
gaps, the deterministic do-not-touch check, and the model-assisted
contradiction fallback (whose own output is validated). No database."""

from __future__ import annotations

import json

import pytest

from app.context.intake import prepare_intake
from app.context.validate import (
    QUOTE_NOT_IN_SOURCE,
    SOURCE_NOT_FOUND,
    DraftRequirement,
    check_acceptance_links,
    check_grounding,
    dedupe,
    find_constraint_conflicts,
    find_statement_contradictions,
    gap_questions,
    mentioned_paths,
    path_in_scope,
)
from app.llm import Completion, CompletionProvider, CompletionRequest, RoutedModelService
from app.models import Citation, Constraint, ContextEvidence

TEXT = "Add rate limiting to the chat endpoint.\n\nLimit is ten requests per minute."
CONSTRAINT_ID = "00000000-0000-0000-0000-000000000001"


def draft(key: str, kind: str = "functional", **kw) -> DraftRequirement:
    kw.setdefault("statement", f"statement for {key} is long enough")
    return DraftRequirement(key=key, kind=kind, **kw)  # pyright: ignore[reportArgumentType]


def do_not_touch(scope: str, *, hard: bool = True, spans: list[str] | None = None) -> Constraint:
    return Constraint(
        id=CONSTRAINT_ID,
        repo_id="r",
        commit_sha="c",
        kind="do_not_touch",
        scope=scope,
        statement="x",
        severity="hard" if hard else "soft",
        source="auto_detected",
        origin="parsed",
        analyzer="t",
        analyzer_version="0",
        evidence=ContextEvidence(
            spans=[Citation(path=p, line_start=1, line_end=1) for p in spans or []]
        ),
    )


class OneShot(CompletionProvider):
    name = "fake"

    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload)
        self.requests: list[CompletionRequest] = []

    def complete(self, request: CompletionRequest) -> Completion:
        self.requests.append(request)
        return Completion(
            text=self.payload, stop_reason="end_turn", served_model="m",
            input_tokens=1, output_tokens=1,
        )


def test_grounding_keeps_resolvable_quote_and_drops_the_rest() -> None:
    chunks = prepare_intake(TEXT).chunks()
    good = draft("R1", chunk_id="T.1", quote="Add  rate limiting to the CHAT endpoint")
    bad_chunk = draft("R2", chunk_id="T.99", quote="Limit is ten requests per minute")
    bad_quote = draft("R3", chunk_id="T.2", quote="Limit is five requests per minute")
    no_ref = draft("R4", quote="Limit is ten requests per minute")
    kept, dropped = check_grounding([good, bad_chunk, bad_quote, no_ref], chunks)
    assert [d.key for d in kept] == ["R1"]
    reasons = {d.key: d.reason for d in dropped}
    assert reasons["R2"] == SOURCE_NOT_FOUND
    assert reasons["R3"] == QUOTE_NOT_IN_SOURCE
    assert reasons["R4"] == "missing_source_ref"
    # The report never echoes the (untrusted) statement text.
    assert all("statement for" not in d.detail for d in dropped)


def test_open_question_may_omit_chunk_but_not_name_a_fake_one() -> None:
    chunks = prepare_intake(TEXT).chunks()
    kept, dropped = check_grounding(
        [draft("Q1", "open_question"), draft("Q2", "open_question", chunk_id="nope")], chunks
    )
    assert [d.key for d in kept] == ["Q1"] and dropped[0].reason == SOURCE_NOT_FOUND


def test_dedupe_drops_exact_and_flags_near_duplicates() -> None:
    a = draft("R1", statement="Reject the eleventh request with HTTP 429.")
    b = draft("R2", statement="reject the eleventh  request with http 429.")
    c = draft("R3", statement="Reject the eleventh request with HTTP 429!")
    kept, dropped = dedupe([a, b, c])
    assert [d.key for d in kept] == ["R1", "R3"]
    assert dropped[0].reason == "duplicate"
    assert kept[1].status == "ambiguous" and "R1" in (kept[1].question_text() or "")


def test_acceptance_criterion_referencing_nothing_is_ambiguous_not_dropped() -> None:
    req = draft("R1")
    ok = draft("R2", "acceptance_criteria", verifies=["R1"])
    orphan = draft("R3", "acceptance_criteria", verifies=["R9"])
    check_acceptance_links([req, ok, orphan])
    assert ok.status == "open" and ok.verifies == ["R1"]
    assert orphan.status == "ambiguous" and orphan.verifies == []


def test_missing_objective_and_criteria_become_parsed_open_questions() -> None:
    gaps = gap_questions([draft("R1")])
    assert len(gaps) == 2
    assert all(g.kind == "open_question" and g.origin == "parsed" for g in gaps)
    assert gap_questions([draft("R1", "objective"), draft("R2", "acceptance_criteria")]) == []


def test_path_mining_and_scope_matching() -> None:
    assert "services/api/app/auth.py" in mentioned_paths("Edit services/api/app/auth.py now.")
    assert mentioned_paths("see https://example.com/a/b") == []
    assert path_in_scope("supabase/migrations/001.sql", "supabase/migrations/")
    assert not path_in_scope("supabase/migrations2/x.sql", "supabase/migrations/")
    assert path_in_scope("a/b/c.min.js", "**/*.min.js")


def test_do_not_touch_contradiction_is_deterministic_and_ambiguous() -> None:
    d = draft("R1", statement="Rewrite vendor/lib/util.py to use async IO.")
    other = draft("R2", statement="Add a flag to services/api/app/main.py.")
    conflicts = find_constraint_conflicts([d, other], [do_not_touch("vendor/")])
    assert [c.requirement_key for c in conflicts] == ["R1"]
    assert d.status == "ambiguous" and "vendor/" in (d.question_text() or "")
    assert d.constraint_ids == [CONSTRAINT_ID]
    assert other.status == "open"


def test_soft_unconfirmed_do_not_touch_is_not_enforced() -> None:
    d = draft("R1", statement="Edit vendor/lib/util.py")
    assert find_constraint_conflicts([d], [do_not_touch("vendor/", hard=False)]) == []


def test_new_file_beside_existing_migrations_is_not_a_conflict() -> None:
    c = do_not_touch("supabase/migrations/", spans=["supabase/migrations/001_init.sql"])
    new = draft("R1", statement="Add supabase/migrations/002_limits.sql with a table.")
    old = draft("R2", statement="Edit supabase/migrations/001_init.sql to add a column.")
    conflicts = find_constraint_conflicts([new, old], [c])
    assert [x.requirement_key for x in conflicts] == ["R2"]


def test_statement_contradiction_is_validated_and_never_resolved() -> None:
    drafts = [draft("R1"), draft("R2"), draft("R3")]
    payload = {
        "contradictions": [
            {"first": "R1", "second": "R2", "explanation": "One says allow, the other deny."},
            {"first": "R1", "second": "R7", "explanation": "invented key"},
            {"first": "R3", "second": "R3", "explanation": "self pair"},
        ]
    }
    provider = OneShot(payload)
    service = RoutedModelService(provider=provider, recorder=lambda _c: None)
    accepted, dropped = find_statement_contradictions(service, drafts)
    assert [(a.first, a.second) for a in accepted] == [("R1", "R2")]
    assert [d.reason for d in dropped] == ["invalid_pair", "invalid_pair"]
    assert drafts[0].status == "ambiguous" and drafts[1].status == "ambiguous"
    assert drafts[2].status == "open"
    # The statements went out inside the fenced data block, not the system prompt.
    req = provider.requests[0]
    assert "statement for R1" not in (req.prompt.system or "")
    assert "BEGIN DATA" in req.prompt.user


def test_contradiction_check_skipped_with_fewer_than_two_statements() -> None:
    provider = OneShot({"contradictions": []})
    service = RoutedModelService(provider=provider, recorder=lambda _c: None)
    assert find_statement_contradictions(service, [draft("R1")]) == ([], [])
    assert provider.requests == []


@pytest.mark.parametrize("kind", ["objective", "functional"])
def test_status_property_reflects_questions(kind: str) -> None:
    d = draft("R1", kind)
    assert d.status == "open"
    d.questions.append("why?")
    assert d.status == "ambiguous"
