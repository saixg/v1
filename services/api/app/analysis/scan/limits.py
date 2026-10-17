"""Resource caps for the scan pass: bomb protection for file count, per-file
size, cumulative repo size, and per-file parse time. See docs/08-security.md
(sandboxing / untrusted repos) and docs/09-performance.md (budgets).

Each field is overridable via an env var, read at import time, so a real
large benchmark repo (docs/11-acceptance-criteria.md requires at least one
1,000+ file repository per Full-tier language) isn't truncated by defaults
tuned for small-repo bomb protection.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value else default


@dataclass(frozen=True)
class ScanLimits:
    max_file_count: int = 50_000
    max_file_size_bytes: int = 5 * 1024 * 1024
    max_total_size_bytes: int = 2 * 1024 * 1024 * 1024
    max_parse_seconds: float = 2.0


DEFAULT_LIMITS = ScanLimits(
    max_file_count=_env_int("CODEAXIS_SCAN_MAX_FILES", ScanLimits.max_file_count),
    max_file_size_bytes=_env_int(
        "CODEAXIS_SCAN_MAX_FILE_SIZE_BYTES", ScanLimits.max_file_size_bytes
    ),
    max_total_size_bytes=_env_int(
        "CODEAXIS_SCAN_MAX_TOTAL_SIZE_BYTES", ScanLimits.max_total_size_bytes
    ),
    max_parse_seconds=_env_float(
        "CODEAXIS_SCAN_MAX_PARSE_SECONDS", ScanLimits.max_parse_seconds
    ),
)
