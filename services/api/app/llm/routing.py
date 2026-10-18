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
