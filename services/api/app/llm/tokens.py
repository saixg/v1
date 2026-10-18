"""Token-count estimate, shared by the chunker and the embedding batcher.

There is no local voyage-code-3 tokenizer available offline (the real one is
an HF tokenizer that would have to be downloaded at runtime, and downloading
a model inside the analysis sandbox is not something this project does), so
chunk sizing and request batching both run on a character heuristic.

Consequences, stated rather than hidden:
- `chunks.token_count` is an estimate, not a measured count. The measured
  count for text that actually reaches the provider is recorded separately and
  exactly in `codeaxis.llm_calls.input_tokens` (`usage.total_tokens` from the
  API response), which is what cost is computed from.
- Batching leaves headroom below the provider's hard caps so an under-estimate
  cannot turn into a rejected request.
"""

from __future__ import annotations

# STUB(CodeAxis): hardcoded heuristic. ~4 characters per token is the usual
# rule of thumb for code and English prose; it is not the model's tokenizer.
# Replace with a real tokenizer if/when one can be used without a download.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """A deliberate over-simplification; see the module docstring."""
    if not text:
        return 0
    return max(1, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)
