"""Per-call cost estimate, USD. An estimate for observability and budget caps,
not an invoice — the provider's billing is authoritative.

Anthropic first-party list prices, cached 2026-10-06 (claude-api reference);
Voyage AI list price cached 2026-10-17 (docs.voyageai.com/docs/pricing); the
Gemini rows are weaker-sourced and carry their own note below.
Update this table when prices change; an unknown model yields cost=None
(recorded as "not priced"), never a guessed number.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.llm.routing import (
    GEMINI_FLASH,
    GEMINI_FLASH_LITE,
    GEMINI_PRO,
    HAIKU,
    OPUS,
    SONNET,
    VOYAGE_CODE_3,
)

_PER_MTOK = 1_000_000


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: float
    output_per_mtok: float
    cache_read_per_mtok: float
    # 5-minute cache writes bill at 1.25x base input.
    cache_write_multiplier: float = 1.25
    # Long-prompt tier: when total prompt tokens exceed `long_context_threshold`,
    # the long_* rates apply to the whole request.
    long_context_threshold: int | None = None
    long_input_per_mtok: float | None = None
    long_output_per_mtok: float | None = None


PRICES: dict[str, ModelPrice] = {
    OPUS: ModelPrice(input_per_mtok=4.00, output_per_mtok=20.00, cache_read_per_mtok=0.20),
    SONNET: ModelPrice(input_per_mtok=2.00, output_per_mtok=10.00, cache_read_per_mtok=0.20),
    HAIKU: ModelPrice(
        input_per_mtok=0.10,
        output_per_mtok=0.50,
        # Not listed in the cached reference; 0.1x base input is the standard
        # cache-read ratio. Unused until prompt caching is built (Part 3 patterns).
        cache_read_per_mtok=0.01,
        long_context_threshold=100_000,
        long_input_per_mtok=0.50,
        long_output_per_mtok=2.50,
    ),
    # Voyage AI list price, read from docs.voyageai.com/docs/pricing on
    # 2026-10-17: voyage-code-3 is $0.18 per million tokens. Embeddings bill
    # input tokens only -- there are no output or cache tokens, hence 0.0.
    VOYAGE_CODE_3: ModelPrice(
        input_per_mtok=0.18, output_per_mtok=0.0, cache_read_per_mtok=0.0
    ),
    # --- Gemini -------------------------------------------------------------
    # These three are the weakest numbers in this table, and the reason is
    # worth recording rather than discovering later: ai.google.dev and
    # cloud.google.com are both unreachable from the build environment, so
    # unlike the rows above these were not read off the vendor's own page.
    # They are the figures Google published for the 2.5 family, corroborated
    # against several third-party price trackers in October 2026, which
    # disagreed with each other on the cache rate.
    #
    # Where sources conflicted the HIGHER number is used, which is the safe
    # direction and not an arbitrary one: this table feeds the pre-call cost
    # cap (app/limits/budget.py), so over-estimating makes the cap refuse
    # slightly early, while under-estimating lets a project spend past it. A
    # model missing from this table is worse than either -- its calls record
    # cost_usd = NULL and contribute nothing to the ledger sum, so it would
    # run with no cap at all. **Confirm against
    # https://ai.google.dev/gemini-api/docs/pricing before this is relied on
    # for billing.**
    GEMINI_PRO: ModelPrice(
        input_per_mtok=1.25,
        output_per_mtok=10.00,
        # Context caching: 25% of the base input rate, Google's standard ratio.
        cache_read_per_mtok=0.3125,
        # Gemini prices the whole request at the long rate past 200K prompt
        # tokens, the same shape as Haiku's long-context tier above.
        long_context_threshold=200_000,
        long_input_per_mtok=2.50,
        long_output_per_mtok=15.00,
    ),
    GEMINI_FLASH: ModelPrice(
        input_per_mtok=0.30,
        output_per_mtok=2.50,
        cache_read_per_mtok=0.075,
    ),
    GEMINI_FLASH_LITE: ModelPrice(
        input_per_mtok=0.10,
        output_per_mtok=0.40,
        cache_read_per_mtok=0.025,
    ),
}


def estimate_cost_usd(
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_input_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
) -> float | None:
    price = PRICES.get(model)
    if price is None:
        return None

    input_rate, output_rate = price.input_per_mtok, price.output_per_mtok
    prompt_tokens = input_tokens + cache_read_input_tokens + cache_creation_input_tokens
    if (
        price.long_context_threshold is not None
        and prompt_tokens > price.long_context_threshold
        and price.long_input_per_mtok is not None
        and price.long_output_per_mtok is not None
    ):
        input_rate, output_rate = price.long_input_per_mtok, price.long_output_per_mtok

    cost = (
        input_tokens * input_rate
        + output_tokens * output_rate
        + cache_read_input_tokens * price.cache_read_per_mtok
        + cache_creation_input_tokens * input_rate * price.cache_write_multiplier
    ) / _PER_MTOK
    return round(cost, 6)
