"""Role -> model routing table (build-plan.md Part 3).

Start with this small table, not a model per subtask. Change a model only
when the eval harness (Part 9) shows a measurable win; swapping one is a
change to this table (or a `routes=` override), never to a caller.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models import ModelRole

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
VOYAGE_CODE_3 = "voyage-code-3"

# Gemini. The 2.5 generation on purpose: these are the GA ids, and a routing
# table is the wrong place to depend on a preview model that can be withdrawn.
# Confirm against `client.models.list()` before changing one -- an id this
# table is wrong about is a 404 on every call of that role.
GEMINI_FLASH_LITE = "gemini-2.5-flash-lite"
GEMINI_FLASH = "gemini-2.5-flash"
GEMINI_PRO = "gemini-2.5-pro"


@dataclass(frozen=True)
class ModelRoute:
    model: str
    # Used instead of `model` in "deep plan" mode, and for the single retry
    # after a failed schema validation. None = retry on the same model.
    escalation_model: str | None = None
    max_tokens: int = 16000  # non-streaming ceiling; keeps requests under SDK HTTP timeouts
    accepts_images: bool = False
    # --- embedding roles only (None on every completion role) ---------------
    # Output vector size. Must match the `chunks.embedding` column's declared
    # dimension, or every insert fails: changing it is a migration, not a
    # config tweak.
    dimensions: int | None = None
    # The provider's hard per-request caps. RoutedModelService.embed batches
    # to stay under both and fails visibly rather than dropping texts.
    max_batch_texts: int | None = None
    max_batch_tokens: int | None = None
    # Per-text context length. A longer text is rejected, never truncated.
    max_input_tokens: int | None = None


# `embed` is hosted voyage-code-3: Part 13 #2 was settled by the owner on
# 2026-10-17 (hosted, not self-hosted) and 1024 dimensions is what the
# `chunks.embedding` column in supabase/migrations/20261019000000_semantic.sql
# declares. `rerank` is still absent on purpose: it ships only if eval shows
# retrieval is insufficient (Part 3).
DEFAULT_ROUTES: dict[ModelRole, ModelRoute] = {
    "bulk": ModelRoute(model=HAIKU, max_tokens=4096),
    "synth": ModelRoute(model=SONNET),
    "design": ModelRoute(model=SONNET, escalation_model=OPUS),
    "vision": ModelRoute(model=SONNET, accepts_images=True),
    "embed": ModelRoute(
        model=VOYAGE_CODE_3,
        # Embeddings have no output-token budget; this field is unused here.
        max_tokens=0,
        dimensions=1024,
        # voyage-code-3 per-request limits, docs.voyageai.com (read 2026-10-17):
        # at most 1,000 texts and 120K tokens per request, 32K tokens per text.
        max_batch_texts=1000,
        max_batch_tokens=120_000,
        max_input_tokens=32_000,
    ),
}


# The same four completion roles on Gemini, for a deployment that has a Gemini
# key and no Anthropic one. Deliberately the same *shape* as DEFAULT_ROUTES --
# cheap model for `bulk`, mid model for `synth`/`vision`, mid escalating to the
# top model for `design` -- so switching provider changes which vendor serves a
# role and not what the role means.
#
# `embed` stays on voyage-code-3 here rather than moving to a Gemini embedding
# model, and that is not an oversight: `chunks.embedding` is 1024 dimensions of
# voyage-code-3, so a different embedder means a migration and a re-embed of
# every stored chunk, not a routing entry. The completion vendor and the
# embedding vendor are chosen independently, which is why the entry is shared
# with DEFAULT_ROUTES instead of being left out -- a table missing `embed`
# would turn a provider switch into an UnroutedRoleError on the chunk pass.
GEMINI_ROUTES: dict[ModelRole, ModelRoute] = {
    "bulk": ModelRoute(model=GEMINI_FLASH_LITE, max_tokens=4096),
    "synth": ModelRoute(model=GEMINI_FLASH),
    "design": ModelRoute(model=GEMINI_FLASH, escalation_model=GEMINI_PRO),
    "vision": ModelRoute(model=GEMINI_FLASH, accepts_images=True),
    "embed": DEFAULT_ROUTES["embed"],
}


def routed_models(
    routes: dict[ModelRole, ModelRoute], *, completions_only: bool = False
) -> set[str]:
    """Every model id a table can send a request to, escalation models included.

    `completions_only` drops the embedding roles, which are served by a
    different vendor from the completion roles and so are not in the
    completion provider's model list.
    """
    selected = [r for r in routes.values() if not (completions_only and r.dimensions is not None)]
    return {r.model for r in selected} | {
        r.escalation_model for r in selected if r.escalation_model
    }


class UnroutedRoleError(LookupError):
    """The role has no entry in the routing table."""


def resolve_route(routes: dict[ModelRole, ModelRoute], role: ModelRole) -> ModelRoute:
    try:
        return routes[role]
    except KeyError:
        raise UnroutedRoleError(f"no model is routed for role {role!r}") from None


def attempt_models(route: ModelRoute, *, deep: bool) -> tuple[str, str]:
    """The (first attempt, retry) models for one structured call.

    Deep-plan mode starts on the escalation model. A failed validation always
    retries on the escalation model when the role has one.
    """
    first = route.escalation_model if deep and route.escalation_model else route.model
    retry = route.escalation_model or route.model
    return first, retry
