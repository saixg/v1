"""Pass 2b: semantic enrichment -- hierarchical summaries and conventions
(build-plan.md Part 4.D).

Reads chunks and graph rows the earlier passes wrote; writes
`codeaxis.summaries` (always `origin='inferred'`, with evidence naming the
chunks/nodes and file+line spans each paragraph was written from) and
`codeaxis.conventions` (`configured`/`parsed`/`inferred`, honestly per row).

No model is ever the source of a fact here. See `summarize.py` for the
bottom-up hierarchy and its fan-out budget, `conventions.py` for the
rules-first extraction, `prompts.py` for the untrusted-input stance, and
`writer.py` for the pass_run lifecycle and the no-API-key degradation.
"""

from .conventions import (
    ANALYZER as CONVENTIONS_ANALYZER,
)
from .conventions import (
    ANALYZER_VERSION as CONVENTIONS_ANALYZER_VERSION,
)
from .conventions import write_conventions_pass
from .summarize import (
    ANALYZER,
    ANALYZER_VERSION,
    SummaryBudget,
    SummaryDraft,
    SummaryStats,
    write_summary_pass,
)
from .writer import write_semantic_pass

__all__ = [
    "ANALYZER",
    "ANALYZER_VERSION",
    "CONVENTIONS_ANALYZER",
    "CONVENTIONS_ANALYZER_VERSION",
    "SummaryBudget",
    "SummaryDraft",
    "SummaryStats",
    "write_conventions_pass",
    "write_semantic_pass",
    "write_summary_pass",
]

# --- Pass 2a: AST-aware chunking + embeddings (build-plan.md Part 4.D) -------
# `chunk.py` is pure (source text + the scan pass's symbol spans in, Chunk
# rows out, secrets redacted before a chunk exists); `chunk_writer.py` is the
# only part that touches the database or the embeddings API.
from .chunk import ANALYZER as CHUNK_ANALYZER  # noqa: E402
from .chunk import ANALYZER_VERSION as CHUNK_ANALYZER_VERSION  # noqa: E402
from .chunk import (  # noqa: E402
    MAX_CHUNK_TOKENS,
    MIN_CHUNK_TOKENS,
    FileSource,
    SymbolSpan,
    content_hash,
    plan_chunks,
    plan_file_chunks,
)
from .chunk_writer import collect_sources, write_chunk_pass  # noqa: E402

__all__ += [
    "CHUNK_ANALYZER",
    "CHUNK_ANALYZER_VERSION",
    "MAX_CHUNK_TOKENS",
    "MIN_CHUNK_TOKENS",
    "FileSource",
    "SymbolSpan",
    "collect_sources",
    "content_hash",
    "plan_chunks",
    "plan_file_chunks",
    "write_chunk_pass",
]
