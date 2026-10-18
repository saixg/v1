"""AST-aware chunking (build-plan.md Part 4.D), against the pure planner.

No database, no network, no API key: `plan_file_chunks` takes text plus the
symbol spans the scan pass found and returns Chunk rows.
"""

from __future__ import annotations

import hashlib

import pytest

from app.analysis.scan.secrets import REDACTION_TOKEN
from app.analysis.semantic.chunk import (
    ANALYZER,
    ANALYZER_VERSION,
    MAX_CHUNK_TOKENS,
    MIN_CHUNK_TOKENS,
    FileSource,
    SymbolSpan,
    content_hash,
    plan_chunks,
    plan_file_chunks,
)

REPO = "11111111-1111-1111-1111-111111111111"
SHA = "deadbeef"


def plan(path: str, content: str, symbols: tuple[SymbolSpan, ...] = ()):
    return plan_file_chunks(FileSource(path=path, content=content, symbols=symbols), REPO, SHA)


def body(tokens: int, label: str = "x") -> str:
    """A line long enough to be worth `tokens` estimated tokens."""
    return f"    {label} = '" + "a" * (tokens * 4) + "'"


# --- one chunk per symbol -----------------------------------------------------


def test_one_chunk_per_symbol_plus_module_lines():
    content = "\n".join(
        [
            "import os",  # 1
            "",  # 2
            "def one():",  # 3
            body(100, "a"),  # 4
            "",  # 5
            "def two():",  # 6
            body(100, "b"),  # 7
        ]
    )
    chunks = plan(
        "m.py",
        content,
        (
            SymbolSpan(node_id=7, name="one", line_start=3, line_end=4),
            SymbolSpan(node_id=8, name="two", line_start=6, line_end=7),
        ),
    )

    # Line 5 is blank, so it is not its own retrieval unit, and the module
    # preamble is NOT folded into `one` -- that would claim `one` starts at
    # line 1. Trailing module lines merging upward is allowed; backdating a
    # symbol is not.
    assert [(c.kind, c.symbol, c.line_start, c.line_end) for c in chunks] == [
        ("module", None, 1, 2),
        ("symbol", "one", 3, 4),
        ("symbol", "two", 6, 7),
    ]
    # Every chunk carries the snapshot and the analyzer that made it.
    for chunk in chunks:
        assert (chunk.repo_id, chunk.commit_sha) == (REPO, SHA)
        assert (chunk.analyzer, chunk.analyzer_version) == (ANALYZER, ANALYZER_VERSION)
        assert chunk.path == "m.py"
    # The symbol chunks link back to the scan pass's node ids.
    assert [c.node_id for c in chunks if c.kind == "symbol"] == [7, 8]


def test_chunk_content_is_exactly_its_line_range():
    content = "\n".join(f"line{i}" for i in range(1, 11))
    (chunk,) = plan("f.txt", content)
    assert chunk.line_start == 1 and chunk.line_end == 10
    assert chunk.content == content


def test_every_line_of_a_symbolless_file_is_covered():
    content = "\n".join(f'{{"key{i}": {i}}}' for i in range(1, 6))
    chunks = plan("data.json", content)
    assert [c.kind for c in chunks] == ["module"]
    assert chunks[0].line_start == 1 and chunks[0].line_end == 5


def test_nested_symbols_are_not_duplicated():
    # A method inside a class that fits in one chunk gets no chunk of its own.
    content = "\n".join(["class C:", "    def m(self):", body(50, "v")])
    chunks = plan(
        "c.py",
        content,
        (
            SymbolSpan(node_id=1, name="C", line_start=1, line_end=3),
            SymbolSpan(node_id=2, name="m", line_start=2, line_end=3),
        ),
    )
    assert [(c.symbol, c.line_start, c.line_end) for c in chunks] == [("C", 1, 3)]


def test_empty_and_blank_files_produce_no_chunks():
    assert plan("empty.py", "") == []
    assert plan("blank.py", "\n\n   \n") == []


# --- splitting by size --------------------------------------------------------


def test_oversized_symbol_is_split_with_part_index_and_count():
    lines = ["def big():"] + [body(300, f"v{i}") for i in range(10)]
    content = "\n".join(lines)
    chunks = plan(
        "big.py",
        content,
        (SymbolSpan(node_id=5, name="big", line_start=1, line_end=len(lines)),),
    )

    assert len(chunks) > 1
    assert [c.part_index for c in chunks] == list(range(len(chunks)))
    assert {c.part_count for c in chunks} == {len(chunks)}
    assert all(c.symbol == "big" and c.node_id == 5 for c in chunks)
    # Parts tile the symbol's line range exactly: no gaps, no overlap.
    assert chunks[0].line_start == 1
    assert chunks[-1].line_end == len(lines)
    for previous, nxt in zip(chunks, chunks[1:], strict=False):
        assert nxt.line_start == previous.line_end + 1
    # Rejoining the parts reproduces the symbol's source.
    assert "\n".join(c.content for c in chunks) == content
    assert all(c.token_count <= MAX_CHUNK_TOKENS for c in chunks)


def test_split_prefers_nested_symbol_boundaries():
    # Two methods, each just inside the budget; the class as a whole is not.
    half = MAX_CHUNK_TOKENS - 20
    lines = [
        "class C:",  # 1
        "    def a(self):",  # 2
        body(half, "a"),  # 3
        "    def b(self):",  # 4
        body(half, "b"),  # 5
    ]
    chunks = plan(
        "c.py",
        "\n".join(lines),
        (
            SymbolSpan(node_id=1, name="C", line_start=1, line_end=5),
            SymbolSpan(node_id=2, name="a", line_start=2, line_end=3),
            SymbolSpan(node_id=3, name="b", line_start=4, line_end=5),
        ),
    )
    # The break lands on the second method's first line, not mid-method.
    assert len(chunks) == 2
    assert chunks[1].line_start == 4


def test_a_single_oversized_line_is_never_cut_mid_line():
    content = "\n".join(["def f():", body(MAX_CHUNK_TOKENS * 3, "huge"), "    return 1"])
    chunks = plan(
        "huge.py",
        content,
        (SymbolSpan(node_id=1, name="f", line_start=1, line_end=3),),
    )
    # The oversized line stays whole even though it blows the budget.
    assert any(c.token_count > MAX_CHUNK_TOKENS for c in chunks)
    assert "\n".join(c.content for c in chunks) == content


# --- merging trivially small chunks -------------------------------------------


def test_trivially_small_adjacent_symbols_are_merged():
    content = "\n".join(["def a():", "    return 1", "def b():", "    return 2"])
    chunks = plan(
        "tiny.py",
        content,
        (
            SymbolSpan(node_id=1, name="a", line_start=1, line_end=2),
            SymbolSpan(node_id=2, name="b", line_start=3, line_end=4),
        ),
    )
    (chunk,) = chunks
    assert chunk.kind == "symbol_group"
    assert chunk.symbol == "a" and chunk.node_id == 1  # anchored on the first
    assert (chunk.line_start, chunk.line_end) == (1, 4)
    assert chunk.content == content


def test_a_symbol_over_the_minimum_is_not_merged():
    content = "\n".join(["def a():", body(MIN_CHUNK_TOKENS * 2, "v"), "def b():", "    return 2"])
    chunks = plan(
        "m.py",
        content,
        (
            SymbolSpan(node_id=1, name="a", line_start=1, line_end=2),
            SymbolSpan(node_id=2, name="b", line_start=3, line_end=4),
        ),
    )
    assert [c.kind for c in chunks] == ["symbol", "symbol"]
    assert [c.symbol for c in chunks] == ["a", "b"]


# --- hashing, determinism, provenance -----------------------------------------


def test_content_hash_is_sha256_of_the_chunk_text():
    (chunk,) = plan("f.txt", "hello world")
    assert chunk.content_hash == hashlib.sha256(b"hello world").hexdigest()
    assert chunk.content_hash == content_hash("hello world")


def test_identical_content_hashes_equal_across_files_and_commits():
    # This is what makes the incremental re-index key work: an unchanged
    # chunk at a new commit finds the vector already computed for it.
    (a,) = plan_file_chunks(FileSource(path="a.py", content="x = 1"), REPO, "sha-one")
    (b,) = plan_file_chunks(FileSource(path="b.py", content="x = 1"), REPO, "sha-two")
    assert a.content_hash == b.content_hash


def test_planning_is_deterministic():
    source = FileSource(
        path="m.py",
        content="\n".join(["def a():", body(200, "v"), "def b():", "    return 2"]),
        symbols=(
            SymbolSpan(node_id=1, name="a", line_start=1, line_end=2),
            SymbolSpan(node_id=2, name="b", line_start=3, line_end=4),
        ),
    )
    first = plan_chunks([source], REPO, SHA)
    second = plan_chunks([source], REPO, SHA)
    assert [c.model_dump() for c in first] == [c.model_dump() for c in second]


def test_no_chunk_claims_an_impossible_line_range():
    # Symbol spans from a mis-parse must not produce a negative-length chunk.
    chunks = plan(
        "weird.py",
        "a = 1\nb = 2",
        (SymbolSpan(node_id=1, name="ghost", line_start=9, line_end=3),),
    )
    assert all(c.line_end >= c.line_start for c in chunks)


# --- secrets never reach a chunk ----------------------------------------------


def test_secrets_are_redacted_at_the_chunking_boundary():
    # Defence in depth: the scan pass already redacts what it stores in
    # codeaxis.blobs, but chunk content is what goes to the embeddings API,
    # so the chunker redacts again rather than trusting its input.
    secret = "AKIAIOSFODNN7EXAMPLE"
    content = "\n".join(["def f():", f'    key = "{secret}"', "    return key"])
    chunks = plan(
        "leak.py",
        content,
        (SymbolSpan(node_id=1, name="f", line_start=1, line_end=3),),
    )
    assert chunks
    for chunk in chunks:
        assert secret not in chunk.content
    assert any(REDACTION_TOKEN in chunk.content for chunk in chunks)


def test_redaction_does_not_shift_line_numbers():
    secret = "AKIAIOSFODNN7EXAMPLE"
    content = "\n".join(["# header", f'KEY = "{secret}"', "TAIL = 1"])
    (chunk,) = plan("leak.py", content)
    assert (chunk.line_start, chunk.line_end) == (1, 3)
    assert chunk.content.splitlines()[-1] == "TAIL = 1"


# --- model-level invariants ---------------------------------------------------


def test_part_index_must_be_within_part_count():
    from app.models import Chunk

    with pytest.raises(ValueError, match="part_index"):
        Chunk(
            repo_id=REPO,
            commit_sha=SHA,
            path="a.py",
            kind="symbol",
            line_start=1,
            line_end=2,
            content="x",
            content_hash="h",
            token_count=1,
            part_index=2,
            part_count=2,
            analyzer=ANALYZER,
            analyzer_version=ANALYZER_VERSION,
        )
