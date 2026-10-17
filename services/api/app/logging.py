"""Stdlib logging setup and a request-logging ASGI middleware.

One log line per request: method, path, status code, latency in ms. Never
request/response bodies -- they may carry repository source text or (before
secret redaction runs) raw credentials. On a 5xx response, the log line
additionally carries the exception message if the handler raised.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("app.request")


def configure_logging() -> None:
    """Configure the root logger once, level from LOG_LEVEL (default INFO)."""
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Logs one line per request: method, path, status, latency_ms."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        start = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.error(
                "%s %s 500 %.1fms error=%s",
                request.method,
                request.url.path,
                elapsed_ms,
                exc,
            )
            raise
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(
            "%s %s %d %.1fms",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response
