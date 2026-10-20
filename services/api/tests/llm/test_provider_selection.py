"""Provider selection by environment, and the Gemini routing/pricing tables.

The Gemini table is a deliberate override of build-plan.md Part 3's Claude
routing table, recorded in docs/STATUS.md (2026-10-20). These tests pin the
override so it cannot drift silently.
"""

from __future__ import annotations

import pytest

from app.llm import (
    PRICES,
    PROVIDER_ENV,
    AnthropicProvider,
    GeminiProvider,
    UnknownProviderError,
    completion_provider,
    estimate_cost_usd,
    model_service,
    provider_name,
    routes_for_provider,
)
from app.llm.routing import (
    ANTHROPIC_ROUTES,
    DEFAULT_ROUTES,
    GEMINI_ROUTES,
    attempt_models,
)
from app.llm.service import RoutedModelService


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (PROVIDER_ENV, "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


# --- selection ----------------------------------------------------------------


def test_env_var_selects_the_provider(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "gemini")
    assert provider_name() == "gemini"
    assert isinstance(completion_provider(), GeminiProvider)

    monkeypatch.setenv(PROVIDER_ENV, "ANTHROPIC")  # case and spacing tolerated
    assert provider_name() == "anthropic"
    assert isinstance(completion_provider(), AnthropicProvider)


def test_unknown_provider_fails_loudly_rather_than_defaulting(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "openai")
    with pytest.raises(UnknownProviderError, match="openai"):
        provider_name()
    with pytest.raises(UnknownProviderError):
        routes_for_provider("openai")


def test_without_the_env_var_the_key_that_exists_decides(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")
    assert provider_name() == "anthropic"
    # A Gemini key wins: it is the one the owner can actually authenticate.
    monkeypatch.setenv("GEMINI_API_KEY", "y")
    assert provider_name() == "gemini"


def test_with_no_keys_at_all_selection_still_resolves():
    # Nothing here reads credentials, so the app must still import and start;
    # the missing key surfaces at the first request instead.
    assert provider_name() == "gemini"
    model_service(recorder=lambda _: None)


def routed(service) -> RoutedModelService:
    """`model_service` is typed as the abstract `ModelService`; these tests
    are about the wiring underneath it."""
    assert isinstance(service, RoutedModelService)
    return service


def test_model_service_pairs_the_provider_with_its_own_routing_table(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "gemini")
    # Pairing a provider with the other vendor's model ids would 404 on every
    # call, so the table must follow the provider.
    assert routed(model_service(recorder=lambda _: None))._routes == GEMINI_ROUTES

    monkeypatch.setenv(PROVIDER_ENV, "anthropic")
    assert routed(model_service(recorder=lambda _: None))._routes == ANTHROPIC_ROUTES


def test_an_explicit_provider_argument_still_picks_up_its_table(monkeypatch):
    monkeypatch.setenv(PROVIDER_ENV, "anthropic")
    service = routed(model_service(recorder=lambda _: None, provider=GeminiProvider(api_key="k")))
    assert service._routes == GEMINI_ROUTES
    assert isinstance(service._provider, GeminiProvider)


def test_anthropic_factory_stays_pinned_regardless_of_the_env_var(monkeypatch):
    from app.llm import anthropic_model_service

    monkeypatch.setenv(PROVIDER_ENV, "gemini")
    service = routed(anthropic_model_service(recorder=lambda _: None))
    assert isinstance(service._provider, AnthropicProvider)


# --- the Gemini table ---------------------------------------------------------


def test_default_routes_are_still_part_3s_claude_table():
    assert DEFAULT_ROUTES is ANTHROPIC_ROUTES
    assert DEFAULT_ROUTES["synth"].model == "claude-sonnet-5-5"


def test_gemini_role_assignments():
    assert GEMINI_ROUTES["bulk"].model == "gemini-3.1-flash-lite"
    assert GEMINI_ROUTES["bulk"].max_tokens == 4096
    assert GEMINI_ROUTES["synth"].model == "gemini-3.8-flash"
    assert GEMINI_ROUTES["design"].model == "gemini-3.8-flash"
    assert GEMINI_ROUTES["vision"].model == "gemini-3.8-flash"
    assert GEMINI_ROUTES["vision"].accepts_images is True
    assert [r for r, v in GEMINI_ROUTES.items() if v.accepts_images] == ["vision"]


def test_no_gemini_role_escalates_because_no_pro_model_is_reachable():
    # Verified live 2026-10-20: gemini-3.1-pro-preview 429s on a one-token
    # request and gemini-2.5-pro is withdrawn. An unreachable escalation model
    # would fail deep-plan mode and every post-validation retry outright, so
    # both stay on the default model instead.
    assert [r for r, v in GEMINI_ROUTES.items() if v.escalation_model] == []
    design = GEMINI_ROUTES["design"]
    assert attempt_models(design, deep=False) == ("gemini-3.8-flash", "gemini-3.8-flash")
    assert attempt_models(design, deep=True) == ("gemini-3.8-flash", "gemini-3.8-flash")


def test_no_preview_model_is_on_a_default_path():
    for route in GEMINI_ROUTES.values():
        assert "preview" not in route.model
        assert "preview" not in (route.escalation_model or "")


def test_embed_is_untouched_and_shared():
    # Changing the embedding model is a migration (chunks.embedding is
    # declared at 1024 dimensions), so it is one route under both providers.
    assert GEMINI_ROUTES["embed"] is ANTHROPIC_ROUTES["embed"]
    assert GEMINI_ROUTES["embed"].model == "voyage-code-3"
    assert GEMINI_ROUTES["embed"].dimensions == 1024


def test_rerank_is_routed_on_neither_provider():
    assert "rerank" not in GEMINI_ROUTES
    assert "rerank" not in ANTHROPIC_ROUTES


def test_every_routed_gemini_model_is_priced():
    routed = {r.model for r in GEMINI_ROUTES.values()} | {
        r.escalation_model for r in GEMINI_ROUTES.values() if r.escalation_model
    }
    assert routed <= set(PRICES)


def test_gemini_cost_estimates():
    # ai.google.dev/gemini-api/docs/pricing, read 2026-10-20: 1M in + 1M out.
    lite = estimate_cost_usd("gemini-3.1-flash-lite", input_tokens=10**6, output_tokens=10**6)
    assert lite == 1.75
    assert estimate_cost_usd("gemini-3.8-flash", input_tokens=10**6, output_tokens=10**6) == 4.5
    assert estimate_cost_usd("gemini-3.8-flash", input_tokens=1000, output_tokens=500) == 0.002625


def test_no_routed_gemini_model_has_a_long_prompt_tier():
    # The 200K tiers apply only to the Pro models, which nothing routes to, so
    # a huge prompt must bill at the flat rate rather than a tier that is not
    # on the price list.
    assert estimate_cost_usd("gemini-3.8-flash", input_tokens=10**6, output_tokens=0) == 0.75
    assert estimate_cost_usd("gemini-3.1-flash-lite", input_tokens=10**6, output_tokens=0) == 0.25


def test_gemini_cache_reads_are_priced_and_there_is_no_write_charge():
    # Cached input is $0.075/MTok on 3.8-flash; Gemini bills cache *storage*
    # per hour rather than a per-token write, hence the 1.0 multiplier.
    assert (
        estimate_cost_usd(
            "gemini-3.8-flash",
            input_tokens=0,
            output_tokens=0,
            cache_read_input_tokens=10**6,
        )
        == 0.075
    )
    assert PRICES["gemini-3.8-flash"].cache_write_multiplier == 1.0


def test_an_unrouted_gemini_model_is_recorded_as_unpriced():
    # The honest path the /projects/{id}/spend route counts as unpriced_calls.
    # gemini-3-flash-preview does serve on this key but nothing routes to it,
    # so it is deliberately absent from the price table rather than guessed.
    assert estimate_cost_usd("gemini-3-flash-preview", input_tokens=10, output_tokens=10) is None


def test_withdrawn_models_are_not_routed():
    # The whole gemini-2.5-* family 404s with "no longer available to new
    # users" (verified live 2026-10-20), and ListModels still lists it -- which
    # is why this table is built from real generateContent calls.
    routed = {r.model for r in GEMINI_ROUTES.values()}
    assert not any(m.startswith("gemini-2.5") for m in routed)
