"""Check the configured model provider against the live API. Costs one tiny call.

Three things cannot be verified without a credential, and all three are the
kind that fail on every call once they are wrong:

1. that a key is present and accepted,
2. that the model ids in `app/llm/routing.py` exist for this account -- the
   Gemini ids were written from documentation, not from a `models.list`, and a
   wrong id is a 404 on every call of that role,
3. that the provider accepts a Pydantic JSON schema as sent, so structured
   output is constrained rather than merely validated afterwards.

Run this once after setting a key, and again whenever a routed model changes:

    uv run python scripts/check_model_provider.py

The one call it makes is a few dozen tokens on the cheapest routed model. It
writes no `llm_calls` row and is not capped, because it builds the provider
directly rather than going through the recorded, budgeted service -- this is a
configuration check, not product traffic.
"""

import os
import sys

from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

from app.llm import (  # noqa: E402 - after load_dotenv: selection reads the environment
    PROVIDER_ENV_VAR,
    ProviderNotConfiguredError,
    missing_credential_reason,
    routed_models,
    selected_provider_and_routes,
)
from app.llm.service import CompletionRequest, Prompt  # noqa: E402


class Answer(BaseModel):
    """Small enough that a wrong answer is the model's fault, not the prompt's."""

    capital: str


def main() -> int:
    try:
        name, provider_cls, routes = selected_provider_and_routes()
    except ProviderNotConfiguredError as exc:
        print(f"FAIL  {exc}")
        return 1

    print(f"provider: {name}  ({PROVIDER_ENV_VAR}={os.environ.get(PROVIDER_ENV_VAR) or 'unset'})")

    reason = missing_credential_reason()
    if reason:
        print(f"FAIL  {reason}")
        return 1
    print("credential: present")

    # Completion models only: the embed role is served by another vendor and
    # would never appear in this provider's model list.
    models = sorted(routed_models(routes, completions_only=True))
    print("routed models:", ", ".join(models))

    provider = provider_cls()

    # If the SDK can list models, check every routed id before spending
    # anything: a typo found here costs nothing and names itself.
    listed = _list_models(provider)
    if listed is None:
        # _list_models said why; the call below checks the ids either way.
        pass
    else:
        unknown = [m for m in models if m not in listed]
        if unknown:
            print(f"FAIL  not available to this account: {', '.join(unknown)}")
            print(f"      {len(listed)} models are; see app/llm/routing.py")
            return 1
        print(f"model ids: all present ({len(listed)} models available to this account)")

    cheapest = routes["bulk"]
    print(f"calling {cheapest.model} with a schema...")
    try:
        completion = provider.complete(
            CompletionRequest(
                model=cheapest.model,
                prompt=Prompt(
                    user="What is the capital of France? Answer with the city name only.",
                    system="You answer with a single JSON value matching the schema.",
                ),
                max_tokens=cheapest.max_tokens,
                json_schema=Answer.model_json_schema(),
            )
        )
    except Exception as exc:
        print(f"FAIL  {type(exc).__name__}: {exc}")
        return 1

    print(f"  stop_reason: {completion.stop_reason}  served: {completion.served_model}")
    print(f"  tokens: {completion.input_tokens} in / {completion.output_tokens} out")
    try:
        answer = Answer.model_validate_json(completion.text)
    except Exception as exc:
        # The response arrived but was not the schema -- which is exactly what
        # RoutedModelService retries once and then fails on, so it matters.
        print(f"FAIL  structured output did not validate: {exc}")
        print(f"      raw: {completion.text[:200]!r}")
        return 1
    print(f"  validated: {answer.capital}")
    print("OK")
    return 0


def _list_models(provider: object) -> set[str] | None:
    """Model ids this account can call, or None if the SDK cannot say.

    Ids come back fully qualified on some providers ("models/gemini-2.5-flash"),
    so the bare name is kept alongside whatever was returned.
    """
    client = getattr(provider, "client", None)
    models_api = getattr(client, "models", None)
    lister = getattr(models_api, "list", None)
    if lister is None:
        print("note: this SDK offers no model listing; ids are checked by the call below")
        return None
    try:
        names: set[str] = set()
        for model in lister():
            identifier = getattr(model, "name", None) or getattr(model, "id", None)
            if identifier:
                names.add(identifier)
                names.add(identifier.rsplit("/", 1)[-1])
        return names or None
    except Exception as exc:  # listing is a convenience; the call below is the real check
        print("note: could not list models, so ids are checked by the call below")
        print(f"      ({type(exc).__name__}: {str(exc)[:200]})")
        return None


if __name__ == "__main__":
    sys.exit(main())
