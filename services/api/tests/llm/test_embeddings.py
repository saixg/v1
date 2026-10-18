"""The `embed` role end to end inside ModelService, plus the Voyage request /
response mapping — against fakes and an httpx MockTransport. No network, no
VOYAGE_API_KEY, no live call.
"""

from __future__ import annotations

import httpx
import pytest

from app.llm import (
    AnthropicProvider,
    EmbeddingNotConfiguredError,
    EmbeddingProvider,
    EmbeddingRequest,
    EmbeddingResponse,
    EmbeddingShapeError,
    ModelCallError,
    ModelRoute,
    ModelServiceError,
    RoutedModelService,
    VoyageProvider,
    embedding_enabled,
)
from app.llm.voyage_provider import API_KEY_ENV, VoyageApiError, build_payload, parse_response
from app.models import LlmCall

DIMS = 1024


def vector(seed: float = 0.5, dims: int = DIMS) -> list[float]:
    return [seed] * dims


class FakeEmbeddingProvider(EmbeddingProvider):
    """Returns a vector per text; records every request it saw."""

    name = "fake-embed"

    def __init__(
        self,
        dims: int = DIMS,
        tokens_per_request: int = 42,
        error: Exception | None = None,
        vectors_override: list[list[float]] | None = None,
    ) -> None:
        self.dims = dims
        self.tokens_per_request = tokens_per_request
        self.error = error
        self.vectors_override = vectors_override
        self.requests: list[EmbeddingRequest] = []

    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        vectors = self.vectors_override
        if vectors is None:
            vectors = [vector(float(i), self.dims) for i in range(len(request.texts))]
        return EmbeddingResponse(
            vectors=vectors,
            served_model=request.model,
            input_tokens=self.tokens_per_request,
            request_id="req_embed",
        )


def make(
    provider: FakeEmbeddingProvider | None = None,
    routes: dict | None = None,
) -> tuple[RoutedModelService, FakeEmbeddingProvider, list[LlmCall]]:
    rows: list[LlmCall] = []
    embedder = provider or FakeEmbeddingProvider()
    ticks = iter(range(0, 10_000))
    service = RoutedModelService(
        provider=AnthropicProvider(client=None),  # never called; credentials stay lazy
        recorder=rows.append,
        routes=routes,
        embedding_provider=embedder,
        clock=lambda: next(ticks) * 0.25,
    )
    return service, embedder, rows


# --- routing and the usage row ------------------------------------------------


def test_embed_returns_one_vector_per_text_and_records_one_row():
    service, embedder, rows = make()

    vectors = service.embed(["def a(): ...", "def b(): ..."])

    assert len(vectors) == 2
    assert all(len(v) == DIMS for v in vectors)
    (request,) = embedder.requests
    assert request.model == "voyage-code-3"
    assert request.texts == ("def a(): ...", "def b(): ...")
    assert request.input_type == "document"
    assert request.dimensions == DIMS

    (row,) = rows
    assert (row.role, row.operation, row.status) == ("embed", "embed", "succeeded")
    assert row.provider == "fake-embed"
    assert row.model == row.served_model == "voyage-code-3"
    assert (row.input_tokens, row.output_tokens) == (42, 0)
    # $0.18 / MTok, input only (estimate_cost_usd rounds to 6 decimals).
    assert row.cost_usd == round(42 * 0.18 / 1e6, 6)
    assert row.latency_ms == 250
    assert row.request_id == "req_embed"


def test_embed_attributes_the_row_to_the_snapshot():
    from app.llm import CallContext

    service, _, rows = make()
    service.embed(["x"], context=CallContext(repo_id="r1", project_id="p1", job_id="j1"))
    (row,) = rows
    assert (row.repo_id, row.project_id, row.job_id) == ("r1", "p1", "j1")


def test_query_side_uses_a_different_input_type():
    # Asymmetric model: indexing and searching must not use the same value.
    service, embedder, _ = make()
    service.embed(["where is auth checked?"], input_type="query")
    assert embedder.requests[0].input_type == "query"


def test_no_texts_means_no_provider_call_and_no_row():
    service, embedder, rows = make()
    assert service.embed([]) == []
    assert embedder.requests == [] and rows == []


def test_embed_without_a_provider_is_a_named_failure():
    service = RoutedModelService(
        provider=AnthropicProvider(client=None), recorder=lambda _row: None
    )
    with pytest.raises(EmbeddingNotConfiguredError, match="VOYAGE_API_KEY"):
        service.embed(["x"])


def test_recorder_failure_fails_the_embed_call():
    # An unrecorded model call is a bug, same rule as completions.
    def broken(_: LlmCall) -> None:
        raise RuntimeError("db down")

    service = RoutedModelService(
        provider=AnthropicProvider(client=None),
        recorder=broken,
        embedding_provider=FakeEmbeddingProvider(),
    )
    with pytest.raises(RuntimeError, match="db down"):
        service.embed(["x"])


# --- batching -----------------------------------------------------------------


def test_batches_respect_the_text_count_cap_and_preserve_order():
    routes = {"embed": ModelRoute(model="voyage-code-3", dimensions=4, max_batch_texts=2)}
    embedder = FakeEmbeddingProvider(dims=4)
    service, _, rows = make(embedder, routes)

    texts = [f"t{i}" for i in range(5)]
    vectors = service.embed(texts)

    assert [tuple(r.texts) for r in embedder.requests] == [
        ("t0", "t1"),
        ("t2", "t3"),
        ("t4",),
    ]
    # One usage row per provider request, and the vectors come back in input
    # order across batch boundaries.
    assert len(rows) == 3
    assert [v[0] for v in vectors] == [0.0, 1.0, 0.0, 1.0, 0.0]


def test_batches_respect_the_token_cap():
    routes = {
        "embed": ModelRoute(
            model="voyage-code-3", dimensions=4, max_batch_texts=100, max_batch_tokens=100
        )
    }
    embedder = FakeEmbeddingProvider(dims=4)
    service, _, _ = make(embedder, routes)

    # ~40 estimated tokens each, 80-token effective budget (100 x 0.8 headroom).
    service.embed(["a" * 160, "b" * 160, "c" * 160])

    assert [len(r.texts) for r in embedder.requests] == [2, 1]


def test_a_text_over_the_context_limit_is_rejected_not_truncated():
    routes = {
        "embed": ModelRoute(model="voyage-code-3", dimensions=DIMS, max_input_tokens=10)
    }
    service, embedder, rows = make(routes=routes)
    with pytest.raises(ModelServiceError, match="context"):
        service.embed(["x" * 1000])
    # Rejected before any spend.
    assert embedder.requests == [] and rows == []


def test_empty_text_is_rejected():
    service, embedder, _ = make()
    with pytest.raises(ModelServiceError, match="empty"):
        service.embed(["ok", "   "])
    assert embedder.requests == []


# --- failure modes: recorded, raised, never silently dropped ------------------


def test_provider_exception_is_wrapped_and_recorded():
    service, _, rows = make(FakeEmbeddingProvider(error=ConnectionError("voyage down")))
    with pytest.raises(ModelCallError, match="voyage down"):
        service.embed(["x"])
    (row,) = rows
    assert row.status == "error"
    assert row.error is not None and "ConnectionError" in row.error
    assert row.input_tokens == 0


def test_wrong_vector_count_fails_visibly():
    service, _, rows = make(FakeEmbeddingProvider(vectors_override=[vector()]))
    with pytest.raises(EmbeddingShapeError, match="1 vectors for 2 texts"):
        service.embed(["a", "b"])
    assert [r.status for r in rows] == ["error"]


def test_wrong_dimension_fails_visibly():
    # A 512-dimension vector would be rejected by the chunks.embedding column
    # anyway; catching it here names the real cause.
    service, _, rows = make(FakeEmbeddingProvider(dims=512))
    with pytest.raises(EmbeddingShapeError, match="512-dimension"):
        service.embed(["a"])
    assert [r.status for r in rows] == ["error"]


# --- the Voyage provider itself ----------------------------------------------


def test_payload_matches_the_documented_request_body():
    payload = build_payload(
        EmbeddingRequest(
            model="voyage-code-3", texts=("a", "b"), input_type="document", dimensions=1024
        )
    )
    assert payload == {
        "model": "voyage-code-3",
        "input": ["a", "b"],
        # False on purpose: an over-long text must fail, not come back as a
        # vector for silently shortened input.
        "truncation": False,
        "input_type": "document",
        "output_dimension": 1024,
    }


def test_parse_response_restores_input_order():
    request = EmbeddingRequest(model="voyage-code-3", texts=("a", "b", "c"))
    body = {
        "object": "list",
        "model": "voyage-code-3",
        "data": [
            {"object": "embedding", "index": 2, "embedding": [3.0]},
            {"object": "embedding", "index": 0, "embedding": [1.0]},
            {"object": "embedding", "index": 1, "embedding": [2.0]},
        ],
        "usage": {"total_tokens": 9},
    }
    response = parse_response(body, request)
    assert response.vectors == [[1.0], [2.0], [3.0]]
    assert response.input_tokens == 9
    assert response.served_model == "voyage-code-3"


@pytest.mark.parametrize(
    "body",
    [
        {"data": [{"index": 0, "embedding": [1.0]}]},  # missing index 1
        {"data": [{"index": 0, "embedding": [1.0]}, {"index": 0, "embedding": [2.0]}]},
        {"data": "not a list"},
        {"nope": 1},
    ],
)
def test_malformed_response_bodies_raise(body):
    request = EmbeddingRequest(model="voyage-code-3", texts=("a", "b"))
    with pytest.raises(VoyageApiError):
        parse_response(body, request)


def test_provider_posts_to_the_documented_endpoint_with_a_bearer_token():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "voyage-code-3",
                "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                "usage": {"total_tokens": 4},
            },
        )

    provider = VoyageProvider(
        api_key="test-key", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    response = provider.embed(EmbeddingRequest(model="voyage-code-3", texts=("x",)))

    assert seen["url"] == "https://api.voyageai.com/v1/embeddings"
    assert seen["auth"] == "Bearer test-key"
    assert response.vectors == [[0.1, 0.2]]


def test_http_error_does_not_echo_the_input_text():
    secretish = "AKIA_THIS_CAME_FROM_THE_REPO"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": f"bad input: {secretish}"})

    provider = VoyageProvider(
        api_key="k", client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(VoyageApiError) as info:
        provider.embed(EmbeddingRequest(model="voyage-code-3", texts=(secretish,)))
    assert secretish not in str(info.value)
    assert "400" in str(info.value)


def test_missing_api_key_is_a_named_failure_and_makes_no_request(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    called = False

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    provider = VoyageProvider(
        api_key=None, client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    with pytest.raises(VoyageApiError, match=API_KEY_ENV):
        provider.embed(EmbeddingRequest(model="voyage-code-3", texts=("x",)))
    assert called is False


def test_embedding_enabled_follows_the_env_var(monkeypatch):
    monkeypatch.delenv(API_KEY_ENV, raising=False)
    assert embedding_enabled() is False
    monkeypatch.setenv(API_KEY_ENV, "k")
    assert embedding_enabled() is True
