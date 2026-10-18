"""Local, static secret detection for scanned file content. Runs entirely
in-memory against text already read by the scan pass — no disk writes, no
network calls, no git. See CLAUDE.md non-negotiable #6 ("Detected secrets
never leave the sandbox") and build-plan.md Part 9 (security).

`detect-secrets`'s default plugin set is purely pattern/entropy based; none
of it performs network verification (that only happens via the separate
`detect-secrets audit --verify` workflow, which this module never invokes).
"""

from __future__ import annotations

import re

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


# Characters that can continue a credential token. detect-secrets reports
# `secret_value` as the regex's first capture group, which for several
# detectors is only a fragment: the GitHub detector reports just "ghp", the
# JWT detector stops before the signature. Replacing only that fragment left
# the rest of the credential in place, so each match is widened to the whole
# contiguous token around it. Over-redacting a few adjacent characters is the
# safe failure; under-redacting a credential is not.
_TOKEN_CHAR = re.compile(r"[A-Za-z0-9_\-+/=.~]")

# A PEM private key's secret material is on the lines *after* the BEGIN
# header, which is the only line detect-secrets matches. The whole block is
# redacted, through the END line (or to the end of the text if it is cut off).
_PEM_BEGIN = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----")
_PEM_END = re.compile(r"-----END [A-Z0-9 ]*PRIVATE KEY( BLOCK)?-----")


def _find_secrets(lines: list[str]) -> list[PotentialSecret]:
    numbered = list(enumerate(lines, start=1))
    with default_settings():
        return list(_process_line_based_plugins(lines=numbered, filename=_SCAN_FILENAME))


def _redact_token(line: str, value: str) -> str:
    """Replace the first occurrence of `value`, widened to the full token."""
    start = line.find(value)
    end = start + len(value)
    while start > 0 and _TOKEN_CHAR.match(line[start - 1]):
        start -= 1
    while end < len(line) and _TOKEN_CHAR.match(line[end]):
        end += 1
    return line[:start] + REDACTION_TOKEN + line[end:]


def _redact_pem_blocks(lines: list[str]) -> int:
    """Redact every PEM private-key block in place; returns blocks redacted."""
    count = 0
    i = 0
    while i < len(lines):
        if _PEM_BEGIN.search(lines[i]):
            count += 1
            j = i
            while j < len(lines):
                done = _PEM_END.search(lines[j]) is not None
                lines[j] = REDACTION_TOKEN
                j += 1
                if done:
                    break
            i = j
            continue
        i += 1
    return count


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

    # PEM blocks first: widening the header match below would otherwise erase
    # the BEGIN marker that locates the block, leaving the key body behind.
    match_count = _redact_pem_blocks(lines)
    for secret in secrets:
        value = secret.secret_value
        if not value:
            continue
        idx = secret.line_number - 1
        if idx < 0 or idx >= len(lines):
            continue
        line = lines[idx]
        if value in line:
            lines[idx] = _redact_token(line, value)
            match_count += 1

    return "\n".join(lines).encode("utf-8"), match_count
