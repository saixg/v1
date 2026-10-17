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


@dataclass(frozen=True)
class ModelRoute:
    model: str
    # Used instead of `model` in "deep plan" mode, and for the single retry
    # after a failed schema validation. None = retry on the same model.
    escalation_model: str | None = None
    max_tokens: int = 16000  # non-streaming ceiling; keeps requests under SDK HTTP timeouts
    accepts_images: bool = False


# embed and rerank are absent on purpose: the embedding model is an open
# decision (Part 13 #2) and reranking ships only if eval says it's needed.
DEFAULT_ROUTES: dict[ModelRole, ModelRoute] = {
    "bulk": ModelRoute(model=HAIKU, max_tokens=4096),
    "synth": ModelRoute(model=SONNET),
    "design": ModelRoute(model=SONNET, escalation_model=OPUS),
    "vision": ModelRoute(model=SONNET, accepts_images=True),
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
