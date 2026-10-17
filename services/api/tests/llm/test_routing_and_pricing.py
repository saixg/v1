"""Routing table matches build-plan.md Part 3; cost estimates match list prices."""

import pytest

from app.llm import DEFAULT_ROUTES, PRICES, estimate_cost_usd
from app.llm.routing import UnroutedRoleError, attempt_models, resolve_route


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


def test_embed_and_rerank_are_not_routed():
    # Part 13 #2 leaves the embedding model open; don't guess one.
    for role in ("embed", "rerank"):
        assert role not in DEFAULT_ROUTES
        with pytest.raises(UnroutedRoleError):
            resolve_route(DEFAULT_ROUTES, role)


def test_attempt_models():
    design, synth = DEFAULT_ROUTES["design"], DEFAULT_ROUTES["synth"]
    assert attempt_models(design, deep=False) == ("claude-sonnet-5-5", "claude-opus-5-5")
    assert attempt_models(design, deep=True) == ("claude-opus-5-5", "claude-opus-5-5")
    # No escalation model: deep is a no-op and the retry stays on the same model.
    assert attempt_models(synth, deep=False) == ("claude-sonnet-5-5", "claude-sonnet-5-5")
    assert attempt_models(synth, deep=True) == ("claude-sonnet-5-5", "claude-sonnet-5-5")


def test_every_routed_model_is_priced():
    routed = {r.model for r in DEFAULT_ROUTES.values()} | {
        r.escalation_model for r in DEFAULT_ROUTES.values() if r.escalation_model
    }
    assert routed <= set(PRICES)


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


def test_unknown_model_is_not_priced():
    assert estimate_cost_usd("some-other-model", input_tokens=10, output_tokens=10) is None
