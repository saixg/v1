"""Local, static secret detection for scanned file content. Runs entirely
in-memory against text already read by the scan pass — no disk writes, no
network calls, no git. See CLAUDE.md non-negotiable #6 ("Detected secrets
never leave the sandbox") and build-plan.md Part 9 (security).

`detect-secrets`'s default plugin set is purely pattern/entropy based; none
of it performs network verification (that only happens via the separate
`detect-secrets audit --verify` workflow, which this module never invokes).
"""

from __future__ import annotations

from detect_secrets.core.potential_secret import PotentialSecret
from detect_secrets.core.scan import _process_line_based_plugins
from detect_secrets.settings import default_settings

# Fixed token for every redaction, regardless of secret type or length, so the
# redacted text itself leaks nothing about what kind of secret was found.
REDACTION_TOKEN = "«REDACTED-SECRET»"

# A generic filename handed to detect-secrets so its extension-aware filters
# have something to key off; it is never a real path and nothing is read
# from or written to disk under this name.
_SCAN_FILENAME = "scanned-content"


def _find_secrets(lines: list[str]) -> list[PotentialSecret]:
    numbered = list(enumerate(lines, start=1))
    with default_settings():
        return list(_process_line_based_plugins(lines=numbered, filename=_SCAN_FILENAME))


def redact_secrets(content: bytes) -> tuple[bytes, int]:
    """Scan `content` for secrets and replace each matched span with the
    fixed `REDACTION_TOKEN`. Returns `(redacted_bytes, match_count)`.

    `content` is decoded as UTF-8 with errors replaced, so this never raises
    on malformed text. A NUL byte marks binary content (Postgres text columns
    can't hold it anyway, and the scanner's own binary check already keeps
    such files out of the content path); binary input is returned unchanged
    with a zero count rather than guessed at.
    """
    if b"\x00" in content:
        return content, 0

    text = content.decode("utf-8", errors="replace")
    lines = text.split("\n")

    secrets = _find_secrets(lines)
    if not secrets:
        return text.encode("utf-8"), 0

    match_count = 0
    for secret in secrets:
        value = secret.secret_value
        if not value:
            continue
        idx = secret.line_number - 1
        if idx < 0 or idx >= len(lines):
            continue
        line = lines[idx]
        if value in line:
            lines[idx] = line.replace(value, REDACTION_TOKEN, 1)
            match_count += 1

    return "\n".join(lines).encode("utf-8"), match_count
