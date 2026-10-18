"""Pass 2a: AST-aware chunking (build-plan.md Part 4.D).

Source text in, `Chunk` rows out. No DB, no network, no exec, and no model
call: a chunk's path, symbol, kind and line range all come from symbol nodes
the scan pass already parsed, never from an LLM.

How a file is cut up:

1. The symbol spans the scan pass found are reduced to the **outermost** ones.
   A method inside a class is not given its own chunk while the class fits in
   one, because storing both would duplicate the same bytes twice in the
   retrieval index and return the same code twice for one query.
2. Lines no kept symbol covers become `module` chunks -- imports, module-level
   statements, and every line of a file that has no symbols at all (JSON,
   Markdown, config). Nothing in a scanned file is silently unindexed.
3. A symbol too large for one chunk is split, preferring its own nested
   symbols' boundaries over arbitrary line counts, and the pieces carry
   `part_index` / `part_count`.
4. Chunks too small to retrieve on their own are merged with the next
   contiguous chunk; the result is a `symbol_group` anchored on the first
   symbol in it.

Secret redaction runs here, on every chunk's text, before a `Chunk` exists at
all. The blobs this reads from were already redacted by the scan pass
(`app.analysis.scan.scanner._scan_file` stores redacted bytes), so in practice
this is a second pass that finds nothing -- which is the point. Nothing can
produce chunk content that skipped redaction, and chunk content is what goes
to the embeddings API (build-plan.md Part 9).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.analysis.scan.secrets import redact_secrets
from app.llm import estimate_tokens
from app.models import Chunk, ChunkKind

ANALYZER = "semantic-chunk"
ANALYZER_VERSION = "0.1.0"

# Retrieval-unit size, in estimated tokens. MAX is well under voyage-code-3's
# 32K context: the limit here is what makes a hit useful to read, not what the
# provider will accept. MIN is the point below which a chunk (a one-line
# re-export, a two-line getter) is noise on its own.
MAX_CHUNK_TOKENS = 800
MIN_CHUNK_TOKENS = 64


@dataclass(frozen=True)
class SymbolSpan:
    """One symbol node from the scan pass, reduced to what chunking needs."""

    node_id: int | None
    name: str
    line_start: int
    line_end: int


@dataclass(frozen=True)
class FileSource:
    """One scanned file: its stored text plus the symbols found in it."""

    path: str
    content: str
    symbols: tuple[SymbolSpan, ...] = ()


@dataclass(frozen=True)
class _Region:
    """A candidate chunk before splitting, merging or hashing."""

    kind: ChunkKind
    line_start: int
    line_end: int
    symbol: SymbolSpan | None = None


def content_hash(text: str) -> str:
    """The incremental re-index key: sha256 of the chunk's own content.

    Chunk-level rather than file-level on purpose. A file-level hash would
    only let an entirely unchanged file reuse its vectors; hashing the chunk
    means an untouched function in an edited file reuses its vector too, and
    the file-unchanged case still falls out for free because all of its chunks
    hash the same.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _redact(text: str) -> str:
    redacted, _count = redact_secrets(text.encode("utf-8"))
    return redacted.decode("utf-8", "replace")


def _outermost(symbols: tuple[SymbolSpan, ...]) -> list[SymbolSpan]:
    """Symbols not contained in another symbol, in file order."""
    ordered = sorted(symbols, key=lambda s: (s.line_start, -s.line_end, s.name))
    kept: list[SymbolSpan] = []
    for symbol in ordered:
        if symbol.line_end < symbol.line_start:
            continue
        outer = kept[-1] if kept else None
        if outer and outer.line_start <= symbol.line_start and symbol.line_end <= outer.line_end:
            continue  # nested inside the symbol we already kept
        kept.append(symbol)
    return kept


def _children(symbols: tuple[SymbolSpan, ...], parent: SymbolSpan) -> list[SymbolSpan]:
    """The outermost symbols strictly inside `parent`."""
    inside = tuple(
        s
        for s in symbols
        if s is not parent
        and s.line_start >= parent.line_start
        and s.line_end <= parent.line_end
        and (s.line_start, s.line_end) != (parent.line_start, parent.line_end)
    )
    return _outermost(inside)


def _regions(source: FileSource, total_lines: int) -> list[_Region]:
    """The whole file, partitioned into symbol and module regions, in order."""
    regions: list[_Region] = []
    cursor = 1
    for symbol in _outermost(source.symbols):
        start = max(symbol.line_start, 1)
        end = min(symbol.line_end, total_lines)
        if end < start:
            continue
        if start > cursor:
            regions.append(_Region("module", cursor, start - 1))
        regions.append(_Region("symbol", start, end, symbol))
        cursor = max(cursor, end + 1)
    if cursor <= total_lines:
        regions.append(_Region("module", cursor, total_lines))
    return regions


def _segments(source: FileSource, region: _Region) -> list[tuple[int, int]]:
    """Line ranges a too-large region may be broken at.

    For a symbol region those are its nested symbols' spans plus the lines
    between them, so a split lands on a real syntactic boundary. For a module
    region there is no structure to respect, so it is one segment and the
    line splitter below handles it.
    """
    if region.kind != "symbol" or region.symbol is None:
        return [(region.line_start, region.line_end)]
    out: list[tuple[int, int]] = []
    cursor = region.line_start
    for child in _children(source.symbols, region.symbol):
        start, end = max(child.line_start, cursor), min(child.line_end, region.line_end)
        if end < start:
            continue
        if start > cursor:
            out.append((cursor, start - 1))
        out.append((start, end))
        cursor = end + 1
    if cursor <= region.line_end:
        out.append((cursor, region.line_end))
    return out or [(region.line_start, region.line_end)]


def _text(lines: list[str], line_start: int, line_end: int) -> str:
    return "\n".join(lines[line_start - 1 : line_end])


def _split_lines(
    lines: list[str], line_start: int, line_end: int
) -> list[tuple[int, int]]:
    """Last-resort split of a span with no usable inner boundary: greedy
    accumulation of whole lines up to MAX_CHUNK_TOKENS. A single line over the
    budget becomes its own oversized piece rather than being cut mid-line --
    chunk boundaries stay on line boundaries so every citation's line range
    remains exact."""
    out: list[tuple[int, int]] = []
    start = line_start
    tokens = 0
    for line_no in range(line_start, line_end + 1):
        line_tokens = estimate_tokens(lines[line_no - 1]) + 1  # +1 for the newline
        if line_no > start and tokens + line_tokens > MAX_CHUNK_TOKENS:
            out.append((start, line_no - 1))
            start, tokens = line_no, 0
        tokens += line_tokens
    out.append((start, line_end))
    return out


def _parts(
    lines: list[str], source: FileSource, region: _Region
) -> list[tuple[int, int]]:
    """A region as one or more line ranges, each within the size budget."""
    whole = _text(lines, region.line_start, region.line_end)
    if estimate_tokens(whole) <= MAX_CHUNK_TOKENS:
        return [(region.line_start, region.line_end)]

    pieces: list[tuple[int, int]] = []
    for seg_start, seg_end in _segments(source, region):
        if estimate_tokens(_text(lines, seg_start, seg_end)) > MAX_CHUNK_TOKENS:
            pieces.extend(_split_lines(lines, seg_start, seg_end))
        else:
            pieces.append((seg_start, seg_end))

    # Pack the pieces back up to the budget so a split symbol produces as few
    # parts as it can rather than one part per nested definition.
    packed: list[tuple[int, int]] = []
    for piece_start, piece_end in pieces:
        if packed:
            prev_start, prev_end = packed[-1]
            if piece_start == prev_end + 1 and (
                estimate_tokens(_text(lines, prev_start, piece_end)) <= MAX_CHUNK_TOKENS
            ):
                packed[-1] = (prev_start, piece_end)
                continue
        packed.append((piece_start, piece_end))
    return packed


@dataclass
class _Candidate:
    kind: ChunkKind
    line_start: int
    line_end: int
    symbol: SymbolSpan | None
    part_index: int
    part_count: int


def _candidates(lines: list[str], source: FileSource) -> list[_Candidate]:
    out: list[_Candidate] = []
    for region in _regions(source, len(lines)):
        parts = _parts(lines, source, region)
        for index, (start, end) in enumerate(parts):
            out.append(
                _Candidate(
                    kind=region.kind,
                    line_start=start,
                    line_end=end,
                    symbol=region.symbol,
                    part_index=index,
                    part_count=len(parts),
                )
            )
    return out


def _merge_small(lines: list[str], candidates: list[_Candidate]) -> list[_Candidate]:
    """Fold a candidate under MIN_CHUNK_TOKENS into the next contiguous one.

    Parts of a split symbol (`part_count > 1`) are never merged: they were
    split precisely because they did not fit, and merging would undo that.

    A `module` region is never merged *into* the symbol that follows it,
    either. Doing so would produce a chunk labelled with that symbol whose
    `line_start` is above where the symbol actually begins -- a false claim
    about structure, which is exactly what this pipeline must not emit. The
    reverse (trailing module lines folded onto the symbol above them) is
    fine: the symbol still starts where the chunk starts.
    """
    merged: list[_Candidate] = []
    for candidate in candidates:
        if merged:
            prev = merged[-1]
            fits = prev.part_count == 1 and candidate.part_count == 1
            contiguous = candidate.line_start == prev.line_end + 1
            prev_small = (
                estimate_tokens(_text(lines, prev.line_start, prev.line_end))
                < MIN_CHUNK_TOKENS
            )
            combined_ok = (
                estimate_tokens(_text(lines, prev.line_start, candidate.line_end))
                <= MAX_CHUNK_TOKENS
            )
            would_backdate_symbol = prev.symbol is None and candidate.symbol is not None
            if fits and contiguous and prev_small and combined_ok and not would_backdate_symbol:
                anchor = prev.symbol
                both_symbols = prev.symbol is not None and candidate.symbol is not None
                merged[-1] = _Candidate(
                    kind="symbol_group" if both_symbols else ("symbol" if anchor else "module"),
                    line_start=prev.line_start,
                    line_end=candidate.line_end,
                    symbol=anchor,
                    part_index=0,
                    part_count=1,
                )
                continue
        merged.append(candidate)
    return merged


def plan_file_chunks(
    source: FileSource, repo_id: str, commit_sha: str
) -> list[Chunk]:
    """Chunks for one file. Deterministic: same input, same output, always."""
    text = _redact(source.content)
    if not text.strip():
        return []
    lines = text.split("\n")

    chunks: list[Chunk] = []
    for candidate in _merge_small(lines, _candidates(lines, source)):
        content = _text(lines, candidate.line_start, candidate.line_end)
        if not content.strip():
            continue  # a run of blank lines is not a retrieval unit
        chunks.append(
            Chunk(
                repo_id=repo_id,
                commit_sha=commit_sha,
                node_id=candidate.symbol.node_id if candidate.symbol else None,
                path=source.path,
                symbol=candidate.symbol.name if candidate.symbol else None,
                kind=candidate.kind,
                line_start=candidate.line_start,
                line_end=candidate.line_end,
                content=content,
                content_hash=content_hash(content),
                token_count=estimate_tokens(content),
                part_index=candidate.part_index,
                part_count=candidate.part_count,
                analyzer=ANALYZER,
                analyzer_version=ANALYZER_VERSION,
            )
        )
    return chunks


def plan_chunks(
    sources: list[FileSource], repo_id: str, commit_sha: str
) -> list[Chunk]:
    """Chunks for a whole snapshot, in file order."""
    out: list[Chunk] = []
    for source in sources:
        out.extend(plan_file_chunks(source, repo_id, commit_sha))
    return out
