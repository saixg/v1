"""Per-call cost estimate, USD. An estimate for observability and budget caps,
not an invoice — the provider's billing is authoritative.

Anthropic first-party list prices, cached 2026-10-06 (claude-api reference).
Update this table when prices change; an unknown model yields cost=None
(recorded as "not priced"), never a guessed number.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.llm.routing import HAIKU, OPUS, SONNET

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
