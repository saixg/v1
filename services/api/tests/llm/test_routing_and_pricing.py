"""Routing table matches build-plan.md Part 3; cost estimates match list prices."""

import pytest

from app.llm import DEFAULT_ROUTES, GEMINI_ROUTES, PRICES, estimate_cost_usd
from app.llm.routing import UnroutedRoleError, attempt_models, resolve_route, routed_models


def test_routing_table_matches_part_3():
    assert DEFAULT_ROUTES["bulk"].model == "claude-haiku-5-5"
    assert DEFAULT_ROUTES["synth"].model == "claude-sonnet-5-5"
    assert DEFAULT_ROUTES["design"].model == "claude-sonnet-5-5"
    assert DEFAULT_ROUTES["design"].escalation_model == "claude-opus-5-5"
    assert DEFAULT_ROUTES["vision"].model == "claude-sonnet-5-5"
    assert DEFAULT_ROUTES["vision"].accepts_images is True
    # Only design escalates; only vision takes images.
    assert [r for r, v in DEFAULT_ROUTES.items() if v.escalation_model] == ["design"]
    assert [r for r, v in DEFAULT_ROUTES.items() if v.accepts_images] == ["vision"]


def test_embed_is_routed_to_voyage_code_3():
    # Part 13 #2 settled 2026-10-17: hosted voyage-code-3, 1024 dimensions,
    # which must match the chunks.embedding column in the semantic migration.
    route = DEFAULT_ROUTES["embed"]
    assert route.model == "voyage-code-3"
    assert route.dimensions == 1024
    assert (route.max_batch_texts, route.max_batch_tokens) == (1000, 120_000)
    assert route.max_input_tokens == 32_000


def test_rerank_is_not_routed():
    # Reranking ships only if the eval harness shows retrieval is insufficient.
    assert "rerank" not in DEFAULT_ROUTES
    with pytest.raises(UnroutedRoleError):
        resolve_route(DEFAULT_ROUTES, "rerank")


def test_only_embed_carries_embedding_limits():
    assert [r for r, v in DEFAULT_ROUTES.items() if v.dimensions is not None] == ["embed"]


def test_attempt_models():
    design, synth = DEFAULT_ROUTES["design"], DEFAULT_ROUTES["synth"]
    assert attempt_models(design, deep=False) == ("claude-sonnet-5-5", "claude-opus-5-5")
    assert attempt_models(design, deep=True) == ("claude-opus-5-5", "claude-opus-5-5")
    # No escalation model: deep is a no-op and the retry stays on the same model.
    assert attempt_models(synth, deep=False) == ("claude-sonnet-5-5", "claude-sonnet-5-5")
    assert attempt_models(synth, deep=True) == ("claude-sonnet-5-5", "claude-sonnet-5-5")


def test_every_routed_model_is_priced():
    """Every table, not just the default one.

    An unpriced model is worse than a mispriced one: its calls record
    cost_usd = NULL, contribute nothing to the ledger sum app/limits/budget.py
    reads, and therefore run with no cost cap at all.
    """
    for table in (DEFAULT_ROUTES, GEMINI_ROUTES):
        assert routed_models(table) <= set(PRICES)


def test_routed_models_can_exclude_the_embedding_vendor():
    # scripts/check_model_provider.py checks routed ids against the completion
    # provider's model list, where an embedding model from another vendor
    # would read as a missing id.
    assert "voyage-code-3" in routed_models(GEMINI_ROUTES)
    assert "voyage-code-3" not in routed_models(GEMINI_ROUTES, completions_only=True)
    assert routed_models(GEMINI_ROUTES, completions_only=True) == {
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash",
        "gemini-2.5-pro",
    }


def test_gemini_routing_table():
    assert GEMINI_ROUTES["bulk"].model == "gemini-2.5-flash-lite"
    assert GEMINI_ROUTES["synth"].model == "gemini-2.5-flash"
    assert GEMINI_ROUTES["design"].model == "gemini-2.5-flash"
    assert GEMINI_ROUTES["design"].escalation_model == "gemini-2.5-pro"
    assert GEMINI_ROUTES["vision"].accepts_images is True
    # Same shape as the Anthropic table: switching provider changes which
    # vendor serves a role, not what the role means.
    assert [r for r, v in GEMINI_ROUTES.items() if v.escalation_model] == ["design"]
    assert [r for r, v in GEMINI_ROUTES.items() if v.accepts_images] == ["vision"]


def test_gemini_cost_estimates():
    # 1M in + 1M out at the rates in pricing.py. Pro is priced below its
    # 200K-token threshold, where a 1M-token prompt would bill at the long tier.
    assert estimate_cost_usd("gemini-2.5-pro", input_tokens=100_000, output_tokens=100_000) == 1.125
    assert estimate_cost_usd("gemini-2.5-flash", input_tokens=10**6, output_tokens=10**6) == 2.8
    assert (
        estimate_cost_usd("gemini-2.5-flash-lite", input_tokens=10**6, output_tokens=10**6) == 0.5
    )


def test_gemini_pro_long_prompt_tier():
    # Over 200K prompt tokens the whole request bills at $2.50 / $15.00 per MTok.
    assert estimate_cost_usd("gemini-2.5-pro", input_tokens=200_000, output_tokens=0) == 0.25
    assert estimate_cost_usd("gemini-2.5-pro", input_tokens=400_000, output_tokens=0) == 1.0


def test_cost_estimates():
    # 1M in + 1M out at list price.
    assert estimate_cost_usd("claude-opus-5-5", input_tokens=10**6, output_tokens=10**6) == 24.0
    assert estimate_cost_usd("claude-sonnet-5-5", input_tokens=10**6, output_tokens=10**6) == 12.0
    assert estimate_cost_usd("claude-haiku-5-5", input_tokens=1000, output_tokens=1000) == 0.0006
    # Sonnet 1,234 in / 567 out = 0.002468 + 0.00567
    assert estimate_cost_usd("claude-sonnet-5-5", input_tokens=1234, output_tokens=567) == 0.008138


def test_haiku_long_prompt_tier():
    # Over 100K prompt tokens, Haiku 5.5 bills $0.50 / $2.50 per MTok.
    assert estimate_cost_usd("claude-haiku-5-5", input_tokens=200_000, output_tokens=0) == 0.1
    assert estimate_cost_usd("claude-haiku-5-5", input_tokens=100_000, output_tokens=0) == 0.01


def test_cache_tokens_priced():
    # Sonnet cache read $0.20/MTok, cache write 1.25 x $2.00.
    cost = estimate_cost_usd(
        "claude-sonnet-5-5",
        input_tokens=0,
        output_tokens=0,
        cache_read_input_tokens=10**6,
        cache_creation_input_tokens=10**6,
    )
    assert cost == pytest.approx(0.20 + 2.50)


def test_voyage_code_3_price():
    # $0.18 / MTok, input only (docs.voyageai.com/docs/pricing, 2026-10-17).
    assert estimate_cost_usd("voyage-code-3", input_tokens=10**6, output_tokens=0) == 0.18
    assert estimate_cost_usd("voyage-code-3", input_tokens=12_500, output_tokens=0) == 0.00225


def test_unknown_model_is_not_priced():
    assert estimate_cost_usd("some-other-model", input_tokens=10, output_tokens=10) is None
