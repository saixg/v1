"""Which vendor serves the completion roles, and that the routing table goes
with it.

The pairing is the part worth pinning: a provider with the other vendor's
routing table is a 404 on every call, and it is the kind of break that only
shows up at the first real request.
"""

from __future__ import annotations

import pytest

from app.llm import (
    DEFAULT_ROUTES,
    GEMINI_ROUTES,
    PROVIDER_ENV_VAR,
    AnthropicProvider,
    GeminiProvider,
    ProviderNotConfiguredError,
    completion_model_service,
    embedding_model_service,
    selected_provider_name,
)

KEY_VARS = (PROVIDER_ENV_VAR, "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    """Selection reads the process environment, and a developer's real keys
    would otherwise decide the outcome of these tests."""
    for name in KEY_VARS:
        monkeypatch.delenv(name, raising=False)


def test_no_keys_at_all_still_resolves_to_a_provider():
    # Not an error: nothing here needs credentials, and the failure belongs at
    # the first request (where it names the vendor), not at import time in a
    # process that may never call a model.
    assert selected_provider_name() == "anthropic"


def test_anthropic_key_alone(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    assert selected_provider_name() == "anthropic"


@pytest.mark.parametrize("var", ["GEMINI_API_KEY", "GOOGLE_API_KEY"])
def test_either_gemini_key_alone_selects_gemini(monkeypatch: pytest.MonkeyPatch, var: str):
    # Both names are what the google-genai SDK itself resolves.
    monkeypatch.setenv(var, "AIza-x")
    assert selected_provider_name() == "gemini"


def test_both_keys_keeps_anthropic(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-x")
    assert selected_provider_name() == "anthropic"


def test_blank_key_does_not_count(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    assert selected_provider_name() == "anthropic"


def test_explicit_choice_overrides_the_keys(monkeypatch: pytest.MonkeyPatch):
    # The reason the variable exists: with both keys set, the operator needs a
    # way to say which one to spend.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-x")
    monkeypatch.setenv(PROVIDER_ENV_VAR, "Gemini")  # case and spacing are forgiven
    assert selected_provider_name() == "gemini"


def test_explicit_choice_wins_even_with_no_key_for_it(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    monkeypatch.setenv(PROVIDER_ENV_VAR, "gemini")
    assert selected_provider_name() == "gemini"


def test_unknown_provider_name_is_an_error_not_a_fallback(monkeypatch: pytest.MonkeyPatch):
    # A typo must not silently bill the other vendor.
    monkeypatch.setenv(PROVIDER_ENV_VAR, "gemnii")
    with pytest.raises(ProviderNotConfiguredError, match="gemnii"):
        selected_provider_name()


# --- the factories honour the selection ---------------------------------------


def _provider(service):
    return service._provider  # the one place a test looks inside RoutedModelService


def _routes(service):
    return service._routes


def test_completion_service_pairs_gemini_provider_with_gemini_routes(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-x")
    service = completion_model_service(recorder=lambda _: None, budget=None)
    assert isinstance(_provider(service), GeminiProvider)
    assert _routes(service) == GEMINI_ROUTES


def test_completion_service_pairs_anthropic_provider_with_default_routes(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-x")
    service = completion_model_service(recorder=lambda _: None, budget=None)
    assert isinstance(_provider(service), AnthropicProvider)
    assert _routes(service) == DEFAULT_ROUTES


def test_embedding_service_follows_the_selection_too(monkeypatch: pytest.MonkeyPatch):
    # The embed role does not use the completion provider, but the service is
    # one object with both halves: the wrong vendor here means any non-embed
    # call through this handle asks for a model the provider cannot serve.
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-x")
    service = embedding_model_service(recorder=lambda _: None, budget=None)
    assert isinstance(_provider(service), GeminiProvider)
    assert _routes(service)["embed"].model == "voyage-code-3"


def test_the_embed_route_is_the_same_on_both_providers():
    # Changing the embedder is a migration (chunks.embedding is 1024-dimension
    # voyage-code-3) and must not ride along with a completion-vendor switch.
    assert GEMINI_ROUTES["embed"] == DEFAULT_ROUTES["embed"]


def test_both_tables_route_the_same_roles():
    assert set(GEMINI_ROUTES) == set(DEFAULT_ROUTES)
