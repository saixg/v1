"""Connection helpers. Reads the role-scoped URLs from the environment —
WORKER_DATABASE_URL (read/write) for services/worker, API_DATABASE_URL
(read-only) for services/api. Never DATABASE_URL: that is the project owner
connection used only by migrations and scripts/setup_db_roles.py.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg


def _connect(env_var: str) -> psycopg.Connection:
    url = os.environ.get(env_var)
    if not url:
        raise RuntimeError(f"{env_var} is not set. Copy .env.example to .env and fill it in.")
    return psycopg.connect(url, connect_timeout=10)


@contextmanager
def worker_connection() -> Iterator[psycopg.Connection]:
    conn = _connect("WORKER_DATABASE_URL")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def api_connection() -> Iterator[psycopg.Connection]:
    conn = _connect("API_DATABASE_URL")
    try:
        yield conn
    finally:
        conn.close()
