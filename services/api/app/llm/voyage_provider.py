"""Voyage AI embeddings provider for ModelService (build-plan.md Part 3).

`voyage-code-3` is the settled choice for the `embed` role (Part 13 #2,
decided by the owner 2026-10-17: hosted, not self-hosted). 1024 dimensions is
what `chunks.embedding` declares in
`supabase/migrations/20261019000000_semantic.sql`.

This is one POST to one documented endpoint, so it talks HTTP directly rather
than pulling in the vendor SDK. The service layer above owns routing,
batching, shape validation and the `llm_calls` row; this file owns only the
request/response mapping.

`truncation=false` on purpose: a text longer than the model's context must
fail the request, not come back as a vector for a silently shortened input.
The chunker already keeps chunks far below the 32K-token context, so hitting
this is a bug worth seeing.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from app.llm.service import EmbeddingProvider, EmbeddingRequest, EmbeddingResponse

API_URL = "https://api.voyageai.com/v1/embeddings"
API_KEY_ENV = "VOYAGE_API_KEY"

# Generous but finite: embedding a full batch of 1,000 chunks is not fast, and
# a hung request must not hang the analysis job forever.
DEFAULT_TIMEOUT_SECONDS = 120.0


class VoyageApiError(RuntimeError):
    """A non-2xx response, or a 2xx body that is not shaped like an
    embeddings response. The message deliberately carries no input text:
    repository content is untrusted and may have held a secret."""


def embedding_enabled() -> bool:
    """Whether an embedding provider can be built at all. Callers use this to
    skip the embedding step instead of failing the pipeline (Part 4.D treats
    embeddings as a pluggable stage)."""
    return bool(os.environ.get(API_KEY_ENV))


def build_payload(request: EmbeddingRequest) -> dict[str, Any]:
    """Map a provider-neutral request to the documented request body."""
    payload: dict[str, Any] = {
        "model": request.model,
        "input": list(request.texts),
        "truncation": False,
    }
    if request.input_type is not None:
        payload["input_type"] = request.input_type
    if request.dimensions is not None:
        payload["output_dimension"] = request.dimensions
    return payload


def parse_response(body: Any, request: EmbeddingRequest) -> EmbeddingResponse:
    """Map the documented response body to a provider-neutral response.

    Vectors are returned in `index` order, not the order the array happens to
    arrive in, so a reordered response cannot silently mis-assign vectors to
    chunks. A missing or duplicated index raises instead of being patched up.
    """
    if not isinstance(body, dict):
        raise VoyageApiError(f"expected a JSON object, got {type(body).__name__}")
    data = body.get("data")
    if not isinstance(data, list):
        raise VoyageApiError("response has no 'data' array")

    by_index: dict[int, list[float]] = {}
    for item in data:
        if not isinstance(item, dict):
            raise VoyageApiError("a 'data' entry is not an object")
        index, vector = item.get("index"), item.get("embedding")
        if not isinstance(index, int) or not isinstance(vector, list):
            raise VoyageApiError("a 'data' entry is missing 'index' or 'embedding'")
        if index in by_index:
            raise VoyageApiError(f"duplicate index {index} in response")
        by_index[index] = [float(value) for value in vector]

    missing = sorted(set(range(len(request.texts))) - set(by_index))
    if missing:
        raise VoyageApiError(
            f"response is missing embeddings for input index(es) {missing[:5]}"
            f"{'...' if len(missing) > 5 else ''}"
        )

    usage = body.get("usage") or {}
    total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    served_model = body.get("model")
    return EmbeddingResponse(
        vectors=[by_index[i] for i in range(len(request.texts))],
        served_model=served_model if isinstance(served_model, str) else None,
        input_tokens=total_tokens if isinstance(total_tokens, int) else 0,
    )


class VoyageProvider(EmbeddingProvider):
    name = "voyage"

    def __init__(
        self,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        url: str = API_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        # Credentials and the HTTP client are both resolved lazily, so
        # constructing this provider never requires either; only a real
        # request does. Mirrors AnthropicProvider.
        self._api_key = api_key
        self._client = client
        self._url = url
        self._timeout = timeout

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self._timeout)
        return self._client

    def _key(self) -> str:
        key = self._api_key or os.environ.get(API_KEY_ENV)
        if not key:
            raise VoyageApiError(f"{API_KEY_ENV} is not set")
        return key

    def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
        response = self.client.post(
            self._url,
            json=build_payload(request),
            headers={
                "Authorization": f"Bearer {self._key()}",
                "Content-Type": "application/json",
            },
        )
        if response.status_code >= 400:
            # The body may echo the input, which may be repository text; only
            # the status and the provider's error code are safe to surface.
            raise VoyageApiError(
                f"{request.model} returned HTTP {response.status_code} "
                f"for {len(request.texts)} texts"
            )
        return parse_response(response.json(), request)
